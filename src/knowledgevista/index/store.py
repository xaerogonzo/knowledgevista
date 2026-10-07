"""The extraction store: extracted pages and the full-text index, in the CACHE, never in the catalog.

Everything in here is derived from artifacts and can be rebuilt, so it is a separate SQLite file under the cache
directory. That separation is what makes two invariants cheap to keep and cheap to test:

  * deleting it loses no catalog state (metadata, collections, location history are in the catalog);
  * a damaged or out-of-date index is thrown away and rebuilt, instead of needing a migration.

It is keyed by ARTIFACT (the content hash), not by path, so renaming or moving a file never touches it. `page_id` is an
INTERNAL key of one extraction's page row: a rebuilt extraction gets new page ids (AUTOINCREMENT never reuses them),
which is why the durable way to cite a page is `artifact_id + pdf_page`, never a page id.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from knowledgevista.db.catalog import transaction
from knowledgevista.db.connection import BUSY_TIMEOUT_MS
from knowledgevista.domain.ids import utc_now

#: The shape of this file. A cache is rebuildable, so a mismatch means "start over", not "migrate".
SCHEMA_VERSION = 1
TOKENIZER = "unicode61 remove_diacritics 2"

_SCHEMA = f"""
CREATE TABLE extraction (
    extraction_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id       TEXT NOT NULL UNIQUE,
    source            TEXT NOT NULL CHECK (source IN ('native', 'imported_openchem_index')),
    extractor         TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    format_version    INTEGER NOT NULL,
    options_hash      TEXT NOT NULL,
    profile_id        TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('complete', 'partial', 'failed')),
    page_count        INTEGER NOT NULL,
    chars             INTEGER NOT NULL,
    legacy_scanned    INTEGER NOT NULL CHECK (legacy_scanned IN (0, 1)),
    first_pages_doi   TEXT,
    first_text        TEXT,
    repaired          INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    seconds           REAL,
    created_at        TEXT NOT NULL
);
CREATE TABLE page (
    page_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    extraction_id  INTEGER NOT NULL REFERENCES extraction (extraction_id) ON DELETE CASCADE,
    pdf_page       INTEGER NOT NULL CHECK (pdf_page >= 1),
    printed_label  TEXT CHECK (printed_label IS NULL OR printed_label <> ''),
    text_state     TEXT NOT NULL CHECK (text_state IN
                   ('text_native', 'text_sparse', 'image_only', 'blank', 'unknown', 'failed')),
    chars          INTEGER NOT NULL,
    error          TEXT,
    UNIQUE (extraction_id, pdf_page)
);
CREATE INDEX page_label ON page (printed_label);
-- rowid = page.page_id. A page with no characters at all has no row here.
CREATE VIRTUAL TABLE page_fts USING fts5(text, tokenize = '{TOKENIZER}');
"""


@dataclass
class PageRecord:
    pdf_page: int
    text: str
    printed_label: str | None
    text_state: str
    error: str | None = None


@dataclass
class ExtractionRecord:
    artifact_id: str
    source: str
    extractor: str
    extractor_version: str
    format_version: int
    options_hash: str
    profile_id: str
    status: str
    page_count: int
    chars: int
    legacy_scanned: bool
    first_pages_doi: str | None
    first_text: str | None
    repaired: bool = False
    error: str | None = None
    seconds: float | None = None
    pages: list[PageRecord] = field(default_factory=list)


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
    else:
        connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if not read_only:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    return connection


def _version_of(path: Path) -> int | None:
    try:
        probe = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            return probe.execute("PRAGMA user_version").fetchone()[0]
        finally:
            probe.close()
    except sqlite3.DatabaseError:
        return None  # not even a database: treat as stale


def _remove(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            Path(str(path) + suffix).unlink()
        except FileNotFoundError:
            pass


def open_index(path: Path | str, *, create: bool, read_only: bool = False) -> sqlite3.Connection | None:
    """Open the extraction store. Returns None if it does not exist and `create` is False.

    A store written by a different schema version (or that is not a database at all) is DELETED and recreated when
    writing: it is derived data. A read-only open of a stale store returns None, i.e. "nothing extracted yet", and the
    next `kv extract` rebuilds it.
    """
    target = Path(path)
    if target.exists():
        version = _version_of(target)
        if version != SCHEMA_VERSION:
            if read_only or not create:
                return None
            _remove(target)
    if not target.exists():
        if not create:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        connection = _connect(target, read_only=False)
        try:
            with transaction(connection):
                for statement in _split(_SCHEMA):
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except BaseException:
            connection.close()
            _remove(target)
            raise
        if read_only:
            connection.close()
            return _connect(target, read_only=True)
        return connection
    return _connect(target, read_only=read_only)


def _split(sql: str) -> list[str]:
    statements, buffer = [], ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    return statements


def delete_extraction(connection: sqlite3.Connection, artifact_id: str) -> bool:
    """Remove an artifact's extraction and its full-text rows. Caller owns the transaction."""
    row = connection.execute("SELECT extraction_id FROM extraction WHERE artifact_id = ?", (artifact_id,)).fetchone()
    if row is None:
        return False
    connection.execute(
        "DELETE FROM page_fts WHERE rowid IN (SELECT page_id FROM page WHERE extraction_id = ?)", (row[0],)
    )
    connection.execute("DELETE FROM extraction WHERE extraction_id = ?", (row[0],))  # pages cascade
    return True


def replace_extraction(connection: sqlite3.Connection, record: ExtractionRecord) -> int:
    """Store an artifact's extraction, replacing any earlier one, in ONE transaction: a crash leaves the old pages
    searchable or the new ones, never a file with half of each or none."""
    with transaction(connection):
        delete_extraction(connection, record.artifact_id)
        cursor = connection.execute(
            "INSERT INTO extraction (artifact_id, source, extractor, extractor_version, format_version, options_hash, "
            "profile_id, status, page_count, chars, legacy_scanned, first_pages_doi, first_text, repaired, error, "
            "seconds, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record.artifact_id, record.source, record.extractor, record.extractor_version, record.format_version,
             record.options_hash, record.profile_id, record.status, record.page_count, record.chars,
             int(record.legacy_scanned), record.first_pages_doi, record.first_text, int(record.repaired),
             record.error, record.seconds, utc_now()),
        )
        extraction_id = cursor.lastrowid
        for page in record.pages:
            cursor = connection.execute(
                "INSERT INTO page (extraction_id, pdf_page, printed_label, text_state, chars, error) VALUES (?, ?, ?, ?, ?, ?)",
                (extraction_id, page.pdf_page, page.printed_label, page.text_state, len(page.text), page.error),
            )
            if page.text:  # whitespace-only text is indexed too (zero tokens), so the row count equals chars > 0
                connection.execute("INSERT INTO page_fts (rowid, text) VALUES (?, ?)", (cursor.lastrowid, page.text))
    return extraction_id


def get_extraction(connection: sqlite3.Connection, artifact_id: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM extraction WHERE artifact_id = ?", (artifact_id,)).fetchone()


def page_text(connection: sqlite3.Connection, page_id: int) -> str:
    row = connection.execute("SELECT text FROM page_fts WHERE rowid = ?", (page_id,)).fetchone()
    return row[0] if row else ""


def consistency_problems(connection: sqlite3.Connection) -> list[str]:
    """Structural disagreements between the tables, for `doctor`. Cheap: counts and anti-joins, no text read."""
    problems = []
    fts = connection.execute("SELECT COUNT(*) FROM page_fts").fetchone()[0]
    with_text = connection.execute("SELECT COUNT(*) FROM page WHERE chars > 0").fetchone()[0]
    if fts != with_text:
        problems.append(f"{fts} full-text rows but {with_text} pages that should have text")
    orphans = connection.execute("SELECT COUNT(*) FROM page_fts WHERE rowid NOT IN (SELECT page_id FROM page)").fetchone()[0]
    if orphans:
        problems.append(f"{orphans} full-text rows belong to no page")
    short = connection.execute(
        "SELECT COUNT(*) FROM extraction e WHERE e.page_count <> (SELECT COUNT(*) FROM page p WHERE p.extraction_id = e.extraction_id)"
    ).fetchone()[0]
    if short:
        problems.append(f"{short} extraction(s) whose page rows do not match their page count")
    return problems
