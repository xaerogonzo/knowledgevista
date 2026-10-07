# Changelog

## Unreleased

- Milestone 3: metadata. `kv resolve` proposes DOIs, titles, authors, ISBNs and arXiv ids from each document's text, Info/XMP and first-page layout, as
  *proposals with their evidence*; a printed DOI is classified own / foreign / ambiguous from several features (position is one of them), and a DOI that
  three or more documents each print as their own is recognised as a parent work (a book's), not any one document's. `--online` asks Crossref (a DOI or a
  title is all that is sent; off until `kv config set online_lookup true`; `--list-requests` shows exactly what would be sent), compares the record with the
  PDF, and a record that does not match proposes nothing. `kv review list/accept/reject` is the queue; `kv resolve --accept-safe` applies one named,
  recorded rule (`safe_batch_v1`) to proposals that earned `safe`; `kv metadata set/clear/lock/unlock` states a value by hand. Rerunning changes
  nothing; a provider outage changes no accepted value and the run resumes from a cache of definite answers. `explain`, `stats` and `doctor` cover
  metadata, and every PDF is either titled or in a stated state. See docs/METADATA.md.
- The plan's 66% DOI-coverage target is restated: it came from a figure that counted one encyclopedia's DOI as each of its 160 entries' (docs/ARCHITECTURE.md).
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
