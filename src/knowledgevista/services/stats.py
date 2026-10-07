"""`kv stats`: what the library holds (inventory) kept apart from how healthy the catalog is (health).

Counts and health are different questions ("1,862 files" is not "7 need attention"), so they are separate sections.
"""

from __future__ import annotations

import sqlite3
from collections import Counter

from knowledgevista.domain.kinds import extension_kind, extension_of, kinds_disagree
from knowledgevista.services.metadata_report import metadata_stats
from knowledgevista.services.search import coverage


def library_stats(conn: sqlite3.Connection, index: sqlite3.Connection | None = None) -> dict:
    scalar = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731 - a one-line local helper
    by_state = {r["state"]: r["n"] for r in conn.execute(
        "SELECT state, COUNT(*) AS n FROM location WHERE ended_at IS NULL GROUP BY state")}
    by_extension_kind: Counter[str] = Counter()
    by_content_kind: Counter[str] = Counter()
    mismatches = 0
    for row in conn.execute(
        "SELECT l.relative_path, a.content_kind FROM location l JOIN artifact a ON a.artifact_id = l.artifact_id "
        "WHERE l.ended_at IS NULL"
    ):
        by_extension_kind[extension_kind(row["relative_path"])] += 1
        by_content_kind[row["content_kind"]] += 1
        if kinds_disagree(extension_of(row["relative_path"]), row["content_kind"]):
            mismatches += 1
    duplicate_groups = conn.execute(
        "SELECT COUNT(*) AS groups, COALESCE(SUM(n), 0) AS files, COALESCE(SUM((n - 1) * size), 0) AS wasted FROM ("
        " SELECT l.artifact_id, COUNT(*) AS n, a.size AS size FROM location l JOIN artifact a ON a.artifact_id = l.artifact_id"
        " WHERE l.ended_at IS NULL AND l.state = 'active' GROUP BY l.artifact_id HAVING n > 1)"
    ).fetchone()
    roots = [dict(r) for r in conn.execute(
        "SELECT r.label, r.status, r.last_scan_at, "
        "(SELECT COUNT(*) FROM location l WHERE l.root_id = r.root_id AND l.ended_at IS NULL) AS locations "
        "FROM root r ORDER BY r.created_at")]
    return {
        "inventory": {
            "roots": len(roots), "documents": scalar("SELECT COUNT(*) FROM document WHERE retired_at IS NULL"),
            "documents_merged_away": scalar("SELECT COUNT(*) FROM document WHERE retired_at IS NOT NULL"),
            "artifacts": scalar("SELECT COUNT(*) FROM artifact"),
            "locations_current": sum(by_state.values()), "locations_by_state": by_state,
            "locations_ended": scalar("SELECT COUNT(*) FROM location WHERE ended_at IS NOT NULL"),
            "total_bytes": scalar("SELECT COALESCE(SUM(size), 0) FROM artifact"),
            "by_extension_kind": dict(by_extension_kind.most_common()),
            "by_content_kind": dict(by_content_kind.most_common()),
            "roots_detail": roots,
        },
        "health": {
            "locations_missing": by_state.get("missing", 0),
            "locations_inaccessible": by_state.get("inaccessible", 0),
            "roots_not_online": sum(1 for r in roots if r["status"] != "online"),
            "exact_duplicate_groups": duplicate_groups["groups"],
            "exact_duplicate_files": duplicate_groups["files"],
            "exact_duplicate_bytes_reclaimable": duplicate_groups["wasted"],
            "extension_content_mismatches": mismatches,
            "interrupted_scans": scalar("SELECT COUNT(*) FROM scan_run WHERE status = 'interrupted'"),
            "catalog_revision": scalar("SELECT catalog_revision FROM library"),
        },
        "search": coverage(conn, index),
        "metadata": metadata_stats(conn, index),
        "organization": {
            "collections": scalar("SELECT COUNT(*) FROM collection WHERE retired_at IS NULL"),
            "tags": scalar("SELECT COUNT(DISTINCT tag_key) FROM document_tag"),
            "saved_searches": scalar("SELECT COUNT(*) FROM saved_search WHERE retired_at IS NULL"),
            "document_relations": scalar("SELECT COUNT(*) FROM document_relation WHERE retracted_at IS NULL"),
            "artifact_relations": scalar("SELECT COUNT(*) FROM artifact_relation WHERE retracted_at IS NULL"),
            "relation_proposals_waiting": scalar("SELECT COUNT(*) FROM relation_candidate WHERE status = 'proposed'"),
            "documents_with_several_artifacts": scalar(
                "SELECT COUNT(*) FROM (SELECT da.document_id FROM document_artifact da JOIN document d ON d.document_id = da.document_id "
                "WHERE d.retired_at IS NULL GROUP BY da.document_id HAVING COUNT(*) > 1)"),
        },
    }
