"""Reading metadata back: one document's values with their provenance (`kv explain`), the library's coverage (`kv stats`),
and the accounting that every PDF either has a title or a stated reason it does not.

Read-only. "No title" is never left unexplained: a document without an accepted title is in exactly one of the states
below, and the stats say how many are in each, so a gap in the library is a number with a cause rather than a silence.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from knowledgevista.domain import fields as fieldmod

#: Why a document has no accepted title, in the order they are tested. Every PDF without one is in exactly one.
TITLE_REASONS = {
    "not_extracted": "its text has not been extracted yet (kv extract)",
    "not_resolved_yet": "kv resolve has not been run",
    "candidates_awaiting_review": "a title was proposed and is waiting for a person (kv review list)",
    "no_text_layer": "it is a scan with no text layer, so there was nothing to read a title from",
    "no_title_found": "nothing in the file or its metadata looked like a title",
}


def document_metadata(conn: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    values = [{
        "field": r["field"], "value": r["value"], "display": fieldmod.display(r["field"], r["value"]), "origin": r["origin"],
        "source": r["source"], "locked": bool(r["locked"]), "accepted_by": r["accepted_by"], "accepted_at": r["accepted_at"],
        "candidate_id": r["source_candidate_id"],
    } for r in conn.execute("SELECT * FROM metadata_value WHERE document_id = ? ORDER BY field", (document_id,))]
    candidates = [{
        "candidate_id": r["candidate_id"], "field": r["field"], "value": r["value"], "display": fieldmod.display(r["field"], r["value"]),
        "source": r["source"], "origin": r["origin"], "status": r["status"], "classification": r["classification"],
        "confidence": r["confidence"], "review": r["review"], "decided_by": r["decided_by"], "evidence": json.loads(r["evidence_json"]),
        "matcher_version": r["matcher_version"],
    } for r in conn.execute("SELECT * FROM metadata_candidate WHERE document_id = ? ORDER BY field, priority, created_at", (document_id,))]
    history = [{"at": r["at"], "field": r["field"], "old": r["old_value"], "old_source": r["old_source"], "new": r["new_value"],
                "new_source": r["new_source"], "actor": r["actor"], "reason": r["reason"]}
               for r in conn.execute("SELECT * FROM metadata_history WHERE document_id = ? ORDER BY history_id DESC LIMIT 25", (document_id,))]
    return {"status": "available", "values": values, "candidates": candidates, "history": history}


def title_states(conn: sqlite3.Connection, index: sqlite3.Connection | None) -> dict[str, str]:
    """document_id -> `accepted` or one of TITLE_REASONS, for every document whose primary artifact is a PDF."""
    resolved_ever = conn.execute("SELECT COUNT(*) FROM resolve_run").fetchone()[0] > 0
    titled = {r[0] for r in conn.execute("SELECT document_id FROM metadata_value WHERE field = 'title'")}
    awaiting = {r[0] for r in conn.execute("SELECT document_id FROM metadata_candidate WHERE field = 'title' AND status = 'proposed'")}
    extractions = {}
    if index is not None:
        extractions = {r["artifact_id"]: r["legacy_scanned"] for r in index.execute("SELECT artifact_id, legacy_scanned FROM extraction")}
    states = {}
    for row in conn.execute(
        "SELECT da.document_id, da.artifact_id FROM document_artifact da JOIN artifact a ON a.artifact_id = da.artifact_id "
        "WHERE da.canonical = 1 AND a.content_kind = 'pdf'"
    ):
        document_id, artifact_id = row["document_id"], row["artifact_id"]
        if document_id in titled:
            states[document_id] = "accepted"
        elif artifact_id not in extractions:
            states[document_id] = "not_extracted"
        elif document_id in awaiting:
            states[document_id] = "candidates_awaiting_review"
        elif not resolved_ever:
            states[document_id] = "not_resolved_yet"
        elif extractions[artifact_id]:
            states[document_id] = "no_text_layer"
        else:
            states[document_id] = "no_title_found"
    return states


def metadata_stats(conn: sqlite3.Connection, index: sqlite3.Connection | None) -> dict[str, Any]:
    pdfs = conn.execute("SELECT COUNT(*) FROM document_artifact da JOIN artifact a ON a.artifact_id = da.artifact_id "
                        "WHERE da.canonical = 1 AND a.content_kind = 'pdf'").fetchone()[0]
    accepted = {r["field"]: r["n"] for r in conn.execute("SELECT field, COUNT(DISTINCT document_id) AS n FROM metadata_value GROUP BY field")}
    states = title_states(conn, index)
    without: dict[str, int] = {}
    for state in states.values():
        if state != "accepted":
            without[state] = without.get(state, 0) + 1
    queue = {"proposed_items": 0, "safe": 0, "required": 0}
    for row in conn.execute("SELECT document_id, field, value, MAX(review = 'safe') AS safe FROM metadata_candidate WHERE status = 'proposed' GROUP BY document_id, field, value"):
        queue["proposed_items"] += 1
        queue["safe" if row["safe"] else "required"] += 1
    last = conn.execute("SELECT run_id, started_at, status, online, accept_safe FROM resolve_run ORDER BY started_at DESC LIMIT 1").fetchone()
    return {
        "pdf_documents": pdfs, "accepted": accepted,
        "doi_coverage": round(accepted.get("doi", 0) / pdfs, 4) if pdfs else None,
        "title_coverage": round(accepted.get("title", 0) / pdfs, 4) if pdfs else None,
        "titles_without": without, "title_reasons": {k: v for k, v in TITLE_REASONS.items() if k in without},
        "review_queue": queue, "resolve_runs": conn.execute("SELECT COUNT(*) FROM resolve_run").fetchone()[0],
        "last_resolve": dict(last) if last else None,
    }
