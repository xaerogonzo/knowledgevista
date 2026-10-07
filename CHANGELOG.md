# Changelog

## Unreleased

- Milestone 5: the integration surface. `kv capabilities` (needs no catalog) reports the four version numbers separately (`protocol_version`, the JSON envelope, the catalog schema, the MCP revisions), every command flagged read-only or not, and the MCP tools; a test holds that table equal to the parser and the contract. `kv locate` answers "where is this NOW" from a document id, a SHA-256 or prefix, a name or a `knowledgevista://` reference, `stat`s every path (the disk beats the catalog), resolves an id a merge retired to its survivor and says so, and never guesses. `knowledgevista://document|artifact/<id>[/page/N|/label/L]` is a strict, one-spelling grammar. `kv open` hands a file to the OS viewer (the page is reported, not navigated to). Listings carry opaque cursors that go `KV_CURSOR_STALE` after any catalog change or reordering instead of skipping or repeating; `--limit` is 1 to 1000 and refused, not clamped. `kv mcp` serves nine read-only tools (search, document, page by position or by printed label, resolve, metadata, related, duplicates, locate) over stdio with no dependency; the catalog is opened `mode=ro` and never migrated, page text is returned as untrusted data, and a test requires every tool to leave the catalog and extraction store byte-identical. 79 planted faults in the new code: 8 survived the first suite and were closed by tests (three of the 8 were guards made redundant by a later check, kept because they give the error its right words, and now pinned by it). See docs/INTEGRATION.md and docs/MCP.md.
- Milestone 4: relations and virtual organisation. `kv relate` looks across the library for the same publication twice (identical extracted text under different bytes, a shared accepted DOI, the same title and first author), supplements, corrections, chapters of a book, preprint/published pairs and replaced files, and PROPOSES each with its evidence; nothing is related or merged by a run, and a rerun with nothing new changes nothing. `kv relations list/accept/reject/add/remove` is the queue; accepting a `same_document` proposal merges two documents (the absorbed one is retired, never deleted; every artifact id is kept; relations, collections, tags, open proposals and accepted values the survivor lacks come along) and `kv document split` takes an artifact back out, reviving a merged-away document under its original id, even after a chain of merges. `kv dupes` keeps the four levels of duplicate apart. Collections, tags, saved searches (stored as a parsed query with its language version) and computed system views (`kv view`) touch no file. Search language 2 adds the filters `author: year: doi: kind: tag: collection:`, which read accepted metadata only. `doctor` gains a `relationships` category. Two defects only real data showed were fixed: old Wiley DOIs containing `<`/`>` were corrupted by markup stripping, and a merge left the open proposals about the absorbed document stale. 89 planted faults in the relation, organisation and filter code were run against the suite: 27 survived the first suite and were each closed by a test (tests/test_relations_edges.py); the 4 that still survive are equivalent (a guard made redundant by the check after it, an anchored regex's slice, and a revival condition no sequence of merges can reach). See docs/RELATIONS.md.
- Milestone 3: metadata. `kv resolve` proposes DOIs, titles, authors, ISBNs and arXiv ids from each document's text, Info/XMP and first-page layout, as
  *proposals with their evidence*; a printed DOI is classified own / foreign / ambiguous from several features (position is one of them), and a DOI that
  three or more documents each print as their own is recognised as a parent work (a book's), not any one document's. `--online` asks Crossref (a DOI or a
  title is all that is sent; off until `kv config set online_lookup true`; `--list-requests` shows exactly what would be sent), compares the record with the
  PDF, and a record that does not match proposes nothing. `kv review list/accept/reject` is the queue; `kv resolve --accept-safe` applies one named,
  recorded rule (`safe_batch_v1`) to proposals that earned `safe`; `kv metadata set/clear/lock/unlock` states a value by hand. Rerunning changes
  nothing; a provider outage changes no accepted value and the run resumes from a cache of definite answers. `explain`, `stats` and `doctor` cover
  metadata, and every PDF is either titled or in a stated state. See docs/METADATA.md.
- The plan's 66% DOI-coverage target is restated: it came from a figure that counted one encyclopedia's DOI as each of its 160 entries' (docs/ARCHITECTURE.md). A full online run over the real 727-PDF library accepted 393 correct DOIs (54%; 69% of the PDFs that are not entries of that encyclopedia) and 360 titles, and found, after the 80-document sample had passed, a matcher hole (a book record with no authors and a file with no layout title could be `exact`), a ligature inside an accepted DOI, and a letter-spaced scan accepted as a title; all fixed and tested (docs/METADATA.md).
- `kv extract`'s worker gained a `front` request (Info, XMP and first-page layout) used only by `resolve`.

- Milestone 2: text extraction and search. `kv extract` reads PDF text in a separate, killable worker process
  (a Windows Job Object caps its memory and kills it with the parent; each page that hangs or crashes the worker is
  recorded as failed and extraction resumes at the next page), into a rebuildable extraction store in the cache keyed by
  artifact hash. `kv search` (phrase, `*` prefix, `|` alternatives, `--near`, `--also`) always states its coverage;
  searching nothing searchable is an error, not an empty success. `kv show` addresses a page by `--pdf-page` (position)
  or `--label` (printed label), never an ambiguous "page". `kv import openchem-index` reuses an OpenChem index as
  provisional text, accepted only for hashes the catalog already holds. Profile versioning marks extractions stale when
  the extractor changes. doctor and stats cover extraction and search. See docs/SEARCH.md.
- Design correction: extraction lives in a cache SQLite file, not catalog tables, so deleting it provably loses no
  catalog state (docs/ARCHITECTURE.md).

- Milestone 1: the identity substrate. Roots, locations (history, never deleted), artifacts (SHA-256) and documents;
  a scanner that reconciles disk with the catalog (moves and renames keep identity, replaced bytes become a new
  artifact and document, absence is `missing` and never a deletion, an unavailable or half-mounted root changes
  nothing, a killed scan is recoverable); `kv root add/list`, `scan`, `stats`, `explain`, `doctor`, `verify`; a stable
  JSON envelope with documented exit codes and error codes (docs/CLI_CONTRACT.md). 241 tests; 36 planted faults in
  the scanner and services were each caught.
- Deviation recorded: artifact state is not a stored column (see docs/ARCHITECTURE.md).
- A root that contains the catalog file is refused; Access `.mdb` files are recognised as databases (found by
  running against a real 1,862-file library).

- Milestone 0: repository bootstrap. Package skeleton, `kv --version`, a single `paths` module for app-data
  locations, a forward-only migration framework (atomic per migration, backup with SQLite's backup API, refuses a
  newer schema), a generated-fixture PDF builder, a safe-repo check, a lockfile-closure licence inventory, and the
  invariants, safety model and architecture documents.
