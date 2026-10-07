-- Milestone 6: the reversible organizer's journal.
--
--   plan_registry   every plan this catalog made: its id, where the file is, the hash of its contents. A plan file that is not registered
--                   here, or whose hash differs, is not applied (it is somebody else's, or it was edited).
--   operation       one apply, undo or recover. `running` is written BEFORE the first file is touched and only ever replaced by a final
--                   status, so a `running` row that nobody is executing is an interrupted operation and `kv recover` says so.
--   operation_item  one planned move. THE INTENT IS COMMITTED (`executing`) BEFORE the filesystem is touched and the outcome after, in the
--                   same transaction as the catalog's location update, so the journal and the catalog cannot disagree about a move that
--                   finished. Rows are never deleted: history is the product.
--
-- Nothing here is authoritative about the filesystem: reality is. Recovery reconciles this journal AGAINST the disk and never retries
-- an ambiguous move.

CREATE TABLE plan_registry (
    plan_id          TEXT PRIMARY KEY,
    path             TEXT NOT NULL,
    plan_hash        TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    catalog_revision INTEGER NOT NULL,
    naming_policy    TEXT NOT NULL,
    item_count       INTEGER NOT NULL,
    planned_count    INTEGER NOT NULL,
    options_json     TEXT NOT NULL,
    app_version      TEXT NOT NULL
);

CREATE TABLE operation (
    operation_id         TEXT PRIMARY KEY,
    kind                 TEXT NOT NULL CHECK (kind IN ('apply', 'undo', 'recover')),
    plan_id              TEXT REFERENCES plan_registry (plan_id),
    undoes_operation_id  TEXT REFERENCES operation (operation_id),
    started_at           TEXT NOT NULL,
    finished_at          TEXT,
    status               TEXT NOT NULL CHECK (status IN ('running', 'completed', 'completed_with_problems', 'interrupted')),
    -- Who caused it: organizer, user, migration, resolver, import (docs/ARCHITECTURE.md).
    actor                TEXT NOT NULL,
    app_version          TEXT NOT NULL,
    operation_schema     INTEGER NOT NULL,
    summary_json         TEXT
);

CREATE TABLE operation_item (
    row_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id         TEXT NOT NULL REFERENCES operation (operation_id),
    seq                  INTEGER NOT NULL,
    item_id              TEXT NOT NULL,
    reverses_row_id      INTEGER REFERENCES operation_item (row_id),
    operation_kind       TEXT NOT NULL,
    root_id              TEXT NOT NULL REFERENCES root (root_id),
    artifact_id          TEXT NOT NULL REFERENCES artifact (artifact_id),
    document_id          TEXT NOT NULL,
    location_id_before   TEXT,
    location_id_after    TEXT,
    old_path             TEXT NOT NULL,
    new_path             TEXT NOT NULL,
    expected_sha256      TEXT NOT NULL,
    state                TEXT NOT NULL CHECK (state IN ('planned', 'prechecked', 'executing', 'succeeded', 'failed', 'uncertain', 'skipped', 'undone')),
    error_code           TEXT,
    message              TEXT,
    -- A temporary name used mid-move (a case-only rename, or one step of a chain), journaled before it exists.
    temp_path            TEXT,
    created_dirs_json    TEXT,
    undone_by_row_id     INTEGER REFERENCES operation_item (row_id),
    started_at           TEXT,
    finished_at          TEXT
);
CREATE INDEX operation_item_operation ON operation_item (operation_id, seq);
CREATE INDEX operation_item_state ON operation_item (state);
