# Changelog

## Unreleased

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
