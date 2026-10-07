-- Milestone 3: metadata. Candidates (proposals), accepted values, and the history of both.
--
-- THE MODEL (docs/METADATA.md):
--   metadata_candidate  a PROPOSAL: a value some evidence suggests for one field of one document. It carries what
--                       the evidence was, how it was scored and by which rules. Never a fact.
--   metadata_value      the ACCEPTED value of a field: at most one per (document, field). A row exists iff a value is
--                       accepted; "unknown" is the absence of a row, never an empty string.
--   metadata_history    every change to an accepted value, with the previous value and where it came from.
--   resolve_run         one run of `kv resolve`, so a candidate says which run proposed it and a run can be listed.
--
-- DEVIATION FROM THE PLAN, recorded on purpose: the plan gave a metadata VALUE a status (proposed / accepted / rejected
-- / stale). Splitting proposals from accepted state makes the invariant structural: nothing in metadata_value can be a
-- guess, so a reader (the GUI, MCP, an export) never has to filter for it. Status lives on the candidate.
--
-- Everything provider-derived is cached OUTSIDE the catalog (index/metacache.py), because it is rebuildable; only the
-- decisions people (or an explicit, recorded rule) made live here.

CREATE TABLE resolve_run (
    run_id           TEXT PRIMARY KEY,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL CHECK (status IN ('running', 'completed', 'interrupted', 'failed')),
    app_version      TEXT NOT NULL,
    -- The rules that produced the candidates (domain/doi_evidence.MATCHER_VERSION and the provider client versions).
    matcher_version  TEXT NOT NULL,
    online           INTEGER NOT NULL CHECK (online IN (0, 1)),
    accept_safe      INTEGER NOT NULL CHECK (accept_safe IN (0, 1)),
    stats_json       TEXT
);

CREATE TABLE metadata_candidate (
    candidate_id     TEXT PRIMARY KEY,
    document_id      TEXT NOT NULL REFERENCES document (document_id),
    -- The artifact whose bytes the evidence was read from. A different artifact is different evidence.
    artifact_id      TEXT NOT NULL REFERENCES artifact (artifact_id),
    field            TEXT NOT NULL CHECK (field IN
                     ('doi', 'title', 'authors', 'year', 'container', 'publisher', 'type', 'volume', 'issue', 'pages', 'isbn', 'arxiv')),
    -- Canonical form (a DOI is lower case; authors are a JSON list). Never ''.
    value            TEXT NOT NULL CHECK (value <> ''),
    -- observed: read from the file; resolved: returned by a provider; inferred: deduced from other values.
    origin           TEXT NOT NULL CHECK (origin IN ('observed', 'resolved', 'inferred')),
    -- A registry name (docs/METADATA.md, "Sources"): pdf_text_doi, pdf_info_title, layout_title, crossref, ...
    source           TEXT NOT NULL,
    -- Identifies the EVIDENCE, so one proposal exists per distinct evidence however often it is found again.
    evidence_key     TEXT NOT NULL,
    evidence_json    TEXT NOT NULL,
    -- For a DOI: printed as the document's own, someone else's, or undecided. NULL for other fields.
    classification   TEXT CHECK (classification IS NULL OR classification IN ('own', 'foreign', 'ambiguous')),
    -- Descriptive, never a bare percentage.
    confidence       TEXT NOT NULL CHECK (confidence IN ('exact', 'high', 'medium', 'low', 'ambiguous')),
    -- safe: a rule may accept it in a batch; required: a person must; a safe rule is a property of the CANDIDATE.
    review           TEXT NOT NULL CHECK (review IN ('safe', 'required')),
    risk             TEXT NOT NULL CHECK (risk IN ('identity', 'duplicate', 'classification', 'filesystem')),
    priority         INTEGER NOT NULL DEFAULT 50,
    matcher_version  TEXT NOT NULL,
    -- proposed: waiting; accepted / rejected: decided; stale: its evidence or rules changed and it was not found
    -- again; set_aside: judged not worth proposing (a foreign DOI), kept only to answer "why not this one?".
    status           TEXT NOT NULL CHECK (status IN ('proposed', 'accepted', 'rejected', 'stale', 'set_aside')),
    first_run_id     TEXT REFERENCES resolve_run (run_id),
    last_run_id      TEXT REFERENCES resolve_run (run_id),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    decided_at       TEXT,
    -- user | rule:<name>
    decided_by       TEXT
);
CREATE UNIQUE INDEX metadata_candidate_evidence ON metadata_candidate (document_id, field, evidence_key);
CREATE INDEX metadata_candidate_queue ON metadata_candidate (status, review, priority);
CREATE INDEX metadata_candidate_document ON metadata_candidate (document_id);

CREATE TABLE metadata_value (
    document_id         TEXT NOT NULL REFERENCES document (document_id),
    field               TEXT NOT NULL CHECK (field IN
                        ('doi', 'title', 'authors', 'year', 'container', 'publisher', 'type', 'volume', 'issue', 'pages', 'isbn', 'arxiv')),
    value               TEXT NOT NULL CHECK (value <> ''),
    origin              TEXT NOT NULL CHECK (origin IN ('observed', 'resolved', 'inferred', 'assigned')),
    source              TEXT NOT NULL,
    source_candidate_id TEXT REFERENCES metadata_candidate (candidate_id),
    evidence_json       TEXT,
    -- A locked value is never replaced by a resolver or a batch rule; only the person who locked it can change it.
    locked              INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
    accepted_by         TEXT NOT NULL,
    accepted_at         TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (document_id, field)
);
CREATE INDEX metadata_value_lookup ON metadata_value (field, value);

CREATE TABLE metadata_history (
    history_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   TEXT NOT NULL REFERENCES document (document_id),
    field         TEXT NOT NULL,
    old_value     TEXT,
    old_source    TEXT,
    new_value     TEXT,
    new_source    TEXT,
    candidate_id  TEXT,
    at            TEXT NOT NULL,
    -- scanner, organizer, user, migration, resolver, import (the same vocabulary as location_event.actor)
    actor         TEXT NOT NULL,
    reason        TEXT
);
CREATE INDEX metadata_history_document ON metadata_history (document_id, history_id);
