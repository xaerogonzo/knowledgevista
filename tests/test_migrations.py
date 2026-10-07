"""Migration framework tests.

Each test that claims a safety property also proves its oracle is alive (tests-that-pass-without-testing.md
section 1): the WAL test shows a naive file copy really DOES lose data before trusting the backup; the rollback
test shows the failing statement really ran after a statement that succeeded.
"""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from knowledgevista.db import migrations as mig
from knowledgevista.db.connection import connect
from knowledgevista.db.migrations import Migration, MigrationError, SchemaTooNew, migrate

V1 = Migration(1, "first", "CREATE TABLE note (id INTEGER PRIMARY KEY, body TEXT NOT NULL);")
V2 = Migration(2, "second", "ALTER TABLE note ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;")


def _tables(path):
    connection = connect(path, read_only=True)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        connection.close()


# --- the shipped migrations -------------------------------------------------------------------------------------


def test_shipped_migrations_are_contiguous_and_nonempty():
    shipped = mig.load_migrations()
    assert shipped, "no migrations were found: the package data is missing, not 'nothing to migrate'"
    assert [m.version for m in shipped] == list(range(1, len(shipped) + 1))


def test_fresh_catalog_reaches_latest_with_a_library_row(tmp_path):
    path = tmp_path / "catalog.sqlite"
    result = migrate(path)
    assert result.from_version == 0 and result.to_version == len(mig.load_migrations())
    assert result.backup is None, "a brand-new catalog has nothing to back up"
    connection = connect(path, read_only=True)
    try:
        row = connection.execute("SELECT library_id, catalog_revision, created_at FROM library").fetchone()
    finally:
        connection.close()
    assert len(row["library_id"]) == 32 and int(row["library_id"], 16) >= 0
    assert row["catalog_revision"] == 0
    assert row["created_at"].endswith("Z")


def test_library_is_a_singleton(tmp_path):
    path = tmp_path / "catalog.sqlite"
    migrate(path)
    connection = connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO library (singleton, library_id, created_at) VALUES (2, 'x', 'y')")
    finally:
        connection.close()


def test_two_catalogs_get_different_library_ids(tmp_path):
    ids = []
    for name in ("a", "b"):
        path = tmp_path / f"{name}.sqlite"
        migrate(path)
        connection = connect(path, read_only=True)
        ids.append(connection.execute("SELECT library_id FROM library").fetchone()[0])
        connection.close()
    assert ids[0] != ids[1]


def test_rerun_on_a_current_catalog_changes_and_writes_nothing(tmp_path):
    path = tmp_path / "catalog.sqlite"
    migrate(path)
    again = migrate(path)
    assert not again.applied and again.backup is None
    assert not (tmp_path / "backups").exists(), "opening a current catalog must not create backups"


# --- connection settings ----------------------------------------------------------------------------------------


def test_connections_enforce_foreign_keys_and_use_wal(tmp_path):
    path = tmp_path / "catalog.sqlite"
    connection = connect(path)
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE child (id INTEGER PRIMARY KEY, p INTEGER NOT NULL REFERENCES parent(id))")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO child (p) VALUES (99)")  # an orphan must be refused, not stored
    finally:
        connection.close()


def test_read_only_connection_cannot_write(tmp_path):
    path = tmp_path / "catalog.sqlite"
    migrate(path)
    connection = connect(path, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE library SET catalog_revision = 5")
    finally:
        connection.close()


# --- rollback and backup ----------------------------------------------------------------------------------------


def test_failed_migration_rolls_back_atomically_and_leaves_a_backup(tmp_path):
    path = tmp_path / "catalog.sqlite"
    migrate(path, migrations=[V1])
    connection = connect(path)
    connection.execute("INSERT INTO note (body) VALUES ('keep me')")
    connection.close()

    broken = Migration(
        2, "broken",
        "CREATE TABLE half_done (x INTEGER);\nINSERT INTO half_done VALUES (1);\nINSERT INTO no_such_table VALUES (1);",
    )
    with pytest.raises(MigrationError) as caught:
        migrate(path, migrations=[V1, broken])

    # The oracle is alive: the statements before the bad one DID run inside the transaction ...
    assert "no_such_table" in str(caught.value)
    # ... and none of it survived: the table the first statement created is gone, and the version did not move.
    assert "half_done" not in _tables(path)
    connection = connect(path, read_only=True)
    try:
        assert mig.current_version(connection) == 1
        assert connection.execute("SELECT body FROM note").fetchone()[0] == "keep me"
    finally:
        connection.close()

    # The safety copy exists, opens independently and holds the pre-migration data.
    backup = caught.value.backup
    assert backup is not None and backup.exists() and backup.name.endswith(mig.BACKUP_SUFFIX)
    copy = sqlite3.connect(backup)
    try:
        assert copy.execute("SELECT body FROM note").fetchone()[0] == "keep me"
        assert copy.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0] == 1
    finally:
        copy.close()

    # A corrected migration then applies cleanly: a failed step is retryable, not a dead end.
    assert migrate(path, migrations=[V1, V2]).to_version == 2


def test_backup_taken_while_wal_is_active_contains_committed_data(tmp_path):
    path = tmp_path / "catalog.sqlite"
    migrate(path, migrations=[V1])

    writer = connect(path)  # stays open: SQLite cannot checkpoint the WAL away under a live writer
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    for index in range(50):
        writer.execute("INSERT INTO note (body) VALUES (?)", (f"row {index}",))
    try:
        wal = path.with_name(path.name + "-wal")
        assert wal.exists() and wal.stat().st_size > 0, "the data must really be sitting in the WAL"

        # The oracle is alive: copying only the main file loses the rows. This is the bug the backup API avoids.
        naive = tmp_path / "naive.sqlite"
        shutil.copy2(path, naive)
        naive_connection = sqlite3.connect(naive)
        try:
            naive_rows = naive_connection.execute("SELECT COUNT(*) FROM note").fetchone()[0]
        finally:
            naive_connection.close()
        assert naive_rows < 50, "a plain file copy kept the WAL rows, so this test proves nothing about the backup"

        result = migrate(path, migrations=[V1, V2])
        assert result.backup is not None
    finally:
        writer.close()

    independent = sqlite3.connect(result.backup)  # no -wal beside it, no shared state with the live catalog
    try:
        assert independent.execute("SELECT COUNT(*) FROM note").fetchone()[0] == 50
        assert independent.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        independent.close()


def test_two_backups_in_one_second_do_not_overwrite_each_other(tmp_path):
    path = tmp_path / "catalog.sqlite"
    migrate(path, migrations=[V1])
    first = migrate(path, migrations=[V1, V2]).backup
    # Roll the schema back by hand so a second migration runs in the same second.
    connection = connect(path)
    connection.execute("DELETE FROM schema_migration WHERE version = 2")
    connection.execute("ALTER TABLE note DROP COLUMN pinned")
    connection.close()
    second = migrate(path, migrations=[V1, V2]).backup
    assert first != second and first.exists() and second.exists()


# --- policy -----------------------------------------------------------------------------------------------------


def _schema_snapshot(path):
    connection = connect(path, read_only=True)
    try:
        rows = connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        versions = connection.execute("SELECT version FROM schema_migration ORDER BY version").fetchall()
        return [tuple(r) for r in rows], [r[0] for r in versions]
    finally:
        connection.close()


def test_a_catalog_from_a_newer_version_is_refused_and_untouched(tmp_path):
    path = tmp_path / "catalog.sqlite"
    migrate(path, migrations=[V1, V2, Migration(3, "future", "CREATE TABLE future (x INTEGER);")])
    before = _schema_snapshot(path)
    assert before[1] == [1, 2, 3]

    with pytest.raises(SchemaTooNew):
        migrate(path, migrations=[V1, V2])

    assert "future" in _tables(path), "an older app must not drop what a newer one added"
    assert _schema_snapshot(path) == before
    assert not (tmp_path / "backups").exists(), "a refused open must not write a backup either"


def test_statement_splitter_keeps_trigger_bodies_whole():
    sql = (
        "CREATE TABLE t (x INTEGER);\n"
        "CREATE TRIGGER trg AFTER INSERT ON t BEGIN\n  UPDATE t SET x = x + 1;\n  UPDATE t SET x = x + 1;\nEND;\n"
        "INSERT INTO t VALUES (1);\n"
    )
    parts = mig._statements(sql)
    assert len(parts) == 3, "a naive split on ';' would cut the trigger into pieces"
    assert parts[1].startswith("CREATE TRIGGER") and parts[1].rstrip().endswith("END;")


def test_incomplete_final_statement_is_an_error_not_silently_dropped():
    with pytest.raises(ValueError):
        mig._statements("CREATE TABLE t (x INTEGER);\nINSERT INTO t VALUES (1")
