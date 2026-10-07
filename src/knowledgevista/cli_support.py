"""What every command module shares: the outcome a command returns, where the catalog and its caches live, a document's
human name, and the one search renderer (so `kv search` and `kv saved run` can never disagree about what a result looks like).

Split out of `cli.py` so the command modules (`cli.py`, `cli_relations.py`) can import it without importing each other.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from knowledgevista import paths
from knowledgevista.db.catalog import open_catalog
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.index.search import Query
from knowledgevista.index.store import open_index
from knowledgevista.services import search as search_service


@dataclass
class Outcome:
    records: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)  # the human rendering
    complete: bool = True  # False when the records are a truncated view of a larger result

    @property
    def ok(self) -> bool:
        return not self.errors


def say(message: str) -> None:
    """Progress and diagnostics go to stderr, so stdout stays one JSON document under --json."""
    print(message, file=sys.stderr, flush=True)


def catalog_path(args: argparse.Namespace) -> Path:
    return Path(args.catalog) if getattr(args, "catalog", None) else paths.catalog_path()


def index_for(args: argparse.Namespace, *, create: bool, read_only: bool = False):
    """The extraction store beside this catalog, or None if there is none (or it is from another schema version)."""
    return open_index(paths.index_path(catalog_path(args)), create=create, read_only=read_only)


def label_of(conn, document_id: str) -> str:
    """A short human name for a document: its current path, else the start of its id."""
    row = conn.execute(
        "SELECT l.relative_path FROM location l JOIN document_artifact da ON da.artifact_id = l.artifact_id "
        "WHERE da.document_id = ? AND l.ended_at IS NULL ORDER BY l.state, l.path_key LIMIT 1", (document_id,)).fetchone()
    return row[0] if row else document_id[:12]


def search_outcome(args: argparse.Namespace, query: Query) -> Outcome:
    """Run a parsed query against the library and render it: the summary with coverage, then the hits."""
    conn = open_catalog(catalog_path(args), create=False, read_only=True)
    index = index_for(args, create=False, read_only=True)
    try:
        result = search_service.search(conn, index, query, limit=args.limit)
    finally:
        conn.close()
        if index is not None:
            index.close()
    cov = result.coverage
    out = Outcome(complete=not result.truncated)
    out.records.append({
        "type": "summary",
        "query": {"alternatives": list(query.alternatives), "near": list(query.near), "within": query.within,
                  "also": list(query.also), "path_contains": query.path_contains, "filters": [list(f) for f in query.filters],
                  "language_version": query.language_version},
        "hits": len(result.hits), "truncated": result.truncated, "coverage": cov, "scope": result.scope,
    })
    out.records += [{"type": "hit", **hit} for hit in result.hits]
    if cov["searchable"] == 0:
        out.errors.append(KvError(ErrorCode.NOTHING_SEARCHABLE,
                                  "No document has extracted text, so this search could not have found anything. Run: kv extract",
                                  {"coverage": cov}).as_dict())
    else:
        caveats = [f"{cov[key]} {label}" for key, label in (
            ("not_yet_extracted", "PDF(s) not extracted yet"), ("no_text_layer", "scan(s) with no text layer"),
            ("partially_indexed", "partially indexed"), ("extraction_failed", "failed extraction(s)")) if cov[key]]
        if caveats:
            out.warnings.append({"code": "KV_SEARCH_COVERAGE", "details": cov,
                                 "message": "Not everything was searchable: " + ", ".join(caveats) + ". A missing hit is not proof of absence."})
    for hit in result.hits:
        where = hit["paths"][0]["path"] if hit["paths"] else hit["artifact_id"][:12]
        label = f" [{hit['printed_label']}]" if hit["printed_label"] else ""
        provisional = "  (provisional)" if hit["extraction"]["provisional"] else ""
        out.lines.append(f"{where}  p{hit['pdf_page']}{label}{provisional}\n    {' '.join(hit['snippet'].split())}")
    out.lines.append(f"{len(result.hits)} page(s)" + (f" (limit {args.limit}; more exist)" if result.truncated else ""))
    if result.scope is not None:
        out.lines.append(f"  filters {' '.join(f'{n}:{v}' for n, v in result.scope['filters'])} selected {result.scope['documents_matching']} document(s); "
                         "only those were searched. Filters read accepted metadata, tags and collections, never a proposal.")
    out.lines += [f"  note: {w['message']}" for w in out.warnings]
    return out
