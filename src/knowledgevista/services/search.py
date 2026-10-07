"""Search the extracted pages and say what the search could and could not see.

A bare "no results" is dangerous in a library: it reads as "this is not in my papers" when the truth may be "that paper
is a scan, or has not been extracted yet". So every search carries COVERAGE, and a search over zero searchable
documents is not an empty success but a warning (`KV_NOTHING_SEARCHABLE`).

A hit is a place to LOOK, not a quotation: the snippet is a navigation aid, extracted tables arrive as running text,
and the PDF page is the authority (docs/INVARIANTS.md, 10). Results are ordered by relevance with a fixed tie-break
(artifact, then page), so the same query returns the same list every time.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from knowledgevista.domain import titles
from knowledgevista.domain.doi import normalise_doi
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.extract.profile import IMPORTED_SOURCE
from knowledgevista.index.search import Query, RawHit, run_search


@dataclass
class SearchResult:
    hits: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    coverage: dict[str, Any] = field(default_factory=dict)
    #: Set when the query had metadata filters: how many documents they selected, so "no hits" is not read as "not in the library".
    scope: dict[str, Any] | None = None


def coverage(catalog: sqlite3.Connection, index: sqlite3.Connection | None) -> dict[str, Any]:
    """What a search over this library can and cannot see."""
    pdfs = {r[0] for r in catalog.execute("SELECT artifact_id FROM artifact WHERE content_kind = 'pdf'")}
    other = catalog.execute("SELECT COUNT(*) FROM artifact WHERE content_kind <> 'pdf'").fetchone()[0]
    by_artifact = {}
    if index is not None:
        by_artifact = {r["artifact_id"]: r for r in index.execute(
            "SELECT artifact_id, source, status, legacy_scanned FROM extraction")}
    no_text = set()
    if index is not None:
        # Extracted fine, but no page has a single character: a scan. It is listed, never reported as "no match".
        no_text = {r[0] for r in index.execute(
            "SELECT e.artifact_id FROM extraction e WHERE e.status <> 'failed' AND "
            "NOT EXISTS (SELECT 1 FROM page p WHERE p.extraction_id = e.extraction_id AND p.chars > 0)")}
    searchable = provisional = partial = failed = scans = 0
    for artifact_id, row in by_artifact.items():
        if artifact_id not in pdfs:
            continue  # extracted text for an artifact the catalog no longer has: ignored, doctor reports it
        if row["status"] == "failed":
            failed += 1
            continue
        if row["status"] == "partial":
            partial += 1
        if row["source"] == IMPORTED_SOURCE:
            provisional += 1
        if artifact_id in no_text:
            scans += 1
        searchable += 1
    return {
        "pdf_documents": len(pdfs),
        "searchable": searchable,  # extracted, with pages in the index (includes partial and provisional)
        "partially_indexed": partial,
        "provisional_imported": provisional,
        "extraction_failed": failed,
        "not_yet_extracted": len(pdfs) - sum(1 for a in by_artifact if a in pdfs),
        "no_text_layer": scans,  # extracted, but no page has any text (a scan): can never match a text query
        "other_files_not_searchable": other,
    }


def _year_bounds(value: str) -> tuple[int, int]:
    low, _, high = value.partition("-")
    if "-" not in value:
        return int(value), int(value)
    return (int(low) if low else 0), (int(high) if high else 9999)


def filter_documents(catalog: sqlite3.Connection, filters: tuple[tuple[str, str], ...]) -> set[str]:
    """Live documents satisfying EVERY filter. Each filter reads accepted metadata, tags or collections; a proposal is never
    matched (an unaccepted guess cannot narrow a search)."""
    from knowledgevista.services import organize

    matching: set[str] | None = None
    for name, value in filters:
        if name == "tag":
            found = {r[0] for r in catalog.execute("SELECT document_id FROM document_tag WHERE tag_key = ?", (organize.key_of(value),))}
        elif name == "collection":
            found = set(organize.collection_members(catalog, value)[1])
        elif name == "doi":
            found = {r[0] for r in catalog.execute("SELECT document_id FROM metadata_value WHERE field = 'doi' AND value = ?", (normalise_doi(value),))}
        elif name == "year":
            low, high = _year_bounds(value)
            found = {r[0] for r in catalog.execute("SELECT document_id FROM metadata_value WHERE field = 'year' AND CAST(value AS INTEGER) BETWEEN ? AND ?", (low, high))}
        elif name == "author":
            wanted = titles.alnum_key(value)
            found = set()
            for r in catalog.execute("SELECT document_id, value FROM metadata_value WHERE field = 'authors'"):
                people = json.loads(r["value"])
                if any(wanted and wanted in titles.alnum_key(" ".join(str(p.get(k) or "") for k in ("family", "given", "name"))) for p in people):
                    found.add(r[0])
        elif name == "kind":
            kind = value.strip().lower()
            found = {r[0] for r in catalog.execute(
                "SELECT da.document_id FROM document_artifact da JOIN artifact a ON a.artifact_id = da.artifact_id WHERE lower(a.content_kind) = ?", (kind,))}
            found |= {r[0] for r in catalog.execute("SELECT document_id FROM metadata_value WHERE field = 'type' AND lower(value) = ?", (kind,))}
        else:
            raise KvError(ErrorCode.QUERY_INVALID, f"Unknown filter {name!r}.", {"filter": name})
        matching = found if matching is None else matching & found
    live = {r[0] for r in catalog.execute("SELECT document_id FROM document WHERE retired_at IS NULL")}
    return (matching or set()) & live


def search(
    catalog: sqlite3.Connection, index: sqlite3.Connection | None, query: Query, *, limit: int = 20
) -> SearchResult:
    result = SearchResult(coverage=coverage(catalog, index))
    if index is None:
        return result
    scope = None
    if query.filters:
        documents = filter_documents(catalog, query.filters)
        scope = {r[0] for d in documents for r in catalog.execute("SELECT artifact_id FROM document_artifact WHERE document_id = ?", (d,))}
        result.scope = {"filters": [list(f) for f in query.filters], "documents_matching": len(documents)}
    if query.path_contains:
        needle = query.path_contains.lower().replace("\\", "/")
        by_path = {
            r["artifact_id"] for r in catalog.execute("SELECT artifact_id, relative_path FROM location WHERE artifact_id IS NOT NULL")
            if needle in r["relative_path"].lower()
        }
        scope = by_path if scope is None else scope & by_path
    raw = run_search(index, query, limit=limit, artifact_scope=scope)
    result.truncated = len(raw) > limit
    result.hits = [_enrich(catalog, hit) for hit in raw[:limit]]
    return result


def current_paths(catalog: sqlite3.Connection, artifact_id: str) -> list[dict[str, Any]]:
    return [
        {"root": r["label"], "path": r["relative_path"], "state": r["state"], "root_status": r["status"]}
        for r in catalog.execute(
            "SELECT r.label, r.status, l.relative_path, l.state FROM location l JOIN root r ON r.root_id = l.root_id "
            "WHERE l.artifact_id = ? AND l.ended_at IS NULL ORDER BY l.path_key", (artifact_id,))
    ]


def _enrich(catalog: sqlite3.Connection, hit: RawHit) -> dict[str, Any]:
    document = catalog.execute("SELECT document_id FROM document_artifact WHERE artifact_id = ?", (hit.artifact_id,)).fetchone()
    paths = current_paths(catalog, hit.artifact_id)
    return {
        "document_id": document[0] if document else None,
        "artifact_id": hit.artifact_id,
        # The durable citation: artifact + 1-based PDF page. The page_id is internal and changes when text is re-extracted.
        "anchor": {"artifact_id": hit.artifact_id, "pdf_page": hit.pdf_page},
        "pdf_page": hit.pdf_page,
        "printed_label": hit.printed_label,
        "snippet": hit.snippet,
        "evidence_type": "search_navigation",
        "extraction": {"source": hit.source, "status": hit.status, "provisional": hit.source == IMPORTED_SOURCE},
        "paths": paths,
        "available": any(p["state"] == "active" and p["root_status"] == "online" for p in paths),
    }
