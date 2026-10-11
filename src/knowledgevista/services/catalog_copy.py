"""Copying a library's catalog to another folder: what the window's "Move catalog…" does.

The catalog is the library's state (documents, metadata, decisions), not the documents themselves, and this module is built so that it
can never touch the latter or lose the former:

  * The source is opened READ-ONLY and copied with SQLite's backup API (never a file copy: the catalog is WAL, and a plain copy can
    lose committed data). Nothing is written to it, and it is not deleted: the old catalog stays exactly where it was. "Move" means
    the library is shown from the new place from now on; the old file is the person's to delete, if they ever want to.
  * No file in any of the library's folders (the roots) is opened, read, written, renamed or deleted. The roots' paths are inside the
    catalog and are copied as they are.
  * The copy is written to a temporary file this module created, verified (integrity check, the same library id and schema), and only
    then renamed into place; the rename refuses to overwrite, and an existing `catalog.sqlite` is refused up front. A failure removes
    only that temporary file.
  * The destination may not be inside one of the library's own folders: a scan would catalogue the catalog.
  * The extraction and metadata caches beside the catalog are copied too, so extracted text need not be redone. They are caches: a
    failure to copy one is a note, never an error, and the text is rebuilt by `kv extract`.

The new library has the same library id as the old one (it IS the same library), so a person's remembered place follows it.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from knowledgevista import paths
from knowledgevista.db.connection import connect
from knowledgevista.db.migrations import current_version
from knowledgevista.domain.pathkeys import is_within, normalise_root
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import roots

CATALOG_NAME = "catalog.sqlite"


@dataclass(frozen=True)
class CopyResult:
    source: Path
    destination: Path
    library_id: str
    cache_copied: bool
    notes: tuple[str, ...]


def _refuse(message: str, **details: str) -> KvError:
    return KvError(ErrorCode.INVALID_ARGUMENTS, message, details)


def _backup_to(source: Path, destination: Path, *, label: str) -> None:
    """Copy one SQLite database to a NEW file `destination` (which must not exist) through a verified temporary file beside it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.stem}-", suffix=".copying")
    os.close(handle)
    temporary = Path(name)  # a file THIS function made; the only file it ever removes
    try:
        reader = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
        writer = sqlite3.connect(temporary)
        try:
            reader.backup(writer)
            result = writer.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise sqlite3.DatabaseError(f"the copy of the {label} did not pass its integrity check ({result})")
        finally:
            writer.close()
            reader.close()
        os.rename(temporary, destination)  # refuses to overwrite on Windows; the existence check above covers the rest
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def copy_catalog(source: Path | str, destination_dir: Path | str) -> CopyResult:
    """Copy the catalog at `source` to `destination_dir/catalog.sqlite`. Raises KvError, having changed nothing, if it may not."""
    source = Path(source)
    directory = Path(destination_dir).expanduser()
    if not directory.is_absolute():
        raise _refuse("Choose a full folder path (for example D:\\Libraries\\Chemistry).", folder=str(directory))
    destination = directory / CATALOG_NAME
    _, source_key = normalise_root(str(source))
    destination_display, destination_key = normalise_root(str(destination))
    if source_key == destination_key:
        raise _refuse("The library is already in that folder.", catalog=str(source))
    if destination.exists():
        raise _refuse(f"{directory} already holds a file named {CATALOG_NAME}. Nothing was copied, and nothing there was changed.", folder=str(directory))
    if not source.is_file():
        raise KvError(ErrorCode.CATALOG_MISSING, f"There is no library file at {source}.", {"catalog": str(source)})
    try:
        conn = connect(source, read_only=True)
        try:
            if current_version(conn) < 1:
                raise _refuse(f"{source} is not a Knowledge Vista library.", catalog=str(source))
            library_id = conn.execute("SELECT library_id FROM library").fetchone()[0]
            for root in roots.list_roots(conn):
                if is_within(root.root_key, destination_key):
                    raise _refuse(f"{directory} is inside the library's own folder {root.configured_path}; a scan would catalogue the catalog. "
                                  "Choose a folder outside every folder the library looks in.", folder=str(directory), root=root.configured_path)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise _refuse(f"{source} could not be read as a library ({exc}).", catalog=str(source)) from exc

    created = not directory.exists()
    try:
        _backup_to(source, destination, label="catalog")
        copied = connect(destination, read_only=True)
        try:
            if copied.execute("SELECT library_id FROM library").fetchone()[0] != library_id:
                raise sqlite3.DatabaseError("the copy is a different library from the original")
        finally:
            copied.close()
    except (sqlite3.Error, OSError) as exc:
        if created:
            try:
                directory.rmdir()  # only a folder this call made, and only if nothing is in it
            except OSError:
                pass
        raise _refuse(f"Could not copy the library ({exc}). The original was not changed.", catalog=str(source)) from exc

    notes: list[str] = []
    copied_caches = 0
    for name, find in (("extracted text", paths.index_path), ("metadata cache", paths.metacache_path)):
        old, new = find(source), find(destination)
        if not old.is_file():
            continue
        if new.exists():
            notes.append(f"The {name} was not copied: a file is already at {new}.")
            continue
        try:
            _backup_to(old, new, label=name)
            copied_caches += 1
        except (sqlite3.Error, OSError) as exc:
            notes.append(f"The {name} was not copied ({exc}). It is a cache: it is rebuilt when needed.")
    return CopyResult(source, Path(destination_display), library_id, copied_caches > 0, tuple(notes))
