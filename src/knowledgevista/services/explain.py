"""`kv explain`: everything the catalog knows about one document, and where each fact came from.

Read-only. Built to answer "what is this, where is it, what happened to it, and is anything wrong?" without anyone
reading SQL. Sections that belong to later milestones (extraction) say so explicitly rather than
being left out: an absent section would read as "none exist".
"""

from __future__ import annotations

import json
import sqlite3

from knowledgevista.domain.kinds import extension_kind, extension_of, kinds_disagree
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import organize, relations
from knowledgevista.services.metadata_report import document_metadata


def explain_document(conn: sqlite3.Connection, document_id: str) -> dict:
    document = conn.execute("SELECT * FROM document WHERE document_id = ?", (document_id,)).fetchone()
    if document is None:
        raise KvError(ErrorCode.NOT_FOUND, f"No document {document_id}.", {"document_id": document_id})
    artifacts = []
    warnings = []
    for art in conn.execute(
        "SELECT a.*, da.role, da.canonical, da.canonical_reason FROM artifact a "
        "JOIN document_artifact da ON da.artifact_id = a.artifact_id WHERE da.document_id = ? "
        "ORDER BY da.canonical DESC, a.first_seen",
        (document_id,),
    ):
        locations = []
        for loc in conn.execute(
            "SELECT l.*, r.label AS root_label, r.status AS root_status FROM location l JOIN root r ON r.root_id = l.root_id "
            "WHERE l.artifact_id = ? ORDER BY l.ended_at IS NOT NULL, l.first_seen, l.relative_path",
            (art["artifact_id"],),
        ):
            events = [
                {"at": e["at"], "event": e["event"], "actor": e["actor"], "detail": e["detail"]}
                for e in conn.execute(
                    "SELECT at, event, actor, detail FROM location_event WHERE location_id = ? ORDER BY event_id",
                    (loc["location_id"],),
                )
            ]
            locations.append({
                "location_id": loc["location_id"], "root": loc["root_label"], "root_status": loc["root_status"],
                "path": loc["relative_path"], "state": loc["state"], "first_seen": loc["first_seen"],
                "last_seen": loc["last_seen"], "ended_at": loc["ended_at"], "end_reason": loc["end_reason"],
                "successor_location_id": loc["successor_location_id"], "history": events,
            })
            extension = extension_of(loc["relative_path"])
            if loc["ended_at"] is None and kinds_disagree(extension, art["content_kind"]):
                warnings.append({
                    "code": "KVD_KIND_MISMATCH",
                    "message": f"{loc['relative_path']} is named .{extension} ({extension_kind(loc['relative_path'])}) "
                               f"but its bytes look like {art['content_kind']}.",
                })
        current = [loc for loc in locations if loc["ended_at"] is None]
        available = any(loc["state"] == "active" and loc["root_status"] == "online" for loc in current)
        artifacts.append({
            "artifact_id": art["artifact_id"], "size": art["size"], "content_kind": art["content_kind"],
            "role": art["role"], "canonical": bool(art["canonical"]), "canonical_reason": art["canonical_reason"],
            "first_seen": art["first_seen"], "last_seen": art["last_seen"],
            # Derived, never stored: some current location is present and its root is reachable.
            "available": available, "locations": locations,
        })
        if not available:
            warnings.append({
                "code": "KVD_NOT_AVAILABLE",
                "message": f"No reachable copy of {art['artifact_id'][:12]} exists right now (missing files or an unavailable root).",
            })
    return {
        "document_id": document_id, "created_at": document["created_at"], "artifacts": artifacts,
        "metadata": document_metadata(conn, document_id),
        "relations": _relations(conn, document_id, document),
        "extraction": {"status": "not_yet_available"},
        "organization": {"collections": organize.collections_of(conn, document_id), "tags": organize.tags_of(conn, document_id)},
        "warnings": warnings,
    }


def _relations(conn: sqlite3.Connection, document_id: str, document: sqlite3.Row) -> dict:
    """Accepted relations, open proposals about this document or its artifacts, and the merge/split history."""
    artifacts = [r[0] for r in conn.execute("SELECT artifact_id FROM document_artifact WHERE document_id = ?", (document_id,))]
    ends = [document_id, *artifacts]
    marks = ",".join("?" * len(ends))
    proposals = [
        {"candidate_id": r["candidate_id"], "kind": r["kind"], "level": r["level"], "source": r["source_id"], "target": r["target_id"],
         "confidence": r["confidence"], "evidence": json.loads(r["evidence_json"])}
        for r in conn.execute(
            f"SELECT * FROM relation_candidate WHERE status = 'proposed' AND level <> 'group' AND (source_id IN ({marks}) OR target_id IN ({marks})) "
            "ORDER BY kind, candidate_id", (*ends, *ends))
    ]
    groups = [{"candidate_id": r["candidate_id"], "kind": r["kind"], "confidence": r["confidence"]} for r in conn.execute(
        "SELECT candidate_id, kind, confidence FROM relation_candidate WHERE status = 'proposed' AND level = 'group' AND members_json LIKE ?",
        (f'%"{document_id}"%',))]
    history = [{"at": r["at"], "event": r["event"], "actor": r["actor"], "detail": json.loads(r["detail"] or "{}")} for r in conn.execute(
        "SELECT * FROM document_event WHERE document_id = ? ORDER BY event_id", (document_id,))]
    return {"status": "available", **relations.relations_of_document(conn, document_id), "proposals": proposals, "group_proposals": groups,
            "history": history, "retired_at": document["retired_at"], "merged_into": document["merged_into"]}
