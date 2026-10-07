# CLI contract

The `kv` command is the integration contract for scripts, OpenChem Studio and coding assistants. This page is the
contract; the help text is not. A change that breaks anything here is an API break and bumps `schema_version`.

Versions are separate numbers and evolve for different reasons: the **JSON envelope** version (this page), the
**catalog schema** version (SQLite, internal), and later the **integration protocol** and **MCP** versions
(`kv capabilities`, milestone 5). Nobody should read the SQLite schema as an API.

## Output

With `--json` (before or after the command), **stdout is exactly one JSON document and nothing else.** Progress and
diagnostics go to **stderr**. JSON is ASCII-escaped, so it survives any console encoding.

```json
{
  "schema_version": 1,
  "command": "scan",
  "ok": true,
  "complete": true,
  "records": [],
  "warnings": [{"code": "KV_UNREADABLE_DIRECTORY", "message": "...", "details": {}}],
  "errors": [{"code": "KV_ROOT_UNAVAILABLE", "message": "...", "details": {}}],
  "next_cursor": null
}
```

- `records` is always an array. `complete: false` would mean a partial result; an empty `records` with
  `complete: true` means *none exist*. They are never conflated.
- `errors` is empty exactly when `ok` is true. Branch on `code`, never on `message`.
- Without `--json`, output is for people and may change freely.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | an expected operational failure: the command ran and the answer is "no" (not found, ambiguous, root unavailable, files changed) |
| 2 | invalid arguments or query |

Argument errors under `--json` are envelopes too (`KV_INVALID_ARGUMENTS`, exit 2), never a usage dump.

## Error codes (stable)

| Code | Meaning |
|---|---|
| `KV_INVALID_ARGUMENTS` | the command line was not valid |
| `KV_NOT_FOUND` | nothing matches the reference |
| `KV_AMBIGUOUS` | more than one candidate; `details.candidates` lists every one. Never guessed |
| `KV_FILE_CHANGED` | a file's bytes no longer match what the catalog recorded |
| `KV_FILE_MISSING` | a file the catalog says is present is gone |
| `KV_DESTINATION_EXISTS` | a plan's destination already exists (organizer) |
| `KV_PERMISSION_DENIED` | the OS refused access |
| `KV_METADATA_UNRESOLVED` | no metadata could be established (metadata milestone) |
| `KV_QUERY_INVALID` | a search query could not be parsed (search milestone) |
| `KV_CURSOR_STALE` | a pagination cursor predates a significant catalog change |
| `KV_ROOT_UNAVAILABLE` | a root cannot be read right now; nothing was changed |
| `KV_ROOT_OVERLAP` | a root would overlap an existing one |
| `KV_CATALOG_TOO_NEW` | the catalog was written by a newer Knowledge Vista and was left untouched |
| `KV_CATALOG_MISSING` | there is no catalog at the given path |
| `KV_DOCTOR_FOUND_PROBLEMS` | `doctor` found catalog errors |
| `KV_INTERNAL` | an unexpected failure (a bug); the message names the exception |

Warning and finding codes (`KV_ROOT_VOLUME_CHANGED`, `KV_MASS_MISSING`, `KV_UNREADABLE_DIRECTORY`,
`KV_LINKS_SKIPPED`, `KV_PATH_KEY_COLLISION`, `KVD_*`) are advisory; they never change `ok`.

## Commands

Each command is one of: **read** (leaves source files and the catalog unchanged), **catalog** (writes the catalog
only), or **filesystem** (touches the user's files; none exist yet). Source files are never written by any command
in this milestone.

| Command | Kind | Does |
|---|---|---|
| `root add <path> [--label L] [--allow-organize]` | catalog | register a folder; refuses an overlapping root |
| `root list` | read | list roots and their status |
| `scan [--root R] [--full]` | catalog | walk, hash what changed, reconcile moves/replacements/absences. `--full` re-reads every file |
| `stats` | read | inventory and health, kept apart |
| `explain <reference>` | read | everything known about one document: identity, locations and their history, warnings |
| `doctor` | read | structural health; **diagnoses, never repairs**; reads no file contents |
| `verify [--root R]` | read | re-read every active file and check its bytes; the slow, expensive check |

`<reference>` is a document id, an artifact SHA-256 (or a unique prefix of 8+ hex characters), or a path / file
name. A name that matches more than one document is `KV_AMBIGUOUS`; a name that only matches a *past* location is
answered from history with a warning.
