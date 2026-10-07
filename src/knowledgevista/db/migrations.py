"""Forward-only schema migrations, each applied atomically, with a backup first.

THE POLICY, stated once so nothing improvises it later:

  * Forward only. There is no downgrade. The safety net is the backup taken before a migration, not a
    "down" script nobody would test.
  * Each migration is ONE transaction (DDL is transactional in SQLite). A failure rolls the whole step back,
    so the catalog is left at the previous version with the backup beside it, never half-migrated.
  * A catalog NEWER than this application is refused untouched: opening it with older code and "fixing" it
    would lose whatever the newer version added.
  * A backup is taken only when there is something to lose (an existing catalog at version >= 1), and only
    when a migration will actually run. Opening a current catalog writes nothing.

THE BACKUP is made with SQLite's own backup API, never by copying the file. The catalog runs in WAL mode, so
committed data can still be in `catalog.sqlite-wal`; a plain copy of the main file silently omits it. The test
suite proves this on the real engine (tests/test_migrations.py), because a backup that "usually works" is the
kind that fails on the day it is needed.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from knowledgevista import __version__
from knowledgevista.db.connection import connect

_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
BACKUP_SUFFIX = ".kv-backup"


class MigrationError(RuntimeError):
    """A migration failed and was rolled back. `backup` is the pre-migration copy, if one was taken."""

    def __init__(self, message: str, *, backup: Path | None):
        super().__init__(message)
        self.backup = backup


class SchemaTooNew(RuntimeError):
    """The catalog was written by a newer Knowledge Vista than this one. It is left untouched."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


@dataclass(frozen=True)
class MigrationResult:
    from_version: int
    to_version: int
    backup: Path | None

    @property
    def applied(self) -> bool:
        return self.to_version != self.from_version


def load_migrations() -> list[Migration]:
    """The migrations shipped in `knowledgevista/db/schema/`, in version order.

    Raises if the numbering has a gap or a duplicate: a missing step would apply the later ones to a schema
    that never got the earlier one, and fail somewhere far from the cause.
    """
    found: list[Migration] = []
    for entry in resources.files("knowledgevista.db.schema").iterdir():
        match = _FILE.match(entry.name)
        if match:
            found.append(Migration(int(match.group(1)), match.group(2), entry.read_text(encoding="utf-8")))
    found.sort(key=lambda migration: migration.version)
    expected = list(range(1, len(found) + 1))
    if [migration.version for migration in found] != expected:
        raise RuntimeError(f"migration versions must be 1..N with no gap or duplicate, got {[m.version for m in found]}")
    return found


def current_version(connection: sqlite3.Connection) -> int:
    """The applied schema version; 0 for a catalog that has never been migrated."""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migration'"
    ).fetchone()
    if not exists:
        return 0
    return connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migration").fetchone()[0]


def _statements(sql: str) -> list[str]:
    """Split a script into complete statements. `sqlite3.complete_statement` understands triggers, whose bodies
    contain semicolons, which a naive split on `;` would cut in half."""
    statements, buffer = [], ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise ValueError(f"migration ends in an incomplete statement: {buffer.strip()[:80]!r}")
    return statements


def _backup(source: sqlite3.Connection, directory: Path, from_version: int, to_version: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    base = f"catalog.v{from_version}-to-v{to_version}.{stamp}"
    destination = directory / f"{base}{BACKUP_SUFFIX}"
    counter = 1
    while destination.exists():  # two migrations in one second must not overwrite each other's safety copy
        destination = directory / f"{base}.{counter}{BACKUP_SUFFIX}"
        counter += 1
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
    return destination


def migrate(
    path: Path | str,
    *,
    backup_dir: Path | str | None = None,
    migrations: Sequence[Migration] | None = None,
) -> MigrationResult:
    """Bring the catalog at `path` to the latest schema. Creates it if absent."""
    available = list(migrations) if migrations is not None else load_migrations()
    latest = available[-1].version if available else 0
    catalog = Path(path)
    directory = Path(backup_dir) if backup_dir is not None else catalog.parent / "backups"

    connection = connect(catalog)
    try:
        # Created outside the numbered migrations: it is how a catalog knows which migrations it has had.
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL, app_version TEXT NOT NULL)"
        )
        version = current_version(connection)
        if version > latest:
            raise SchemaTooNew(
                f"{catalog} is at schema version {version}, but this Knowledge Vista only knows up to {latest}. "
                "It was written by a newer version and has been left untouched."
            )
        pending = [migration for migration in available if migration.version > version]
        if not pending:
            return MigrationResult(version, version, None)

        backup = _backup(connection, directory, version, latest) if version >= 1 else None
        for migration in pending:
            try:
                connection.execute("BEGIN IMMEDIATE")
                for statement in _statements(migration.sql):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migration (version, name, applied_at, app_version) "
                    "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?)",
                    (migration.version, migration.name, __version__),
                )
                connection.execute("COMMIT")
            except Exception as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                where = f" The pre-migration backup is {backup}." if backup else ""
                raise MigrationError(
                    f"migration {migration.version} ({migration.name}) failed and was rolled back: {exc}.{where}",
                    backup=backup,
                ) from exc
        return MigrationResult(version, latest, backup)
    finally:
        connection.close()
