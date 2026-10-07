"""`kv doctor`: structural health of the catalog and the roots. READ-ONLY, and cheap.

It diagnoses and never repairs (a diagnostic that mutates is a surprise), and it never reads file contents: hashing
a 2.3 GB library is `kv verify`, a different command with a different cost. The only filesystem call it makes is a
listing of each root's top directory, to compare reality with the stored root status.

Findings are grouped by category so the output scales: filesystem, catalog, extraction, search, metadata,
relationships, operations, integration, cache. Categories whose features do not exist yet report nothing, which here
means "not applicable", and `categories_checked` says exactly which ones were examined so silence is not misread.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from knowledgevista.db.migrations import current_version, load_migrations
from knowledgevista.domain.pathkeys import fs_path

CHECKED_CATEGORIES = ["filesystem", "catalog"]


@dataclass
class Finding:
    category: str
    severity: str  # info | warning | error
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"category": self.category, "severity": self.severity, "code": self.code,
                "message": self.message, "details": self.details}


def run_doctor(conn: sqlite3.Connection) -> list[Finding]:
    found: list[Finding] = []
    add = lambda *a, **k: found.append(Finding(*a, **k))  # noqa: E731

    # --- catalog ---
    quick = conn.execute("PRAGMA quick_check").fetchone()[0]
    if quick != "ok":
        add("catalog", "error", "KVD_INTEGRITY", f"SQLite quick_check reported: {quick}")
    for row in conn.execute("PRAGMA foreign_key_check").fetchall()[:50]:
        add("catalog", "error", "KVD_ORPHAN_ROW", f"{row['table']} row {row['rowid']} points at a missing {row['parent']} row.",
            {"table": row["table"], "rowid": row["rowid"], "parent": row["parent"]})
    latest = load_migrations()[-1].version
    if current_version(conn) != latest:
        add("catalog", "warning", "KVD_SCHEMA_BEHIND", f"Schema is at version {current_version(conn)}, latest is {latest}.")
    for row in conn.execute(
        "SELECT d.document_id FROM document d WHERE NOT EXISTS ("
        "SELECT 1 FROM document_artifact da WHERE da.document_id = d.document_id AND da.canonical = 1)"
    ):
        add("catalog", "error", "KVD_NO_CANONICAL", "A document has no canonical artifact.", {"document_id": row[0]})
    for row in conn.execute(
        "SELECT a.artifact_id FROM artifact a WHERE NOT EXISTS ("
        "SELECT 1 FROM document_artifact da WHERE da.artifact_id = a.artifact_id)"
    ):
        add("catalog", "error", "KVD_ARTIFACT_WITHOUT_DOCUMENT", "An artifact belongs to no document.", {"artifact_id": row[0]})
    for row in conn.execute(
        "SELECT location_id FROM location WHERE ended_at IS NOT NULL AND end_reason = 'moved' AND successor_location_id IS NULL"
    ):
        add("catalog", "error", "KVD_MOVE_WITHOUT_SUCCESSOR", "A location was ended as moved but has no successor.", {"location_id": row[0]})
    for row in conn.execute("SELECT run_id, root_id, started_at FROM scan_run WHERE status = 'running'"):
        add("catalog", "warning", "KVD_STALE_RUN", "A scan was started and never finished (killed?). The next scan closes it.",
            {"run_id": row["run_id"], "started_at": row["started_at"]})

    # --- filesystem ---
    for root in conn.execute("SELECT * FROM root ORDER BY created_at"):
        try:
            with os.scandir(fs_path(root["configured_path"])):
                actual = "online"
        except PermissionError:
            actual = "permission_denied"
        except OSError:
            actual = "unavailable"
        if actual != "online":
            add("filesystem", "warning", "KVD_ROOT_NOT_ONLINE",
                f"{root['label']} ({root['configured_path']}) cannot be read right now: {actual}.",
                {"root_id": root["root_id"], "status": actual})
        elif root["status"] != "online":
            add("filesystem", "info", "KVD_ROOT_STATUS_STALE",
                f"{root['label']} is reachable again but still recorded as {root['status']}; scan to reconcile.",
                {"root_id": root["root_id"]})
        if root["last_scan_at"] is None:
            add("filesystem", "info", "KVD_NEVER_SCANNED", f"{root['label']} has never been scanned.", {"root_id": root["root_id"]})
        counts = {r["state"]: r["n"] for r in conn.execute(
            "SELECT state, COUNT(*) AS n FROM location WHERE root_id = ? AND ended_at IS NULL GROUP BY state", (root["root_id"],))}
        for state, code in (("missing", "KVD_MISSING_LOCATIONS"), ("inaccessible", "KVD_INACCESSIBLE_LOCATIONS")):
            if counts.get(state):
                add("filesystem", "warning", code, f"{root['label']}: {counts[state]} file(s) are {state}.",
                    {"root_id": root["root_id"], "count": counts[state]})
    return found


def has_errors(findings: list[Finding]) -> bool:
    return any(f.severity == "error" for f in findings)
