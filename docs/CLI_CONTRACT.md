# CLI contract

The `kv` command is the integration contract for scripts, OpenChem Studio and coding assistants. This page is the
contract; the help text is not. A change that breaks anything here is an API break and bumps `schema_version`.

Versions are separate numbers and evolve for different reasons: the **JSON envelope** version (this page), the
**catalog schema** version (SQLite, internal), and later the **integration protocol** and **MCP** versions
(`kv capabilities`; see [INTEGRATION.md](INTEGRATION.md) and [MCP.md](MCP.md)). Nobody should read the SQLite schema as an API.

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
| `KV_CURSOR_STALE` | a pagination cursor predates a catalog change, or the listing it pointed into is no longer in the same order; run the command again without `--cursor` |
| `KV_ROOT_UNAVAILABLE` | a root cannot be read right now; nothing was changed |
| `KV_ROOT_OVERLAP` | a root would overlap an existing one |
| `KV_ROOT_NOT_ORGANIZABLE` | the root does not allow the organizer to move its files (the default); a person sets it per root: `kv root allow-organize` |
| `KV_PLAN_INVALID` | a plan file is not usable: not JSON, a format this program does not know, edited after it was made (hash mismatch), a rule broken by an item, or not made by this catalog. Exit 2 |
| `KV_PLAN_STALE` | the library no longer matches the plan (a file moved, the accepted metadata or its naming policy changed, a document merged). The whole plan is refused and nothing was moved; make a new one. `details.items` lists why |
| `KV_PATH_UNSAFE` | an organizer move was refused for its path: outside the root, through a link (symlink or junction), a name Windows forbids, too long |
| `KV_OPERATION_UNFINISHED` | an earlier apply or undo never finished; run `kv recover` before anything else moves a file |
| `KV_INTERRUPTED` | an item of an interrupted operation: it was recorded but did not happen (the file is where it started). Appears on items, never as a command's error |
| `KV_UNCERTAIN` | an organizer move that cannot be confirmed either way. It is reported with what was seen and never retried |
| `KV_CATALOG_TOO_NEW` | the catalog was written by a newer Knowledge Vista and was left untouched |
| `KV_CATALOG_OUTDATED` | the catalog is from an older program and a read-only caller (`kv mcp`) will not upgrade it; any other `kv` command does, with a backup |
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
only), **cache** (writes only the rebuildable extraction store, never the catalog), **config** (writes only the settings
file) or **filesystem** (moves the user's files: only `apply`, `undo` and `recover`). `kv capabilities` flags every command, so
a caller can refuse to run the ones that are not read-only.

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
| `search <query> [--near T] [--within N] [--also T] [--file S] [--limit N] [--cursor C] [--save NAME [--replace]]` | read | find pages by their words; every result carries a `coverage` statement of what could not be searched |
| `show <reference> (--pdf-page N or --label L)` | read | one extracted page. Physical position and printed label are different parameters, never one ambiguous "page" |
| `import openchem-index <path>` | cache | use an OpenChem `<library>.index.sqlite` as provisional search text for documents whose hash the catalog already holds |
| `resolve [--online] [--list-requests] [--accept-safe] [--document D] [--root R] [--limit N] [--max-requests N] [--no-front] [--refresh-front]` | catalog | propose DOIs, titles, authors, ISBNs and arXiv ids from each document's text and file metadata, and with `--online` a provider's record of it. **Proposes; accepts nothing** unless `--accept-safe` (the named rule `safe_batch_v1`). Rerunning with nothing new changes nothing. `--online` needs `online_lookup` switched on and sends only a DOI or a title; `--list-requests` shows what that would be and sends nothing |
| `review list [--field F] [--review safe\|required] [--status S] [--limit N] [--cursor C]` | read | the proposals waiting for a person, agreeing sources grouped as one item, most urgent first |
| `review accept <id>... \| --safe` | catalog | accept proposals by id (8+ hex characters), or apply the safe batch rule to all documents |
| `review reject <id>...` | catalog | decide a proposal is wrong; it is kept and not proposed again |
| `metadata set <reference> <field> <value> [--no-lock]` | catalog | state a value by hand: validated, normalised, recorded as `assigned` and locked |
| `metadata clear <reference> <field>` | catalog | back to unknown; the proposals that produced the value are rejected so no rule restores it |
| `metadata lock <reference> <field>` | catalog | protect a value from every resolver and batch rule |
| `metadata unlock <reference> <field>` | catalog | allow a different value to be accepted again |
| `config list` | read | every setting, its value and its default |
| `config get <key>` | read | one setting |
| `config set <key> <value>` | config | change a setting (`online_lookup`, `mailto`, `request_budget`); writes only the settings file |
| `relate` | catalog | look across the whole library for the same publication twice, supplements, chapters of a book and versions, and PROPOSE each with its evidence. Relates and merges nothing |
| `dupes` | read | the four levels of "the same thing twice", kept apart: same bytes at several paths, byte-different files with identical text, one DOI (or title and first author) under several documents, a preprint and its published version |
| `related <reference>` | read | one document's accepted relations, open proposals, collections, tags and merge/split history |
| `relations list [--kind K] [--status S] [--limit N] [--cursor C]` | read | relation proposals, best evidence first |
| `relations accept <id>... [--keep D]` | catalog | accept proposals. A `same_document` proposal MERGES the two documents; a `collection` proposal makes the collection |
| `relations reject <id>...` | catalog | decide a proposal is wrong (kept, and not proposed again) |
| `relations add <kind> <source> <target> [--artifacts] [--note T] [--position P]` | catalog | state a relation yourself. Document kinds: `supplement_of`, `part_of`, `version_of`, `related_to`; with `--artifacts`: `duplicate_of`, `derivative_of`, `replaces`, `equivalent_to` |
| `relations remove <relation id>` | catalog | take a relation back; it stays recorded as retracted |
| `document merge <keep> <absorb> [--reason T]` | catalog | make two documents one. The absorbed document's artifacts move to the survivor (every artifact id unchanged) and it is retired, not deleted |
| `document split <document> <artifact>` | catalog | take one artifact out into its own document; if a merge retired a document that held only that artifact, it is revived under its original id |
| `document canonical <document> <artifact> [--reason T]` | catalog | choose which artifact is read for text and metadata |
| `collection create <name> [documents...] [--description T]` | catalog | a named group of documents (nothing is moved) |
| `collection add <name> <documents...>` | catalog | add documents to a collection |
| `collection remove <name> <documents...>` | catalog | take documents out of a collection (they are untouched) |
| `collection list` | read | every collection and its size |
| `collection show <name> [--limit N] [--cursor C]` | read | a collection's members |
| `collection delete <name>` | catalog | remove a collection from view; its record is kept |
| `tag add <tag> <documents...>` | catalog | tag documents |
| `tag remove <tag> <documents...>` | catalog | remove a tag from documents |
| `tag list` | read | every tag and how many documents carry it |
| `saved list` | read | saved searches |
| `saved run <name> [--limit N] [--cursor C]` | read | run a saved search against the library as it is now |
| `saved delete <name>` | catalog | remove a saved search from view; its record is kept |
| `root allow-organize <root> [--off]` | catalog | let the organizer move files in this root (or stop it). Off by default; this is the only way to change it |
| `plan create [--root R]... [--layout L] [--document D]... [--out FILE] [--no-disk-check] [--status S] [--limit N] [--cursor C]` | catalog | PROPOSE renames (and, with `--layout by_year`, moves) from ACCEPTED metadata, as a frozen, hashed plan file written outside the library and registered here. **Moves nothing** |
| `plan show <plan> [--status S] [--limit N] [--cursor C]` | read | read a plan (its file, or an id this catalog made): every item with its old and new path, risk and reason |
| `apply <plan> [--dry-run] [--verify-hashes]` | filesystem | MOVE FILES as a plan says. Checks the plan still matches the library, then each move's preconditions (source hash, destination absent, no link on the path, root allows it), journals the intent before touching a file, and never overwrites. `--dry-run` runs every check and moves nothing |
| `undo [--operation ID] [--dry-run] [--verify-hashes]` | filesystem | move files back as an apply moved them (default: the latest apply with moves still in place). Refuses any file whose bytes changed since, or whose old path is occupied; never overwrites |
| `recover [--dry-run]` | filesystem | reconcile an interrupted apply or undo with what is on disk: finishes the bookkeeping for a move that happened, marks one that did not, puts a file left at a temporary name back. Never retries and never moves a file forward |
| `history [operation] [--limit N]` | read | the organizer's journal: operations (apply, undo, recover) with their item counts, or with an id, each move and its outcome |
| `capabilities` | read | what this installation can do, for a program: the four version numbers, every command flagged read-only or not, the MCP tools, the `knowledgevista://` forms. Needs no catalog |
| `locate <reference> [--no-disk-check]` | read | where a document or file is NOW, from a document id, a SHA-256 (or prefix), a path or a `knowledgevista://` reference: every current path, whether each is really on disk, what the library knows. The call OpenChem makes |
| `open <reference> [--pdf-page N \| --label L] [--no-launch]` | read | hand the file to the operating system's viewer (starts a program; changes nothing in the library). The page is reported, not navigated to |
| `mcp` | read | serve the read-only Model Context Protocol tools on stdin/stdout (no envelope: stdout carries only protocol replies) |
| `view [name] [--limit N] [--cursor C]` | read | a system view (`inbox`, `unresolved`, `ambiguous`, `missing`, `duplicates`, `new`, `untagged`, `uncollected`). A view is a query, not stored state; with no name, lists them |

## Cursors and limits

A listing that has more than one page prints `"complete": false` and a `next_cursor`; pass it back as `--cursor` to get the next page. A
cursor is opaque and belongs to the command and arguments that issued it. It records the catalog revision, so after ANY meaningful change
to the catalog (a scan, an accepted proposal) it is `KV_CURSOR_STALE` rather than a page that might skip or repeat items; it also checks that
the item just before the page is still the one last returned. A cursor handed to a different command or different arguments is
`KV_INVALID_ARGUMENTS`. `--offset` still works on `review list` and `relations list` but notices nothing: prefer `--cursor`.

`--limit` is between 1 and 1000 and a value outside that is refused (`KV_INVALID_ARGUMENTS`), never silently clamped; a listing cannot be
paged past 5000 items (narrow the query instead). Listings are in a deterministic order, so the same catalog gives byte-identical output.

## Integration commands

`capabilities`, `locate`, `open` and `mcp` are the surface other programs use; their shapes and the `knowledgevista://` grammar are in
[INTEGRATION.md](INTEGRATION.md), and any change to them bumps `protocol_version`. `locate` returns one record: `status` (`available`,
`root_offline`, `missing`, `unlocated`), `available`, `document_id`, `artifact_id`, `uri`, and `locations` each with `absolute_path`,
`root_status`, `state` and `on_disk` (`true`, `false`, or `null` when the root is offline and nothing was checked). Warning codes:
`KV_HISTORICAL_MATCH` (a name that only matched a past location), `KV_DOCUMENT_MERGED` (a merged-away id was resolved to its survivor).

## Organizer commands

Records: `plan create` and `plan show` print a `summary` (`plan_id`, `path`, `hash`, `naming_policy`, `layout`, counts by `operation`, `status` and
`risk`) and one `item` per listed item (`item_id`, `operation` rename | move | rename+move | unchanged, `status` planned | unchanged | blocked, `old_path`,
`new_path`, `expected_sha256`, `risk`, `confidence`, `source`, `reason`, `blocked_reason`, and the `snapshot` of accepted metadata the name was built from).
`apply`, `undo` and `recover` print a `summary` (`kind`, `operation_id`, `dry_run`, `already_done`, `counts`, `problems`) and one `item` each with its `state`:
`succeeded`, `already_applied`, `undone`, `would_move` (dry run), `skipped` (a locked file: a **warning** `KV_ITEM_SKIPPED`, never a failure), `failed` or `uncertain`
(an **error** entry with the item's own code: `KV_FILE_CHANGED`, `KV_FILE_MISSING`, `KV_DESTINATION_EXISTS`, `KV_PERMISSION_DENIED`, `KV_PATH_UNSAFE`,
`KV_UNCERTAIN`). `ok` is false, and the exit code 1, exactly when an item `failed` or is `uncertain`.

A plan the library no longer matches is `KV_PLAN_STALE` and nothing moves; applying the same plan twice reports `already_applied` and changes nothing.
Warning codes: `KV_ITEMS_BLOCKED` (a plan holds items whose destination is occupied or unusable). The reasoning is in [ORGANIZER.md](ORGANIZER.md).

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

## Relation, duplicate and organisation commands

**Two levels, never mixed.** An *artifact* relation is about bytes (`duplicate_of`, `derivative_of`, `replaces`, `equivalent_to`). A
*document* relation is about library items (`supplement_of`, `part_of`, `version_of`, `related_to`). Making two documents ONE is a third
thing, a merge, and is proposed as `same_document`. A `relation` record carries `level`, `kind`, `source`, `target`, `accepted_by` and
`accepted_from_candidate`; a proposal carries `evidence` and a descriptive `confidence`.

`relate` summary record: `documents`, `fingerprinted`, `proposals` (found, by kind), `outcomes` (`new`, `updated`, `unchanged`, `stale`,
`decided`), `exact_copy_groups`, `proposals_waiting`. A rerun with nothing new changes nothing.

Nothing is deleted. `document merge` retires the absorbed document (`retired_at`, `merged_into`) and keeps its record; `relations remove`
and `collection delete` retract. A retired document is refused wherever a live one is needed (`KV_INVALID_ARGUMENTS`, naming the
document it was merged into).

**Search filters** (query language 2). `tag:`, `collection:`, `doi:`, `year:`, `author:`, `kind:` at the start of a word narrow a search
to documents whose ACCEPTED metadata, tags or collections match (a proposal never narrows a search); `year:` takes `2020`, `2015-2020`,
`2015-` or `-2020`; quote a value with spaces (`collection:"To read"`). Only those six names are filters, so `Cu(II):` or `pH:7.4` stay
search text, and a filter with no value is `KV_QUERY_INVALID`. The search summary says how many documents the filters selected, so "no
hits" is not read as "not in the library". A saved search stores the parsed query and its language version; a version newer than the
program is refused, never reinterpreted.

Warning codes: `KV_NO_TEXT_INDEX`, `KV_PROPOSAL_STALE` (a batch `relations accept` skipped a proposal whose evidence is gone; the rest were applied).
