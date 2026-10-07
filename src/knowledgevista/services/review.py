"""The review queue and the one batch rule.

THE BATCH RULE, `safe_batch_v1` (it is named because it is recorded: `decided_by = rule:safe_batch_v1`):

  For each field of a document, among the proposals marked `safe`:
    * if they agree on one value, accept the best-sourced of them (siblings with that value are accepted with it);
    * if they disagree, accept NOTHING for that field: two independent safe proposals contradicting each other is a
      question for a person;
    * if the field already has a value, accept only the SAME value (a no-op) or an upgrade of it: replacing a value that a
      rule accepted, with the same letters spelled by a better source (Crossref's "Cu2O" for the layout's "Cu 2 O").
      A person's value, a locked value, and any different value are never replaced by a rule.

`safe` is a property of the candidate, set by the code that proposed it with the reasons in its evidence; this rule only
decides among candidates that already earned it. Everything else waits in the queue for a person.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from knowledgevista.domain import fields as fieldmod
from knowledgevista.domain import titles
from knowledgevista.domain.candidate import SOURCE_RANK
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import metadata

RULE = "rule:safe_batch_v1"


@dataclass
class BatchResult:
    accepted: list[metadata.Accepted] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)  # {field, reason, ...}


def _upgrade_allowed(existing: sqlite3.Row, new_value: str, new_source: str, field_: str) -> bool:
    return (
        not existing["locked"]
        and existing["accepted_by"].startswith("rule:")
        and field_ in fieldmod.TEXT_FIELDS
        and titles.alnum_key(existing["value"]) == titles.alnum_key(new_value)
        and SOURCE_RANK.get(new_source, 0) > SOURCE_RANK.get(existing["source"], 0)
    )


def accept_safe_for_document(conn: sqlite3.Connection, document_id: str) -> BatchResult:
    result = BatchResult()
    rows = conn.execute(
        "SELECT * FROM metadata_candidate WHERE document_id = ? AND status = 'proposed' AND review = 'safe' ORDER BY field, priority, candidate_id",
        (document_id,),
    ).fetchall()
    existing = metadata.get_values(conn, document_id)
    by_field: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if row["field"] == "doi" and row["classification"] != "own":
            continue  # defensive: a safe DOI is always an own one
        by_field.setdefault(row["field"], []).append(row)
    for field_, candidates in by_field.items():
        keys = {fieldmod.agreement_key(field_, c["value"]) for c in candidates}
        if len(keys) > 1:
            values = sorted({c["value"] for c in candidates})
            result.skipped.append({"field": field_, "reason": "safe candidates disagree", "values": [fieldmod.display(field_, v) for v in values]})
            continue
        best = max(candidates, key=lambda c: (SOURCE_RANK.get(c["source"], 0), -c["priority"], c["value"]))
        current = existing.get(field_)
        if current is not None and current["value"] != best["value"]:
            if fieldmod.agreement_key(field_, current["value"]) == fieldmod.agreement_key(field_, best["value"]):
                if not _upgrade_allowed(current, best["value"], best["source"], field_):
                    continue  # the same words, spelled differently: nothing to decide, nothing to report
            else:
                result.skipped.append({"field": field_, "reason": "differs from the accepted value", "accepted": fieldmod.display(field_, current["value"]),
                                       "proposed": fieldmod.display(field_, best["value"])})
                continue
        try:
            result.accepted.append(metadata.accept_candidate(conn, best["candidate_id"], actor=RULE, reason="safe batch rule"))
        except KvError as exc:
            if exc.code != ErrorCode.METADATA_LOCKED:
                raise
            result.skipped.append({"field": field_, "reason": "the accepted value is locked"})
    return result


def accept_safe(conn: sqlite3.Connection, document_ids: list[str] | None = None) -> BatchResult:
    total = BatchResult()
    ids = document_ids if document_ids is not None else [
        r[0] for r in conn.execute("SELECT DISTINCT document_id FROM metadata_candidate WHERE status = 'proposed' AND review = 'safe'")]
    for document_id in sorted(ids):
        one = accept_safe_for_document(conn, document_id)
        total.accepted += one.accepted
        total.skipped += [{**s, "document_id": document_id} for s in one.skipped]
    return total


# ---------------------------------------------------------------------------------------------------- the queue

_CONFIDENCE_ORDER = {"exact": 0, "high": 1, "medium": 2, "low": 3, "ambiguous": 4}


def resolve_candidate_id(conn: sqlite3.Connection, reference: str) -> str:
    """A candidate id or an unambiguous prefix of one (8+ characters)."""
    text = (reference or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{8,32}", text):
        raise KvError(ErrorCode.INVALID_ARGUMENTS, "Give 8 to 32 hex characters of the candidate id (see: kv review list).")
    rows = conn.execute("SELECT candidate_id FROM metadata_candidate WHERE candidate_id LIKE ? LIMIT 6", (text + "%",)).fetchall()
    if not rows:
        raise KvError(ErrorCode.NOT_FOUND, f"No candidate starts with {text!r}.", {"reference": reference})
    if len(rows) > 1:
        raise KvError(ErrorCode.AMBIGUOUS, f"{text!r} matches more than one candidate.", {"candidates": [r[0] for r in rows]})
    return rows[0][0]


def queue(
    conn: sqlite3.Connection, *, field_: str | None = None, review: str | None = None, include: tuple[str, ...] = ("proposed",), limit: int = 50, offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Open proposals, grouped by (document, field, value) so that several sources agreeing are ONE item, most urgent first.
    Returns (items, total_items)."""
    sql = f"SELECT * FROM metadata_candidate WHERE status IN ({','.join('?' * len(include))})"
    args: list[Any] = list(include)
    if field_:
        sql += " AND field = ?"
        args.append(field_)
    rows = conn.execute(sql, args).fetchall()
    groups: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault((row["document_id"], row["field"], row["value"]), []).append(row)
    items = []
    for (document_id, field_name, value), members in groups.items():
        best = min(members, key=lambda m: (_CONFIDENCE_ORDER[m["confidence"]], m["priority"]))
        level = "safe" if any(m["review"] == "safe" for m in members) else "required"
        if review and level != review:
            continue
        accepted = conn.execute("SELECT value, locked FROM metadata_value WHERE document_id = ? AND field = ?", (document_id, field_name)).fetchone()
        if accepted is not None and fieldmod.agreement_key(field_name, accepted["value"]) == fieldmod.agreement_key(field_name, value):
            continue  # it says what the accepted value says: nothing to review
        items.append({
            "document_id": document_id, "field": field_name, "value": value, "display": fieldmod.display(field_name, value),
            "candidate_ids": sorted(m["candidate_id"] for m in members), "sources": sorted({m["source"] for m in members}),
            "confidence": best["confidence"], "classification": best["classification"], "review": level,
            "priority": min(m["priority"] for m in members), "differs_from_accepted": (
                None if accepted is None or accepted["value"] == value else fieldmod.display(field_name, accepted["value"])),
            "evidence": json.loads(best["evidence_json"]),
        })
    items.sort(key=lambda i: (i["priority"], _CONFIDENCE_ORDER[i["confidence"]], i["document_id"], i["field"], i["value"]))
    return items[offset: offset + limit], len(items)
