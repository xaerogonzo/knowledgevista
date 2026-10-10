# Knowledge Vista — Basic Instructions

@project-baseline.md

---

## Project Overview

**Name:** Knowledge Vista (package `knowledgevista`, command `kv`)
**Stack:** Python 3.11+, SQLite (stdlib) for the catalog and FTS5 index, PyMuPDF for PDF parsing (optional `extract` group), PySide6 for the window (optional `gui` group; the reader is milestone 8), uv for dependencies, pytest.
**Entry point:** `uv run kv` (or `uv run python -m knowledgevista`)
**Purpose:** A local-first, public (AGPL-3.0-or-later) library manager, search index and reader for messy document folders; identifies documents by content hash, keeps metadata with provenance, and serves a page-level evidence layer to the CLI, a read-only MCP server and OpenChem Studio.

---

## Project Structure

- `src/knowledgevista/` — the package (src layout). `paths.py` is the only module that decides where app data lives; `db/` holds connections and forward-only SQL migrations (`db/schema/NNNN_name.sql`); `cli.py` is a thin adapter over `services/` (scan, roots, resolve, explain, stats, doctor, verify, extract, search, pages); `domain/` is pure (ids, path keys, kind detection, text logic); `extract/` is the PDF worker process, its supervisor and the memory-limit mechanism; `index/` is the rebuildable extraction store (a cache SQLite file, never the catalog), the search query and the OpenChem-index importer; `errors.py` and `envelope.py` are the machine contract. `network/` is the only code that opens a socket (policy, pacing, retries, the offline switch) and `providers/` turns a DOI or title into a normalised work (Crossref now); `domain/` also holds the metadata logic (`doi_evidence`, `local_evidence`, `match`, `frontmatter`, `titles`, `fields`); `services/resolve_metadata.py` is `kv resolve`, `review.py` the queue and the one batch rule, `metadata.py` the proposal/value/history store. `services/relations.py` is the relation/proposal store and merge/split, `relate.py` the detectors behind `kv relate`, `organize.py` collections, tags, saved searches and views (`domain/relation_evidence.py` is their pure evidence); `cli_support.py` holds what every command module shares and `cli_relations.py` the relation and organisation commands. `cli_integration.py` holds `capabilities`, `locate`, `open` and `mcp`; `services/locate.py` answers "where is it now", `services/capabilities.py` is the one table of which commands only read, `services/cursor.py` the pagination cursors, `domain/reference.py` the `knowledgevista://` grammar, and `mcp/` the read-only Model Context Protocol server (no dependency). `cli_organize.py` is the organizer's commands; `domain/naming.py` (the versioned naming policy), `domain/plan.py` (the frozen, hashed plan file), `services/organizer_plan.py` (planning: reads, writes nothing), `services/organizer_fs.py` (the filesystem primitives: each one a refusal) and `services/organizer.py` (apply, undo, recover and the journal) are the only code that moves a user's file. `library_view.py` (services) is the read model the window shows (list, detail, "why this value?", review items, search) and `domain/evidence_view.py` / `health_view.py` its pure text; `gui/` is the optional PySide6 window: `jobs.py` (the only place work leaves the interface thread: a write lane of ONE thread and a read lane, latest-request-wins channels), `work.py` (what each job does, one service call each), `window.py`, `models.py`, `detail.py`, `dialogs.py`, `state.py`, `libraries.py` (the catalogs the window has opened and the one opened last; File > Open/New/Manage libraries, the launch choice, and what `kv gui` opens with no `--catalog`; `services/catalog_copy.py` is the read-only, never-overwriting catalog copy behind Move catalog), `text.py` (outside text is shown as text; `audit()` checks a live window), `shell.py` (open/reveal seam) and `drive.py` + `drive_ledger.py` (the in-app scripted run; the ledger is `drive_template/`'s, byte for byte). `services/opener.py` is `kv open`'s launch logic, shared. See `docs/GUI.md`.
- `tests/` — pytest suite. `pdfbuilders.py` generates every PDF fixture in code from synthetic text; `test_pdfbuilders.py` proves each fixture is what its name claims.
- `tools/` — `check_repo_safe.py` (nothing a public repo must not carry) and `license_inventory.py` (licences over the lockfile's transitive closure). Both run in CI and are themselves tested.
- `docs/` — architecture and policy documents; `docs/gotchas/` is delivered by TokenSave Manager and is not edited here.
- `drive_template/` — TokenSave Manager's live-driver template; `drive_ledger.py` is used by `gui/drive_ledger.py` byte for byte (a test keeps it so) and the Tk skeleton is only reference. `drive/` holds the committed driver scripts (`library_tour.json` is the milestone-7 acceptance run). `build.ps1`, `build.bat` — a Nuitka build template with unfilled placeholders; packaging is decided when the reader exists.
- `.tokensave/` — code-graph index, local only.

---

## Documentation Files

| File | Location | Purpose |
|---|---|---|
| README.md | `/README.md` | What it is, principles, development, licence |
| INVARIANTS.md | `/docs/INVARIANTS.md` | The rules everything is checked against |
| SAFETY_MODEL.md | `/docs/SAFETY_MODEL.md` | What each class of operation may touch |
| CLI_CONTRACT.md | `/docs/CLI_CONTRACT.md` | The JSON envelope, exit codes, stable error codes and commands; a test keeps it in sync with the code |
| ARCHITECTURE.md | `/docs/ARCHITECTURE.md` | Layering, object model, algorithms, milestones and their acceptance tests |
| design-notes.md | `/docs/design-notes.md` | Deferred ideas with reopen triggers; open questions to settle by measurement |
| CHANGELOG.md | `/CHANGELOG.md` | Changes |
| LICENSE | `/LICENSE` | AGPL-3.0-or-later |

---

## Architecture

```
CLI / MCP / GUI  ->  services  ->  pure domain  ->  boundaries (SQLite, filesystem, network, worker processes)
```

Object model: Library > Root > Location > Artifact (sha256) > Extraction > Page; Document (stable UUID) linked to Artifacts; relations are split into artifact-level and document-level; metadata values carry origin, status, evidence and a lock; candidates are proposals. See `docs/ARCHITECTURE.md`.

---

## Key Files

- `src/knowledgevista/db/migrations.py` — the migration policy (forward only, atomic per step, backup via the SQLite backup API, newer schema refused). Read its docstring before touching the schema.
- `src/knowledgevista/db/connection.py` — how every connection is opened (foreign keys ON, WAL, busy timeout).
- `src/knowledgevista/extract/client.py` — the supervisor: `ExtractionSession.extract(path)` ALWAYS returns (a hang, crash, memory kill or garbage becomes a failed page, and extraction resumes at the next page). Its docstring is the failure policy. `tests/fakeworker.py` is a scriptable real process used to test it.
- `docs/SEARCH.md` — what search means (quoting, prefix, no stemming or transliteration, coverage). Changing search semantics means changing this file and its query-set tests together.
- `src/knowledgevista/services/scan.py` — the reconciliation algorithm (its docstring is the spec: probe, walk, fast path, hash, replace/move, absence). Read it before changing scan behaviour.
- `src/knowledgevista/db/schema/0002_identity.sql` — the identity schema; its constraints (one current location per path, one document per artifact, no active location without an artifact) are tested.
- `tests/support.py` — `make_env` builds a library on disk with a catalog; `Env.snapshot()` compares catalogs without random IDs.
- `tests/pdfbuilders.py` — the fixture builders; `10.5555` is Crossref's reserved test DOI prefix.
- `docs/METADATA.md` — what a proposal is, how a printed DOI is judged to be a document's own, the safe rule and the privacy rules for the network. Read it before changing any score, threshold or provider behaviour.
- `src/knowledgevista/domain/doi_evidence.py`, `local_evidence.py`, `match.py` — the scores and thresholds; each was chosen by looking at the real library and a provider's answers, and `MATCHER_VERSION` changes with them.
- `src/knowledgevista/network/policy.py` — pacing, retries, `Retry-After`, the budget and the offline switch. No other module may open a socket.
- `tools/check_repo_safe.py`, `tools/license_inventory.py` — the two CI guards.

---

## Project-Specific Rules

- **Invariants first.** `docs/INVARIANTS.md` is the contract. Filenames are locators, never identity; uncertainty never silently becomes a stored fact; a missing file or unavailable root never deletes catalog state; nothing in a user's library is ever deleted.
- **Run tests with `uv run python -m pytest`**, not the bare `pytest` script (the tests import sibling helper modules).
- **A test must be seen failing.** For any guard or safety test, plant the fault it names (a mutation) and confirm it fails; a first-run green means little. Assert the oracle is alive before trusting a negative result (e.g. the WAL test first shows a naive file copy really loses data). See `docs/gotchas/tests-that-pass-without-testing.md`.
- **Never use a bare `""` for "unknown".** Use NULL plus a status; see `docs/gotchas/empty-is-not-unknown.md`. A result that could be partial is never serialised as a bare array.
- **No corpus, ever.** Fixtures are generated in code from invented text with the reserved DOI prefix. `.gitignore` ignores `*.pdf`, local catalogs and caches, and `tools/check_repo_safe.py` enforces it; do not `git add -f` around either. Maintainer-only strings that must never be committed go in the `KV_SAFE_REPO_FORBID` environment variable, not in a file.
- **License discipline.** The default install must contain no noncommercial or unknown-licence dependency. `pymupdf4llm` requires `pymupdf_layout` (Polyform Noncommercial): the `reflow` extra stays empty until that is resolved. Run `uv run python tools/license_inventory.py --extras extract` after any dependency change.
- **Identity follows the bytes; absence is never deletion.** A scan never deletes a location, artifact or document: a vanished file is `missing`, an unlistable directory is `inaccessible`, an unavailable root changes only its status, and ended locations stay as history. `size + mtime` is a freshness hint only; `kv verify` and `scan --full` read the bytes.
- **Mutation-test scanner changes.** `tests/test_scan.py` is the acceptance suite; after changing `services/scan.py`, plant faults (drop the move pairing, ignore `--full`, treat an unlistable directory as empty) and confirm a test goes red.
- **Extracted text is derived, keyed by hash, and never trusted over the PDF.** The extraction store lives in the cache and is deleted-and-rebuilt on any schema mismatch; extraction refuses a file whose size/mtime differs from the scan (so text is never filed under the wrong bytes); `page_id` is internal and changes on re-extraction, so cite `artifact_id + pdf_page`. `pdf_page` (1-based position) and `printed_label` (a string) are never one parameter.
- **A search must say what it could not see.** Never return a bare empty success: results carry coverage, and searching zero searchable documents is `KV_NOTHING_SEARCHABLE`.
- **A proposal is not a fact.** `kv resolve` writes `metadata_candidate`; only a person, `kv metadata set`, or the named rule `safe_batch_v1` writes `metadata_value`. Unknown is no row. A provider that cannot answer is a state, never "no match", never cached, and never changes an accepted value.
- **Only a DOI or a title leaves the machine**, and only when `online_lookup` is on AND `--online` is given. Tests prove "off" by making the transport fail the test if it is touched.
- **A chapter never inherits its parent's DOI.** A DOI printed as "own" by three or more documents is a parent work (measured: 160 encyclopedia entries share one).
- **Backups never copy the file.** The catalog is WAL; use `sqlite3.Connection.backup`.
- **Writing patch scripts:** do not rely on shell heredocs for code containing backslashes or adjacent quotes (they get mangled); use the Edit or Write tools.
