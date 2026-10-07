"""Shared test helpers: build a library on disk, scan it, and snapshot the catalog and the source tree."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from knowledgevista.db.catalog import open_catalog
from knowledgevista.services.roots import Root, add_root
from knowledgevista.services.scan import ScanReport, scan_root


def write(path: Path, content: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


def tree_hashes(root: Path) -> dict[str, str]:
    """SHA-256 of every file under `root`, keyed by relative path: the 'source bytes are unchanged' oracle."""
    out = {}
    for base, _dirs, names in os.walk(root):
        for name in names:
            full = Path(base) / name
            out[full.relative_to(root).as_posix()] = hashlib.sha256(full.read_bytes()).hexdigest()
    return out


@dataclass
class Env:
    conn: sqlite3.Connection
    root: Root
    lib: Path
    catalog: Path

    def scan(self, **kwargs) -> ScanReport:
        return scan_root(self.conn, self.root.root_id, **kwargs)

    def rows(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, *args):
        return self.conn.execute(sql, args).fetchone()[0]

    def location(self, relative: str) -> sqlite3.Row | None:
        """The CURRENT location for a path, if any."""
        return self.conn.execute(
            "SELECT * FROM location WHERE relative_path = ? AND ended_at IS NULL", (relative,)
        ).fetchone()

    def revision(self) -> int:
        return self.one("SELECT catalog_revision FROM library")

    def snapshot(self) -> dict:
        """The catalog's shape with the random IDs removed, so two catalogs built from the same tree compare equal."""
        locations = sorted(
            (r["relative_path"], r["state"], r["end_reason"], r["artifact_id"])
            for r in self.rows("SELECT relative_path, state, end_reason, artifact_id FROM location")
        )
        documents = sorted(
            tuple(sorted(a["artifact_id"] for a in self.rows(
                "SELECT artifact_id FROM document_artifact WHERE document_id = ?", d["document_id"])))
            for d in self.rows("SELECT document_id FROM document")
        )
        return {"locations": locations, "documents": documents}


def make_env(tmp_path: Path, files: dict[str, bytes | str] | None = None, name: str = "lib") -> Env:
    lib = tmp_path / name
    lib.mkdir(parents=True, exist_ok=True)
    for relative, content in (files or {}).items():
        write(lib / relative, content)
    catalog = tmp_path / f"{name}.catalog.sqlite"
    conn = open_catalog(catalog, create=True)
    return Env(conn, add_root(conn, str(lib)), lib, catalog)
