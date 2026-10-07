"""Scan a root and reconcile what is on disk with what the catalog knows.

THE ALGORITHM (docs/ARCHITECTURE.md, "Scanning and reconciliation"), in the order it runs:

  0. Probe the root. If it cannot be listed, change nothing except the root's status: an unplugged drive is not
     a deletion, and nothing is marked missing because of it.
  1. Walk and stat (no file is read). A directory that cannot be listed is NOT empty: its contents are unseen.
  2. Compare with the known CURRENT locations. A file whose size and mtime match is "unchanged" by policy, and is
     not read. That is a freshness HINT, never proof (`scan --full` and `kv verify` re-read everything).
  3. Hash new and changed files, one transaction per file, so a crash loses at most one file's work.
       - same path, same bytes      -> refresh the hint (and revive the location if it was missing)
       - same path, DIFFERENT bytes -> end the old location ("replaced"), make a new artifact AND a new document.
                                       Nothing is inherited from the old one: that would be metadata that belongs
                                       to bytes that are no longer there.
       - new path, known bytes      -> a new location of an existing artifact: a copy, or half of a move
       - new path, new bytes        -> new artifact, new document, new location
  4. Anything not seen: `missing` (revivable) or, under an unreadable directory, `inaccessible`. NEVER deleted.
  5. Pair each NEW path with a MISSING location of the same artifact: that is a move or rename. The old location
     is ended ("moved", with its successor) and no new document is created: identity follows the bytes.

Every step records an event on the location, so "where did this file go?" is answerable from history.
"""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from knowledgevista import __version__
from knowledgevista.db.catalog import bump_revision, transaction
from knowledgevista.domain.ids import new_id, utc_now
from knowledgevista.domain.kinds import content_kind
from knowledgevista.domain.pathkeys import fs_path, join_relative, path_key
from knowledgevista.errors import ErrorCode
from knowledgevista.services.hashing import HashOutcome, hash_file
from knowledgevista.services.roots import get_root, volume_id_of
from knowledgevista.services.walker import Entry, walk_tree

MASS_MISSING_MIN = 10
MASS_MISSING_FRACTION = 0.5
MAX_LISTED_WARNINGS = 20


@dataclass
class ScanReport:
    root_id: str
    run_id: str = ""
    status: str = "completed"  # completed | skipped_root_unavailable
    root_status: str = "online"
    files_seen: int = 0
    unchanged: int = 0
    hashed: int = 0
    bytes_hashed: int = 0
    new_artifacts: int = 0
    new_documents: int = 0
    new_locations: int = 0
    moved: int = 0
    replaced: int = 0
    content_touched: int = 0
    renamed: int = 0
    went_missing: int = 0
    reappeared: int = 0
    inaccessible: int = 0
    unreadable: int = 0
    changing: int = 0
    changes: int = 0
    seconds: float = 0.0
    timing: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def warn(self, code: str, message: str, **details: Any) -> None:
        self.warnings.append({"code": code, "message": message, "details": details})

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in vars(self).items()}


def _probe(path: str, scandir: Callable) -> str:
    try:
        with scandir(fs_path(path)):
            return "online"
    except PermissionError:
        return "permission_denied"
    except OSError:
        return "unavailable"


def _event(conn: sqlite3.Connection, location_id: str, run_id: str, event: str, now: str, detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO location_event (location_id, run_id, event, at, detail) VALUES (?, ?, ?, ?, ?)",
        (location_id, run_id, event, now, detail),
    )


def _ensure_artifact(conn: sqlite3.Connection, out: HashOutcome, now: str, report: ScanReport) -> None:
    """The artifact for these bytes, with its document if it is new. Existing bytes keep their existing document."""
    existing = conn.execute("SELECT 1 FROM artifact WHERE artifact_id = ?", (out.sha256,)).fetchone()
    if existing:
        conn.execute("UPDATE artifact SET last_seen = ? WHERE artifact_id = ?", (now, out.sha256))
        return
    conn.execute(
        "INSERT INTO artifact (artifact_id, size, content_kind, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)",
        (out.sha256, out.size, content_kind(out.head, out.size), now, now),
    )
    document_id = new_id()
    conn.execute("INSERT INTO document (document_id, created_at) VALUES (?, ?)", (document_id, now))
    conn.execute(
        "INSERT INTO document_artifact (document_id, artifact_id, role, canonical, canonical_reason) "
        "VALUES (?, ?, 'primary', 1, 'first artifact of this document')",
        (document_id, out.sha256),
    )
    report.new_artifacts += 1
    report.new_documents += 1


def _insert_location(
    conn: sqlite3.Connection, root_id: str, entry: Entry, artifact_id: str | None, size: int | None,
    mtime_ns: int | None, state: str, now: str,
) -> str:
    location_id = new_id()
    conn.execute(
        "INSERT INTO location (location_id, root_id, relative_path, path_key, artifact_id, size, mtime_ns, state, "
        "first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (location_id, root_id, entry.relative_path, entry.key, artifact_id, size, mtime_ns, state, now, now),
    )
    return location_id


def _is_under(key: str, directory_keys: list[str]) -> bool:
    return any(d == "" or key == d or key.startswith(d + "/") for d in directory_keys)


def _close_stale_runs(conn: sqlite3.Connection, root_id: str, now: str) -> None:
    """A run still `running` when a new one starts was killed. Mark it, so history does not claim it is in progress."""
    with transaction(conn):
        conn.execute(
            "UPDATE scan_run SET status = 'interrupted', finished_at = ? WHERE root_id = ? AND status = 'running'",
            (now, root_id),
        )


def scan_root(
    conn: sqlite3.Connection,
    root_id: str,
    *,
    full: bool = False,
    progress: Callable[[str], None] | None = None,
    after_file: Callable[[str], None] | None = None,
    scandir: Callable = os.scandir,
    hasher: Callable[[str], HashOutcome] = hash_file,
) -> ScanReport:
    """`after_file` and `scandir`/`hasher` are test seams: a test raises from `after_file` to simulate a kill
    between two files, makes one directory unreadable through `scandir`, and changes a file mid-read through `hasher`."""
    started = time.perf_counter()
    root = get_root(conn, root_id)
    report = ScanReport(root_id=root_id)
    say = progress or (lambda _message: None)
    now = utc_now()
    _close_stale_runs(conn, root_id, now)
    run_id = new_id()
    report.run_id = run_id

    # 0. Probe. An unavailable root changes its status and NOTHING else.
    probed = _probe(root.configured_path, scandir)
    if probed != "online":
        with transaction(conn):
            conn.execute(
                "INSERT INTO scan_run (run_id, root_id, started_at, finished_at, status, app_version, full_rehash) "
                "VALUES (?, ?, ?, ?, 'skipped_root_unavailable', ?, ?)",
                (run_id, root_id, now, now, __version__, int(full)),
            )
            if probed != root.status:
                conn.execute("UPDATE root SET status = ? WHERE root_id = ?", (probed, root_id))
                bump_revision(conn)
        report.status, report.root_status = "skipped_root_unavailable", probed
        report.warn(
            ErrorCode.ROOT_UNAVAILABLE,
            f"{root.configured_path} cannot be read ({probed}). Nothing was changed; scan again when it is back.",
            path=root.configured_path, root_status=probed,
        )
        report.seconds = time.perf_counter() - started
        return report

    with transaction(conn):
        conn.execute(
            "INSERT INTO scan_run (run_id, root_id, started_at, status, app_version, full_rehash) "
            "VALUES (?, ?, ?, 'running', ?, ?)",
            (run_id, root_id, now, __version__, int(full)),
        )
        if root.status != "online":
            conn.execute("UPDATE root SET status = 'online' WHERE root_id = ?", (root_id,))
            report.changes += 1
    current_volume = volume_id_of(root.configured_path)
    if root.volume_id and current_volume and root.volume_id != current_volume:
        report.warn(
            "KV_ROOT_VOLUME_CHANGED",
            "The volume behind this root is not the one it had when it was added. Scanning continues; "
            "nothing is assumed about whether the folder moved.",
            was=root.volume_id, now=current_volume,
        )

    try:
        say(f"walking {root.configured_path}")
        walk = walk_tree(root.configured_path, scandir=scandir)
        report.files_seen = len(walk.entries)
        unreadable_keys = [path_key(d) for d in walk.unreadable_dirs]
        for directory in walk.unreadable_dirs[:MAX_LISTED_WARNINGS]:
            report.warn("KV_UNREADABLE_DIRECTORY", f"Could not list {directory or '(the root)'}; its files were left as they were.", directory=directory)
        if walk.skipped_links:
            report.warn("KV_LINKS_SKIPPED", f"{len(walk.skipped_links)} symlink/junction(s) were not followed.", examples=walk.skipped_links[:5])
        for collided in walk.key_collisions[:MAX_LISTED_WARNINGS]:
            report.warn("KV_PATH_KEY_COLLISION", f"{collided} compares equal to another file's path and was skipped.", path=collided)

        known = {
            row["path_key"]: row
            for row in conn.execute("SELECT * FROM location WHERE root_id = ? AND ended_at IS NULL", (root_id,))
        }
        seen: set[str] = set()
        to_hash: list[tuple[Entry, sqlite3.Row | None]] = []

        # 2. The fast path: size and mtime say nothing changed, so nothing is read.
        with transaction(conn):
            touched: list[str] = []
            for entry in walk.entries:
                seen.add(entry.key)
                loc = known.get(entry.key)
                fast = (
                    loc is not None and not full and loc["artifact_id"] is not None
                    and loc["size"] == entry.size and loc["mtime_ns"] == entry.mtime_ns
                )
                if not fast:
                    to_hash.append((entry, loc))
                    continue
                report.unchanged += 1
                touched.append(loc["location_id"])
                if loc["state"] != "active":
                    conn.execute("UPDATE location SET state = 'active' WHERE location_id = ?", (loc["location_id"],))
                    _event(conn, loc["location_id"], run_id, "reappeared", now)
                    report.reappeared += 1
                    report.changes += 1
                if loc["relative_path"] != entry.relative_path:
                    # A case-only rename on a case-insensitive filesystem: same file, new spelling to show.
                    conn.execute("UPDATE location SET relative_path = ? WHERE location_id = ?", (entry.relative_path, loc["location_id"]))
                    _event(conn, loc["location_id"], run_id, "renamed", now, f"{loc['relative_path']} -> {entry.relative_path}")
                    report.renamed += 1
                    report.changes += 1
            for start in range(0, len(touched), 500):
                chunk = touched[start:start + 500]
                conn.execute(
                    f"UPDATE location SET last_seen = ? WHERE location_id IN ({','.join('?' * len(chunk))})",
                    [now, *chunk],
                )

        # 3. Hash what is new or changed: one transaction per file.
        new_locations: list[tuple[str, str]] = []  # (location_id, artifact_id) for brand-new paths this run
        durations: list[tuple[float, int, str]] = []
        for index, (entry, loc) in enumerate(to_hash, start=1):
            if index == 1 or index % 100 == 0:
                say(f"hashing {index}/{len(to_hash)}")
            out = hasher(fs_path(join_relative(root.configured_path, entry.relative_path)))
            _record(conn, run_id, root_id, entry, loc, out, now, report, new_locations)
            if out.kind == "ok":
                durations.append((out.seconds, out.size or 0, entry.relative_path))
            if after_file is not None:
                after_file(entry.relative_path)

        # 4 and 5. Absence, then moves. One transaction: either both are recorded or neither.
        with transaction(conn):
            for key, loc in known.items():
                if key in seen:
                    continue
                if _is_under(key, unreadable_keys):
                    if loc["state"] == "active":
                        conn.execute("UPDATE location SET state = 'inaccessible' WHERE location_id = ?", (loc["location_id"],))
                        _event(conn, loc["location_id"], run_id, "inaccessible", now, "its directory could not be listed")
                        report.inaccessible += 1
                        report.changes += 1
                elif loc["state"] != "missing":
                    conn.execute("UPDATE location SET state = 'missing' WHERE location_id = ?", (loc["location_id"],))
                    _event(conn, loc["location_id"], run_id, "missing", now)
                    report.went_missing += 1
                    report.changes += 1
            if report.went_missing >= MASS_MISSING_MIN and report.went_missing >= MASS_MISSING_FRACTION * max(1, len(known)):
                report.warn(
                    "KV_MASS_MISSING",
                    f"{report.went_missing} of {len(known)} known files vanished at once. If this folder is on a drive that "
                    "is only partly mounted, they will reappear when it is fully back; nothing was deleted.",
                    missing=report.went_missing, known=len(known),
                )
            _pair_moves(conn, run_id, new_locations, now, report)
            if durations:
                seconds = sorted(d[0] for d in durations)
                total_seconds, total_bytes = sum(d[0] for d in durations), sum(d[1] for d in durations)
                slowest = max(durations)
                report.timing = {
                    "files": len(durations), "bytes": total_bytes, "median_s": statistics.median(seconds),
                    "p95_s": seconds[min(len(seconds) - 1, int(0.95 * len(seconds)))], "max_s": slowest[0],
                    "slowest": slowest[2], "mb_per_s": (total_bytes / 1e6) / total_seconds if total_seconds else None,
                }
            report.seconds = time.perf_counter() - started
            conn.execute(
                "UPDATE scan_run SET status = 'completed', finished_at = ?, stats_json = ? WHERE run_id = ?",
                (utc_now(), json.dumps({k: v for k, v in report.as_dict().items() if k != "warnings"}, default=str), run_id),
            )
            conn.execute("UPDATE root SET last_scan_at = ? WHERE root_id = ?", (now, root_id))
            if report.changes:
                bump_revision(conn)
    except BaseException:
        # Best effort: a hard kill never reaches here, which is why the NEXT scan also closes stale runs.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        with transaction(conn):
            conn.execute("UPDATE scan_run SET status = 'interrupted', finished_at = ? WHERE run_id = ?", (utc_now(), run_id))
            if report.changes:
                bump_revision(conn)
        raise
    return report


def _record(
    conn: sqlite3.Connection, run_id: str, root_id: str, entry: Entry, loc: sqlite3.Row | None, out: HashOutcome,
    now: str, report: ScanReport, new_locations: list[tuple[str, str]],
) -> None:
    if out.kind == "changing":
        report.changing += 1  # nothing is written: the next scan reads it again
        return
    with transaction(conn):
        if out.kind == "unreadable":
            report.unreadable += 1
            if loc is not None:
                if loc["state"] != "inaccessible":
                    conn.execute("UPDATE location SET state = 'inaccessible' WHERE location_id = ?", (loc["location_id"],))
                    _event(conn, loc["location_id"], run_id, "inaccessible", now, out.error)
                    report.inaccessible += 1
                    report.changes += 1
            else:
                location_id = _insert_location(conn, root_id, entry, None, None, None, "inaccessible", now)
                _event(conn, location_id, run_id, "first_seen", now, "unreadable: " + (out.error or ""))
                report.new_locations += 1
                report.changes += 1
            return

        report.hashed += 1
        report.bytes_hashed += out.size or 0
        if loc is not None and loc["artifact_id"] == out.sha256:
            # Same bytes. Refresh the hint; this is not a content change, only a new mtime (or a --full re-read).
            was = loc["state"]
            conn.execute(
                "UPDATE location SET size = ?, mtime_ns = ?, state = 'active', last_seen = ?, relative_path = ? WHERE location_id = ?",
                (out.size, out.mtime_ns, now, entry.relative_path, loc["location_id"]),
            )
            if was != "active":
                _event(conn, loc["location_id"], run_id, "reappeared", now)
                report.reappeared += 1
                report.changes += 1
            elif (loc["size"], loc["mtime_ns"]) != (out.size, out.mtime_ns):
                _event(conn, loc["location_id"], run_id, "content_touched", now, "same bytes, new modification time")
                report.content_touched += 1
            return

        _ensure_artifact(conn, out, now, report)
        report.changes += 1
        if loc is not None and loc["artifact_id"] is None:
            conn.execute(
                "UPDATE location SET artifact_id = ?, size = ?, mtime_ns = ?, state = 'active', last_seen = ? WHERE location_id = ?",
                (out.sha256, out.size, out.mtime_ns, now, loc["location_id"]),
            )
            _event(conn, loc["location_id"], run_id, "accessible", now, "first time its bytes could be read")
        elif loc is not None:
            # Different bytes at the same path. End the old location BEFORE inserting the new one (one current
            # location per path), then link them. The new bytes get their own document unless they are known bytes.
            conn.execute(
                "UPDATE location SET ended_at = ?, end_reason = 'replaced' WHERE location_id = ?", (now, loc["location_id"])
            )
            successor = _insert_location(conn, root_id, entry, out.sha256, out.size, out.mtime_ns, "active", now)
            conn.execute("UPDATE location SET successor_location_id = ? WHERE location_id = ?", (successor, loc["location_id"]))
            _event(conn, loc["location_id"], run_id, "replaced", now, f"now holds {out.sha256[:12]}")
            _event(conn, successor, run_id, "first_seen", now, f"replaced {loc['artifact_id'][:12] if loc['artifact_id'] else '?'}")
            report.replaced += 1
            report.new_locations += 1
        else:
            location_id = _insert_location(conn, root_id, entry, out.sha256, out.size, out.mtime_ns, "active", now)
            _event(conn, location_id, run_id, "first_seen", now)
            new_locations.append((location_id, out.sha256))
            report.new_locations += 1


def _pair_moves(
    conn: sqlite3.Connection, run_id: str, new_locations: list[tuple[str, str]], now: str, report: ScanReport
) -> None:
    """A new path whose bytes match a MISSING location is that location's move. Pair one to one.

    When several missing locations hold the same bytes, which one "moved" must not depend on random IDs (a
    scan has to give the same answer twice). Prefer the one with the same file name (a move), then the same
    folder (a rename), then the one seen first, then path order."""
    consumed: set[str] = set()
    for new_location, artifact_id in new_locations:
        new_path = conn.execute("SELECT relative_path FROM location WHERE location_id = ?", (new_location,)).fetchone()[0]
        new_name, new_dir = new_path.lower().rpartition("/")[2], new_path.lower().rpartition("/")[0]
        rows = [
            row for row in conn.execute(
                "SELECT location_id, relative_path, first_seen FROM location WHERE artifact_id = ? AND state = 'missing' "
                "AND ended_at IS NULL AND location_id <> ?",
                (artifact_id, new_location),
            )
            if row["location_id"] not in consumed
        ]
        if not rows:
            continue

        def preference(row: sqlite3.Row) -> tuple:
            old = row["relative_path"].lower()
            return (old.rpartition("/")[2] != new_name, old.rpartition("/")[0] != new_dir, row["first_seen"], old)

        candidate = min(rows, key=preference)
        consumed.add(candidate["location_id"])
        conn.execute(
            "UPDATE location SET ended_at = ?, end_reason = 'moved', successor_location_id = ? WHERE location_id = ?",
            (now, new_location, candidate["location_id"]),
        )
        _event(conn, candidate["location_id"], run_id, "moved_to", now, new_path)
        _event(conn, new_location, run_id, "moved_from", now, candidate["relative_path"])
        report.moved += 1
        report.changes += 1
