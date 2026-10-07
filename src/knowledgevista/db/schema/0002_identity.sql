-- Milestone 1: the identity substrate. Roots, locations, artifacts, documents, scan history.
--
-- THE MODEL (docs/ARCHITECTURE.md, "Object model"):
--   root      a configured folder that is scanned.
--   location  one observed path under a root. Locations are HISTORY: they are ended, never deleted.
--   artifact  immutable bytes, identified by their SHA-256.
--   document  a stable logical library item (a UUID). Links to artifacts through document_artifact.
--
-- DEVIATION FROM THE PLAN, recorded on purpose: the plan gave an artifact a `state` (stable / changing /
-- unreadable). Bytes that could not be read, or that changed while being read, have no hash and therefore no
-- artifact, so that state cannot live on one. It is a property of an OBSERVATION: a location whose state is
-- `inaccessible`, or a scan report's `changing` count. An artifact's availability is derived (some active
-- location exists), never stored.

CREATE TABLE root (
    root_id          TEXT PRIMARY KEY,
    configured_path  TEXT NOT NULL,
    root_key         TEXT NOT NULL UNIQUE,
    label            TEXT NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    status           TEXT NOT NULL DEFAULT 'online'
                     CHECK (status IN ('online', 'unavailable', 'permission_denied', 'moved_candidate')),
    volume_id        TEXT,
    -- The organizer refuses a root unless this is 1, even if the OS would allow the write.
    allow_organize   INTEGER NOT NULL DEFAULT 0 CHECK (allow_organize IN (0, 1)),
    created_at       TEXT NOT NULL,
    last_scan_at     TEXT
);

CREATE TABLE artifact (
    artifact_id   TEXT PRIMARY KEY CHECK (length(artifact_id) = 64 AND artifact_id = lower(artifact_id)),
    size          INTEGER NOT NULL CHECK (size >= 0),
    -- What the first bytes show. The extension's claim is derived from a path when needed, never stored here.
    content_kind  TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);

CREATE TABLE document (
    document_id  TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL
);

CREATE TABLE document_artifact (
    document_id       TEXT NOT NULL REFERENCES document (document_id),
    artifact_id       TEXT NOT NULL REFERENCES artifact (artifact_id),
    role              TEXT NOT NULL CHECK (role IN ('primary', 'ocr_derivative', 'alternate_copy')),
    canonical         INTEGER NOT NULL DEFAULT 0 CHECK (canonical IN (0, 1)),
    canonical_reason  TEXT,
    PRIMARY KEY (document_id, artifact_id)
);
-- An artifact belongs to exactly one document, and a document has at most one canonical artifact.
CREATE UNIQUE INDEX document_artifact_one_document ON document_artifact (artifact_id);
CREATE UNIQUE INDEX document_artifact_one_canonical ON document_artifact (document_id) WHERE canonical = 1;

CREATE TABLE scan_run (
    run_id       TEXT PRIMARY KEY,
    root_id      TEXT NOT NULL REFERENCES root (root_id),
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL
                 CHECK (status IN ('running', 'completed', 'interrupted', 'skipped_root_unavailable', 'failed')),
    app_version  TEXT NOT NULL,
    full_rehash  INTEGER NOT NULL DEFAULT 0 CHECK (full_rehash IN (0, 1)),
    stats_json   TEXT
);

CREATE TABLE location (
    location_id            TEXT PRIMARY KEY,
    root_id                TEXT NOT NULL REFERENCES root (root_id),
    -- The path as the filesystem spelled it, '/'-separated, relative to the root. Shown to the user.
    relative_path          TEXT NOT NULL,
    -- The comparison key (domain/pathkeys.py). Decides whether two paths are the same file. Never shown.
    path_key               TEXT NOT NULL,
    -- NULL only for a file that has never been readable.
    artifact_id            TEXT REFERENCES artifact (artifact_id),
    -- The stat taken immediately BEFORE the bytes were read: a freshness HINT for the next scan, never proof.
    size                   INTEGER,
    mtime_ns               INTEGER,
    state                  TEXT NOT NULL CHECK (state IN ('active', 'missing', 'inaccessible')),
    first_seen             TEXT NOT NULL,
    last_seen              TEXT NOT NULL,
    -- A location is ended when it is superseded: the same path now holds different bytes, or the bytes moved.
    ended_at               TEXT,
    end_reason             TEXT CHECK (end_reason IN ('replaced', 'moved')),
    successor_location_id  TEXT REFERENCES location (location_id),
    CHECK ((ended_at IS NULL) = (end_reason IS NULL)),
    CHECK (state <> 'active' OR artifact_id IS NOT NULL)
);
-- At most one CURRENT location per path; any number of ended ones (the history).
CREATE UNIQUE INDEX location_current_path ON location (root_id, path_key) WHERE ended_at IS NULL;
CREATE INDEX location_artifact ON location (artifact_id);
CREATE INDEX location_path_key ON location (path_key);

CREATE TABLE location_event (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id  TEXT NOT NULL REFERENCES location (location_id),
    run_id       TEXT REFERENCES scan_run (run_id),
    event        TEXT NOT NULL,
    at           TEXT NOT NULL,
    -- Who caused it: scanner, organizer, user, migration, resolver, import.
    actor        TEXT NOT NULL DEFAULT 'scanner',
    detail       TEXT
);
CREATE INDEX location_event_location ON location_event (location_id);
