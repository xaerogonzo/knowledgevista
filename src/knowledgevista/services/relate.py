"""`kv relate`: look across the whole library for items that are the same, parts of one another, supplements, or versions,
and PROPOSE each with its evidence. Nothing is related, merged or grouped by a run; a person (or a named rule) accepts.

What is looked for, and what each finding means (docs/RELATIONS.md has the reasoning):

  same_document    two documents are one publication: they print/hold the same accepted DOI, have identical text, or share title
                   and first author. Accepting MERGES them (artifacts move, the other document is retired, nothing is deleted).
  equivalent_to    artifact level: two byte-different files with identical text.
  replaces         artifact level: a path whose bytes were replaced; the newer artifact replaces the older.
  supplement_of    a document that announces itself as a supplement and names another document by its DOI or its printed title.
  part_of          a chapter or entry of a book that is itself a document of the library.
  collection       the entries/chapters of a book that is NOT in the library, grouped under what the records say that book is.
  version_of       a preprint and the published work with the same title.
  related_to       the same title under different DOIs, neither a preprint.

Level one of duplicates, the same bytes at several paths, is not a relation at all and is reported by `kv dupes`, not proposed:
the library holds one item, and the files are copies of it.

A rerun with nothing new changes nothing: proposals are keyed by their evidence, and proposals whose evidence has gone are marked
stale, never deleted. A decision is never reopened.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from knowledgevista.db.catalog import bump_revision, transaction
from knowledgevista.domain import titles
from knowledgevista.domain.doi import find_dois, normalise_doi
from knowledgevista.domain.ids import new_id, utc_now
from knowledgevista.domain.local_evidence import SHARED_DOI_DOCUMENTS
from knowledgevista.domain.relation_evidence import (
    MATCHER_VERSION, PART_TYPES, PREPRINT_TYPES, WHOLE_TYPES, is_correction, pairs, supplement_markers, text_fingerprint,
)
from knowledgevista.index import metacache
from knowledgevista.index.store import extraction_pages
from knowledgevista.services import organize, relations
from knowledgevista.services.relations import RelationSpec

MIN_TITLE_KEY = 20
FRONT_PAGES = 2


@dataclass
class Doc:
    document_id: str
    artifacts: list[str]
    canonical: str | None
    stem: str | None
    values: dict[str, str] = field(default_factory=dict)
    first_text: str = ""

    @property
    def doi(self) -> str | None:
        return self.values.get("doi")

    @property
    def title(self) -> str | None:
        return self.values.get("title")

    @property
    def title_key(self) -> str | None:
        key = titles.alnum_key(self.title or "")
        return key if len(key) >= MIN_TITLE_KEY else None

    @property
    def kind(self) -> str:
        return (self.values.get("type") or "").lower()

    @property
    def container_key(self) -> str | None:
        return titles.alnum_key(self.values["container"]) if self.values.get("container") else None

    @property
    def first_family(self) -> str | None:
        try:
            people = json.loads(self.values.get("authors") or "[]")
        except ValueError:
            return None
        for person in people:
            name = person.get("family") or person.get("name")
            if name:
                return titles.alnum_key(name) or None
        return None

    @property
    def preprint(self) -> bool:
        return self.kind in PREPRINT_TYPES or "arxiv" in self.values


def _spec(level: str, kind: str, source: str, target: str, key: str, confidence: str, evidence: dict[str, Any], members: list[str] | None = None) -> RelationSpec:
    return RelationSpec(level=level, kind=kind, source_id=source, target_id=target, evidence_key=key, evidence=evidence, confidence=confidence, members=members)


def load_documents(conn: sqlite3.Connection) -> dict[str, Doc]:
    docs: dict[str, Doc] = {}
    for row in conn.execute("SELECT d.document_id FROM document d WHERE d.retired_at IS NULL ORDER BY d.document_id"):
        arts = conn.execute("SELECT artifact_id, canonical FROM document_artifact WHERE document_id = ? ORDER BY canonical DESC, artifact_id", (row[0],)).fetchall()
        if not arts:
            continue
        loc = conn.execute(
            "SELECT l.relative_path FROM location l WHERE l.artifact_id = ? ORDER BY l.ended_at IS NOT NULL, l.last_seen DESC LIMIT 1", (arts[0]["artifact_id"],)).fetchone()
        docs[row[0]] = Doc(row[0], [a["artifact_id"] for a in arts], arts[0]["artifact_id"] if arts[0]["canonical"] else None,
                           Path(loc[0]).stem if loc else None)
    for r in conn.execute("SELECT document_id, field, value FROM metadata_value"):
        if r["document_id"] in docs:
            docs[r["document_id"]].values[r["field"]] = r["value"]
    return docs


# ------------------------------------------------------------------------------------------------ the detectors


def _corrections(docs: dict[str, Doc]) -> tuple[list[RelationSpec], set[str]]:
    """A correction, comment or reply is RELATED to the work it is about: it is not a supplement of it, though it may mention one."""
    out: list[RelationSpec] = []
    corrections = {d.document_id for d in docs.values() if d.first_text and is_correction(d.title, d.first_text)}
    for document_id in sorted(corrections):
        d = docs[document_id]
        for other in docs.values():
            if other.document_id in corrections or other.document_id == document_id or not other.title_key:
                continue
            if titles.is_printed(other.title or "", d.first_text):
                out.append(_spec("document", "related_to", document_id, other.document_id, "correction-of", "high",
                                 {"correction": True, "its_title_is_printed_in_the_correction": True, "corrected": other.title}))
    return out, corrections


def _supplements(docs: dict[str, Doc], excluded: set[str] = frozenset()) -> list[RelationSpec]:
    by_doi: dict[str, list[str]] = {}
    for d in docs.values():
        if d.doi:
            by_doi.setdefault(d.doi, []).append(d.document_id)
    out: list[RelationSpec] = []
    marked = {d.document_id: supplement_markers(d.first_text, d.stem) for d in docs.values()}
    for d in docs.values():
        markers = marked[d.document_id]
        if d.document_id in excluded or not (markers["says_so"] or markers["file_name_hint"]) or not d.first_text:
            continue
        printed = {x.doi for x in find_dois(d.first_text)} | ({d.doi} if d.doi else set())
        found: dict[str, list[str]] = {}
        for doi in printed:
            for other in by_doi.get(doi, []):
                if other != d.document_id and not marked[other]["says_so"]:
                    found.setdefault(other, []).append("its DOI is printed on the supplement's first pages")
        for other in docs.values():
            if other.document_id != d.document_id and other.title_key and not marked[other.document_id]["says_so"] and titles.is_printed(other.title or "", d.first_text):
                found.setdefault(other.document_id, []).append("its title is printed on the supplement's first pages")
        for target, signals in sorted(found.items()):
            if not markers["says_so"] and len(signals) < 2:
                continue  # a file name alone nominates a document to be examined; it never decides
            confidence = "ambiguous" if len(found) > 1 else ("high" if markers["says_so"] else "medium")
            out.append(_spec("document", "supplement_of", d.document_id, target, "supplement",
                             confidence, {"markers": markers, "signals": sorted(set(signals)), "candidates_for_the_main_paper": len(found),
                                          "shared_doi": d.doi is not None and d.doi == docs[target].doi}))
    return out


def _same_document_by_doi(docs: dict[str, Doc], supplement_pairs: set[frozenset[str]], fingerprints: dict[str, str | None]) -> tuple[list[RelationSpec], dict[str, list[str]]]:
    groups: dict[str, list[str]] = {}
    for d in docs.values():
        if d.doi:
            groups.setdefault(d.doi, []).append(d.document_id)
    out: list[RelationSpec] = []
    parent_groups: dict[str, list[str]] = {}
    for doi, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        keys = {docs[m].title_key for m in members if docs[m].title_key}
        if len(members) >= SHARED_DOI_DOCUMENTS and len(keys) > 1:
            parent_groups[doi] = members  # many different titles under one DOI: a parent work, not copies of one paper
            continue
        for a, b in pairs(members):
            if frozenset((a, b)) in supplement_pairs:
                continue
            same_text = bool(fingerprints.get(docs[a].canonical or "")) and fingerprints.get(docs[a].canonical or "") == fingerprints.get(docs[b].canonical or "")
            out.append(_spec("document", "same_document", a, b, "shared-doi", "exact" if same_text else ("high" if len(keys) <= 1 else "medium"),
                             {"doi": doi, "titles_agree": len(keys) <= 1, "identical_text": same_text}))
    return out, parent_groups


def _identical_text(docs: dict[str, Doc], fingerprints: dict[str, str | None]) -> list[RelationSpec]:
    owner = {a: d.document_id for d in docs.values() for a in d.artifacts}
    groups: dict[str, list[str]] = {}
    for artifact_id, fingerprint in fingerprints.items():
        if fingerprint and artifact_id in owner:
            groups.setdefault(fingerprint, []).append(artifact_id)
    out: list[RelationSpec] = []
    for fingerprint, artifacts in sorted(groups.items()):
        if len(artifacts) < 2:
            continue
        for a, b in pairs(artifacts):
            out.append(_spec("artifact", "equivalent_to", a, b, "identical-text", "exact", {"text_fingerprint": fingerprint[:16], "same_document": owner[a] == owner[b]}))
        docs_in_group = sorted({owner[a] for a in artifacts})
        for a, b in pairs(docs_in_group):
            out.append(_spec("document", "same_document", a, b, "identical-text", "exact",
                             {"text_fingerprint": fingerprint[:16], "artifacts": sorted(x for x in artifacts if owner[x] in (a, b))}))
    return out


def _same_title_and_author(docs: dict[str, Doc], already: set[frozenset[str]]) -> list[RelationSpec]:
    groups: dict[tuple[str, str], list[str]] = {}
    for d in docs.values():
        if d.title_key and d.first_family:
            groups.setdefault((d.title_key, d.first_family), []).append(d.document_id)
    out: list[RelationSpec] = []
    for (title_key, family), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        for a, b in pairs(members):
            da, db = docs[a], docs[b]
            if frozenset((a, b)) in already or (da.doi and db.doi and da.doi != db.doi):
                continue  # two different DOIs are two publications, however alike
            out.append(_spec("document", "same_document", a, b, "same-title-and-first-author", "medium",
                             {"title": da.title, "first_author": family, "dois": [da.doi, db.doi]}))
    return out


def _versions(docs: dict[str, Doc]) -> list[RelationSpec]:
    groups: dict[str, list[str]] = {}
    for d in docs.values():
        if d.title_key and d.doi:
            groups.setdefault(d.title_key, []).append(d.document_id)
    out: list[RelationSpec] = []
    for members in groups.values():
        for a, b in pairs(members):
            da, db = docs[a], docs[b]
            if da.doi == db.doi:
                continue
            if da.preprint != db.preprint:
                pre, pub = (da, db) if da.preprint else (db, da)
                out.append(_spec("document", "version_of", pre.document_id, pub.document_id, "preprint-and-published", "high",
                                 {"title": pub.title, "preprint_doi": pre.doi, "published_doi": pub.doi}))
            elif not da.preprint:
                out.append(_spec("document", "related_to", a, b, "same-title-different-doi", "low", {"title": da.title, "dois": [da.doi, db.doi]}))
    return out


def _printed_parent_groups(conn: sqlite3.Connection, docs: dict[str, Doc]) -> dict[str, list[str]]:
    """DOIs that many live documents PRINT as their own, whether or not any of them has accepted it. Nothing may accept such a DOI
    for any one document (it names the work they share), so waiting for an acceptance would hide the group for good."""
    members: dict[str, set[str]] = {}
    for r in conn.execute(
        "SELECT value, document_id FROM metadata_candidate WHERE field = 'doi' AND source = 'pdf_text_doi' AND status IN ('proposed', 'accepted') "
        "AND json_extract(evidence_json, '$.shared_by_documents') IS NOT NULL"
    ):
        if r["document_id"] in docs:
            members.setdefault(r["value"], set()).add(r["document_id"])
    return {doi: sorted(ids) for doi, ids in members.items() if len(ids) >= SHARED_DOI_DOCUMENTS}


def _parts_and_collections(docs: dict[str, Doc], parent_groups: dict[str, list[str]], cache: sqlite3.Connection | None) -> list[RelationSpec]:
    out: list[RelationSpec] = []
    # A parent work held in the library is a document of a WHOLE type (a book) with the shared DOI. A member entry that merely has the
    # same DOI is not the parent of its siblings (found by a test: the last member used to be taken for it).
    by_doi = {d.doi: d for d in docs.values() if d.doi and d.kind in WHOLE_TYPES}
    wholes_by_title = {d.title_key: d for d in docs.values() if d.kind in WHOLE_TYPES and d.title_key}

    def parent_title(doi: str) -> str | None:
        if cache is None:
            return None
        row = metacache.get_response(cache, f"crossref:1:doi:{doi}")
        return json.loads(row["payload"]).get("title") if row and row["payload"] else None

    for doi, members in sorted(parent_groups.items()):
        whole = by_doi.get(doi)
        if whole is not None:
            for m in members:
                if m != whole.document_id:
                    out.append(_spec("document", "part_of", m, whole.document_id, "parent-doi", "high", {"parent_doi": doi}))
            continue
        name = parent_title(doi) or f"Documents that print the DOI {doi}"
        out.append(_spec("group", "collection", f"parent-doi:{doi}", "", f"parent-doi:{doi}", "medium",
                         {"name": name, "parent_doi": doi, "reason": f"{len(members)} documents each print this DOI as their own, so it names the work they belong to",
                          "members": len(members)}, members))
    chapters: dict[str, list[Doc]] = {}
    for d in docs.values():
        if d.kind in PART_TYPES and d.container_key:
            chapters.setdefault(d.container_key, []).append(d)
    for key, members in sorted(chapters.items()):
        ids = sorted(m.document_id for m in members)
        whole = wholes_by_title.get(key)
        if whole is not None:
            out.extend(_spec("document", "part_of", m, whole.document_id, "container-title", "high", {"container": members[0].values["container"]}) for m in ids if m != whole.document_id)
        elif len(ids) >= 2:
            out.append(_spec("group", "collection", f"container:{key[:60]}", "", f"container:{key[:60]}", "medium",
                             {"name": members[0].values["container"], "reason": f"{len(ids)} chapters or entries of the same book", "members": len(ids)}, ids))
    return out


def _replacements(conn: sqlite3.Connection, docs: dict[str, Doc]) -> list[RelationSpec]:
    out: list[RelationSpec] = []
    owner = {a: d.document_id for d in docs.values() for a in d.artifacts}
    for r in conn.execute(
        "SELECT l.artifact_id AS old, s.artifact_id AS new, l.relative_path FROM location l JOIN location s ON s.location_id = l.successor_location_id "
        "WHERE l.end_reason = 'replaced' AND l.artifact_id IS NOT NULL AND s.artifact_id IS NOT NULL AND l.artifact_id <> s.artifact_id"):
        if r["old"] in owner and r["new"] in owner:
            same_doi = bool(docs[owner[r["old"]]].doi and docs[owner[r["old"]]].doi == docs[owner[r["new"]]].doi)
            out.append(_spec("artifact", "replaces", r["new"], r["old"], "same-path", "medium" if not same_doi else "high",
                             {"path": r["relative_path"], "same_doi": same_doi}))
    return out


# ------------------------------------------------------------------------------------------------ the run


def detect(
    conn: sqlite3.Connection, index: sqlite3.Connection | None, cache: sqlite3.Connection | None = None, *, progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    say = progress or (lambda _m: None)
    started = time.perf_counter()
    run_id = new_id()
    with transaction(conn):
        conn.execute("UPDATE relation_run SET status = 'interrupted', finished_at = ? WHERE status = 'running'", (utc_now(),))
        conn.execute("INSERT INTO relation_run (run_id, started_at, status, matcher_version) VALUES (?, ?, 'running', ?)", (run_id, utc_now(), MATCHER_VERSION))
    report: dict[str, Any] = {"run_id": run_id, "documents": 0, "fingerprinted": 0, "proposals": {}, "outcomes": {}, "exact_copy_groups": 0}
    status = "interrupted"
    try:
        docs = load_documents(conn)
        report["documents"] = len(docs)
        fingerprints: dict[str, str | None] = {}
        if index is not None:
            say("reading the text of every document")
            canonical_of = {d.canonical: d for d in docs.values() if d.canonical}
            for number, (document, artifact_id) in enumerate(((d, a) for d in docs.values() for a in d.artifacts), start=1):
                if number % 200 == 0:
                    say(f"fingerprinting {number}")
                pages = extraction_pages(index, artifact_id)
                if not pages:
                    continue
                fingerprints[artifact_id] = text_fingerprint(pages)
                if artifact_id in canonical_of:
                    document.first_text = "\n".join(t for n, t in pages if n <= FRONT_PAGES)
        report["fingerprinted"] = sum(1 for f in fingerprints.values() if f)

        specs: list[RelationSpec] = []
        corrections, correction_docs = _corrections(docs)
        supplements = _supplements(docs, correction_docs)
        # A correction (or a supplement) and the paper it is about may share a DOI; sharing one is not being the same document.
        supplement_pairs = {frozenset((s.source_id, s.target_id)) for s in supplements + corrections}
        by_doi, parent_groups = _same_document_by_doi(docs, supplement_pairs, fingerprints)
        identical = _identical_text(docs, fingerprints)
        for doi, members in _printed_parent_groups(conn, docs).items():
            parent_groups[doi] = sorted(set(parent_groups.get(doi, [])) | set(members))
        already = {frozenset((s.source_id, s.target_id)) for s in by_doi + identical if s.level == "document"} | supplement_pairs
        specs += corrections + supplements + by_doi + identical + _same_title_and_author(docs, already) + _versions(docs) + _parts_and_collections(docs, parent_groups, cache) + _replacements(conn, docs)

        with transaction(conn):
            keep: set[tuple[str, str, str, str]] = set()
            for spec in specs:
                source, target = relations.orient(spec.kind, spec.source_id, spec.target_id)
                keep.add((spec.kind, source, target, spec.evidence_key))
                outcome = relations.upsert_candidate(conn, spec, run_id, MATCHER_VERSION)
                report["outcomes"][outcome] = report["outcomes"].get(outcome, 0) + 1
                report["proposals"][spec.kind] = report["proposals"].get(spec.kind, 0) + 1
            stale = relations.mark_stale(conn, keep, run_id)
            if stale:
                report["outcomes"]["stale"] = stale
            if any(v for k, v in report["outcomes"].items() if k not in ("unchanged", "decided")):
                bump_revision(conn)
        report["exact_copy_groups"] = len(organize.exact_copy_groups(conn))
        status = "completed"
    finally:
        report["seconds"] = round(time.perf_counter() - started, 2)
        with transaction(conn):
            conn.execute("UPDATE relation_run SET status = ?, finished_at = ?, stats_json = ? WHERE run_id = ?", (status, utc_now(), json.dumps(report, sort_keys=True), run_id))
    return report


# ------------------------------------------------------------------------------------------------ reading proposals back


def list_candidates(conn: sqlite3.Connection, *, kind: str | None = None, statuses: tuple[str, ...] = ("proposed",), limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    sql, args = f"SELECT * FROM relation_candidate WHERE status IN ({','.join('?' * len(statuses))})", list(statuses)
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    rows = conn.execute(sql + " ORDER BY CASE confidence WHEN 'exact' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, kind, candidate_id", args).fetchall()
    items = [{
        "candidate_id": r["candidate_id"], "level": r["level"], "kind": r["kind"], "source": r["source_id"], "target": r["target_id"] or None,
        "members": json.loads(r["members_json"]) if r["members_json"] else None, "confidence": r["confidence"], "status": r["status"],
        "evidence": json.loads(r["evidence_json"]),
    } for r in rows]
    return items[offset: offset + limit], len(items)


def duplicates_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """The four levels of "the same thing twice", kept apart because each asks for a different decision.

      exact_bytes      one artifact at several paths: nothing to decide, the library has one item
      identical_text   byte-different files with the same text: almost always one publication
      same_publication one DOI (or title and first author) under several documents
      related_work     a preprint and its published version, or the same title under different DOIs
    Plus the documents already merged (several artifacts under one document)."""
    def proposals(kinds: tuple[str, ...], key: str | None = None) -> list[dict[str, Any]]:
        rows = conn.execute(
            f"SELECT * FROM relation_candidate WHERE status = 'proposed' AND kind IN ({','.join('?' * len(kinds))})" + (" AND evidence_key = ?" if key else ""),
            (*kinds, *([key] if key else []))).fetchall()
        return [{"candidate_id": r["candidate_id"], "kind": r["kind"], "source": r["source_id"], "target": r["target_id"], "confidence": r["confidence"],
                 "evidence": json.loads(r["evidence_json"])} for r in rows]

    merged = [{"document_id": r["document_id"], "artifacts": r["n"]} for r in conn.execute(
        "SELECT da.document_id, COUNT(*) AS n FROM document_artifact da JOIN document d ON d.document_id = da.document_id WHERE d.retired_at IS NULL "
        "GROUP BY da.document_id HAVING n > 1 ORDER BY da.document_id")]
    return {
        "exact_bytes": organize.exact_copy_groups(conn),
        "identical_text": [p for p in proposals(("same_document", "equivalent_to")) if p["evidence"].get("text_fingerprint")],
        "same_publication": [p for p in proposals(("same_document",)) if not p["evidence"].get("text_fingerprint")],
        "related_work": proposals(("version_of", "related_to")),
        "documents_with_several_artifacts": merged,
    }
