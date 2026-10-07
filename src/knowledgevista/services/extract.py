"""Extract text from the PDFs the catalog knows about and store it in the extraction store.

DECIDING WHAT TO DO for each PDF artifact (each rule has a test):

  no extraction yet            -> extract
  --force                      -> extract
  imported (provisional)       -> leave alone, unless --rebuild-imported
  profile differs from current -> STALE: extract again (the extractor or its options changed)
  failed or partial            -> leave alone, unless --retry-failed (so a hostile file is not re-attacked every run)
  otherwise                    -> current: nothing to do

THE DERIVATIVE MUST COME FROM THE ARTIFACT. The catalog identifies bytes by hash, but extraction reads a PATH. If the
file at that path changed since the last scan, its text would be filed under the wrong identity. So the file's size and
mtime are compared with what the scan recorded, before AND after extraction; a mismatch skips the file with a clear
reason ("run kv scan") and stores nothing. That is the freshness hint again, with the same caveat: `kv scan --full`
is what proves bytes.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from knowledgevista.domain.pathkeys import fs_path, join_relative
from knowledgevista.domain.text import (
    DOI_PAGES, FIRST_TEXT_CHARS, find_doi, is_scanned, page_state,
)
from knowledgevista.extract.client import ExtractionSession, FileResult
from knowledgevista.extract.profile import IMPORTED_SOURCE, NATIVE_SOURCE, Profile, current_profile
from knowledgevista.index.store import ExtractionRecord, PageRecord, get_extraction, replace_extraction

MAX_LISTED = 25


@dataclass
class ExtractReport:
    profile: dict[str, Any] = field(default_factory=dict)
    candidates: int = 0  # PDF artifacts considered
    extracted: int = 0
    complete: int = 0
    partial: int = 0
    failed: int = 0
    pages: int = 0
    stale_redone: int = 0
    current: int = 0
    provisional_imported: int = 0
    skipped_failed: int = 0
    unreachable: int = 0
    changed_since_scan: int = 0
    other_kinds_not_extracted: int = 0
    stopped: bool = False  # a caller asked to stop; what was done is stored, the rest is untouched
    seconds: float = 0.0
    limiter: str | None = None
    problems: list[dict[str, Any]] = field(default_factory=list)
    timing: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def build_record(artifact_id: str, profile: Profile, result: FileResult) -> ExtractionRecord:
    """Turn a worker result into the stored shape. Pure, so it is tested without a PDF."""
    pages = []
    for page in result.pages:
        stripped = len(page.text.strip())
        pages.append(PageRecord(page.n, page.text, page.label, page_state(stripped, page.images, error=page.error is not None), page.error))
    texts = [p.text for p in result.pages]
    chars = sum(len(t) for t in texts)
    doi = find_doi("\n".join(texts[:DOI_PAGES]))
    first = (texts[0] if texts else "")[:FIRST_TEXT_CHARS]
    return ExtractionRecord(
        artifact_id=artifact_id, source=NATIVE_SOURCE, extractor=profile.extractor, extractor_version=profile.extractor_version,
        format_version=profile.format_version, options_hash=profile.options_hash, profile_id=profile.profile_id,
        status=result.status, page_count=result.page_count, chars=chars, legacy_scanned=is_scanned(chars, result.page_count),
        first_pages_doi=doi or None, first_text=first or None, repaired=result.repaired,
        error=result.error or ("; ".join(result.notes) or None), seconds=result.seconds, pages=pages,
    )


def _reachable_location(catalog: sqlite3.Connection, artifact_id: str, root_ids: list[str] | None) -> sqlite3.Row | None:
    sql = (
        "SELECT l.relative_path, l.size, l.mtime_ns, r.configured_path FROM location l JOIN root r ON r.root_id = l.root_id "
        "WHERE l.artifact_id = ? AND l.ended_at IS NULL AND l.state = 'active' AND r.status = 'online' AND r.enabled = 1 "
    )
    args: list = [artifact_id]
    if root_ids is not None:
        sql += f"AND r.root_id IN ({','.join('?' * len(root_ids))}) "
        args += root_ids
    return catalog.execute(sql + "ORDER BY l.path_key LIMIT 1", args).fetchone()


def _stat_matches(path: str, size: int | None, mtime_ns: int | None) -> bool:
    try:
        info = os.stat(path)
    except OSError:
        return False
    return (info.st_size, info.st_mtime_ns) == (size, mtime_ns)


def extract_library(
    catalog: sqlite3.Connection,
    index: sqlite3.Connection,
    *,
    root_ids: list[str] | None = None,
    force: bool = False,
    retry_failed: bool = False,
    rebuild_imported: bool = False,
    limit: int | None = None,
    session: ExtractionSession | None = None,
    progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> ExtractReport:
    profile = current_profile()
    report = ExtractReport(profile=profile.as_dict())
    say = progress or (lambda _m: None)
    started = time.perf_counter()
    report.other_kinds_not_extracted = catalog.execute("SELECT COUNT(*) FROM artifact WHERE content_kind <> 'pdf'").fetchone()[0]

    targets: list[tuple[str, str, int | None, int | None, bool]] = []  # artifact, fs path, size, mtime, was_stale
    for artifact in catalog.execute("SELECT artifact_id FROM artifact WHERE content_kind = 'pdf' ORDER BY artifact_id"):
        report.candidates += 1
        artifact_id = artifact["artifact_id"]
        existing = get_extraction(index, artifact_id)
        stale = False
        if existing is not None and not force:
            native = existing["source"] == NATIVE_SOURCE
            if not native and not rebuild_imported:
                report.provisional_imported += 1
                continue
            if native and existing["profile_id"] == profile.profile_id:
                if existing["status"] == "complete":
                    report.current += 1
                    continue
                if not retry_failed:
                    report.skipped_failed += 1
                    continue
            stale = native and existing["profile_id"] != profile.profile_id
        location = _reachable_location(catalog, artifact_id, root_ids)
        if location is None:
            report.unreachable += 1
            continue
        path = fs_path(join_relative(location["configured_path"], location["relative_path"]))
        targets.append((artifact_id, path, location["size"], location["mtime_ns"], stale))
    if limit is not None:
        targets = targets[:limit]

    own_session = session is None
    session = session or ExtractionSession()
    durations: list[float] = []
    try:
        for number, (artifact_id, path, size, mtime_ns, stale) in enumerate(targets, start=1):
            if should_stop is not None and should_stop():
                report.stopped = True  # checked between files: one file is stored whole or not at all
                break
            if number == 1 or number % 25 == 0:
                say(f"extracting {number}/{len(targets)}")
            if not _stat_matches(path, size, mtime_ns):
                report.changed_since_scan += 1
                _problem(report, artifact_id, "the file changed (or vanished) since the last scan; run: kv scan")
                continue
            result = session.extract(path)
            if not _stat_matches(path, size, mtime_ns):
                report.changed_since_scan += 1
                _problem(report, artifact_id, "the file changed while it was being extracted; nothing was stored")
                continue
            replace_extraction(index, build_record(artifact_id, profile, result))
            report.extracted += 1
            report.stale_redone += 1 if stale else 0
            report.pages += result.page_count
            setattr(report, result.status, getattr(report, result.status) + 1)
            durations.append(result.seconds)
            if result.status != "complete":
                _problem(report, artifact_id, f"{result.status}: {result.error or 'some pages failed'}")
    finally:
        report.limiter = session.limiter_note
        if own_session:
            session.close()
    if durations:
        ordered = sorted(durations)
        report.timing = {"files": len(ordered), "median_s": ordered[len(ordered) // 2],
                         "p95_s": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], "max_s": ordered[-1],
                         "pages_per_s": report.pages / sum(ordered) if sum(ordered) else None}
    report.seconds = time.perf_counter() - started
    return report


def _problem(report: ExtractReport, artifact_id: str, message: str) -> None:
    if len(report.problems) < MAX_LISTED:
        report.problems.append({"artifact_id": artifact_id, "message": message})
