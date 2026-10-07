"""Opening the catalog for a service, and the transaction helper every write goes through."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from knowledgevista.db.connection import connect
from knowledgevista.db.migrations import migrate
from knowledgevista.errors import ErrorCode, KvError


def open_catalog(path: Path | str, *, create: bool, read_only: bool = False) -> sqlite3.Connection:
    """Open (and migrate) the catalog.

    `create=False` is for commands that must not invent a catalog: a typo in `--catalog` should fail loudly, not
    quietly make a new empty library and report "0 documents".
    """
    target = Path(path)
    if not target.exists() and not create:
        raise KvError(
            ErrorCode.CATALOG_MISSING,
            f"No catalog at {target}. Add a folder first: kv root add <path>",
            {"catalog": str(target)},
        )
    migrate(target)
    return connect(target, read_only=read_only)


def open_catalog_strict(path: Path | str) -> sqlite3.Connection:
    """Open the catalog for a reader that must NEVER write, not even to migrate (the MCP server, `capabilities`).

    `open_catalog` brings an older catalog up to date, which is a write; a read-only server must instead say the catalog needs
    one `kv` command to upgrade it. A catalog from a newer program is refused as everywhere else."""
    from knowledgevista.db.migrations import SchemaTooNew, current_version, load_migrations

    target = Path(path)
    if not target.exists():
        raise KvError(ErrorCode.CATALOG_MISSING, f"No catalog at {target}. Add a folder first: kv root add <path>", {"catalog": str(target)})
    connection = connect(target, read_only=True)
    version, latest = current_version(connection), load_migrations()[-1].version
    if version > latest:
        connection.close()
        raise SchemaTooNew(f"the catalog is schema {version}, newer than this program's {latest}")
    if version < latest:
        connection.close()
        raise KvError(ErrorCode.CATALOG_OUTDATED, f"The catalog is schema {version} and this program expects {latest}. Any `kv` command upgrades it (a backup is taken first); "
                      "a read-only server will not.", {"catalog_version": version, "expected": latest})
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One atomic unit of catalog writes. The code, not the sqlite3 module, decides where a transaction begins and ends."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def bump_revision(connection: sqlite3.Connection) -> int:
    """Mark a meaningful catalog change, so GUI/MCP clients and pagination cursors can tell their view is stale."""
    connection.execute("UPDATE library SET catalog_revision = catalog_revision + 1")
    return connection.execute("SELECT catalog_revision FROM library").fetchone()[0]


def revision(connection: sqlite3.Connection) -> int:
    return connection.execute("SELECT catalog_revision FROM library").fetchone()[0]
