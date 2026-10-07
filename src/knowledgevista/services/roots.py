"""Library roots: the folders Knowledge Vista is allowed to look in.

Scanning is only ever done under an explicitly added root. There is no "everything under Documents": a user's
Documents folder holds unrelated personal files, and a library manager that wandered into it would catalogue them.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

from knowledgevista.db.catalog import bump_revision, transaction
from knowledgevista.domain.ids import new_id, utc_now
from knowledgevista.domain.pathkeys import fs_path, is_within, normalise_root, overlaps
from knowledgevista.errors import ErrorCode, KvError


@dataclass(frozen=True)
class Root:
    root_id: str
    configured_path: str
    root_key: str
    label: str
    enabled: bool
    status: str
    volume_id: str | None
    allow_organize: bool
    created_at: str
    last_scan_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Root:
        return cls(
            row["root_id"], row["configured_path"], row["root_key"], row["label"], bool(row["enabled"]),
            row["status"], row["volume_id"], bool(row["allow_organize"]), row["created_at"], row["last_scan_at"],
        )

    def as_dict(self) -> dict:
        return {
            "root_id": self.root_id, "path": self.configured_path, "label": self.label, "enabled": self.enabled,
            "status": self.status, "allow_organize": self.allow_organize, "last_scan_at": self.last_scan_at,
        }


def volume_id_of(path: str) -> str | None:
    """The filesystem's device id for a path (the volume serial on Windows), as evidence that a root is still the
    same volume. Informational: it is never used to claim a root moved."""
    try:
        return str(os.stat(fs_path(path)).st_dev)
    except OSError:
        return None


def add_root(
    connection: sqlite3.Connection, path: str, *, label: str | None = None, allow_organize: bool = False,
    catalog_path: str | None = None,
) -> Root:
    display, key = normalise_root(path)
    if not os.path.isdir(fs_path(display)):
        raise KvError(ErrorCode.ROOT_UNAVAILABLE, f"{display} is not a folder that exists right now.", {"path": display})
    if catalog_path is not None and is_within(key, normalise_root(catalog_path)[1]):
        # Scanning would hash the live catalog (and its WAL) as if it were a document, and every scan would
        # change it. The catalog belongs outside any library.
        raise KvError(
            ErrorCode.ROOT_OVERLAP, f"{display} contains the catalog file itself ({catalog_path}); choose a folder that does not.",
            {"path": display, "catalog": catalog_path},
        )
    for existing in list_roots(connection):
        if existing.root_key == key:
            raise KvError(
                ErrorCode.ROOT_OVERLAP, f"{display} is already a root ({existing.label}).",
                {"existing_root_id": existing.root_id, "path": display},
            )
        if overlaps(existing.root_key, key):
            # A file under two roots would be two locations of one thing, scanned twice and moved by whichever
            # root's plan came first. Refuse rather than let the ambiguity in.
            raise KvError(
                ErrorCode.ROOT_OVERLAP,
                f"{display} overlaps the existing root {existing.configured_path}; a path may belong to only one root.",
                {"existing_root_id": existing.root_id, "existing_path": existing.configured_path, "path": display},
            )
    root_id = new_id()
    name = label or os.path.basename(display.rstrip("\\/")) or display
    with transaction(connection):
        connection.execute(
            "INSERT INTO root (root_id, configured_path, root_key, label, volume_id, allow_organize, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (root_id, display, key, name, volume_id_of(display), int(allow_organize), utc_now()),
        )
        bump_revision(connection)
    return get_root(connection, root_id)


def list_roots(connection: sqlite3.Connection) -> list[Root]:
    return [Root.from_row(row) for row in connection.execute("SELECT * FROM root ORDER BY created_at, root_id")]


def get_root(connection: sqlite3.Connection, root_id: str) -> Root:
    row = connection.execute("SELECT * FROM root WHERE root_id = ?", (root_id,)).fetchone()
    if row is None:
        raise KvError(ErrorCode.NOT_FOUND, f"No root {root_id}.", {"root_id": root_id})
    return Root.from_row(row)


def find_roots(connection: sqlite3.Connection, selector: str | None) -> list[Root]:
    """Roots named by `selector` (an id, an exact label, or a path), or every enabled root when it is None.

    An ambiguous label is an error that lists the candidates; it never picks one."""
    roots = list_roots(connection)
    if selector is None:
        return [root for root in roots if root.enabled]
    _, key = normalise_root(selector) if os.path.exists(selector) else (selector, selector)
    matches = [r for r in roots if r.root_id == selector or r.root_key == key]
    if not matches:
        matches = [r for r in roots if r.label.lower() == selector.lower()]
    if not matches:
        raise KvError(ErrorCode.NOT_FOUND, f"No root matches {selector!r}.", {"selector": selector})
    if len(matches) > 1:
        raise KvError(
            ErrorCode.AMBIGUOUS, f"{selector!r} matches {len(matches)} roots.",
            {"candidates": [r.as_dict() for r in matches]},
        )
    return matches
