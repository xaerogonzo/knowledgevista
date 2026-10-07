"""`kv resolve`: turn what each document says about itself, and what a provider says about it, into candidates.

THE STAGES for each document (each commits on its own, so a killed run loses at most the document in flight and
a rerun picks up where it stopped):

  1. LOCAL (always; no network). The extracted text and the file's front matter become candidates: printed DOIs
     classified own / foreign / ambiguous, the title (layout, file metadata), authors, ISBN, arXiv id.
  2. ONLINE (only with `online=True`, which needs the user's switch). For a document whose DOI is not yet settled, a
     DOI candidate is looked up and its record compared with the PDF (`domain/match.py`); a document with no DOI
     candidate gets a title search whose results must verify against the PDF. Only a DOI or a title is sent.
  3. BATCH (only with `accept_safe=True`). The named rule in `services/review.py` accepts the proposals that earned
     `safe`. Nothing else is ever accepted by a run.
  4. METADATA (online, DOI accepted). The record of the ACCEPTED DOI proposes title, authors, year, container,
     publisher, type, volume, issue and pages, compared first with the PDF; a record that does not match proposes nothing.

What a run never does: alter an accepted value (that is `services/review.py`'s rule or a person), act on a provider
outage as if it were an answer ("could not ask" is not "no match", and changes nothing), or hold a database write across
a network call.

A rerun with nothing new changes nothing: proposals are keyed by their evidence, and an unchanged proposal is not written.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from knowledgevista import __version__
from knowledgevista.db.catalog import bump_revision, transaction
from knowledgevista.domain import fields as fieldmod
from knowledgevista.domain import frontmatter, titles
from knowledgevista.domain.candidate import LOCAL_SOURCES, CandidateSpec, evidence_key
from knowledgevista.domain.ids import new_id, utc_now
from knowledgevista.domain.local_evidence import MATCHER_VERSION as LOCAL_MATCHER_VERSION
from knowledgevista.domain.doi_evidence import classify_dois
from knowledgevista.domain.local_evidence import front_text, local_specs, shared_dois
from knowledgevista.domain.match import MATCHER_VERSION as MATCH_VERSION
from knowledgevista.domain.match import Verdict, choose_search_match, verify_work
from knowledgevista.domain.pathkeys import fs_path, join_relative
from knowledgevista.extract.client import ExtractionSession
from knowledgevista.extract.profile import installed_pymupdf_version
from knowledgevista.index import metacache
from knowledgevista.index.store import extraction_pages
from knowledgevista.network.policy import LookupState
from knowledgevista.providers.base import MetadataProvider, ProviderResult
from knowledgevista.services import metadata, review
from knowledgevista.services.extract import _reachable_location, _stat_matches

MATCHER_VERSION = f"{LOCAL_MATCHER_VERSION}+{MATCH_VERSION}"
FRONT_FORMAT = "front1"
#: Stop asking a provider after this many consecutive requests it could not answer.
MAX_CONSECUTIVE_FAILURES = 5
#: DOI candidates looked up per document: the best few, so one cover page cannot cost a dozen requests.
MAX_DOI_LOOKUPS = 3
METADATA_FIELDS = ("title", "authors", "year", "container", "publisher", "type", "volume", "issue", "pages")


@dataclass
class _Document:
    document_id: str
    artifact_id: str
    stem: str | None
    path: str | None  # the file system path to read, if a root is online
    size: int | None
    mtime_ns: int | None
    pages: list[tuple[int, str]] = field(default_factory=list)
    page_count: int = 0
    status: str = "complete"


@dataclass
class _Online:
    provider: MetadataProvider
    stop: str | None = None
    consecutive_failures: int = 0
    failed: set[str] = field(default_factory=set)  # DOIs whose lookup got no answer in THIS run: not asked again within it


def _bump(counts: dict[str, int], key: str, by: int = 1) -> None:
    counts[key] = counts.get(key, 0) + by


# --------------------------------------------------------------------------------------------------- selecting


def _documents(catalog: sqlite3.Connection, index: sqlite3.Connection | None, document_ids: list[str] | None, root_ids: list[str] | None) -> list[_Document]:
    rows = catalog.execute(
        "SELECT d.document_id, da.artifact_id FROM document d JOIN document_artifact da ON da.document_id = d.document_id AND da.canonical = 1 "
        "JOIN artifact a ON a.artifact_id = da.artifact_id WHERE a.content_kind = 'pdf' ORDER BY d.document_id"
    ).fetchall()
    wanted = set(document_ids) if document_ids is not None else None
    out = []
    for row in rows:
        if wanted is not None and row["document_id"] not in wanted:
            continue
        location = _reachable_location(catalog, row["artifact_id"], root_ids)
        path = fs_path(join_relative(location["configured_path"], location["relative_path"])) if location else None
        any_location = location or catalog.execute(
            "SELECT relative_path FROM location WHERE artifact_id = ? ORDER BY ended_at IS NOT NULL, last_seen DESC LIMIT 1", (row["artifact_id"],)).fetchone()
        stem = Path(any_location["relative_path"]).stem if any_location else None
        out.append(_Document(row["document_id"], row["artifact_id"], stem, path, location["size"] if location else None,
                             location["mtime_ns"] if location else None))
    return out


def _library_shared_dois(catalog: sqlite3.Connection, index: sqlite3.Connection | None) -> dict[str, int]:
    """DOIs many documents of the library each print as their own (a book's, an issue's). Read from the whole library,
    never from the documents this run happens to be resolving, so a `--document` run and a full run agree."""
    if index is None:
        return {}
    evidences = []
    for row in catalog.execute(
        "SELECT da.artifact_id FROM document_artifact da JOIN artifact a ON a.artifact_id = da.artifact_id "
        "WHERE da.canonical = 1 AND a.content_kind = 'pdf' ORDER BY da.artifact_id"
    ):
        pages = extraction_pages(index, row["artifact_id"])
        if pages:
            evidences.append(classify_dois(pages, len(pages)))
    return shared_dois(evidences)


# --------------------------------------------------------------------------------------------------- front matter


def _front_facts(cache: sqlite3.Connection | None, session: ExtractionSession | None, doc: _Document, report: dict, force: bool) -> frontmatter.FrontFacts | None:
    version = installed_pymupdf_version()
    front = report["front"]
    if cache is None or version is None:
        _bump(front, "unavailable")
        return None
    tag = f"{version}+{FRONT_FORMAT}"
    row = None if force else metacache.get_front(cache, doc.artifact_id, tag)
    if row is None:
        if session is None or doc.path is None:
            _bump(front, "unavailable")
            return None
        if not _stat_matches(doc.path, doc.size, doc.mtime_ns):
            _bump(front, "changed_since_scan")
            return None
        result = session.front_matter(doc.path)
        if not _stat_matches(doc.path, doc.size, doc.mtime_ns):
            _bump(front, "changed_since_scan")
            return None
        payload = json.dumps({"info": result.info, "xmp": result.xmp, "lines": result.lines, "page_size": result.page_size}) if result.ok else None
        metacache.put_front(cache, doc.artifact_id, tag, ok=result.ok, payload=payload, error=result.error)
        _bump(front, "read" if result.ok else "failed")
        if not result.ok:
            return None
        data = json.loads(payload)
    else:
        if not row["ok"]:
            _bump(front, "failed_before")
            return None
        _bump(front, "cached")
        data = json.loads(row["payload"])
    return frontmatter.read_front(data["info"], data["xmp"], data["lines"], data["page_size"], doc.stem)


# --------------------------------------------------------------------------------------------------- storing


def _provider_checks(catalog: sqlite3.Connection, document_id: str) -> dict[str, str]:
    """What earlier online runs concluded about this document's DOIs: doi -> contradicted | no_match | <verdict level>."""
    out = {}
    for row in catalog.execute("SELECT value, evidence_json FROM metadata_candidate WHERE document_id = ? AND field = 'doi' AND source = 'crossref'", (document_id,)):
        evidence = json.loads(row["evidence_json"])
        out[row["value"]] = "no_match" if evidence.get("state") == "no_match" else evidence.get("verdict", "")
    return out


def _apply(catalog: sqlite3.Connection, doc: _Document, specs: list[CandidateSpec], run_id: str, *, stale_sources: tuple[str, ...] | None) -> dict[str, int]:
    """Store a document's proposals in one transaction; mark the local ones this run did not find again stale."""
    counts: dict[str, int] = {}
    with transaction(catalog):
        for spec in specs:
            _bump(counts, metadata.upsert_candidate(catalog, doc.document_id, doc.artifact_id, spec, run_id, MATCHER_VERSION))
        if stale_sources:
            stale = metadata.mark_stale(catalog, doc.document_id, stale_sources, {(s.field, s.evidence_key) for s in specs}, run_id)
            if stale:
                counts["stale"] = stale
        if any(v for k, v in counts.items() if k not in ("unchanged", "decided")):
            bump_revision(catalog)
    return counts


# --------------------------------------------------------------------------------------------------- online


def _record_summary(work: dict[str, Any]) -> dict[str, Any]:
    return {"title": work.get("title"), "year": work.get("year"), "type": work.get("type"), "container": work.get("container"),
            "preprint": bool(work.get("preprint")), "authors": [a.get("family") or a.get("name") for a in (work.get("authors") or [])[:4]]}


def _note(report: dict, online: _Online, result: ProviderResult) -> None:
    o = report["online"]
    _bump(o["states"], result.state.value)
    if result.from_cache:
        _bump(o, "cache_hits")
    elif result.sent:
        _bump(o, "requests")
    if result.state.could_not_answer and result.state != LookupState.NOT_ATTEMPTED:
        online.consecutive_failures += 1
    elif not result.state.could_not_answer:
        online.consecutive_failures = 0
    if result.state == LookupState.UNAUTHORIZED:
        online.stop = "the provider refused the requests (unauthorized)"
    elif result.state == LookupState.RATE_LIMITED:
        online.stop = f"the provider asked us to wait {result.retry_after:g}s" if result.retry_after else "the provider is rate limiting us"
    elif result.state == LookupState.NOT_ATTEMPTED:
        online.stop = result.detail or "no request could be made"
    elif online.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        online.stop = f"{online.consecutive_failures} requests in a row got no answer ({result.state.value})"
    elif result.state == LookupState.OFFLINE and online.consecutive_failures >= 2:
        online.stop = "the network is not reachable"


def _doi_check_spec(doi: str, result: ProviderResult, verdict: Verdict | None) -> CandidateSpec | None:
    """The provider's verdict on a printed DOI, as a candidate of its own (so the local pass never has to rewrite it)."""
    key = evidence_key("doi-check", doi, "crossref")
    if result.state == LookupState.NO_MATCH:
        return CandidateSpec("doi", doi, "resolved", "crossref", key, {"provider": "crossref", "state": "no_match", "client": "1"},
                             confidence="low", review="required", priority=35, status="set_aside")
    if result.state != LookupState.SUCCESS or verdict is None or result.work is None:
        return None
    level = verdict.level
    evidence = {"provider": "crossref", "state": "success", "verdict": level, "components": verdict.components, "reasons": verdict.reasons,
                "record": _record_summary(result.work), "matcher": MATCH_VERSION}
    if level == "exact":
        return CandidateSpec("doi", doi, "resolved", "crossref", key, evidence, classification="own", confidence="exact", review="safe", priority=5)
    if level == "strong":
        return CandidateSpec("doi", doi, "resolved", "crossref", key, evidence, classification="own", confidence="high", priority=10)
    if level == "contradicted":
        return CandidateSpec("doi", doi, "resolved", "crossref", key, evidence, classification="foreign", confidence="low", priority=35, status="set_aside")
    return CandidateSpec("doi", doi, "resolved", "crossref", key, evidence, classification="ambiguous", confidence="ambiguous", priority=20)


def _search_is_safe(verdict: Verdict) -> bool:
    """A title-search match is safe only when more than the title agrees: the record's authors are on the first pages too."""
    found, checked = verdict.components.get("authors_found", 0), verdict.components.get("authors_checked", 0)
    return verdict.level == "exact" and checked >= 1 and found >= min(2, checked)


def _best_title(catalog: sqlite3.Connection, document_id: str) -> str | None:
    rows = catalog.execute(
        "SELECT value, source FROM metadata_candidate WHERE document_id = ? AND field = 'title' AND status IN ('proposed', 'accepted') "
        "AND source IN ('layout_title', 'pdf_xmp_title', 'pdf_info_title') ORDER BY CASE source WHEN 'layout_title' THEN 0 WHEN 'pdf_xmp_title' THEN 1 ELSE 2 END, "
        "confidence, value", (document_id,)).fetchall()
    return rows[0]["value"] if rows else None


def _local_titles(catalog: sqlite3.Connection, document_id: str) -> list[str]:
    return [r[0] for r in catalog.execute(
        "SELECT DISTINCT value FROM metadata_candidate WHERE document_id = ? AND field = 'title' AND source IN ('layout_title', 'pdf_xmp_title', 'pdf_info_title') "
        "AND status IN ('proposed', 'accepted', 'stale')", (document_id,))]


def _online_doi_stage(catalog: sqlite3.Connection, doc: _Document, online: _Online, run_id: str, report: dict, text: str) -> tuple[dict[str, dict[str, Any]], bool]:
    """Check printed DOIs and, failing any, search by title. Returns the provider records seen, by DOI (for stage 4), and
    whether a provider verdict was stored (so the local proposals must be recomputed with it before any batch rule runs)."""
    works: dict[str, dict[str, Any]] = {}
    wrote = False
    values = metadata.get_values(catalog, doc.document_id)
    if "doi" in values:
        return works, wrote
    local_titles = _local_titles(catalog, doc.document_id)
    candidates = catalog.execute(
        "SELECT value, evidence_json FROM metadata_candidate WHERE document_id = ? AND field = 'doi' AND status = 'proposed' "
        "AND source IN ('pdf_text_doi', 'pdf_metadata_doi') AND classification IN ('own', 'ambiguous') ORDER BY priority, confidence, value LIMIT ?",
        (doc.document_id, MAX_DOI_LOOKUPS)).fetchall()
    usable = 0
    for row in candidates:
        if online.stop:
            return works, wrote
        result = online.provider.lookup_doi(row["value"])
        _note(report, online, result)
        _bump(report["online"], "dois_checked")
        if result.state.could_not_answer:
            online.failed.add(row["value"])
        verdict = verify_work(result.work, text, local_titles) if result.state == LookupState.SUCCESS and result.work else None
        if verdict:
            _bump(report["online"]["verdicts"], verdict.level)
            works[row["value"]] = result.work
        spec = _doi_check_spec(row["value"], result, verdict)
        if spec is not None:
            _apply(catalog, doc, [spec], run_id, stale_sources=None)
            wrote = True
        # A DOI is usable if the provider confirmed it, or could not answer (a failure is not evidence). A record that only
        # weakly matches, contradicts, or does not exist is not, and the document is then looked for by its title.
        if (verdict is not None and verdict.confirms) or (verdict is None and result.state.could_not_answer):
            usable += 1
    if usable or online.stop:
        return works, wrote
    # No printed DOI can be used: look for the document by its title.
    query = _best_title(catalog, doc.document_id)
    if not query:
        return works, wrote
    result = online.provider.search_title(query)
    _note(report, online, result)
    _bump(report["online"], "title_searches")
    if result.state != LookupState.SUCCESS:
        return works, wrote
    match = choose_search_match(result.items, query, text, local_titles)
    specs = []
    if match.chosen is not None:
        work = match.chosen
        _bump(report["online"], "title_matches")
        works[work["doi"]] = work
        safe = _search_is_safe(match.verdict)
        specs.append(CandidateSpec(
            "doi", work["doi"], "resolved", "crossref_title_search", evidence_key("doi-search", work["doi"]),
            {"query": query, "verdict": match.verdict.level, "components": match.verdict.components, "reasons": match.verdict.reasons + match.reasons,
             "record": _record_summary(work), "also_matched": [w["doi"] for w in match.others], "score": work.get("score"), "matcher": MATCH_VERSION},
            classification="own", confidence="high", review="safe" if safe else "required", priority=15))
    elif match.ambiguous:
        _bump(report["online"], "title_ambiguous")
        for work in match.others:
            specs.append(CandidateSpec(
                "doi", work["doi"], "resolved", "crossref_title_search", evidence_key("doi-search", work["doi"]),
                {"query": query, "reasons": match.reasons, "record": _record_summary(work), "score": work.get("score"), "matcher": MATCH_VERSION},
                classification="ambiguous", confidence="ambiguous", priority=20))
    if specs:
        _apply(catalog, doc, specs, run_id, stale_sources=None)
    return works, wrote


def _work_specs(work: dict[str, Any], verdict: Verdict) -> list[CandidateSpec]:
    raw = {"title": work.get("title"), "year": work.get("year"), "container": work.get("container"), "publisher": work.get("publisher"),
           "type": work.get("type"), "volume": work.get("volume"), "issue": work.get("issue"), "pages": work.get("pages"),
           "authors": [{"family": a.get("family"), "given": a.get("given"), "name": a.get("name")} for a in (work.get("authors") or [])]}
    confidence = {"exact": "exact", "strong": "high"}.get(verdict.level, "medium")
    specs = []
    for field_ in METADATA_FIELDS:
        if not raw.get(field_):
            continue
        try:
            value = fieldmod.normalise(field_, raw[field_])
        except ValueError:
            continue  # a provider's value that does not normalise is dropped, not stored raw
        specs.append(CandidateSpec(
            field_, value, "resolved", "crossref", evidence_key("crossref", field_, value, work["doi"]),
            {"doi": work["doi"], "verdict": verdict.level, "years": work.get("years"), "record_is_preprint": bool(work.get("preprint")),
             "reasons": verdict.reasons, "matcher": MATCH_VERSION},
            confidence=confidence, review="safe" if verdict.level == "exact" else "required", priority=25))
    return specs


def _online_metadata_stage(catalog: sqlite3.Connection, doc: _Document, online: _Online, run_id: str, report: dict, text: str,
                           works: dict[str, dict[str, Any]]) -> None:
    accepted = metadata.get_values(catalog, doc.document_id).get("doi")
    if accepted is None or online.stop:
        return
    doi = accepted["value"]
    work = works.get(doi)
    if work is None:
        if doi in online.failed:
            return  # it just got no answer in this run
        result = online.provider.lookup_doi(doi)
        _note(report, online, result)
        _bump(report["online"], "dois_checked")
        if result.state == LookupState.NO_MATCH:
            # Accepted earlier (by local evidence, before a provider was asked) and the provider has never heard of it: a misread
            # DOI, or one registered elsewhere. Never undone automatically; said, so a person looks.
            _bump(report["online"], "accepted_doi_unknown_to_provider")
            report["problems"].append({"document_id": doc.document_id, "message": f"the accepted DOI {doi} is not known to the provider "
                                       "(a misread DOI, or one registered elsewhere); check it: kv explain"})
            return
        if result.state != LookupState.SUCCESS:
            online.failed.add(doi)
            return
        work = result.work
    verdict = verify_work(work, text, _local_titles(catalog, doc.document_id))
    if verdict.level == "contradicted":
        _bump(report["online"], "accepted_doi_contradicted")
        report["problems"].append({"document_id": doc.document_id, "message": f"the accepted DOI {doi}'s record does not match the document; no metadata was proposed from it"})
        return
    if verdict.level == "weak" and accepted["accepted_by"].startswith("rule:"):
        # Accepted on local evidence alone, and the provider's record fits the pages only weakly (a PDF that opens on its
        # reference list, a wrong DOI). Not undone; said, so a person looks.
        _bump(report["online"], "accepted_doi_weak")
        report["problems"].append({"document_id": doc.document_id, "message": f"the accepted DOI {doi} was accepted by a rule and its provider record "
                                   "only weakly matches the document's first pages; check it: kv explain"})
    _bump(report["online"], "metadata_documents")
    _apply(catalog, doc, _work_specs(work, verdict), run_id, stale_sources=None)


# --------------------------------------------------------------------------------------------------- the run


def resolve_library(
    catalog: sqlite3.Connection,
    index: sqlite3.Connection | None,
    cache: sqlite3.Connection | None,
    *,
    provider: MetadataProvider | None = None,
    online: bool = False,
    accept_safe: bool = False,
    session: ExtractionSession | None = None,
    read_front: bool = True,
    force_front: bool = False,
    document_ids: list[str] | None = None,
    root_ids: list[str] | None = None,
    limit: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the stages over the library's PDFs and return a report. See the module docstring for what each stage may do."""
    if online and provider is None:
        raise ValueError("online resolution needs a provider")
    say = progress or (lambda _m: None)
    started = time.perf_counter()
    report: dict[str, Any] = {
        "run_id": new_id(), "documents": 0, "not_extracted": 0, "local": {}, "front": {}, "doi_classes": {}, "accepted": {"total": 0},
        "skipped_by_rule": [], "problems": [], "notes": [],
        "online": {"enabled": online, "requests": 0, "cache_hits": 0, "states": {}, "verdicts": {}, "stopped": None},
    }
    run_id = report["run_id"]
    with transaction(catalog):
        catalog.execute("UPDATE resolve_run SET status = 'interrupted', finished_at = ? WHERE status = 'running'", (utc_now(),))
        catalog.execute("INSERT INTO resolve_run (run_id, started_at, status, app_version, matcher_version, online, accept_safe) VALUES (?, ?, 'running', ?, ?, ?, ?)",
                        (run_id, utc_now(), __version__, MATCHER_VERSION, int(online), int(accept_safe)))
    state = _Online(provider) if online and provider else None
    status = "interrupted"
    try:
        documents = _documents(catalog, index, document_ids, root_ids)
        if limit is not None:
            documents = documents[:limit]
        shared = _library_shared_dois(catalog, index)
        report["shared_dois"] = shared
        for number, doc in enumerate(documents, start=1):
            if number == 1 or number % 50 == 0:
                say(f"resolving {number}/{len(documents)}")
            report["documents"] += 1
            if index is None:
                doc.pages, doc.page_count = [], 0
            else:
                doc.pages = extraction_pages(index, doc.artifact_id)
                doc.page_count = len(doc.pages)
            if not doc.pages:
                _bump(report, "not_extracted")
                continue
            facts = _front_facts(cache, session, doc, report, force_front) if read_front else None
            result = local_specs(doc.pages, doc.page_count, facts, doc.stem, _provider_checks(catalog, doc.document_id), shared)
            for note in result.notes:
                _bump(report["front"], note)
            counts = _apply(catalog, doc, result.specs, run_id, stale_sources=LOCAL_SOURCES)
            for key, value in counts.items():
                _bump(report["local"], key, value)
            # Counted from the proposals, not the raw classification: a DOI demoted because the library shares it is not "own".
            doi_classes = {s.classification for s in result.specs if s.field == "doi" and s.status == "proposed"}
            _bump(report["doi_classes"], "own" if "own" in doi_classes else ("ambiguous" if doi_classes else "none"))
            text = front_text(doc.pages)
            works: dict[str, dict[str, Any]] = {}
            if state is not None and not state.stop:
                works, wrote = _online_doi_stage(catalog, doc, state, run_id, report, text)
                if wrote:  # a provider verdict changes what the local evidence is worth: recompute before any batch rule runs
                    again = local_specs(doc.pages, doc.page_count, facts, doc.stem, _provider_checks(catalog, doc.document_id), shared)
                    _apply(catalog, doc, again.specs, run_id, stale_sources=LOCAL_SOURCES)
            if accept_safe:
                _tally(report, review.accept_safe_for_document(catalog, doc.document_id))
            if state is not None and not state.stop:
                _online_metadata_stage(catalog, doc, state, run_id, report, text, works)
                if accept_safe:
                    _tally(report, review.accept_safe_for_document(catalog, doc.document_id))
        status = "completed"
    finally:
        if state is not None:
            report["online"]["stopped"] = state.stop
        report["seconds"] = round(time.perf_counter() - started, 2)
        with transaction(catalog):
            catalog.execute("UPDATE resolve_run SET status = ?, finished_at = ?, stats_json = ? WHERE run_id = ?",
                            (status, utc_now(), json.dumps(report, sort_keys=True, default=str), run_id))
    return report


def _tally(report: dict, batch: review.BatchResult) -> None:
    for accepted in batch.accepted:
        if accepted.changed:
            _bump(report["accepted"], accepted.field)
            _bump(report["accepted"], "total")
    report["skipped_by_rule"] += batch.skipped[:5]


# --------------------------------------------------------------------------------------------------- listing requests


def planned_requests(catalog: sqlite3.Connection, cache: sqlite3.Connection | None, provider_name: str = "crossref") -> list[dict[str, Any]]:
    """What `--online` would send, from the candidates already in the catalog: nothing is sent and nothing is written.
    Each item says what leaves the machine (a DOI, or a title) and whether the cache already has the answer."""
    from knowledgevista.providers.crossref import CLIENT_VERSION

    def cached(key: str) -> bool:
        return cache is not None and metacache.get_response(cache, key) is not None

    plan = []
    docs = catalog.execute("SELECT DISTINCT document_id FROM metadata_candidate ORDER BY document_id").fetchall()
    for doc in docs:
        document_id = doc[0]
        if catalog.execute("SELECT 1 FROM metadata_value WHERE document_id = ? AND field = 'doi'", (document_id,)).fetchone():
            row = catalog.execute("SELECT value FROM metadata_value WHERE document_id = ? AND field = 'doi'", (document_id,)).fetchone()
            doi_rows = [row]
            have_doi = True
        else:
            doi_rows = catalog.execute(
                "SELECT value FROM metadata_candidate WHERE document_id = ? AND field = 'doi' AND status = 'proposed' "
                "AND source IN ('pdf_text_doi', 'pdf_metadata_doi') AND classification IN ('own', 'ambiguous') ORDER BY priority, confidence, value LIMIT ?",
                (document_id, MAX_DOI_LOOKUPS)).fetchall()
            have_doi = False
        for row in doi_rows:
            key = f"{provider_name}:{CLIENT_VERSION}:doi:{row['value']}"
            plan.append({"document_id": document_id, "sends": "doi", "value": row["value"], "cached": cached(key)})
        if not doi_rows and not have_doi:
            title = _best_title(catalog, document_id)
            if title and len(titles.alnum_key(title)) >= titles.MIN_DECISIVE_LENGTH:
                key = f"{provider_name}:{CLIENT_VERSION}:title:5:{titles.alnum_key(titles.clean(title)[:300])[:200]}"
                plan.append({"document_id": document_id, "sends": "title", "value": titles.clean(title)[:300], "cached": cached(key)})
    return plan
