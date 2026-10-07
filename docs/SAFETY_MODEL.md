# Safety model

What each kind of operation is allowed to touch. A command's category is part of its contract
(`kv capabilities --json` reports it).

| Category | Touches | Examples | Rules |
|---|---|---|---|
| **Read** | the catalog and cache, read-only | `search`, `show`, `explain`, `doctor`, `verify`, `dupes` | Leaves source bytes unchanged (asserted by hash in tests). `doctor` diagnoses and never repairs by default. |
| **Filesystem mutation** | files under a root with `allow_organize`, and nothing else | `apply`, `undo`, `recover` | Only after a plan a person reviewed; the plan must match the library, every source is re-hashed, nothing is overwritten or deleted, no link is followed, the intent is journaled before a file moves, and every move has an undo that refuses a file changed since. See [ORGANIZER.md](ORGANIZER.md) |
| **Read-only server** | nothing: the catalog and extraction store are opened `mode=ro` and never migrated | `kv mcp` (and `capabilities`, `locate`, which `stat` paths) | Structural: SQLite refuses a write on those connections, and a test requires every tool to leave both stores byte-identical. `kv open` starts the OS viewer on a file and changes nothing |
| **Catalog mutation** | the catalog only | `scan`, `resolve`, `relate`, accepting a candidate or a relation, merging and splitting documents, tagging, collecting, annotating | Recorded with an actor and a history entry. Never deletes history. Never holds a write transaction across network I/O. |
| **Network** | outbound requests | `resolve --online` | Off by default: the `online_lookup` setting must be on AND `--online` given; a damaged settings file means off. Sends only a DOI or title (and the contact address the user chose), listed beforehand by `--list-requests`. Never document text, a path, a file name or a hash. `network/policy.py` owns the User-Agent, timeouts, retries, pacing, request budget and the offline flag, and is the only code that touches a socket. A provider that cannot answer changes nothing (never "no match", never cached). Opening a file never triggers a lookup. |
| **Filesystem mutation** | the user's files | `apply`, `undo` | Plan, review, precheck, apply, verify. Reversible. Refuses unless the root has `allow_organize`. Aborts an item whose source hash changed. Never deletes. |

## Default posture

`sync` with default policy: no network, no organizing, nothing written to the library.

## Untrusted input

- PDFs are hostile input. Extraction runs in a worker process with a mandatory timeout and, where the OS supports
  it, a memory limit; a crashing or runaway parser must not take down a scan.
- PDF metadata, filenames and provider responses are text, never markup, and never shell input. External tools are
  invoked with argument arrays, never `shell=True`.
- Paths taken from commands or from the MCP must resolve inside an explicitly configured root.

## Storage classes

- **Catalog** (valuable): back it up. `kv export` writes portable JSON of the user-created state.
- **Cache** (rebuildable or ephemeral): safe to delete. Nothing user-owned lives here.
- **Source library** (the user's files): Knowledge Vista never writes into it except through a reviewed plan.

Backups of the catalog use SQLite's backup API, never a file copy: the catalog runs in WAL mode and committed data
can sit in the `-wal` file.
