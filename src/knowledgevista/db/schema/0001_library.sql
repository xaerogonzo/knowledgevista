-- The library: one row naming this catalog, and the revision counter every client uses to notice change.
--
-- library_id is opaque and generated once; exports and backups carry it so a restore is recognisable.
-- catalog_revision is bumped by meaningful writes (milestone 1 onwards); GUI and MCP clients compare it to
-- learn that what they hold is stale, and a pagination cursor encodes it so a stale cursor can be refused.
CREATE TABLE library (
    singleton        INTEGER PRIMARY KEY CHECK (singleton = 1),
    library_id       TEXT    NOT NULL,
    created_at       TEXT    NOT NULL,
    catalog_revision INTEGER NOT NULL DEFAULT 0
);

INSERT INTO library (singleton, library_id, created_at)
VALUES (1, lower(hex(randomblob(16))), strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
