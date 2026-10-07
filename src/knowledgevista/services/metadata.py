"""The metadata store: candidates (proposals), accepted values, locks and history.

THE RULES THIS MODULE KEEPS (each has a test in tests/test_metadata_store.py):

  * A candidate is a proposal. Only `accept_candidate` (a person, or a NAMED rule) or `set_value` (a person) writes an
    accepted value, and every write leaves a history row with the previous value and where it came from.
  * One proposal per distinct EVIDENCE: finding the same thing again is `unchanged`, writes nothing and does not move
    `catalog_revision`. That is what makes re-running `kv resolve` change nothing.
  * A decision is not overwritten by a rerun. An accepted or rejected candidate keeps its status when it is found again,
    even if the rules that produced it have since changed.
  * A locked value is never replaced by anything. Not by a rule, not by the user's own `accept` (they unlock first), so
    a lock cannot be forgotten into a surprise.
  * An empty value is refused (`domain/fields.normalise`): unknown is the absence of a value.

Functions that take no transaction of their own say so; the caller (the resolver, one document at a time) owns it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from knowledgevista.db.catalog import bump_revision, transaction
from knowledgevista.domain.candidate import CandidateSpec, evidence_key  # noqa: F401 - re-exported for callers
from knowledgevista.domain import fields as fieldmod
from knowledgevista.domain.ids import new_id, utc_now
from knowledgevista.errors import ErrorCode, KvError

DECIDED = ("accepted", "rejected")
OPEN = ("proposed", "stale", "set_aside")


def _dump(evidence: dict[str, Any]) -> str:
    return json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _actor_for_history(actor: str) -> str:
    return "resolver" if actor.startswith("rule:") else actor


def upsert_candidate(conn: sqlite3.Connection, document_id: str, artifact_id: str, spec: CandidateSpec, run_id: str | None, matcher_version: str) -> str:
    """Record a proposal. Returns `new`, `unchanged`, `updated`, `revived` (was stale, found again) or `decided` (a person
    already accepted or rejected it: left alone). Caller owns the transaction and the revision bump."""
    row = conn.execute(
        "SELECT * FROM metadata_candidate WHERE document_id = ? AND field = ? AND evidence_key = ?",
        (document_id, spec.field, spec.evidence_key),
    ).fetchone()
    evidence = _dump(spec.evidence)
    now = utc_now()
    if row is None:
        conn.execute(
            "INSERT INTO metadata_candidate (candidate_id, document_id, artifact_id, field, value, origin, source, evidence_key, "
            "evidence_json, classification, confidence, review, risk, priority, matcher_version, status, first_run_id, last_run_id, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id(), document_id, artifact_id, spec.field, spec.value, spec.origin, spec.source, spec.evidence_key, evidence,
             spec.classification, spec.confidence, spec.review, spec.risk, spec.priority, matcher_version, spec.status,
             run_id, run_id, now, now),
        )
        return "new"
    if row["status"] in DECIDED:
        return "decided"
    same = (row["value"], row["origin"], row["source"], row["evidence_json"], row["classification"], row["confidence"], row["review"],
            row["risk"], row["priority"], row["matcher_version"], row["status"]) == (
        spec.value, spec.origin, spec.source, evidence, spec.classification, spec.confidence, spec.review, spec.risk,
        spec.priority, matcher_version, spec.status)
    if same:
        return "unchanged"
    conn.execute(
        "UPDATE metadata_candidate SET value = ?, origin = ?, source = ?, evidence_json = ?, classification = ?, confidence = ?, "
        "review = ?, risk = ?, priority = ?, matcher_version = ?, status = ?, artifact_id = ?, last_run_id = ?, updated_at = ? "
        "WHERE candidate_id = ?",
        (spec.value, spec.origin, spec.source, evidence, spec.classification, spec.confidence, spec.review, spec.risk,
         spec.priority, matcher_version, spec.status, artifact_id, run_id, now, row["candidate_id"]),
    )
    return "revived" if row["status"] == "stale" else "updated"


def mark_stale(conn: sqlite3.Connection, document_id: str, sources: Iterable[str], keep: set[tuple[str, str]], run_id: str | None) -> int:
    """Proposals from `sources` that this run did not find again are stale: their evidence is gone (the file changed, the
    extractor or the rules changed). Decided candidates are never touched. Caller owns the transaction."""
    sources = list(sources)
    if not sources:
        return 0
    rows = conn.execute(
        f"SELECT candidate_id, field, evidence_key FROM metadata_candidate WHERE document_id = ? AND status IN ('proposed', 'set_aside') "
        f"AND source IN ({','.join('?' * len(sources))})",
        (document_id, *sources),
    ).fetchall()
    stale = [r["candidate_id"] for r in rows if (r["field"], r["evidence_key"]) not in keep]
    now = utc_now()
    for candidate_id in stale:
        conn.execute("UPDATE metadata_candidate SET status = 'stale', last_run_id = ?, updated_at = ? WHERE candidate_id = ?", (run_id, now, candidate_id))
    return len(stale)


# ------------------------------------------------------------------------------------------------ accepted values


def get_values(conn: sqlite3.Connection, document_id: str) -> dict[str, sqlite3.Row]:
    return {r["field"]: r for r in conn.execute("SELECT * FROM metadata_value WHERE document_id = ?", (document_id,))}


def _history(conn: sqlite3.Connection, document_id: str, field_: str, old: sqlite3.Row | None, new_value: str | None,
             new_source: str | None, candidate_id: str | None, actor: str, reason: str | None) -> None:
    conn.execute(
        "INSERT INTO metadata_history (document_id, field, old_value, old_source, new_value, new_source, candidate_id, at, actor, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (document_id, field_, old["value"] if old else None, old["source"] if old else None, new_value, new_source,
         candidate_id, utc_now(), _actor_for_history(actor), reason),
    )


def _refuse_if_locked(existing: sqlite3.Row | None, field_: str) -> None:
    if existing is not None and existing["locked"]:
        raise KvError(ErrorCode.METADATA_LOCKED, f"The {field_} of this document is locked. Unlock it first: kv metadata unlock <document> {field_}",
                      {"field": field_})


def _write_value(conn: sqlite3.Connection, document_id: str, field_: str, value: str, origin: str, source: str, candidate_id: str | None,
                 evidence_json: str | None, locked: int, actor: str, reason: str | None) -> bool:
    """Set a field's accepted value; True if the value changed. The same value again is a no-op (even when locked: nothing
    is being replaced). Caller owns the transaction."""
    existing = conn.execute("SELECT * FROM metadata_value WHERE document_id = ? AND field = ?", (document_id, field_)).fetchone()
    if existing is not None and existing["value"] == value:
        return False
    _refuse_if_locked(existing, field_)
    now = utc_now()
    _history(conn, document_id, field_, existing, value, source, candidate_id, actor, reason)
    conn.execute(
        "INSERT INTO metadata_value (document_id, field, value, origin, source, source_candidate_id, evidence_json, locked, accepted_by, "
        "accepted_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (document_id, field) DO UPDATE SET "
        "value = excluded.value, origin = excluded.origin, source = excluded.source, source_candidate_id = excluded.source_candidate_id, "
        "evidence_json = excluded.evidence_json, locked = excluded.locked, accepted_by = excluded.accepted_by, "
        "accepted_at = excluded.accepted_at, updated_at = excluded.updated_at",
        (document_id, field_, value, origin, source, candidate_id, evidence_json, locked, actor, now, now),
    )
    return True


@dataclass
class Accepted:
    candidate_id: str
    document_id: str
    field: str
    value: str
    changed: bool  # the accepted value of the field changed (False: it already had this value)
    replaced: str | None


def accept_candidate(conn: sqlite3.Connection, candidate_id: str, *, actor: str, reason: str | None = None) -> Accepted:
    """Accept a proposal as the field's value. `actor` is `user` or `rule:<name>`; both are recorded.

    Candidates for the same field with the same value (other evidence agreeing) are accepted with it."""
    with transaction(conn):
        row = conn.execute("SELECT * FROM metadata_candidate WHERE candidate_id = ?", (candidate_id,)).fetchone()
        if row is None:
            raise KvError(ErrorCode.NOT_FOUND, f"No candidate {candidate_id}.", {"candidate_id": candidate_id})
        existing = conn.execute("SELECT * FROM metadata_value WHERE document_id = ? AND field = ?", (row["document_id"], row["field"])).fetchone()
        changed = _write_value(conn, row["document_id"], row["field"], row["value"], row["origin"], row["source"], candidate_id,
                               row["evidence_json"], 0, actor, reason or f"accepted candidate from {row['source']}")
        now = utc_now()
        key = fieldmod.agreement_key(row["field"], row["value"])
        agreeing = [r["candidate_id"] for r in conn.execute(
            "SELECT candidate_id, value FROM metadata_candidate WHERE document_id = ? AND field = ? AND status IN ('proposed', 'stale', 'set_aside')",
            (row["document_id"], row["field"])) if fieldmod.agreement_key(row["field"], r["value"]) == key]
        moved = 0
        for sibling in {candidate_id, *agreeing}:
            moved += conn.execute(
                "UPDATE metadata_candidate SET status = 'accepted', decided_at = ?, decided_by = ?, updated_at = ? "
                "WHERE candidate_id = ? AND status <> 'accepted'", (now, actor, now, sibling)).rowcount
        if changed or moved:  # accepting what is already accepted changes nothing, so it moves nothing
            bump_revision(conn)
        return Accepted(candidate_id, row["document_id"], row["field"], row["value"], changed, existing["value"] if existing and changed else None)


def reject_candidate(conn: sqlite3.Connection, candidate_id: str, *, actor: str = "user") -> bool:
    """Decide a proposal is wrong. It stays recorded (so it is not proposed again) and is never deleted."""
    with transaction(conn):
        row = conn.execute("SELECT * FROM metadata_candidate WHERE candidate_id = ?", (candidate_id,)).fetchone()
        if row is None:
            raise KvError(ErrorCode.NOT_FOUND, f"No candidate {candidate_id}.", {"candidate_id": candidate_id})
        if row["status"] == "accepted":
            raise KvError(ErrorCode.INVALID_ARGUMENTS, "That candidate is the accepted value. Clear the value instead: kv metadata clear <document> <field>")
        if row["status"] == "rejected":
            return False
        now = utc_now()
        conn.execute("UPDATE metadata_candidate SET status = 'rejected', decided_at = ?, decided_by = ?, updated_at = ? WHERE candidate_id = ?",
                     (now, actor, now, candidate_id))
        bump_revision(conn)
        return True


def set_value(conn: sqlite3.Connection, document_id: str, field_: str, raw: object, *, lock: bool = True, actor: str = "user") -> bool:
    """A person states a value. It is validated and normalised like any other, recorded as `assigned`, and locked by
    default: they said it, so no resolver may undo it. Returns True if anything changed."""
    try:
        value = fieldmod.normalise(field_, raw)
    except ValueError as exc:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, str(exc), {"field": field_}) from exc
    with transaction(conn):
        if conn.execute("SELECT 1 FROM document WHERE document_id = ?", (document_id,)).fetchone() is None:
            raise KvError(ErrorCode.NOT_FOUND, f"No document {document_id}.", {"document_id": document_id})
        existing = conn.execute("SELECT * FROM metadata_value WHERE document_id = ? AND field = ?", (document_id, field_)).fetchone()
        now = utc_now()
        if existing is not None and existing["value"] == value:
            if existing["origin"] == "assigned" and bool(existing["locked"]) == lock:
                return False
            _history(conn, document_id, field_, existing, value, "manual", None, actor, "confirmed by the user")
            conn.execute("UPDATE metadata_value SET origin = 'assigned', source = 'manual', locked = ?, accepted_by = ?, updated_at = ? "
                         "WHERE document_id = ? AND field = ?", (int(lock), actor, now, document_id, field_))
            bump_revision(conn)
            return True
        if existing is not None and existing["locked"]:  # a person replacing their own lock
            conn.execute("UPDATE metadata_value SET locked = 0 WHERE document_id = ? AND field = ?", (document_id, field_))
        _write_value(conn, document_id, field_, value, "assigned", "manual", None, None, int(lock), actor, "stated by the user")
        bump_revision(conn)
        return True


def clear_value(conn: sqlite3.Connection, document_id: str, field_: str, *, actor: str = "user") -> bool:
    """Remove an accepted value (back to unknown). A locked value is cleared only by a person."""
    with transaction(conn):
        existing = conn.execute("SELECT * FROM metadata_value WHERE document_id = ? AND field = ?", (document_id, field_)).fetchone()
        if existing is None:
            return False
        if existing["locked"] and actor != "user":
            _refuse_if_locked(existing, field_)
        _history(conn, document_id, field_, existing, None, None, None, actor, "cleared")
        conn.execute("DELETE FROM metadata_value WHERE document_id = ? AND field = ?", (document_id, field_))
        # The person who clears a value is saying it was wrong, so the proposals that produced it are rejected, not
        # returned to the queue: a safe-batch rule would otherwise accept them again on the next run.
        conn.execute("UPDATE metadata_candidate SET status = 'rejected', decided_at = ?, decided_by = ?, updated_at = ? "
                     "WHERE document_id = ? AND field = ? AND value = ? AND status = 'accepted'",
                     (utc_now(), actor, utc_now(), document_id, field_, existing["value"]))
        bump_revision(conn)
        return True


def set_lock(conn: sqlite3.Connection, document_id: str, field_: str, locked: bool) -> bool:
    with transaction(conn):
        existing = conn.execute("SELECT * FROM metadata_value WHERE document_id = ? AND field = ?", (document_id, field_)).fetchone()
        if existing is None:
            raise KvError(ErrorCode.NOT_FOUND, f"The {field_} of this document has no value to lock.", {"field": field_})
        if bool(existing["locked"]) == locked:
            return False
        conn.execute("UPDATE metadata_value SET locked = ?, updated_at = ? WHERE document_id = ? AND field = ?", (int(locked), utc_now(), document_id, field_))
        bump_revision(conn)
        return True


# ------------------------------------------------------------------------------------------------ reading


def get_candidates(conn: sqlite3.Connection, document_id: str, statuses: Iterable[str] | None = None) -> list[sqlite3.Row]:
    sql, args = "SELECT * FROM metadata_candidate WHERE document_id = ?", [document_id]
    if statuses is not None:
        statuses = list(statuses)
        sql += f" AND status IN ({','.join('?' * len(statuses))})"
        args += statuses
    return conn.execute(sql + " ORDER BY field, priority, created_at, candidate_id", args).fetchall()


def history(conn: sqlite3.Connection, document_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM metadata_history WHERE document_id = ? ORDER BY history_id", (document_id,)).fetchall()
