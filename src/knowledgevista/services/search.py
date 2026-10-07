"""Search the extracted pages and say what the search could and could not see.

A bare "no results" is dangerous in a library: it reads as "this is not in my papers" when the truth may be "that paper
is a scan, or has not been extracted yet". So every search carries COVERAGE, and a search over zero searchable
documents is not an empty success but a warning (`KV_NOTHING_SEARCHABLE`).

A hit is a place to LOOK, not a quotation: the snippet is a navigation aid, extracted tables arrive as running text,
and the PDF page is the authority (docs/INVARIANTS.md, 10). Results are ordered by relevance with a fixed tie-break
(artifact, then page), so the same query returns the same list every time.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from knowledgevista.extract.profile import IMPORTED_SOURCE
from knowledgevista.index.search import Query, RawHit, run_search


@dataclass
class SearchResult:
    hits: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    coverage: dict[str, Any] = field(default_factory=dict)


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


def search(
    catalog: sqlite3.Connection, index: sqlite3.Connection | None, query: Query, *, limit: int = 20
) -> SearchResult:
    result = SearchResult(coverage=coverage(catalog, index))
    if index is None:
        return result
    scope = None
    if query.path_contains:
        needle = query.path_contains.lower().replace("\\", "/")
        scope = {
            r["artifact_id"] for r in catalog.execute("SELECT artifact_id, relative_path FROM location WHERE artifact_id IS NOT NULL")
            if needle in r["relative_path"].lower()
        }
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
