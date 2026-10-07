"""Relations between artifacts and between documents, the proposals that precede them, and merge / split.

THE RULES THIS MODULE KEEPS (each has a test in tests/test_relations_store.py):

  * Two LEVELS, never mixed. An ARTIFACT relation is about bytes (this file is a copy / a derivative / a replacement of that
    one). A DOCUMENT relation is about library items (this is the supplement of that paper; this chapter is part of that
    book). Merging two documents is neither: it is a decision that they are ONE item, and it is proposed as `same_document`.
  * Every automatic relation is first a PROPOSAL with its evidence. It becomes a relation only when a person (or `rule:`)
    accepts it, and the relation records which proposal it came from and who accepted it.
  * Nothing is deleted. A relation is retracted (`retracted_at`); a merge retires the absorbed document (`retired_at`,
    `merged_into`) and moves its artifacts, keeping every artifact id; a split can revive the retired document under its
    original id. Every merge and split is an event with what moved.
  * A relation that would make a document its own ancestor (A part_of B, B part_of A) is refused.

Functions that take no transaction of their own say so; the detector (services/relate.py) owns one per run.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from knowledgevista.db.catalog import bump_revision, transaction
from knowledgevista.domain.ids import new_id, utc_now
from knowledgevista.errors import ErrorCode, KvError

ARTIFACT_KINDS = ("duplicate_of", "derivative_of", "replaces", "equivalent_to")
DOCUMENT_KINDS = ("supplement_of", "part_of", "version_of", "related_to")
#: Relations that read the same from either end are stored in one orientation (smaller id first).
SYMMETRIC = frozenset({"equivalent_to", "same_document", "related_to"})
#: Directional kinds in which following the relation upward must never come back to where it started.
HIERARCHICAL = frozenset({"part_of", "supplement_of", "version_of"})
DECIDED = ("accepted", "rejected")


@dataclass
class RelationSpec:
    """What the detector proposes."""

    level: str  # artifact | document | group
    kind: str
    source_id: str
    target_id: str = ""
    evidence_key: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: str = "medium"
    members: list[str] | None = None  # a group's document ids


def orient(kind: str, source: str, target: str) -> tuple[str, str]:
    return (source, target) if kind not in SYMMETRIC or source <= target else (target, source)


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ------------------------------------------------------------------------------------------------ proposals


def upsert_candidate(conn: sqlite3.Connection, spec: RelationSpec, run_id: str | None, matcher_version: str) -> str:
    """Record a proposal: `new`, `unchanged`, `updated`, `revived` (was stale, found again) or `decided` (left alone).
    Caller owns the transaction."""
    source, target = orient(spec.kind, spec.source_id, spec.target_id)
    row = conn.execute(
        "SELECT * FROM relation_candidate WHERE kind = ? AND source_id = ? AND target_id = ? AND evidence_key = ?",
        (spec.kind, source, target, spec.evidence_key),
    ).fetchone()
    evidence, members, now = _dump(spec.evidence), _dump(sorted(spec.members)) if spec.members is not None else None, utc_now()
    if row is None:
        conn.execute(
            "INSERT INTO relation_candidate (candidate_id, level, kind, source_id, target_id, members_json, evidence_key, evidence_json, "
            "confidence, matcher_version, status, first_run_id, last_run_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?)",
            (new_id(), spec.level, spec.kind, source, target, members, spec.evidence_key, evidence, spec.confidence, matcher_version,
             run_id, run_id, now, now),
        )
        return "new"
    if row["status"] in DECIDED:
        return "decided"
    if (row["evidence_json"], row["members_json"], row["confidence"], row["matcher_version"], row["status"]) == (evidence, members, spec.confidence, matcher_version, "proposed"):
        return "unchanged"
    conn.execute(
        "UPDATE relation_candidate SET evidence_json = ?, members_json = ?, confidence = ?, matcher_version = ?, status = 'proposed', "
        "last_run_id = ?, updated_at = ? WHERE candidate_id = ?",
        (evidence, members, spec.confidence, matcher_version, run_id, now, row["candidate_id"]),
    )
    return "revived" if row["status"] == "stale" else "updated"


def mark_stale(conn: sqlite3.Connection, keep: set[tuple[str, str, str, str]], run_id: str | None) -> int:
    """Proposals this run did not find again have lost their evidence. Decided ones are never touched. Caller owns the transaction."""
    stale = [r["candidate_id"] for r in conn.execute(
        "SELECT candidate_id, kind, source_id, target_id, evidence_key FROM relation_candidate WHERE status = 'proposed'")
        if (r["kind"], r["source_id"], r["target_id"], r["evidence_key"]) not in keep]
    now = utc_now()
    for candidate_id in stale:
        conn.execute("UPDATE relation_candidate SET status = 'stale', last_run_id = ?, updated_at = ? WHERE candidate_id = ?", (run_id, now, candidate_id))
    return len(stale)


def resolve_candidate_id(conn: sqlite3.Connection, reference: str) -> str:
    import re
    text = (reference or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{8,32}", text):
        raise KvError(ErrorCode.INVALID_ARGUMENTS, "Give 8 to 32 hex characters of the proposal id (see: kv relations list).")
    rows = conn.execute("SELECT candidate_id FROM relation_candidate WHERE candidate_id LIKE ? LIMIT 6", (text + "%",)).fetchall()
    if not rows:
        raise KvError(ErrorCode.NOT_FOUND, f"No relation proposal starts with {text!r}.", {"reference": reference})
    if len(rows) > 1:
        raise KvError(ErrorCode.AMBIGUOUS, f"{text!r} matches more than one proposal.", {"candidates": [r[0] for r in rows]})
    return rows[0][0]


def reject_candidate(conn: sqlite3.Connection, candidate_id: str, *, actor: str = "user") -> bool:
    with transaction(conn):
        row = conn.execute("SELECT * FROM relation_candidate WHERE candidate_id = ?", (candidate_id,)).fetchone()
        if row is None:
            raise KvError(ErrorCode.NOT_FOUND, f"No relation proposal {candidate_id}.", {"candidate_id": candidate_id})
        if row["status"] == "accepted":
            raise KvError(ErrorCode.INVALID_ARGUMENTS, "That proposal was accepted. Retract the relation instead: kv relations remove <relation id>")
        if row["status"] == "rejected":
            return False
        now = utc_now()
        conn.execute("UPDATE relation_candidate SET status = 'rejected', decided_at = ?, decided_by = ?, updated_at = ? WHERE candidate_id = ?",
                     (now, actor, now, candidate_id))
        bump_revision(conn)
        return True


# ------------------------------------------------------------------------------------------------ relations


def _active_document(conn: sqlite3.Connection, document_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM document WHERE document_id = ?", (document_id,)).fetchone()
    if row is None:
        raise KvError(ErrorCode.NOT_FOUND, f"No document {document_id}.", {"document_id": document_id})
    if row["retired_at"] is not None:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"Document {document_id[:12]} was merged into {str(row['merged_into'])[:12]} and is retired; use that one.",
                      {"document_id": document_id, "merged_into": row["merged_into"]})
    return row


def _would_cycle(conn: sqlite3.Connection, kind: str, source: str, target: str) -> bool:
    """Whether adding `source -kind-> target` would let someone follow `kind` from the target back to the source."""
    if kind not in HIERARCHICAL:
        return False
    seen, frontier = set(), [target]
    while frontier:
        current = frontier.pop()
        if current == source:
            return True
        if current in seen:
            continue
        seen.add(current)
        frontier += [r[0] for r in conn.execute(
            "SELECT target_id FROM document_relation WHERE kind = ? AND source_id = ? AND retracted_at IS NULL", (kind, current))]
    return False


def add_relation(conn: sqlite3.Connection, level: str, kind: str, source: str, target: str, *, actor: str, note: str | None = None,
                 position: str | None = None, candidate_id: str | None = None) -> tuple[str, bool]:
    """Record an accepted relation. Returns (relation_id, created); an identical live relation is returned, not duplicated.
    Caller owns the transaction."""
    if source == target:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, "A relation needs two different ends.")
    if level == "artifact":
        if kind not in ARTIFACT_KINDS:
            raise KvError(ErrorCode.INVALID_ARGUMENTS, f"{kind!r} is not an artifact relation. Artifact relations: {', '.join(ARTIFACT_KINDS)}.")
        table = "artifact_relation"
        for end in (source, target):
            if conn.execute("SELECT 1 FROM artifact WHERE artifact_id = ?", (end,)).fetchone() is None:
                raise KvError(ErrorCode.NOT_FOUND, f"No artifact {end[:16]}.", {"artifact_id": end})
    elif level == "document":
        if kind not in DOCUMENT_KINDS:
            raise KvError(ErrorCode.INVALID_ARGUMENTS, f"{kind!r} is not a document relation. Document relations: {', '.join(DOCUMENT_KINDS)}.")
        table = "document_relation"
        _active_document(conn, source)
        _active_document(conn, target)
    else:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"Unknown relation level {level!r}.")
    source, target = orient(kind, source, target)
    existing = conn.execute(f"SELECT relation_id FROM {table} WHERE kind = ? AND source_id = ? AND target_id = ? AND retracted_at IS NULL",
                            (kind, source, target)).fetchone()
    if existing is not None:
        return existing[0], False
    if level == "document" and _would_cycle(conn, kind, source, target):
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"That would make a document its own ancestor through {kind}: the other end already {kind} this one, directly or not.")
    relation_id = new_id()
    now = utc_now()
    if level == "artifact":
        conn.execute("INSERT INTO artifact_relation (relation_id, kind, source_id, target_id, accepted_from_candidate, accepted_by, accepted_at, note) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (relation_id, kind, source, target, candidate_id, actor, now, note))
    else:
        conn.execute("INSERT INTO document_relation (relation_id, kind, source_id, target_id, position, accepted_from_candidate, accepted_by, accepted_at, note) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (relation_id, kind, source, target, position, candidate_id, actor, now, note))
    return relation_id, True


def add_relation_by_user(conn: sqlite3.Connection, level: str, kind: str, source: str, target: str, *, note: str | None = None,
                         position: str | None = None) -> tuple[str, bool]:
    with transaction(conn):
        result = add_relation(conn, level, kind, source, target, actor="user", note=note, position=position)
        if result[1]:
            bump_revision(conn)
        return result


def retract_relation(conn: sqlite3.Connection, relation_id: str, *, actor: str = "user") -> bool:
    """Take a relation back. It stays recorded as retracted; nothing is deleted. Matching by id prefix is exact-or-unique."""
    with transaction(conn):
        for table in ("artifact_relation", "document_relation"):
            rows = conn.execute(f"SELECT relation_id, retracted_at FROM {table} WHERE relation_id LIKE ?", (relation_id.lower() + "%",)).fetchall()
            if len(rows) > 1:
                raise KvError(ErrorCode.AMBIGUOUS, f"{relation_id!r} matches more than one relation.", {"candidates": [r[0] for r in rows]})
            if rows:
                if rows[0]["retracted_at"] is not None:
                    return False
                conn.execute(f"UPDATE {table} SET retracted_at = ?, retracted_by = ? WHERE relation_id = ?", (utc_now(), actor, rows[0]["relation_id"]))
                bump_revision(conn)
                return True
    raise KvError(ErrorCode.NOT_FOUND, f"No relation {relation_id}.", {"relation_id": relation_id})


def relations_of_document(conn: sqlite3.Connection, document_id: str) -> dict[str, list[dict[str, Any]]]:
    """Live relations touching a document and its artifacts, each with the other end named."""
    artifacts = [r[0] for r in conn.execute("SELECT artifact_id FROM document_artifact WHERE document_id = ?", (document_id,))]
    out: dict[str, list[dict[str, Any]]] = {"document": [], "artifact": []}
    for r in conn.execute(
        "SELECT * FROM document_relation WHERE retracted_at IS NULL AND (source_id = ? OR target_id = ?) ORDER BY kind, accepted_at",
        (document_id, document_id),
    ):
        outgoing = r["source_id"] == document_id
        out["document"].append({"relation_id": r["relation_id"], "kind": r["kind"], "direction": "outgoing" if outgoing else "incoming",
                                "other_document": r["target_id"] if outgoing else r["source_id"], "position": r["position"],
                                "accepted_by": r["accepted_by"], "accepted_at": r["accepted_at"], "note": r["note"]})
    for artifact_id in artifacts:
        for r in conn.execute(
            "SELECT * FROM artifact_relation WHERE retracted_at IS NULL AND (source_id = ? OR target_id = ?) ORDER BY kind, accepted_at",
            (artifact_id, artifact_id),
        ):
            outgoing = r["source_id"] == artifact_id
            out["artifact"].append({"relation_id": r["relation_id"], "kind": r["kind"], "direction": "outgoing" if outgoing else "incoming",
                                    "artifact_id": artifact_id, "other_artifact": r["target_id"] if outgoing else r["source_id"],
                                    "accepted_by": r["accepted_by"], "accepted_at": r["accepted_at"], "note": r["note"]})
    return out


# ------------------------------------------------------------------------------------------------ merge and split


def _event(conn: sqlite3.Connection, document_id: str, event: str, actor: str, detail: dict[str, Any]) -> None:
    conn.execute("INSERT INTO document_event (document_id, event, at, actor, detail) VALUES (?, ?, ?, ?, ?)",
                 (document_id, event, utc_now(), actor, _dump(detail)))


def merge_documents(conn: sqlite3.Connection, keep: str, absorb: str, *, actor: str, reason: str | None = None) -> dict[str, Any]:
    """Make two documents ONE: `absorb`'s artifacts move to `keep` as alternate copies, `absorb` is retired (not deleted), and
    what hung off it (relations, collections, tags, accepted values `keep` lacks) comes with it. Every artifact id is
    unchanged. Caller owns the transaction."""
    if keep == absorb:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, "A document cannot be merged into itself.")
    _active_document(conn, keep)
    _active_document(conn, absorb)
    moved = [r["artifact_id"] for r in conn.execute("SELECT artifact_id FROM document_artifact WHERE document_id = ? ORDER BY canonical DESC, artifact_id", (absorb,))]
    old_canonical = conn.execute("SELECT artifact_id FROM document_artifact WHERE document_id = ? AND canonical = 1", (absorb,)).fetchone()
    now = utc_now()
    conn.execute("UPDATE document_artifact SET document_id = ?, canonical = 0, canonical_reason = ?, "
                 "role = CASE role WHEN 'primary' THEN 'alternate_copy' ELSE role END WHERE document_id = ?",
                 (keep, f"merged from {absorb}", absorb))
    conn.execute("UPDATE document SET retired_at = ?, merged_into = ? WHERE document_id = ?", (now, keep, absorb))
    # Documents merged into the absorbed one earlier now name the survivor, so a retired document always points at a LIVE one and a
    # split can still find its way back to it.
    conn.execute("UPDATE document SET merged_into = ? WHERE merged_into = ?", (keep, absorb))
    # Relations that pointed at the absorbed document now point at the survivor; one that would relate it to itself is retracted.
    for r in conn.execute("SELECT * FROM document_relation WHERE retracted_at IS NULL AND (source_id = ? OR target_id = ?)", (absorb, absorb)).fetchall():
        source, target = (keep if r["source_id"] == absorb else r["source_id"]), (keep if r["target_id"] == absorb else r["target_id"])
        duplicate = conn.execute("SELECT 1 FROM document_relation WHERE kind = ? AND source_id = ? AND target_id = ? AND retracted_at IS NULL AND relation_id <> ?",
                                 (r["kind"], source, target, r["relation_id"])).fetchone()
        if source == target or duplicate:
            conn.execute("UPDATE document_relation SET retracted_at = ?, retracted_by = ?, note = COALESCE(note || '; ', '') || 'folded by a merge' WHERE relation_id = ?",
                         (now, actor, r["relation_id"]))
        else:
            conn.execute("UPDATE document_relation SET source_id = ?, target_id = ? WHERE relation_id = ?", (source, target, r["relation_id"]))
    conn.execute("INSERT OR IGNORE INTO collection_member (collection_id, document_id, added_at, added_by) "
                 "SELECT collection_id, ?, added_at, added_by FROM collection_member WHERE document_id = ?", (keep, absorb))
    conn.execute("INSERT OR IGNORE INTO document_tag (document_id, tag_key, tag, added_at, added_by) "
                 "SELECT ?, tag_key, tag, added_at, added_by FROM document_tag WHERE document_id = ?", (keep, absorb))
    copied = []
    for r in conn.execute("SELECT * FROM metadata_value WHERE document_id = ?", (absorb,)).fetchall():
        if conn.execute("SELECT 1 FROM metadata_value WHERE document_id = ? AND field = ?", (keep, r["field"])).fetchone() is None:
            conn.execute(
                "INSERT INTO metadata_value (document_id, field, value, origin, source, source_candidate_id, evidence_json, locked, accepted_by, accepted_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                (keep, r["field"], r["value"], r["origin"], r["source"], r["evidence_json"], r["locked"], r["accepted_by"], r["accepted_at"], now))
            conn.execute("INSERT INTO metadata_history (document_id, field, old_value, old_source, new_value, new_source, candidate_id, at, actor, reason) "
                         "VALUES (?, ?, NULL, NULL, ?, ?, NULL, ?, ?, ?)",
                         (keep, r["field"], r["value"], r["source"], now, "resolver" if actor.startswith("rule:") else actor, f"carried over from the merged document {absorb[:12]}"))
            copied.append(r["field"])
    _repoint_proposals(conn, keep, absorb, actor, now)
    detail = {"artifacts": moved, "reason": reason, "carried_over_fields": copied}
    _event(conn, absorb, "merged_into", actor, {**detail, "into": keep, "canonical_artifact": old_canonical[0] if old_canonical else None})
    _event(conn, keep, "absorbed", actor, {**detail, "from": absorb})
    bump_revision(conn)
    return {"kept": keep, "absorbed": absorb, "artifacts_moved": moved, "fields_carried_over": copied}


def _repoint_proposals(conn: sqlite3.Connection, keep: str, absorb: str, actor: str, now: str) -> None:
    """Open proposals that named the absorbed document now name the survivor. Their evidence did not change (a third copy that
    is identical to the absorbed one is identical to the survivor too), so they stay valid; letting them go stale would make
    merging a group of fourteen copies one at a time impossible. A proposal that only said "these two are one" is now true."""
    for r in conn.execute("SELECT * FROM relation_candidate WHERE status = 'proposed' AND level = 'document' AND (source_id = ? OR target_id = ?)", (absorb, absorb)).fetchall():
        other = r["target_id"] if r["source_id"] == absorb else r["source_id"]
        if other == keep:
            conn.execute("UPDATE relation_candidate SET status = 'accepted', decided_at = ?, decided_by = ?, updated_at = ? WHERE candidate_id = ?",
                         (now, f"merge:{actor}", now, r["candidate_id"]))
            continue
        source, target = orient(r["kind"], keep if r["source_id"] == absorb else r["source_id"], keep if r["target_id"] == absorb else r["target_id"])
        clash = conn.execute("SELECT 1 FROM relation_candidate WHERE kind = ? AND source_id = ? AND target_id = ? AND evidence_key = ? AND candidate_id <> ?",
                             (r["kind"], source, target, r["evidence_key"], r["candidate_id"])).fetchone()
        if source == target or clash:
            conn.execute("UPDATE relation_candidate SET status = 'stale', updated_at = ? WHERE candidate_id = ?", (now, r["candidate_id"]))
        else:
            conn.execute("UPDATE relation_candidate SET source_id = ?, target_id = ?, updated_at = ? WHERE candidate_id = ?", (source, target, now, r["candidate_id"]))
    for r in conn.execute("SELECT candidate_id, members_json FROM relation_candidate WHERE status = 'proposed' AND level = 'group' AND members_json LIKE ?", (f'%"{absorb}"%',)).fetchall():
        members = sorted({keep if m == absorb else m for m in json.loads(r["members_json"])})
        conn.execute("UPDATE relation_candidate SET members_json = ?, updated_at = ? WHERE candidate_id = ?", (_dump(members), now, r["candidate_id"]))


def merge(conn: sqlite3.Connection, keep: str, absorb: str, *, actor: str = "user", reason: str | None = None) -> dict[str, Any]:
    with transaction(conn):
        return merge_documents(conn, keep, absorb, actor=actor, reason=reason)


def set_canonical(conn: sqlite3.Connection, document_id: str, artifact_id: str, *, actor: str = "user", reason: str | None = None) -> bool:
    """Choose which of a document's artifacts is the one read for text and metadata."""
    with transaction(conn):
        _active_document(conn, document_id)
        row = conn.execute("SELECT canonical FROM document_artifact WHERE document_id = ? AND artifact_id = ?", (document_id, artifact_id)).fetchone()
        if row is None:
            raise KvError(ErrorCode.NOT_FOUND, f"Artifact {artifact_id[:16]} does not belong to that document.", {"artifact_id": artifact_id})
        if row["canonical"]:
            return False
        conn.execute("UPDATE document_artifact SET canonical = 0 WHERE document_id = ?", (document_id,))
        conn.execute("UPDATE document_artifact SET canonical = 1, canonical_reason = ? WHERE document_id = ? AND artifact_id = ?",
                     (reason or f"chosen by {actor}", document_id, artifact_id))
        bump_revision(conn)
        return True


def split_document(conn: sqlite3.Connection, document_id: str, artifact_id: str, *, actor: str = "user") -> dict[str, Any]:
    """Take one artifact out of a document that has several, into a document of its own. If that artifact used to be a document
    that a merge retired, that document is REVIVED under its original id; otherwise a new one is made."""
    with transaction(conn):
        _active_document(conn, document_id)
        members = conn.execute("SELECT * FROM document_artifact WHERE document_id = ? ORDER BY canonical DESC, artifact_id", (document_id,)).fetchall()
        if artifact_id not in {m["artifact_id"] for m in members}:
            raise KvError(ErrorCode.NOT_FOUND, f"Artifact {artifact_id[:16]} does not belong to that document.", {"artifact_id": artifact_id})
        if len(members) < 2:
            raise KvError(ErrorCode.INVALID_ARGUMENTS, "That document has only one artifact; there is nothing to split off.")
        revive = None
        for event in conn.execute("SELECT * FROM document_event WHERE event = 'merged_into' ORDER BY event_id DESC"):
            detail = json.loads(event["detail"] or "{}")
            retired = conn.execute("SELECT * FROM document WHERE document_id = ?", (event["document_id"],)).fetchone()
            if detail.get("artifacts") == [artifact_id] and retired["retired_at"] is not None and retired["merged_into"] == document_id:
                revive = event["document_id"]
                break
        now = utc_now()
        was_canonical = any(m["canonical"] for m in members if m["artifact_id"] == artifact_id)
        if revive is not None:
            new_id_ = revive
            conn.execute("UPDATE document SET retired_at = NULL, merged_into = NULL WHERE document_id = ?", (new_id_,))
            _event(conn, new_id_, "revived", actor, {"from": document_id, "artifact": artifact_id})
        else:
            new_id_ = new_id()
            conn.execute("INSERT INTO document (document_id, created_at) VALUES (?, ?)", (new_id_, now))
            _event(conn, new_id_, "created_by_split", actor, {"from": document_id, "artifact": artifact_id})
        conn.execute("UPDATE document_artifact SET document_id = ?, role = 'primary', canonical = 1, canonical_reason = ? WHERE artifact_id = ?",
                     (new_id_, f"split from {document_id}", artifact_id))
        if was_canonical:
            successor = next(m["artifact_id"] for m in members if m["artifact_id"] != artifact_id)
            conn.execute("UPDATE document_artifact SET canonical = 1, canonical_reason = 'promoted when the canonical artifact was split off' "
                         "WHERE document_id = ? AND artifact_id = ?", (document_id, successor))
        _event(conn, document_id, "split_off", actor, {"artifact": artifact_id, "into": new_id_, "revived": revive is not None})
        bump_revision(conn)
        return {"document_id": new_id_, "from": document_id, "artifact_id": artifact_id, "revived": revive is not None}


# ------------------------------------------------------------------------------------------------ accepting a proposal


def accept_candidate(conn: sqlite3.Connection, candidate_id: str, *, actor: str = "user", keep: str | None = None) -> dict[str, Any]:
    """Accept a proposal: record the relation, merge the documents, or make the collection it describes."""
    from knowledgevista.services import organize  # local: organize imports nothing from here, but keeps the import graph acyclic

    with transaction(conn):
        row = conn.execute("SELECT * FROM relation_candidate WHERE candidate_id = ?", (candidate_id,)).fetchone()
        if row is None:
            raise KvError(ErrorCode.NOT_FOUND, f"No relation proposal {candidate_id}.", {"candidate_id": candidate_id})
        if row["status"] == "accepted":
            return {"candidate_id": candidate_id, "kind": row["kind"], "effect": "already_accepted"}
        if row["status"] == "stale":
            raise KvError(ErrorCode.INVALID_ARGUMENTS, "That proposal is stale: the evidence for it was not found the last time. Run: kv relate", {"candidate_id": candidate_id})
        kind, effect = row["kind"], {}
        if kind == "same_document":
            first, second = row["source_id"], row["target_id"]
            if keep is not None and keep not in (first, second):
                raise KvError(ErrorCode.INVALID_ARGUMENTS, "--keep must be one of the two documents being merged.")
            survivor = keep or _preferred_survivor(conn, first, second)
            effect = {"effect": "merged", **merge_documents(conn, survivor, second if survivor == first else first, actor=actor,
                                                              reason=f"accepted proposal {candidate_id[:8]}")}
        elif kind == "collection":
            members = json.loads(row["members_json"] or "[]")
            evidence = json.loads(row["evidence_json"])
            effect = {"effect": "collection", **organize.create_collection_from_group(conn, evidence.get("name") or "Untitled group", members,
                                                                                       source_doi=evidence.get("parent_doi"), actor=actor)}
        elif row["level"] == "artifact":
            relation_id, created = add_relation(conn, "artifact", kind, row["source_id"], row["target_id"], actor=actor, candidate_id=candidate_id)
            effect = {"effect": "relation", "relation_id": relation_id, "created": created}
        else:
            relation_id, created = add_relation(conn, "document", kind, row["source_id"], row["target_id"], actor=actor, candidate_id=candidate_id)
            effect = {"effect": "relation", "relation_id": relation_id, "created": created}
        now = utc_now()
        conn.execute("UPDATE relation_candidate SET status = 'accepted', decided_at = ?, decided_by = ?, updated_at = ? WHERE candidate_id = ?",
                     (now, actor, now, candidate_id))
        bump_revision(conn)
        return {"candidate_id": candidate_id, "kind": kind, **effect}


def _preferred_survivor(conn: sqlite3.Connection, a: str, b: str) -> str:
    """Which of two documents survives a merge when the person did not say: the one with more accepted metadata, then the older."""
    def weight(document_id: str) -> tuple[int, str]:
        values = conn.execute("SELECT COUNT(*) FROM metadata_value WHERE document_id = ?", (document_id,)).fetchone()[0]
        created = conn.execute("SELECT created_at FROM document WHERE document_id = ?", (document_id,)).fetchone()[0]
        return (-values, created + document_id)
    return min((a, b), key=weight)
