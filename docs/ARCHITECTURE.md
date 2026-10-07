# Architecture

Status: **design baseline (milestone 0).** This is what is being built, in the order it will be built. Sections marked
as milestones are not implemented yet; [INVARIANTS.md](INVARIANTS.md) is what every part must satisfy and
[SAFETY_MODEL.md](SAFETY_MODEL.md) is what each operation may touch.

Knowledge Vista is a local-first library manager, search index and reader. It is headless first (a CLI and a read-only
MCP server over one set of services), with a PySide6 GUI and reader built on the same services later.

## Layering

```
CLI / MCP / GUI   (thin adapters: parse input, call a service, render a DTO)
      |
services          (scan, extract, resolve, search, relate, plan/apply: all business logic)
      |
domain            (pure: ids, normalisers, matching, naming, query AST, path safety, operation planning)
      |
boundaries        (SQLite, filesystem, network, worker processes)
```

Side effects live at the edges; the domain is pure and tiny-tested. CLI, MCP and GUI never carry their own SQL or
mutation logic, and no API returns a raw database row.

## Licensing

Core depends on the standard library plus `pymupdf` (the `extract` group). PyMuPDF is AGPL-3.0 or commercial, so this
project is **AGPL-3.0-or-later**. OpenChem Studio (GPL-3.0-or-later) is a separate program reached through the CLI; no
PyMuPDF-dependent module is shared between the two repositories.

Measured on the maintainer's machine in October 2026: `pymupdf4llm` (Markdown reflow) requires `pymupdf_layout`, which is
licensed **Polyform Noncommercial**, so it is not free software and may not enter the default install, the lockfile
closure of the core, or any build. The `reflow` and `ocr` groups are empty until that is resolved, and
`tools/license_inventory.py` (run in CI over the lockfile's transitive closure) fails if a blocked or unknown licence
appears in the selected install. Before any reflow work, benchmark whether plain PyMuPDF text extraction is enough.

## Object model

```
Library
 ├─ Root ── Location ── Artifact(bytes, sha256) ── Extraction ── Page
 ├─ Document ── (document_artifact link: role, canonical flag) ── Artifact
 ├─ Collection / SavedSearch / Tag
 ├─ Metadata(value) / Candidate / Review item
 ├─ Annotation / Note / ReadingPosition
 ├─ Operation / OperationItem / Plan
 └─ ResolverCache, SchemaMigration
```

| Entity | Meaning and rules |
|---|---|
| **root** | `root_id`, configured path, label, enabled, `status` (`online / unavailable / permission_denied / moved_candidate`), optional volume id, `allow_organize` (default false). The organizer refuses a root without it, even if the OS would allow writes. A configured UNC root is legitimate; the rule is "paths stay inside an explicitly configured root; `..`, reparse-point and alternate-data-stream escapes are rejected". |
| **location** | One observed path: `root_id + relative_path`, `first_seen`, `last_seen`, `state` (`active / missing / inaccessible`), `raw display path` and a separate normalised comparison key (Windows case semantics explicit; `\\?\` extended-length paths accepted and normalised internally). Location history is retained and never purged by default. |
| **artifact** | Immutable bytes, `artifact_id = sha256`, size, `state` (`stable / changing / unreadable`). Availability is *derived*: an artifact is available iff some active, accessible location exists. Not a stored flag. |
| **document** | A stable opaque UUID: *a user-addressable logical library item*. It is not a bibliographic work and not a hash. Linked to artifacts through `document_artifact(role: primary / ocr_derivative / alternate_copy, canonical, canonical_reason)`. Replaced bytes make a new artifact that does **not** inherit metadata; joining it to the old document is a proposal. A document can be split or merged by an explicit audited action; artifact IDs are unaffected. |
| **artifact_relation** | Byte-level: `duplicate_of`, `derivative_of`, `replaces`, `equivalent_to`. |
| **document_relation** | Logical: `supplement_of`, `part_of` (chapter order lives here), `version_of`, `related_to`. A chapter is its own document, `part_of` its book. |
| **relation_candidate** | Every automatic relation is first a candidate (source, target, kind, evidence, score, `matcher_version`, status). Accepting it creates the relation with `accepted_at`, `accepted_from_candidate`, `accepted_by` (`user / rule / import`). Filename patterns (`_si`, chapter folders) are signals only. |
| **identifier** | `kind, raw, normalized, source`: DOI, ISBN, arXiv, PMID, ISSN. Detects identifier collisions between unrelated documents (reported, never merged). A document with no DOI is still fully identified by its document ID. A chapter or supplement never inherits its parent's DOI. |
| **extraction** | `artifact, extractor, extractor_version, extraction_format_version, options_hash, profile_id, status (complete / partial / failed), page-level errors`. Per-page text state: `text_native / text_sparse / image_only / ocr_available / failed`. |
| **page** | `page_id` is an *internal* key of one extraction. The durable citation anchor is `artifact_id + pdf_page`, plus optional `printed_label` (a string: `iii`, `A-1`, `S12`). `pdf_page` is **1-based**, globally. APIs never take a bare `page`: `get_page(pdf_page=)` and `get_page_by_label(label=)` are different calls. |
| **kind** | `article, book, chapter, supplement, dataset, thesis, report, ...` from a data-driven registry (name, extensions, content detector, reader capabilities). `observed` kind (extension and content) is separate from `assigned` kind. "Scan" is a *text state*, not a kind. |
| **metadata value** | `field, value (NULL = unknown, never ""), origin (observed / resolved / inferred / assigned), status (proposed / accepted / rejected / stale), locked, source (enum registry), structured source_locator, evidence (short excerpt), resolver_version, timestamps`. `origin` and `status` and `locked` are independent. Dates keep `publication_date` and `publication_year`; provider extra dates are retained. Field history records the previous value *and its source*. |
| **candidate / review** | Candidates carry a *match explanation* (score components, negative evidence, `matcher_version`), are deduplicated by evidence identity so one proposal exists per distinct evidence, go `stale` (not `rejected`) when their source artifact or extractor changes, and have a priority and a risk class (identity / duplicate / classification / filesystem). Safe-batch (exact DOI) vs review-required (title-only) is a property of the candidate. |
| **annotation** | Owned by `document_id`, anchored to `artifact_id + pdf_page + quad geometry (normalised for page rotation, media/crop box) + selected text + annotation_schema_version`. Survives moves; not silently carried to a replacement artifact (re-anchoring is an explicit future operation). Reading position is per artifact. Bookmarks (user) are distinct from the PDF outline (observed). Metadata notes, document notes and page annotations are separate types. |
| **operation / item / plan** | See Organizer. All have UUIDs; items have their own IDs. |

**Conventions:** typed ID wrappers and dataclass DTOs (never raw SQLite rows in any API); UTC timestamps internally, local at display; `first_seen / last_seen / resolved_at / updated_at` are distinct; `PRAGMA foreign_keys=ON` on every connection; no cascade-deletes on user state; soft-retract rather than delete; one `paths` module for app-data locations; catalog, cache and source library are separate storage classes. Cache is subdivided: *rebuildable* (extractions, thumbnails), *ephemeral* (temp work dirs, cleaned at startup), and anything *user-owned* (never under cache cleanup).
**Catalog:** SQLite, WAL, one logical writer (serialised writes, bounded busy timeout, readers coexist), a `catalog_revision` bumped on meaningful writes so GUI/MCP detect staleness, forward-only migrations with a pre-migration backup made with SQLite's backup API (a naive file copy ignores the WAL), a `library_id`, and no write transaction ever held across network I/O.

## Scanning and reconciliation (explicit algorithm)

Stages: `scan` (walk + stat) -> `hash` -> `extract` -> `resolve` -> `index`; `sync` runs the stages a policy names. Safe default policy: no network, no organizing, nothing written to the library.

`size + mtime` is a **freshness hint** that lets a fast scan skip hashing; it is never identity proof. Hashing is required for any new or changed file, before any mutation, and in `kv verify --hashes` (separate from the structural `kv doctor`, so doctor never performs a 2.3 GB read).

Reconcile on each scan: (1) walk and stat; (2) compare with known locations; (3) hash new/changed candidates (stat before and after; if it changed mid-read the artifact is `changing`, retried, not committed); (4) match hashes to known artifacts; (5) pair newly seen locations with newly missing locations of the same artifact, so an external move or rename adds a location and ends the old one without creating a document; (6) same path with different bytes: end the old artifact's location, create a new artifact, create no metadata inheritance, and propose (not assert) a link to the old document from DOI/title; (7) a vanished file marks the location `missing`; (8) an unavailable root suppresses per-file missing notices and changes nothing, and reconciles normally when it returns. A path-based restore of the same bytes revives the same artifact. Extraction runs in a **worker process**: a mandatory timeout, a Windows Job Object memory limit where available (spiked in milestone 2, with an explicit fallback and warning), idempotent cancellation, a per-job temp dir cleaned at startup, results committed by a single writer. Page-level errors do not discard other pages. Files being downloaded are not extracted until size/mtime are stable.

## Metadata

1. **Local:** DOI *candidates* with page, region (front matter / body / bibliography), evidence and a classification `own | foreign | ambiguous` decided from several features (front-matter likelihood, agreement with the printed title/authors/journal, resolver agreement), position being one feature; PDF Info/XMP as untrusted evidence; largest-font title candidate; arXiv/ISBN; filename hints labelled `filename_hint`, which can suggest but never override stronger evidence.
2. **Online, opt-in, off by default** (application-wide switch; per-library override later): providers behind one `MetadataProvider` protocol (Crossref, OpenAlex, Open Library, arXiv). Explicit result states: `success / no_match / transient_error / rate_limited / unauthorized / offline / provider_unavailable`; "provider could not answer" is never "unresolved". The cache stores positive **and negative** results with TTL, keyed by provider + normalised request + matcher version; provider response cache is separate from local match computation. Rate policy is per provider (concurrency, minimum interval, honour `Retry-After`, jittered retries, a per-run request budget); resolution is resumable and one provider failing never rolls back another. `mailto` is a user setting, never hard-coded; one central network-policy module owns the `KnowledgeVista/x.y` User-Agent, timeouts, retries and offline flag. Opening a file never triggers a lookup. Only a DOI or title leaves the machine, listed to the user beforehand. Raw responses have configurable size and retention; abstracts and reference lists are optional and labelled provider content.
3. **Manual:** a field-level `locked` flag; manual values are still validated and normalised.

Precedence is a *ranking of proposals*, not an automatic override: a corrected Crossref title appears as a candidate beside a typo in the printed one, and the chosen value records why. Conflicts (PDF 2022 vs Crossref 2023, differing titles/authors/pages) are first-class and shown as agreement / discrepancy / unresolved. Confidence is descriptive in the UI (Exact / High / Medium / Low / Ambiguous), never a bare percentage. Provider failure never invalidates accepted metadata; re-resolve produces candidates first.

## Search

One search service shared by CLI, GUI and MCP. User text parses to a **versioned query AST** (`query_language_version`), which compiles to FTS/SQL with parameters only; saved searches store the AST, not result lists. Filters: `author: year: doi: kind: tag: collection:`, with a defined escape/quoting rule; malformed queries return `KV_QUERY_INVALID`, never zero results. FTS stays quoted, prefix-by-explicit-`*`, `a | b`, NEAR; no stemming, no hidden typo correction, chemistry strings (`2,4-DNT`, `Cu(II)`, `β-lactam`, `Al2O3`, CAS numbers, `10.1021/...`) tested and never altered. Ranking ties break deterministically (score, document id, page). Results carry `document_id, artifact_id, source page anchor, pdf_page, printed_label, current_path, extraction version, snippet`; a snippet is a navigation aid, not a quotation. Every "no match" is qualified by coverage (documents searched, image-only pages, partial extractions). Compatibility with the old OpenChem index means *same hit sets*, not same ranks, and the old FTS schema is a bridge with an end date, not an architecture.

## Interfaces

- **CLI is the contract.** One global `--json`; stdout is pure JSON and progress goes to stderr; envelope `{schema_version, command, ok, records, warnings, errors:[{code, message, details}], next_cursor}`; documented exit codes (0 ok, 1 operational failure, 2 invalid arguments/query); stable error codes (`KV_AMBIGUOUS` with candidates in `details`, `KV_FILE_CHANGED`, `KV_FILE_MISSING`, `KV_DESTINATION_EXISTS`, `KV_PERMISSION_DENIED`, `KV_METADATA_UNRESOLVED`, `KV_QUERY_INVALID`, `KV_CURSOR_STALE`).
- Commands: `scan, sync, search, show, explain, doctor (read-only by default, categories: filesystem / catalog / extraction / search / metadata / relationships / operations / integration / cache), verify, resolve, related, dupes, plan, apply, undo, export, capabilities`. No more until a semantic need appears. `capabilities --json` reports protocol version, catalog schema version, JSON schema version, supported commands (each flagged read-only or mutating), URI schemes. These versions are separate numbers.
- **Cursors** encode catalog revision and last stable key; a cursor from before a significant change returns `KV_CURSOR_STALE`. Hard result and size limits everywhere.
- **References:** `knowledgevista://document/<id>`, `.../artifact/<sha256>`, `.../document/<id>/page/<pdf_page>`, `.../document/<id>/label/<printed_label>`; percent-encoding, ID charset and addressing are specified before any consumer uses them; resolved locally, no service. OS protocol registration is deferred. `kv open --document <id> --page <n>` is the open command.
- **MCP (stdio, thin adapter over the same services, read-only):** `search_pages, get_document, get_page, resolve_reference, get_metadata, list_related, list_duplicates, locate_artifact`. Responses carry stable IDs, provenance, `evidence_type = search_navigation`, source status (current path, missing paths, root status, artifact status) and ambiguity. `get_*` take exact IDs only; fuzzy matching lives only in `resolve_reference`. Extracted table text is untrusted; the PDF page is the source of truth, so the MCP serves pages and never asserts "the value is X". Mutations, when they come, are `propose_*` -> plan -> `apply_plan(plan_id)`.
- `kv explain <doc>` is the debugging golden path: identity, artifacts, locations and root status, metadata with provenance and conflicts, candidates, identifiers, relations, extraction state and staleness, annotations, collections, filesystem and operation history, warnings. `kv export` writes portable JSON (metadata, collections, annotations, stable IDs, no caches); import into another library detects ID collisions and requires review. A `knowledgevista` package split: core (stdlib + sqlite), `extract` (pymupdf), `network`, `mcp`, `gui` (PySide6), `reflow`, `ocr` as optional groups.

## Organizer

Virtual organization (collections, tags, saved searches, system views like Inbox/Unresolved/Missing/Duplicates as *saved queries*, not stored state) ships before it and covers most needs. The organizer is an optional presentation layer over the filesystem, never required for any other feature.

`OBSERVE -> PROPOSE -> REVIEW -> PLAN -> PRECHECK -> APPLY -> VERIFY -> HISTORY`.

- **Plan** is a frozen JSON file with its own `plan_id`, `created_at`, `catalog_revision`, `naming_policy` version, schema version and hash. Each item: `item_id, operation (rename | move | rename+move | unchanged), artifact_id, document_id, old_path, new_path, expected_sha256, reason, metadata snapshot used for the name, source, confidence, risk`. Planning is a pure function that may read but never writes the filesystem. A human table view (old / new / why / evidence / risk) and the GUI preview render the same plan.
- **Naming policy** is versioned and deterministic across machines: stable rules for et-al, punctuation, Unicode, truncation with a stable hash suffix, and collision resolution *after* sanitisation (two titles can sanitise to one name); never dependent on directory order. One naming service serves CLI, GUI and organizer.
- **Plan validity:** before apply, verify schema, hash, roots, expected artifact hashes and policy compatibility; a plan older than the data says `stale`; unknown plan versions are refused. Applying a plan twice reports `already_applied`, never an error or a second move. `old == new` is a no-op.
- **Apply is a state machine per item:** `planned -> prechecked -> executing -> succeeded | failed | uncertain`. Precheck snapshots expected vs observed values (source hash, destination state, root identity). Chains (`A->B`, `B->C`) are detected and ordered or staged, never failed by order. A `safe_rename` helper owns case-only and Unicode-only renames (two-step move). Recovery reconciles the journal (intent/history) with the actual filesystem (reality) and never blindly retries an `uncertain` item. Root containment is re-validated immediately before each move.
- **Hard preconditions per item:** source exists; **source hash equals expected, else abort that item**; destination absent or not the same object; parent valid; path length ok; not locked (skipped and reported, never a batch failure); root `allow_organize`.
- **Undo has preconditions too:** if the moved file's hash or the old path's state changed, undo is refused (`unsafe`/`uncertain`) with an explanation and a manual recovery path; it never overwrites user work. Undo undoes the last explicit filesystem operation; scans and metadata edits have their own history and never enter it. History records an actor (`scanner / organizer / user / migration / resolver / import`) and the app and operation-schema version.
- **Windows test matrix:** long and `\\?\` paths, reserved names, trailing dots/spaces, Unicode normalisation, case-only renames, targets differing only by case, read-only/hidden, junctions/symlinks/reparse points, UNC roots, `&` quotes parentheses, locked PDFs, antivirus races. File Converter's `sanitize_stem` is a starting point.
- A rename never changes document identity, collection membership, tags, notes or relations. Never deletes; duplicate removal is a separate Recycle-Bin action. No auto-apply policy in v1.
- **Hard-link browse folder: deferred out of the roadmap** until a concrete workflow needs it. A hard link is the *same file*, so editing it in place edits the original; it removes rename/move risk, not write-through risk. If built, it is labelled "hard-link mirror: edits affect the original", NTFS same-volume only.

## OpenChem integration (order is a safety rule)

1. KV resolves by sha256 and by document ID.
2. **OpenChem PR A:** `index_literature.py --check` and `library_index.py` fall back to a KV lookup by sha256 (via `kv resolve --json`) when the recorded filename is gone, and print the current name. A *mock KV CLI* in OpenChem's tests covers success, ambiguous, missing, malformed JSON, unsupported protocol version, timeout, executable missing and non-zero exit, so CI never needs the real library. KV absent or its root offline degrades gracefully; that is an acceptance test.
3. **OpenChem PR B:** tool path setting beside Vina/ORCA; "Open in Knowledge Vista" at a page; `literature.toml` / `sources.toml` entries gain `kv_document_id` beside the existing `sha256`. Hierarchy: `kv_document_id` for "this paper is a source", `artifact_sha256` for "this exact held PDF was verified", `file` as display/legacy locator only. A one-off script labelled a **migration of legacy locator fields** rewrites existing `file` names from the organizer journal.
4. Only then may the organizer touch the real folder.
5. `kv openchem check` verifies every referenced sha256 resolves. KV -> OpenChem ("open this dataset") is later. KV does not become "OpenChem 2": no editing, calculation or chemistry logic; a chemistry *profile* (property/role vocabulary, CAS/formula) is versioned separately and kept out of the core schema.

## Milestones and acceptance (each answers: what can go wrong, how is it detected, how does it recover, what proves it)

| # | Deliverable | Done when |
|---|---|---|
| 0 | Bootstrap: git + GitHub repo, `pyproject.toml` (uv), dependency groups, migration framework, pytest harness, generated-fixture builder, safe-repo CI check (no PDFs over a size, no local paths/DBs), licence inventory over the *lockfile* (transitive), invariants + safety-model docs, final name check, fill the placeholder `BASIC_INSTRUCTIONS.md` | clean-clone `python -m pytest` green; core install pulls no noncommercial dependency; a backup taken while the WAL is active reopens independently with all data; a simulated crash mid-migration leaves a recoverable old DB and a backup |
| 1 | Identity substrate: roots, locations, artifacts, documents, reconciliation, `scan / stats / explain / doctor / verify`, JSON envelope, error codes, exit codes | same bytes at a new path = one artifact, one document; external rename/move keeps IDs and ends the old location; same path with new bytes = new artifact, history kept, metadata not inherited; same size + same mtime + different bytes is caught by `verify` (test shows why mtime is not proof); unavailable root deletes nothing and recovers on return; kill during scan then reopen is consistent; read-only commands leave source bytes unchanged (asserted by hash). Baseline timings recorded as distributions (median, p95, slowest, MB/s) on the real corpus |
| 2 | Extraction + FTS keyed by `page_id`; page labels; worker-process extraction with the Job Object spike; versioned extraction profiles; importer of the OpenChem index as an *imported derivative* (path becomes a historical locator, verified against real bytes before it counts) | counts 738 / 57,721 and scanned flags and DOIs match the existing index ("corpus baseline 2026-10-06"); fixed query set (exact, prefix, OR, NEAR, Unicode, chemistry punctuation, **plus negative cases**) returns the same hit sets; extractor version bump marks extractions stale and re-extracts, unchanged version does nothing; rebuilt extraction changes page ids but the artifact+page anchor still resolves; partial extraction is reported as partial; a front-matter fixture separates `pdf_page` from `printed_label`; malformed/huge/decompression-heavy fixtures fail safely; deleting the extraction cache loses no catalog state |
| 3 | Metadata providers, candidates, evidence, review queue, DOI normalisation and candidates, negative caching, resumable resolution, run IDs | canonical-DOI coverage above 66% (defined as accepted DOI, not any candidate); every PDF has a title or a stated reason; ~30 hand-checked matches with 0 wrong titles; false-positive fixtures pass (DOI only in references, wrong year, wrong author, preprint vs VoR, similar editions, foreign DOI in body, OCR garbage); re-running `resolve` changes nothing; a provider outage does not alter accepted metadata |
| 4 | Relations + virtual organization: artifact vs document relations, four-level duplicates, supplement and book-chapter proposals, collections, tags, saved searches, system views | fixtures separate *two artifacts of one document*, *two documents `version_of`*, *two documents `supplement_of`*, *artifact `derivative_of` artifact*; the 12 hash groups and 7 DOI groups are found and classed; nothing merges without acceptance; split/merge keeps artifact IDs and history |
| 5 | Stable CLI/API, read-only MCP, `knowledgevista://`, `capabilities`, cursors; OpenChem PRs A and B | deterministic JSON; ambiguity surfaced; mock clients test supported, old and unknown protocol versions; renamed file resolves; OpenChem `--check` stays at 0 problems after renaming a held file; bidirectional check: OpenChem reference -> KV document, path changed -> both still resolve, KV unavailable -> OpenChem degrades |
| 6 | Reversible organizer | on a scratch **copy**: apply then undo gives a byte-identical tree (hashed automatically); interrupted apply, `uncertain` item, source changed, destination appeared, locked file, case-only rename, long/reserved names, move chains, stale plan, repeated apply, and undo after the moved file was edited (must refuse, not overwrite) all pass |
| 7 | PySide6 library window (search, table, detail with a "why this value?" evidence view and origin badges, review queue, collections; Inbox-first sorting: unresolved, ambiguous, missing, new, then alphabetical); health vs inventory stats; own JobManager-style job layer, no ad-hoc threads | driven by an in-app script with a verdict (the `drive_template/` pattern): add root, scan, search, show evidence, propose rename, open reader, restart, state intact; also an external CLI mutation while the GUI is open refreshes via `catalog_revision`; all external strings rendered as text, never markup |
| 8 | Reader: QtPdf, lazy rendering for 900+ page books, per-page render failure isolation, search-hit navigation, annotations with rotation-normalised geometry, per-artifact reading position, printed-label jump, outline | a large-book test does search -> click result -> render -> annotate -> scroll together; annotations survive restart, move and a byte-identical copy; source PDF bytes unchanged after reading and annotating; rotated-page geometry test |
| 9 | Extras, each only when wanted: optional reflow (labelled "reflowed view", with "open original page"), EPUB, OCR as a derivative that never replaces original text, BibTeX/CSL-JSON export (missing fields stay missing, never invented), KV -> OpenChem | per feature; cache invalidation test: a changed source artifact marks every derivative stale |

**Test strategy:** the real corpus is never in the repo. A minimal *private* manifest (hashes, page counts, scan state, DOIs, duplicate groups, a curated sample set: easy, ambiguous, no DOI, old, book, scan, preprint) lives outside the public repo, with a check that the manifest still matches the corpus (so a stale oracle is noticed). Public fixtures are tiny, generated in code, synthetic titles/authors/DOI-like strings (including a *same-paper, different-bytes* pair that is genuinely the same synthetic publication), covering native text, scan, hybrid, malformed, very long, non-Latin names, DOI placements, split titles, rotation, labels, tables. Pure normalisers (DOI, title, path, filename, kind, URI) get tiny tests, with property tests where cheap (`f(f(x)) == f(x)`, URI round-trip). Failure-mode tests count as much as happy paths.

## What KV is not

Not a cloud store, publisher downloader, bibliography authority, AI truth engine, backup system, filesystem replacement or automatic organizer; no telemetry; no AI in core (any later AI output is a versioned derivative and a proposal, never metadata authority; network access does not imply permission to send document text). The README leads with the user value ("point it at your mess of PDFs; search by contents; identify what they are; nothing moves until you approve") and the no-move workflow, shows OpenChem as an optional integration, and states the privacy and "you are responsible for the rights to your files" position.

## Deferred

Ideas that are intentionally not built yet, each with the trigger that reopens it, are in [design-notes.md](design-notes.md).
