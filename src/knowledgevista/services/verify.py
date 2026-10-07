"""`kv verify`: re-read every active file and check its bytes still hash to the artifact the catalog says it is.

This is the proof that `size + mtime` is only a hint. A scan skips a file whose size and mtime are unchanged; a file
rewritten with the same size and a restored mtime slips past it, and only this command reads the bytes and notices.
It is READ-ONLY: it reports, it does not "fix" the catalog (run `kv scan --full` to reconcile what it found).

It reads every active file, so it costs a full pass over the library. That is why it is not part of `doctor`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from knowledgevista.domain.pathkeys import fs_path, join_relative
from knowledgevista.services.hashing import HashOutcome, hash_file


@dataclass
class VerifyReport:
    checked: int = 0
    ok: int = 0
    mismatched: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unreadable: list[dict[str, Any]] = field(default_factory=list)
    changing: list[str] = field(default_factory=list)
    skipped_roots: list[dict[str, Any]] = field(default_factory=list)
    bytes_read: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def verify_hashes(
    conn: sqlite3.Connection, root_ids: list[str] | None = None, *, hasher: Callable[[str], HashOutcome] = hash_file,
    progress: Callable[[str], None] | None = None,
) -> VerifyReport:
    report = VerifyReport()
    roots = conn.execute("SELECT * FROM root WHERE enabled = 1 ORDER BY created_at").fetchall()
    for root in roots:
        if root_ids is not None and root["root_id"] not in root_ids:
            continue
        if root["status"] != "online":
            report.skipped_roots.append({"root": root["label"], "status": root["status"]})
            continue
        rows = conn.execute(
            "SELECT location_id, relative_path, artifact_id FROM location "
            "WHERE root_id = ? AND ended_at IS NULL AND state = 'active' ORDER BY path_key",
            (root["root_id"],),
        ).fetchall()
        for index, row in enumerate(rows, start=1):
            if progress and (index == 1 or index % 100 == 0):
                progress(f"verifying {root['label']} {index}/{len(rows)}")
            out = hasher(fs_path(join_relative(root["configured_path"], row["relative_path"])))
            report.checked += 1
            if out.kind == "unreadable":
                if "FileNotFoundError" in (out.error or ""):
                    report.missing.append(row["relative_path"])
                else:
                    report.unreadable.append({"path": row["relative_path"], "error": out.error})
            elif out.kind == "changing":
                report.changing.append(row["relative_path"])
            else:
                report.bytes_read += out.size or 0
                if out.sha256 == row["artifact_id"]:
                    report.ok += 1
                else:
                    report.mismatched.append({
                        "location_id": row["location_id"], "path": row["relative_path"],
                        "expected": row["artifact_id"], "actual": out.sha256,
                    })
    return report
