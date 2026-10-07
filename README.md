# Knowledge Vista

> **Status: pre-alpha (milestone 0).** The foundations are being laid. There is no usable library manager yet.

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
