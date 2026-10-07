# Knowledge Vista

> **Status: pre-alpha (milestone 3).** It scans folders and tracks files by content hash (surviving renames, moves and re-downloads),
> reads PDF text safely and searches it (saying what it could not search), and proposes a title and DOI for each document from the
> file itself and, if you allow it, from Crossref, as proposals you review. No organiser or reader yet; see
> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Point it at your mess of PDFs. Search them by what they contain. Find out what each one actually is. Nothing is moved,
renamed or deleted unless you review a plan and approve it.

Knowledge Vista (`kv`) is a local-first library manager, search index and reader for document folders: a folder of
papers named `cm4c01978.pdf`, a pile of downloaded books, anything you cannot tell apart from its filename.

## What it is for

1. **"Which paper is this file?"** Identify a document from what it contains and from its DOI, in seconds.
2. **"I remember a phrase but not the file."** Full-text search across every page, with a hit that takes you to the page.
3. **"Which of my documents supports this claim?"** A source-aware, page-level evidence layer that other tools
   (a coding assistant over MCP, [OpenChem Studio](https://github.com/xaerogonzo/OpenChem-Studio)) can query without
   needing to understand your filenames.

You can leave every file exactly where it is and still get all of that. Reorganising files on disk is optional, reviewed
first, and reversible.

## Principles

- **The filesystem is reality.** Filenames are locators, never identity. A file is recognised by its content hash, so
  renaming or moving it does not make it a "new" document.
- **Uncertainty never silently becomes fact.** A guessed title is a proposal with its evidence, not a stored truth.
- **Local-first.** Everything works with the network off. Online metadata lookup is opt-in and sends only a DOI or title.
- **No telemetry. No uploads of your documents. Ever.**

See [docs/INVARIANTS.md](docs/INVARIANTS.md), [docs/SAFETY_MODEL.md](docs/SAFETY_MODEL.md) and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Try it (milestones 1 to 5)

```bash
uv sync --extra extract        # PyMuPDF, needed only to read PDF text
uv run kv root add "D:/path/to/your/papers" --label papers
uv run kv scan                 # hashes new/changed files; reads nothing it does not have to
uv run kv stats                # inventory, and health kept separate
uv run kv explain kaya2022.pdf # what is it, where is it, what happened to it
uv run kv doctor               # structural check, read-only
uv run kv verify               # re-reads every file and checks its bytes (slow)
uv run kv extract              # reads PDF text in a memory-capped, killable worker process
uv run kv search "aqueous solubility"       # pages, with a statement of what could NOT be searched
uv run kv search "solub*" --near compound   # explicit prefix; words within 30 of each other
uv run kv show kaya2022.pdf --pdf-page 12   # or --label 164: physical position vs printed page number
uv run kv import openchem-index "D:/path/Sci Downloads.index.sqlite"   # reuse an OpenChem index as provisional text
uv run kv resolve              # propose DOIs and titles from the files themselves; accepts nothing; sends nothing
uv run kv resolve --accept-safe   # ...and accept only the proposals that earned `safe` (a named, recorded rule)
uv run kv review list          # what is waiting for you, most urgent first
uv run kv review accept 3fa9c2d1   # or: kv metadata set kaya2022.pdf year 2022   (stated by you, so it is locked)
```

Looking things up online is **off** and stays off until you switch it on. It sends a DOI or a title to Crossref and nothing
else (not a file name, a path or any text):

```bash
uv run kv resolve --online --list-requests   # exactly what would be sent; sends nothing
uv run kv config set online_lookup true
uv run kv config set mailto you@example.org  # optional: Crossref's courtesy address, sent only if you set it
uv run kv resolve --online --accept-safe     # resumable: a stopped run continues from its cache
```

Find the same thing twice, and organise without moving anything:

```bash
uv run kv relate               # propose duplicates, supplements, chapters, versions; changes nothing
uv run kv dupes                # the four levels: same bytes, same text, same publication, related work
uv run kv relations list       # each proposal with its evidence
uv run kv relations accept 3fa9c2d1   # a same-document proposal MERGES two documents (nothing is deleted; `kv document split` undoes it)
uv run kv collection create "To read" kaya2022.pdf
uv run kv tag add toxicology kaya2022.pdf
uv run kv search "solubility tag:toxicology year:2015-"   # filters read accepted metadata only
uv run kv view inbox           # what needs a person first
```

Let other programs ask where a paper is (nothing here writes anything):

```bash
uv run kv locate 3fa9c2d1                     # a hash prefix, document id, name or knowledgevista:// reference -> the current path, checked on disk
uv run kv capabilities --json                 # versions, which commands only read, the MCP tools; needs no catalog
uv run kv mcp                                 # read-only Model Context Protocol server on stdin/stdout for a coding assistant
```

Add `--json` for machine output (see [docs/CLI_CONTRACT.md](docs/CLI_CONTRACT.md)); [docs/SEARCH.md](docs/SEARCH.md) says
exactly what search does and does not do, and [docs/METADATA.md](docs/METADATA.md) what a proposal is, how a DOI is judged to be
a document's own, and what the batch rule may and may not do; [docs/RELATIONS.md](docs/RELATIONS.md) says what a relation, a duplicate and a merge are, [docs/INTEGRATION.md](docs/INTEGRATION.md) what another program may rely on, and [docs/MCP.md](docs/MCP.md) what the assistant server can and cannot do. Nothing here writes to your folders.

## Develop

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra extract
uv run python -m pytest
uv run kv --version
```

## Your files and your rights

Knowledge Vista catalogues and reads files **you already have**. It does not download papers, and this repository
contains no one's documents: test fixtures are generated in code from synthetic text. You are responsible for having the
right to hold and use the files you point it at.

## Licence

AGPL-3.0-or-later (see [LICENSE](LICENSE)). The PDF engine, PyMuPDF, is AGPL-3.0 (or commercial); the licence is chosen
to match. Optional extras are kept out of the default install, and CI checks that no dependency in it carries a
non-commercial-only licence.
