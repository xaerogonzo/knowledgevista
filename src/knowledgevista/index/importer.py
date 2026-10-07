"""Import OpenChem's `<library>.index.sqlite` as a PROVISIONAL derivative, so a first run does not re-read every PDF.

The old index is treated as an external legacy format ("openchem_index_v1"), not as a Knowledge Vista structure, and it
is never authoritative. Three rules keep it honest:

  1. A row counts only if its SHA-256 is an artifact the catalog already holds. The catalog's hashes came from reading
     the real bytes during a scan, so "verified against the real bytes" is exactly "the old index's hash matches one the
     scanner computed". A row whose hash is unknown is reported as unmatched and imported as nothing.
  2. The old index's PATH is a historical locator and is discarded: where a file is now is the scanner's business.
  3. Imported text is marked `source = imported_openchem_index`. It is searchable, but `kv extract` still treats it as
     provisional and replaces it with native extraction when asked (`--rebuild-imported`). A native extraction is never
     overwritten by an import.

What the legacy index cannot give: printed page labels (so they are NULL) and the difference between a blank page and a
scanned one (so an empty page is `unknown`).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from knowledgevista.domain.ids import normalise_sha256
from knowledgevista.domain.text import imported_page_state
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.extract.profile import IMPORTED_PROFILE, IMPORTED_SOURCE
from knowledgevista.index.store import ExtractionRecord, PageRecord, get_extraction, replace_extraction


@dataclass
class ImportReport:
    legacy_files: int = 0
    imported: int = 0
    pages: int = 0
    skipped_native_exists: int = 0
    skipped_already_imported: int = 0
    skipped_duplicate_hash: int = 0
    skipped_legacy_error: int = 0
    skipped_size_mismatch: int = 0
    unmatched: int = 0
    unmatched_examples: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(vars(self))


def _open_legacy(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise KvError(ErrorCode.NOT_FOUND, f"No OpenChem index at {path}.", {"path": str(path)})
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        files = {r[1] for r in connection.execute("PRAGMA table_info(files)")}
        needed = {"path", "sha256", "size", "pages", "chars", "scanned", "doi", "first_text", "error"}
        if not needed <= files:
            raise KvError(ErrorCode.INVALID_ARGUMENTS, f"{path} is not an OpenChem library index (files table lacks {sorted(needed - files)}).")
        return connection
    except sqlite3.DatabaseError as exc:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"{path} is not a readable SQLite database: {exc}") from exc


def _legacy_pages(legacy: sqlite3.Connection, path: str, span: tuple[int, int, int] | None) -> list[sqlite3.Row]:
    """One file's pages in page order: by rowid range when its rows are contiguous (the normal case), else the slow
    lookup by path (a file whose rows were interleaved with another's by a re-index)."""
    if span is not None and span[1] - span[0] + 1 == span[2]:
        rows = legacy.execute("SELECT page, text FROM pages WHERE rowid BETWEEN ? AND ? ORDER BY page", (span[0], span[1])).fetchall()
        if len(rows) == span[2]:
            return rows
    return legacy.execute("SELECT page, text FROM pages WHERE path = ? ORDER BY page", (path,)).fetchall()


def import_openchem_index(
    catalog: sqlite3.Connection, index: sqlite3.Connection, legacy_path: Path | str, *, progress: Callable[[str], None] | None = None
) -> ImportReport:
    report = ImportReport()
    legacy = _open_legacy(Path(legacy_path))
    try:
        known = {r["artifact_id"]: r["size"] for r in catalog.execute("SELECT artifact_id, size FROM artifact")}
        seen: set[str] = set()
        # An FTS5 table cannot be searched by its UNINDEXED `path` column without scanning every row, so asking for
        # each file's pages by path costs one full scan PER FILE (measured: 0.44 s each, ~5 minutes for 738 files).
        # One grouped scan finds where each file's rows are; a contiguous file is then read by rowid range, instantly.
        spans = {r["path"]: (r["lo"], r["hi"], r["n"]) for r in legacy.execute(
            "SELECT path, MIN(rowid) AS lo, MAX(rowid) AS hi, COUNT(*) AS n FROM pages GROUP BY path")}
        rows = legacy.execute("SELECT * FROM files ORDER BY path").fetchall()
        report.legacy_files = len(rows)
        for number, row in enumerate(rows, start=1):
            if progress and number % 200 == 0:
                progress(f"importing {number}/{len(rows)}")
            sha = normalise_sha256(row["sha256"] or "")
            if row["error"]:
                report.skipped_legacy_error += 1
                continue
            if sha is None or sha not in known:
                report.unmatched += 1
                if len(report.unmatched_examples) < 10:
                    report.unmatched_examples.append(row["path"])
                continue
            if known[sha] != row["size"]:
                report.skipped_size_mismatch += 1  # same hash, different size is impossible unless the legacy row is corrupt
                continue
            if sha in seen:
                report.skipped_duplicate_hash += 1
                continue
            seen.add(sha)
            existing = get_extraction(index, sha)
            if existing is not None:
                if existing["source"] == IMPORTED_SOURCE:
                    report.skipped_already_imported += 1
                else:
                    report.skipped_native_exists += 1
                continue
            pages = [
                PageRecord(p["page"], p["text"] or "", None, imported_page_state(len((p["text"] or "").strip())))
                for p in _legacy_pages(legacy, row["path"], spans.get(row["path"]))
            ]
            if len(pages) != row["pages"]:
                report.skipped_legacy_error += 1  # the legacy row disagrees with its own pages
                continue
            replace_extraction(index, ExtractionRecord(
                artifact_id=sha, source=IMPORTED_SOURCE, extractor="openchem-library-index", extractor_version="1",
                format_version=1, options_hash="-", profile_id=IMPORTED_PROFILE, status="complete",
                page_count=row["pages"], chars=row["chars"], legacy_scanned=bool(row["scanned"]),
                first_pages_doi=row["doi"] or None, first_text=row["first_text"] or None, pages=pages,
            ))
            report.imported += 1
            report.pages += len(pages)
    finally:
        legacy.close()
    return report
