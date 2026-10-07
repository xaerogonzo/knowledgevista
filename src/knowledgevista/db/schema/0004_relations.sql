-- Milestone 4: relations, duplicates, and virtual organisation (collections, tags, saved searches).
--
-- THE MODEL (docs/RELATIONS.md):
--   artifact_relation   a fact about two byte objects:  duplicate_of | derivative_of | replaces | equivalent_to
--   document_relation   a fact about two library items: supplement_of | part_of | version_of | related_to
--   relation_candidate  a PROPOSAL of either, or of a merge ('same_document') or a collection. Never a fact. Every
--                       automatic relation starts here, with its evidence, and becomes a relation only when accepted.
--   document_event      the history of merges and splits. A merge MOVES artifacts to the surviving document and RETIRES the
--                       other (retired_at, merged_into); it never deletes anything, and a split can revive the retired
--                       document under its original id, so a merge followed by a split restores what was there.
--   collection / collection_member / document_tag / saved_search   virtual organisation: nothing here touches a file.
--
-- Soft retraction, not deletion: a relation, collection or saved search that is removed gets retracted_at/retired_at, so
-- the record that it existed (and who removed it) stays.

ALTER TABLE document ADD COLUMN retired_at TEXT;
ALTER TABLE document ADD COLUMN merged_into TEXT REFERENCES document (document_id);

CREATE TABLE document_event (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  TEXT NOT NULL REFERENCES document (document_id),
    event        TEXT NOT NULL CHECK (event IN ('merged_into', 'absorbed', 'split_off', 'revived', 'created_by_split')),
    at           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    -- JSON: the artifacts moved, the other document, the reason.
    detail       TEXT
);
CREATE INDEX document_event_document ON document_event (document_id, event_id);

CREATE TABLE relation_run (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL CHECK (status IN ('running', 'completed', 'interrupted')),
    matcher_version TEXT NOT NULL,
    stats_json   TEXT
);

CREATE TABLE relation_candidate (
    candidate_id    TEXT PRIMARY KEY,
    -- artifact: between two artifacts; document: between two documents (or a merge); group: a set of documents.
    level           TEXT NOT NULL CHECK (level IN ('artifact', 'document', 'group')),
    kind            TEXT NOT NULL CHECK (kind IN
                    ('duplicate_of', 'derivative_of', 'replaces', 'equivalent_to',
                     'same_document', 'supplement_of', 'part_of', 'version_of', 'related_to', 'collection')),
    -- An artifact id or a document id; for a group, a stable key naming it. target_id is '' for a group.
    source_id       TEXT NOT NULL,
    target_id       TEXT NOT NULL DEFAULT '',
    -- JSON list of document ids for a group.
    members_json    TEXT,
    -- What kind of evidence this is (a shared DOI, identical text, a printed DOI, ...): one proposal per distinct evidence.
    evidence_key    TEXT NOT NULL,
    evidence_json   TEXT NOT NULL,
    confidence      TEXT NOT NULL CHECK (confidence IN ('exact', 'high', 'medium', 'low', 'ambiguous')),
    matcher_version TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('proposed', 'accepted', 'rejected', 'stale')),
    first_run_id    TEXT REFERENCES relation_run (run_id),
    last_run_id     TEXT REFERENCES relation_run (run_id),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    decided_at      TEXT,
    decided_by      TEXT,
    CHECK (level = 'group' OR target_id <> ''),
    CHECK (source_id <> target_id)
);
CREATE UNIQUE INDEX relation_candidate_evidence ON relation_candidate (kind, source_id, target_id, evidence_key);
CREATE INDEX relation_candidate_queue ON relation_candidate (status, kind);

CREATE TABLE artifact_relation (
    relation_id   TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('duplicate_of', 'derivative_of', 'replaces', 'equivalent_to')),
    source_id     TEXT NOT NULL REFERENCES artifact (artifact_id),
    target_id     TEXT NOT NULL REFERENCES artifact (artifact_id),
    accepted_from_candidate TEXT REFERENCES relation_candidate (candidate_id),
    accepted_by   TEXT NOT NULL,
    accepted_at   TEXT NOT NULL,
    note          TEXT,
    retracted_at  TEXT,
    retracted_by  TEXT,
    CHECK (source_id <> target_id)
);
CREATE UNIQUE INDEX artifact_relation_live ON artifact_relation (kind, source_id, target_id) WHERE retracted_at IS NULL;

CREATE TABLE document_relation (
    relation_id   TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('supplement_of', 'part_of', 'version_of', 'related_to')),
    source_id     TEXT NOT NULL REFERENCES document (document_id),
    target_id     TEXT NOT NULL REFERENCES document (document_id),
    -- Where a part sits in its whole (a chapter number); text, because "A-1" and "iv" are positions too.
    position      TEXT,
    accepted_from_candidate TEXT REFERENCES relation_candidate (candidate_id),
    accepted_by   TEXT NOT NULL,
    accepted_at   TEXT NOT NULL,
    note          TEXT,
    retracted_at  TEXT,
    retracted_by  TEXT,
    CHECK (source_id <> target_id)
);
CREATE UNIQUE INDEX document_relation_live ON document_relation (kind, source_id, target_id) WHERE retracted_at IS NULL;

CREATE TABLE collection (
    collection_id TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    -- The comparison key (case- and punctuation-insensitive): two collections never differ only by "Papers" vs "papers".
    name_key      TEXT NOT NULL,
    description   TEXT,
    -- A collection made from a parent work records it, so "these are the entries of that book" is not lost.
    source_doi    TEXT,
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    retired_at    TEXT
);
CREATE UNIQUE INDEX collection_live_name ON collection (name_key) WHERE retired_at IS NULL;

CREATE TABLE collection_member (
    collection_id TEXT NOT NULL REFERENCES collection (collection_id),
    document_id   TEXT NOT NULL REFERENCES document (document_id),
    added_at      TEXT NOT NULL,
    added_by      TEXT NOT NULL,
    PRIMARY KEY (collection_id, document_id)
);
CREATE INDEX collection_member_document ON collection_member (document_id);

CREATE TABLE document_tag (
    document_id TEXT NOT NULL REFERENCES document (document_id),
    tag_key     TEXT NOT NULL,
    tag         TEXT NOT NULL,
    added_at    TEXT NOT NULL,
    added_by    TEXT NOT NULL,
    PRIMARY KEY (document_id, tag_key)
);
CREATE INDEX document_tag_key ON document_tag (tag_key);

CREATE TABLE saved_search (
    search_id        TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    name_key         TEXT NOT NULL,
    -- The parsed query (the AST), never a result list, and the language version it was parsed with.
    query_json       TEXT NOT NULL,
    language_version INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    retired_at       TEXT
);
CREATE UNIQUE INDEX saved_search_live_name ON saved_search (name_key) WHERE retired_at IS NULL;
