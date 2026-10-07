# Safety model

What each kind of operation is allowed to touch. A command's category is part of its contract
(`kv capabilities --json` reports it).

| Category | Touches | Examples | Rules |
|---|---|---|---|
| **Read** | the catalog and cache, read-only | `search`, `show`, `explain`, `doctor`, `verify`, `dupes` | Leaves source bytes unchanged (asserted by hash in tests). `doctor` diagnoses and never repairs by default. |
| **Catalog mutation** | the catalog only | `scan`, `resolve`, accepting a candidate, tagging, annotating | Recorded with an actor and a history entry. Never deletes history. Never holds a write transaction across network I/O. |
| **Network** | outbound requests | metadata lookup | Off by default. Sends only a DOI or title, listed beforehand. Never document text. One central policy owns the User-Agent, timeouts, retries and the offline flag. Opening a file never triggers a lookup. |
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
