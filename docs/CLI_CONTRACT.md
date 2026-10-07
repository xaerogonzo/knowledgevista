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
| `KV_METADATA_LOCKED` | the field's accepted value is locked; unlock it first (`kv metadata unlock`) |
| `KV_NETWORK_DISABLED` | an online lookup was asked for but online lookups are switched off; the message says how to switch them on |
| `KV_QUERY_INVALID` | a search query could not be parsed (search milestone) |
| `KV_CURSOR_STALE` | a pagination cursor predates a significant catalog change |
| `KV_ROOT_UNAVAILABLE` | a root cannot be read right now; nothing was changed |
| `KV_ROOT_OVERLAP` | a root would overlap an existing one |
| `KV_CATALOG_TOO_NEW` | the catalog was written by a newer Knowledge Vista and was left untouched |
| `KV_CATALOG_MISSING` | there is no catalog at the given path |
| `KV_DOCTOR_FOUND_PROBLEMS` | `doctor` found catalog errors |
| `KV_DEPENDENCY_MISSING` | an optional group is not installed (for example `extract`); the message names the install command |
| `KV_NOT_EXTRACTED` | the document exists but its text has not been extracted yet. Distinct from `KV_NOT_FOUND`: "could not look" is not "none exist" |
| `KV_EXTRACTION_FAILED` | `extract` could not read any document it tried |
| `KV_NOTHING_SEARCHABLE` | a search ran over zero searchable documents, so "no hits" would mean nothing |
| `KV_INTERNAL` | an unexpected failure (a bug); the message names the exception |

Warning and finding codes (`KV_ROOT_VOLUME_CHANGED`, `KV_MASS_MISSING`, `KV_UNREADABLE_DIRECTORY`,
`KV_LINKS_SKIPPED`, `KV_PATH_KEY_COLLISION`, `KVD_*`) are advisory; they never change `ok`.

## Commands

Each command is one of: **read** (leaves source files and the catalog unchanged), **catalog** (writes the catalog
only), **cache** (writes only the rebuildable extraction store, never the catalog), or **filesystem** (touches the
user's files; none exist yet). Source files are never written by any command in this milestone.

| Command | Kind | Does |
|---|---|---|
| `root add <path> [--label L] [--allow-organize]` | catalog | register a folder; refuses an overlapping root |
| `root list` | read | list roots and their status |
| `scan [--root R] [--full]` | catalog | walk, hash what changed, reconcile moves/replacements/absences. `--full` re-reads every file |
| `stats` | read | inventory and health, kept apart |
| `explain <reference>` | read | everything known about one document: identity, locations and their history, warnings |
| `doctor` | read | structural health; **diagnoses, never repairs**; reads no file contents |
| `verify [--root R]` | read | re-read every active file and check its bytes; the slow, expensive check |
| `extract [--root R] [--force] [--retry-failed] [--rebuild-imported] [--limit N]` | cache | read PDF text into the extraction store, in a bounded worker process. Needs the `extract` group. Re-extracts only what is new or stale |
| `search <query> [--near T] [--within N] [--also T] [--file S] [--limit N]` | read | find pages by their words; every result carries a `coverage` statement of what could not be searched |
| `show <reference> (--pdf-page N or --label L)` | read | one extracted page. Physical position and printed label are different parameters, never one ambiguous "page" |
| `import openchem-index <path>` | cache | use an OpenChem `<library>.index.sqlite` as provisional search text for documents whose hash the catalog already holds |
| `resolve [--online] [--list-requests] [--accept-safe] [--document D] [--root R] [--limit N] [--max-requests N] [--no-front] [--refresh-front]` | catalog | propose DOIs, titles, authors, ISBNs and arXiv ids from each document's text and file metadata, and with `--online` a provider's record of it. **Proposes; accepts nothing** unless `--accept-safe` (the named rule `safe_batch_v1`). Rerunning with nothing new changes nothing. `--online` needs `online_lookup` switched on and sends only a DOI or a title; `--list-requests` shows what that would be and sends nothing |
| `review list [--field F] [--review safe\|required] [--status S] [--limit N] [--offset N]` | read | the proposals waiting for a person, agreeing sources grouped as one item, most urgent first |
| `review accept <id>... \| --safe` | catalog | accept proposals by id (8+ hex characters), or apply the safe batch rule to all documents |
| `review reject <id>...` | catalog | decide a proposal is wrong; it is kept and not proposed again |
| `metadata set <reference> <field> <value> [--no-lock]` | catalog | state a value by hand: validated, normalised, recorded as `assigned` and locked |
| `metadata clear <reference> <field>` | catalog | back to unknown; the proposals that produced the value are rejected so no rule restores it |
| `metadata lock <reference> <field>` | catalog | protect a value from every resolver and batch rule |
| `metadata unlock <reference> <field>` | catalog | allow a different value to be accepted again |
| `config list` | read | every setting, its value and its default |
| `config get <key>` | read | one setting |
| `config set <key> <value>` | config | change a setting (`online_lookup`, `mailto`, `request_budget`); writes only the settings file |

`<reference>` is a document id, an artifact SHA-256 (or a unique prefix of 8+ hex characters), or a path / file
name. A name that matches more than one document is `KV_AMBIGUOUS`; a name that only matches a *past* location is
answered from history with a warning.

## Metadata commands

`kv resolve` writes **proposals** (`metadata_candidate`); an **accepted value** (`metadata_value`) exists only after
`review accept`, `resolve --accept-safe` (recorded as `rule:safe_batch_v1`) or `metadata set`. Unknown is the absence of a
value, never an empty string. Every accepted value carries its origin (`observed`, `resolved`, `inferred`, `assigned`),
its source, who accepted it, and a lock.

`resolve` summary record (`type: "summary"`) fields: `run_id`, `documents`, `not_extracted`, `local` (proposal counts by
outcome: `new`, `updated`, `unchanged`, `stale`, `decided`), `doi_classes` (`own`, `ambiguous`, `none`), `front`, `accepted`
(by field), `online` (`enabled`, `requests`, `cache_hits`, `states`, `verdicts`, `stopped`), `skipped_by_rule`, `problems`.
`complete` is `false` when `--online` stopped early (a rate limit, an outage, the request budget); the run then resumes from
the cache. A provider that cannot answer is a **state** (`rate_limited`, `offline`, ...), never "no match", and never
changes an accepted value.

Warning codes: `KV_PROVIDER_STOPPED`, `KV_SAFE_RULE_SKIPPED`, `KV_RESOLVE_PROBLEM`, `KV_FRONT_MATTER_UNAVAILABLE`,
`KV_SETTINGS_IGNORED`.

### Settings

| Key | Default | Meaning |
|---|---|---|
| `online_lookup` | `false` | allow `kv resolve --online` to send DOIs and titles to a metadata provider |
| `mailto` | none | the user's own contact address, sent to the provider with requests (never hard-coded) |
| `request_budget` | `1000` | the most requests one online run may make, retries included |

A damaged settings file yields the defaults (offline) and a `KV_SETTINGS_IGNORED` warning; it can never switch the network on.
