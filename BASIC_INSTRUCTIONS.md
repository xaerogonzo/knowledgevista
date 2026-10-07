# Knowledge Vista — Basic Instructions

@project-baseline.md

---

## Project Overview

**Name:** Knowledge Vista (package `knowledgevista`, command `kv`)
**Stack:** Python 3.11+, SQLite (stdlib) for the catalog and FTS5 index, PyMuPDF for PDF parsing (optional `extract` group), PySide6 for the GUI and reader (optional `gui` group, later milestones), uv for dependencies, pytest.
**Entry point:** `uv run kv` (or `uv run python -m knowledgevista`)
**Purpose:** A local-first, public (AGPL-3.0-or-later) library manager, search index and reader for messy document folders; identifies documents by content hash, keeps metadata with provenance, and serves a page-level evidence layer to the CLI, a read-only MCP server and OpenChem Studio.

---

## Project Structure

- `src/knowledgevista/` — the package (src layout). `paths.py` is the only module that decides where app data lives; `db/` holds connections and forward-only SQL migrations (`db/schema/NNNN_name.sql`); `cli.py` is a thin adapter over `services/` (scan, roots, resolve, explain, stats, doctor, verify); `domain/` is pure (ids, path keys, kind detection); `errors.py` and `envelope.py` are the machine contract. Extraction, metadata, MCP and GUI arrive with later milestones (see `docs/ARCHITECTURE.md`).
- `tests/` — pytest suite. `pdfbuilders.py` generates every PDF fixture in code from synthetic text; `test_pdfbuilders.py` proves each fixture is what its name claims.
- `tools/` — `check_repo_safe.py` (nothing a public repo must not carry) and `license_inventory.py` (licences over the lockfile's transitive closure). Both run in CI and are themselves tested.
- `docs/` — architecture and policy documents; `docs/gotchas/` is delivered by TokenSave Manager and is not edited here.
- `drive_template/`, `build.ps1`, `build.bat` — TokenSave Manager scaffolding (a Tk live-driver template and a Nuitka build template with unfilled placeholders). Not wired up; packaging is decided when the GUI exists.
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
- `src/knowledgevista/services/scan.py` — the reconciliation algorithm (its docstring is the spec: probe, walk, fast path, hash, replace/move, absence). Read it before changing scan behaviour.
- `src/knowledgevista/db/schema/0002_identity.sql` — the identity schema; its constraints (one current location per path, one document per artifact, no active location without an artifact) are tested.
- `tests/support.py` — `make_env` builds a library on disk with a catalog; `Env.snapshot()` compares catalogs without random IDs.
- `tests/pdfbuilders.py` — the fixture builders; `10.5555` is Crossref's reserved test DOI prefix.
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
- **Backups never copy the file.** The catalog is WAL; use `sqlite3.Connection.backup`.
- **Writing patch scripts:** do not rely on shell heredocs for code containing backslashes or adjacent quotes (they get mangled); use the Edit or Write tools.
