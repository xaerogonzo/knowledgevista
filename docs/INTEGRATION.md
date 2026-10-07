# Integration contract

Milestone 5. This page is what another program may rely on: OpenChem Studio today, a script or a coding assistant tomorrow. It is
small on purpose. Everything here is read-only, and a change to anything on this page bumps `protocol_version` (`kv capabilities`).

The CLI is the contract ([CLI_CONTRACT.md](CLI_CONTRACT.md) has every command and error code); the Model Context Protocol server is the
same services behind a different door ([MCP.md](MCP.md)). Nothing here needs the other program to import Knowledge Vista, to share a
database with it, or to be running when the other one is not.

## The four numbers

| Number | Where | Changes when |
|---|---|---|
| `protocol_version` | `kv capabilities` | `capabilities`, `locate`, the `knowledgevista://` grammar or an error code's meaning breaks |
| `json_schema_version` | every envelope's `schema_version` | the envelope's shape breaks |
| `catalog_schema_version` | `kv capabilities` | the SQLite schema moves. **Internal**: reported so a caller can see a catalog is newer than the program, never so it can read the file |
| `mcp.protocol_versions` | `kv capabilities` | the Model Context Protocol revisions `kv mcp` speaks change |

A caller checks `protocol_version` once and treats an unknown value as "KV is present but I cannot use it": it degrades to what it
did before KV existed, and says so. It never guesses.

## Asking where something is: `kv locate`

```
kv --json locate <document id | sha256 | sha256 prefix (8+) | path or file name | knowledgevista:// reference> [--no-disk-check]
```

One record, `type: "location"`:

| Field | Meaning |
|---|---|
| `document_id`, `uri` | the live document, and its `knowledgevista://document/<id>` reference (with `/page/N` or `/label/L` if the question named a page) |
| `artifact_id`, `artifact_uri` | the bytes asked about (the document's canonical artifact if a document was asked) |
| `status` | `available`: some active location is under an online root and exists on disk. `root_offline`: it is catalogued but its root cannot be reached right now. `missing`: catalogued, and not there. `unlocated`: no current location at all |
| `locations[]` | each current path: `absolute_path`, `relative_path`, `root`, `root_status`, `state`, and `on_disk` (`true`, `false`, or `null` when the root is offline and nothing was checked) |
| `artifacts[]` | every artifact of the document (a merged document has several), each with its own status and locations |
| `title`, `doi`, `year` | **accepted** metadata only; a proposal is never returned as a fact |
| `matched_by`, `historical` | how the reference was understood; `historical: true` means the NAME only matched where the file used to be |
| `requested_document_id`, `retired_document` | present when the id asked about was merged into another document: `document_id` is the survivor |

What a caller may rely on:

* **One answer or a refusal.** More than one document is `KV_AMBIGUOUS` with `details.candidates`; nothing is chosen. A hash is never
  ambiguous (one hash is one artifact); a hash *prefix* can be.
* **The disk beats the catalog.** `on_disk` is a `stat` made now. An `active` location with `on_disk: false` means the catalog is behind
  (someone renamed the file after the last `kv scan`): run `kv scan`, or treat the file as missing. A caller that is about to *trust*
  the bytes (OpenChem's `--check`) hashes the path itself instead of trusting the catalog's hash.
* **A stored id survives a merge.** A `kv_document_id` recorded before two documents were merged still resolves, to the survivor, with
  the `KV_DOCUMENT_MERGED` warning; the caller should store the new id.
* **It writes nothing and opens no file.** It reads the catalog and calls `stat`.

Exit codes are the contract's: 0 answered, 1 not found / ambiguous / no catalog, 2 a malformed argument or reference.

## References: `knowledgevista://`

```
knowledgevista://document/<document id>
knowledgevista://document/<document id>/page/<pdf page>          physical position, counted from 1
knowledgevista://document/<document id>/label/<printed label>    the number printed on the page; percent-encoded
knowledgevista://artifact/<sha256>
knowledgevista://artifact/<sha256>/page/<pdf page>
knowledgevista://artifact/<sha256>/label/<printed label>
```

* **Exact.** An id is the lowercase hex the catalog stores (a document id is 32 characters, an artifact is its SHA-256). A reference is
  never a prefix or a file name; loose matching is `kv locate` of a plain string, or the MCP `resolve_reference` tool.
* **A page is `page` or `label`, never a bare number.** A physical position and a printed label only coincide by luck.
* **One spelling.** No query string, no fragment, no trailing slash, no leading zeros; a label is percent-encoded and at most 64
  characters. `format(parse(text))` is canonical, so a stored reference can be compared as text.
* **Document or artifact?** A *document* reference names a library item and survives its files being replaced, merged or split; an
  *artifact* reference names exactly one set of bytes. A note that means "this paper" stores the document; a record that means
  "this exact PDF was read" stores the artifact.
* **Resolved locally, no service.** The scheme is not registered with the operating system yet (design-notes.md); a consumer calls
  `kv locate <reference>`.

## Opening: `kv open`

`kv open <reference> [--pdf-page N | --label L] [--no-launch]` finds a reachable copy (`KV_FILE_MISSING` or `KV_ROOT_UNAVAILABLE` if
there is none) and hands it to the operating system's default viewer. The page is **reported, not navigated to**: there is no
portable way to tell an arbitrary viewer to go to page 12, and pretending would be worse than saying so (`page_targeted: false`). The
built-in reader (a later milestone) opens at the page. `--no-launch` resolves and reports and starts nothing.

## Cursors and limits

See [CLI_CONTRACT.md](CLI_CONTRACT.md). In short: a truncated listing carries `next_cursor`; a cursor is refused as `KV_CURSOR_STALE` after
any meaningful catalog change; `--limit` is 1 to 1000 and out-of-range values are refused, not clamped.

## What OpenChem Studio does with this

OpenChem works without Knowledge Vista and says so when it is absent (invariant 7). When `kv` is installed and configured:

1. `tools/index_literature.py --check` verifies each held paper by SHA-256. If the recorded file name is gone, it asks
   `kv locate <sha256>`; if KV names a current path, it **re-hashes that path itself** and passes with a note saying where the paper went. A
   renamed paper is not a problem; a changed one still is.
2. The literature and sources records may carry `kv_document_id` beside the existing hash. The three have different jobs:
   `kv_document_id` says *this paper is a source*, the SHA-256 says *this exact PDF was verified*, and `file` is a display name that may
   be stale. `tools/index_literature.py --link-kv` proposes the ids; nothing is guessed.
3. A setting beside Vina and ORCA names the `kv` executable; "Open in Knowledge Vista" calls `kv open`.

A caller that wants the same behaviour for itself needs three things: run `kv --json capabilities` and require `protocol_version == 1`;
treat every non-zero exit, timeout, missing executable or unparsable output as "KV unavailable" (degrade, never fail); and never act on
a path without checking it exists.
