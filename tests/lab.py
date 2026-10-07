"""A small real library for the window's tests: generated PDFs, real extraction, offline resolution, accepted and waiting metadata.

The extraction store sits where `kv` itself would put it (beside the catalog), because that is where the window looks.

Building one takes seconds (generating PDFs, a worker process per extraction), and a test file makes dozens. So the first request
for a given shape builds a TEMPLATE once per process; every `Lab(...)` after that is a copy of it, with the root re-pointed at the
copy. A copy is a real, independent library: its own catalog, its own files, its own extraction store.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

import pdfbuilders as b
from support import Env, make_env

from knowledgevista import paths
from knowledgevista.db.catalog import open_catalog
from knowledgevista.domain.pathkeys import normalise_root
from knowledgevista.extract.client import ExtractionSession
from knowledgevista.index import metacache, store
from knowledgevista.services import metadata
from knowledgevista.services.extract import extract_library
from knowledgevista.services.resolve_metadata import resolve_library
from knowledgevista.services.roots import list_roots

#: Markup in every place a PDF can put a string. The window must show all of it as the letters it is.
HOSTILE_TITLE = '<b>Bold</b> &amp; <img src="http://example.invalid/leak.png"> <script>alert(1)</script>'
HOSTILE_AUTHORS = "<i>Slanted</i> Examplar and B. Placeholder"

PAPER_TITLE = "Aqueous Solubility of Invented Esters"
PAPER_DOI = "10.5555/kv.lab.0001"
SECOND_TITLE = "Partition Coefficients of Imaginary Amides"
SECOND_DOI = "10.5555/kv.lab.0002"

_TEMPLATES: dict[tuple[bool, bool], Path] = {}


def _template(extract: bool, resolve: bool) -> Path:
    key = (extract, resolve)
    if key not in _TEMPLATES:
        base = Path(tempfile.mkdtemp(prefix="kv-lab-template-"))
        atexit.register(shutil.rmtree, base, ignore_errors=True)
        built = _build(base, extract=extract, resolve=resolve)
        built.close()
        _TEMPLATES[key] = base
    return _TEMPLATES[key]


def _build(tmp_path: Path, *, extract: bool, resolve: bool) -> Lab:
    lab = Lab.__new__(Lab)
    lab.env = make_env(tmp_path, {"notes/readme.txt": "A plain note about invented esters.\n"})
    lib = lab.env.lib
    for folder in ("papers", "books", "scans"):
        (lib / folder).mkdir(parents=True, exist_ok=True)
    b.native_pdf(lib / "papers" / "aqueous.pdf", title=PAPER_TITLE, doi=PAPER_DOI, body_pages=6, producer="One")
    b.native_pdf(lib / "papers" / "aqueous-copy.pdf", title=PAPER_TITLE, doi=PAPER_DOI, body_pages=6, producer="Two")
    b.native_pdf(lib / "papers" / "second.pdf", title=SECOND_TITLE, doi=SECOND_DOI, body_pages=3)
    b.native_pdf(lib / "papers" / "a&amp;b.pdf", title=HOSTILE_TITLE, authors=HOSTILE_AUTHORS, doi=None, body_pages=2)
    b.labelled_pdf(lib / "books" / "book.pdf")
    b.scanned_pdf(lib / "scans" / "scan.pdf")
    lab.env.scan()
    lab._open_stores()
    if extract:
        extract_library(lab.conn, lab.index, session=lab.session)
    if extract and resolve:
        resolve_library(lab.conn, lab.index, lab.cache, session=lab.session)
    lab.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return lab


class Lab:
    """Documents: aqueous.pdf (+ a byte-different copy of the same paper), second.pdf, hostile.pdf, book.pdf, scan.pdf, readme.txt."""

    def __init__(self, tmp_path: Path, *, extract: bool = True, resolve: bool = True):
        source = _template(extract, resolve)
        base = Path(tmp_path) / "labcopy"
        shutil.copytree(source, base)  # copy2 semantics: sizes and modification times survive, so the first scan finds nothing changed
        catalog = base / "lib.catalog.sqlite"
        conn = open_catalog(catalog, create=False)
        (root,) = list_roots(conn)
        display, key = normalise_root(str(base / "lib"))
        conn.execute("UPDATE root SET configured_path = ?, root_key = ? WHERE root_id = ?", (display, key, root.root_id))
        self.env = Env(conn, list_roots(conn)[0], base / "lib", catalog)
        self._open_stores()

    def _open_stores(self) -> None:
        self.conn = self.env.conn
        self.catalog = self.env.catalog
        self.index = store.open_index(paths.index_path(self.catalog), create=True)
        self.cache = metacache.open_metacache(paths.metacache_path(self.catalog), create=True)
        self.session = ExtractionSession()

    def doc(self, relative: str) -> str:
        return self.env.one(
            "SELECT da.document_id FROM location l JOIN document_artifact da ON da.artifact_id = l.artifact_id "
            "WHERE l.relative_path = ? AND l.ended_at IS NULL", relative)

    def accept_title(self, relative: str, title: str) -> str:
        document_id = self.doc(relative)
        metadata.set_value(self.conn, document_id, "title", title, lock=False)
        return document_id

    def reader(self):
        """A read-only connection, the kind the window's read jobs use: any write through it raises."""
        return open_catalog(self.catalog, create=False, read_only=True)

    def close(self) -> None:
        self.session.close()
        self.index.close()
        self.cache.close()
        self.conn.close()
