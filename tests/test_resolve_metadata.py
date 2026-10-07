"""`kv resolve` as a service: real PDFs, the real extraction worker, a real catalog, and a scripted provider.

The properties under test are the plan's acceptance for this milestone, stated as behaviour: a rerun changes nothing; a
DOI only in the references is never proposed; a printed DOI the provider contradicts is set aside, not accepted; a provider
outage alters no accepted value; nothing is sent unless asked; and what is sent is only a DOI or a title.
"""

from __future__ import annotations

import json

import pdfbuilders as b
import pytest
from support import make_env, tree_hashes

from knowledgevista.domain import titles
from knowledgevista.extract.client import ExtractionSession
from knowledgevista.index import metacache, store
from knowledgevista.network.policy import LookupState
from knowledgevista.providers.base import ProviderResult
from knowledgevista.services import metadata, resolve_metadata, review
from knowledgevista.services.extract import extract_library
from knowledgevista.services.resolve_metadata import planned_requests, resolve_library

TITLE = "A Synthetic Study of Imaginary Lattices"
AUTHORS = "A. Examplar and B. Placeholder"
JOURNAL = "Cite This: J. Invented Results 2021, 12, 54-58"
DOI = "10.5555/kv.resolve.0001"
DOI2 = "10.5555/kv.resolve.0002"


def crossref_work(doi=DOI, title=TITLE, families=("Examplar", "Placeholder"), year=2021, **changes) -> dict:
    work = {"provider": "crossref", "doi": doi, "title": title, "subtitle": None,
            "authors": [{"family": f, "given": "A.", "name": None} for f in families], "year": year,
            "years": {"issued": year, "print": year, "online": year}, "container": "Journal of Invented Results",
            "publisher": "Invented Press", "type": "journal-article", "volume": "12", "issue": "3", "pages": "54-58",
            "preprint": False, "score": 50.0}
    work.update(changes)
    return work


class FakeProvider:
    """Scripted answers; records every request, because what was SENT is part of what is tested."""

    name, client_version = "crossref", "1"

    def __init__(self, works=None, searches=None, fail: LookupState | None = None, retry_after=None):
        self.works, self.searches, self.fail, self.retry_after, self.calls = works or {}, searches or {}, fail, retry_after, []

    def _failure(self):
        return ProviderResult(self.fail, detail=f"scripted {self.fail.value}", retry_after=self.retry_after) if self.fail else None

    def lookup_doi(self, doi):
        self.calls.append(("doi", doi))
        if self._failure():
            return self._failure()
        if doi in self.works:
            return ProviderResult(LookupState.SUCCESS, work=self.works[doi])
        return ProviderResult(LookupState.NO_MATCH, detail="scripted: unknown")

    def search_title(self, title, *, rows=5):
        self.calls.append(("title", title))
        if self._failure():
            return self._failure()
        items = self.searches.get(title) or self.searches.get("*")
        return ProviderResult(LookupState.SUCCESS, items=items) if items else ProviderResult(LookupState.NO_MATCH, detail="scripted: none")


class Library:
    def __init__(self, tmp_path, pdfs: dict[str, dict]):
        self.env = make_env(tmp_path, {})
        for name, kwargs in pdfs.items():
            builder = kwargs.pop("builder", b.native_pdf)
            builder(self.env.lib / name, **kwargs)
        self.env.scan()
        self.conn = self.env.conn
        self.index = store.open_index(tmp_path / "idx" / "extractions.sqlite", create=True)
        self.cache = metacache.open_metacache(tmp_path / "idx" / "metadata.sqlite", create=True)
        self.session = ExtractionSession()
        extract_library(self.conn, self.index, session=self.session)

    def resolve(self, **kwargs) -> dict:
        return resolve_library(self.conn, self.index, self.cache, session=self.session, **kwargs)

    def doc(self, name) -> str:
        return self.env.one("SELECT da.document_id FROM location l JOIN document_artifact da ON da.artifact_id = l.artifact_id "
                            "WHERE l.relative_path = ? AND l.ended_at IS NULL", name)

    def values(self, name) -> dict:
        return {f: v["value"] for f, v in metadata.get_values(self.conn, self.doc(name)).items()}

    def candidates(self, name, field=None, source=None) -> list:
        rows = [r for r in metadata.get_candidates(self.conn, self.doc(name))]
        return [r for r in rows if (field is None or r["field"] == field) and (source is None or r["source"] == source)]

    def dump(self) -> dict:
        """Everything a rerun must leave alone, with the run bookkeeping left out."""
        return {
            "candidates": sorted(tuple(r) for r in self.conn.execute(
                "SELECT document_id, field, value, source, status, review, confidence, classification, evidence_json, decided_by, "
                "matcher_version, created_at, updated_at, first_run_id FROM metadata_candidate")),
            "values": sorted(tuple(r) for r in self.conn.execute("SELECT * FROM metadata_value")),
            "history": self.conn.execute("SELECT COUNT(*) FROM metadata_history").fetchone()[0],
            "revision": self.env.revision(),
        }

    def close(self):
        self.session.close()
        self.index.close()
        self.cache.close()


@pytest.fixture
def make_library(tmp_path):
    made = []

    def build(pdfs):
        library = Library(tmp_path, pdfs)
        made.append(library)
        return library

    yield build
    for library in made:
        library.close()


def article(**kw) -> dict:
    return {"title": TITLE, "authors": AUTHORS, "journal": JOURNAL, "doi": DOI, **kw}


# ------------------------------------------------------------------------------------------------ the local pass


def test_the_local_pass_proposes_what_the_document_supports_and_accepts_nothing(make_library):
    lib = make_library({"a.pdf": article()})
    report = lib.resolve()
    assert report["documents"] == 1 and report["doi_classes"] == {"own": 1} and not report["online"]["enabled"]
    (doi,) = lib.candidates("a.pdf", "doi")
    assert (doi["value"], doi["source"], doi["classification"], doi["review"], doi["status"]) == (DOI, "pdf_text_doi", "own", "safe", "proposed")
    assert {c["source"] for c in lib.candidates("a.pdf", "title")} == {"layout_title", "pdf_info_title"}
    assert all(c["review"] == "safe" and c["value"] == TITLE for c in lib.candidates("a.pdf", "title"))
    (authors,) = lib.candidates("a.pdf", "authors")
    assert authors["review"] == "required"
    assert lib.values("a.pdf") == {}  # proposals only: nothing was accepted by running resolve


def test_accept_safe_accepts_the_safe_proposals_by_a_named_rule_and_not_the_authors(make_library):
    lib = make_library({"a.pdf": article()})
    report = lib.resolve(accept_safe=True)
    assert lib.values("a.pdf") == {"doi": DOI, "title": TITLE}
    assert report["accepted"]["total"] == 2
    values = metadata.get_values(lib.conn, lib.doc("a.pdf"))
    assert {v["accepted_by"] for v in values.values()} == {"rule:safe_batch_v1"}
    assert [h["actor"] for h in metadata.history(lib.conn, lib.doc("a.pdf"))] == ["resolver", "resolver"]


def test_a_rerun_with_nothing_new_changes_nothing_at_all(make_library):
    lib = make_library({"a.pdf": article(), "b.pdf": article(title="Another Study Of Entirely Different Matters", doi=DOI2)})
    lib.resolve(accept_safe=True)
    before = lib.dump()
    runs_before = lib.env.one("SELECT COUNT(*) FROM resolve_run")
    report = lib.resolve(accept_safe=True)
    assert lib.dump() == before  # candidates, values, history, timestamps and the catalog revision: all identical
    assert set(report["local"]) <= {"unchanged", "decided"} and report["accepted"]["total"] == 0
    assert lib.env.one("SELECT COUNT(*) FROM resolve_run") == runs_before + 1  # the run itself is history


def test_a_doi_only_in_the_references_is_never_proposed_or_accepted(make_library):
    lib = make_library({"a.pdf": {"builder": b.doi_only_in_references_pdf}})
    lib.resolve(accept_safe=True)
    assert lib.candidates("a.pdf", "doi") == [] and "doi" not in lib.values("a.pdf")
    assert lib.candidates("a.pdf", "title")  # it still has a title to offer


def test_a_document_without_extracted_text_is_counted_and_proposes_nothing(make_library, tmp_path):
    lib = make_library({"a.pdf": article()})
    lib.index.execute("DELETE FROM extraction")
    report = lib.resolve(accept_safe=True)
    assert report["not_extracted"] == 1 and lib.candidates("a.pdf") == []


def test_a_scan_has_no_text_to_propose_from_and_says_so(make_library):
    lib = make_library({"s.pdf": {"builder": b.scanned_pdf}})
    report = lib.resolve(accept_safe=True)
    assert lib.candidates("s.pdf") == [] and any("no text layer" in k for k in report["front"])


def test_proposals_whose_evidence_disappears_go_stale_and_accepted_values_are_untouched(make_library):
    lib = make_library({"a.pdf": article()})
    lib.resolve(accept_safe=True)
    artifact = lib.env.one("SELECT artifact_id FROM location WHERE relative_path = 'a.pdf'")
    row = store.get_extraction(lib.index, artifact)
    pages = [store.PageRecord(n, text.replace(DOI, "REDACTED"), None, "text_native") for n, text in store.extraction_pages(lib.index, artifact)]
    store.replace_extraction(lib.index, store.ExtractionRecord(
        artifact_id=artifact, source=row["source"], extractor=row["extractor"], extractor_version=row["extractor_version"],
        format_version=row["format_version"], options_hash=row["options_hash"], profile_id=row["profile_id"], status="complete",
        page_count=row["page_count"], chars=row["chars"], legacy_scanned=False, first_pages_doi=None, first_text=None, pages=pages))
    lib.resolve(accept_safe=True)
    (doi,) = lib.candidates("a.pdf", "doi")
    assert doi["status"] == "accepted"  # a decision is not reopened because the evidence moved
    assert lib.values("a.pdf")["doi"] == DOI  # and the accepted value still stands


def test_stale_when_the_evidence_is_gone_and_nothing_was_decided(make_library):
    lib = make_library({"a.pdf": article()})
    lib.resolve()
    artifact = lib.env.one("SELECT artifact_id FROM location WHERE relative_path = 'a.pdf'")
    row = store.get_extraction(lib.index, artifact)
    pages = [store.PageRecord(n, text.replace(DOI, "REDACTED"), None, "text_native") for n, text in store.extraction_pages(lib.index, artifact)]
    store.replace_extraction(lib.index, store.ExtractionRecord(
        artifact_id=artifact, source=row["source"], extractor=row["extractor"], extractor_version=row["extractor_version"],
        format_version=row["format_version"], options_hash=row["options_hash"], profile_id=row["profile_id"], status="complete",
        page_count=row["page_count"], chars=row["chars"], legacy_scanned=False, first_pages_doi=None, first_text=None, pages=pages))
    report = lib.resolve()
    (doi,) = lib.candidates("a.pdf", "doi")
    assert doi["status"] == "stale" and report["local"]["stale"] >= 1


def test_a_rejected_proposal_stays_rejected_and_the_rule_does_not_bring_it_back(make_library):
    lib = make_library({"a.pdf": article()})
    lib.resolve()
    (doi,) = lib.candidates("a.pdf", "doi")
    metadata.reject_candidate(lib.conn, doi["candidate_id"])
    lib.resolve(accept_safe=True)
    assert "doi" not in lib.values("a.pdf") and lib.candidates("a.pdf", "doi")[0]["status"] == "rejected"


def test_a_value_the_user_locked_is_never_changed_by_a_run(make_library):
    lib = make_library({"a.pdf": article()})
    metadata.set_value(lib.conn, lib.doc("a.pdf"), "title", "The Title I Chose Myself, Which Differs")
    lib.resolve(accept_safe=True)
    assert lib.values("a.pdf")["title"] == "The Title I Chose Myself, Which Differs"
    assert lib.values("a.pdf")["doi"] == DOI  # other fields still resolve


def test_the_source_pdf_is_never_modified(make_library, tmp_path):
    lib = make_library({"a.pdf": article()})
    before = tree_hashes(lib.env.lib)
    lib.resolve(accept_safe=True, provider=FakeProvider({DOI: crossref_work()}), online=True)
    assert tree_hashes(lib.env.lib) == before


def test_without_the_online_flag_no_provider_is_ever_asked(make_library):
    lib = make_library({"a.pdf": article()})
    provider = FakeProvider({DOI: crossref_work()})
    lib.resolve(accept_safe=True, provider=provider, online=False)
    assert provider.calls == []


def test_front_matter_is_read_once_and_then_served_from_the_cache(make_library):
    lib = make_library({"a.pdf": article()})
    first = lib.resolve()
    again = lib.resolve()
    assert first["front"].get("read") == 1 and again["front"].get("cached") == 1 and "read" not in again["front"]


def test_the_run_is_recorded_with_its_rules_and_ends_completed(make_library):
    lib = make_library({"a.pdf": article()})
    report = lib.resolve(accept_safe=True)
    row = lib.env.rows("SELECT * FROM resolve_run WHERE run_id = ?", report["run_id"])[0]
    assert (row["status"], row["online"], row["accept_safe"], row["matcher_version"]) == ("completed", 0, 1, resolve_metadata.MATCHER_VERSION)
    assert json.loads(row["stats_json"])["documents"] == 1
    first = lib.env.one("SELECT first_run_id FROM metadata_candidate LIMIT 1")
    assert first == report["run_id"]


# ------------------------------------------------------------------------------------------------ online


def test_a_provider_record_that_matches_the_pdf_confirms_the_doi_and_proposes_the_metadata(make_library):
    lib = make_library({"a.pdf": article()})
    provider = FakeProvider({DOI: crossref_work()})
    report = lib.resolve(online=True, provider=provider, accept_safe=True)
    assert provider.calls == [("doi", DOI)]  # one request: the record is reused for the metadata stage
    check = [c for c in lib.candidates("a.pdf", "doi") if c["source"] == "crossref"][0]
    assert (check["confidence"], check["review"], check["classification"]) == ("exact", "safe", "own")
    assert json.loads(check["evidence_json"])["verdict"] == "exact"
    values = lib.values("a.pdf")
    assert values["doi"] == DOI and values["title"] == TITLE and values["year"] == "2021"
    assert (values["container"], values["publisher"], values["type"], values["volume"], values["issue"], values["pages"]) == (
        "Journal of Invented Results", "Invented Press", "journal-article", "12", "3", "54-58")
    authors = json.loads(values["authors"])
    assert [a["family"] for a in authors] == ["Examplar", "Placeholder"]
    assert report["online"]["verdicts"] == {"exact": 1} and report["online"]["requests"] == 1


def test_a_record_for_a_different_paper_sets_the_doi_aside_and_proposes_nothing_from_it(make_library):
    other = crossref_work(title="Crystal Structures of Imaginary Perovskite Oxides at Pressure", families=("Differentperson", "Anotherone"))
    lib = make_library({"a.pdf": article()})
    provider = FakeProvider({DOI: other})
    lib.resolve(online=True, provider=provider, accept_safe=True)
    assert "doi" not in lib.values("a.pdf") and "year" not in lib.values("a.pdf")
    local = lib.candidates("a.pdf", "doi", "pdf_text_doi")[0]
    assert (local["status"], local["classification"], local["review"]) == ("set_aside", "foreign", "required")
    check = lib.candidates("a.pdf", "doi", "crossref")[0]
    assert (check["status"], check["classification"]) == ("set_aside", "foreign")
    assert not any(c["source"] == "crossref" and c["field"] != "doi" for c in lib.candidates("a.pdf"))
    before = lib.dump()
    lib.resolve(online=True, provider=FakeProvider({DOI: other}), accept_safe=True)
    assert lib.dump() == before  # the verdict is remembered: the local pass does not propose the DOI again


def test_a_printed_doi_the_provider_has_never_heard_of_is_downgraded_before_the_batch_rule_runs(make_library):
    lib = make_library({"a.pdf": article()})
    lib.resolve(online=True, provider=FakeProvider({}), accept_safe=True)  # the provider knows no works
    assert "doi" not in lib.values("a.pdf")  # same run: it was safe locally, but the provider's silence is evidence
    local = lib.candidates("a.pdf", "doi", "pdf_text_doi")[0]
    assert (local["status"], local["review"]) == ("proposed", "required")
    assert json.loads(local["evidence_json"])["notes"] == ["the provider has no record of this DOI"]
    assert lib.candidates("a.pdf", "doi", "crossref")[0]["status"] == "set_aside"


def test_a_record_that_only_partly_matches_waits_for_a_person(make_library):
    partial = crossref_work(title=TITLE + " and Their Remarkable Aqueous Solubility Behaviour Under Pressure", year=2021)
    lib = make_library({"a.pdf": article()})
    lib.resolve(online=True, provider=FakeProvider({DOI: partial}), accept_safe=True)
    check = lib.candidates("a.pdf", "doi", "crossref")[0]
    assert json.loads(check["evidence_json"])["verdict"] == "weak" and check["review"] == "required"
    # The local evidence alone was safe, but a provider saying "only weakly matches" is evidence against: the DOI waits.
    assert "doi" not in lib.values("a.pdf")
    local = lib.candidates("a.pdf", "doi", "pdf_text_doi")[0]
    assert local["review"] == "required" and "only weakly matches" in json.loads(local["evidence_json"])["notes"][0]
    assert "year" not in lib.values("a.pdf")  # and nothing is taken from a record that did not verify


def test_a_doi_many_documents_each_call_their_own_is_a_parent_work_and_is_never_accepted_unconfirmed(make_library):
    """An encyclopedia's entries each print the encyclopedia's DOI (measured: 160 of them). Position and cues cannot see that;
    the library can."""
    entries = {f"entry{i}.pdf": article(title=f"Entry Number {i} of the Imaginary Encyclopedia of Things", authors=f"Author{i}") for i in range(3)}
    lib = make_library({**entries, "paper.pdf": article(title="A Separate Paper With Its Own Identifier", doi="10.5555/kv.resolve.0042")})
    report = lib.resolve(accept_safe=True)
    assert report["shared_dois"] == {DOI: 3}
    for name in entries:
        assert "doi" not in lib.values(name)
        (doi,) = lib.candidates(name, "doi")
        assert (doi["classification"], doi["review"]) == ("ambiguous", "required")
        assert "3 documents in this library print this DOI as their own" in json.loads(doi["evidence_json"])["notes"][0]
    assert lib.values("paper.pdf")["doi"] == "10.5555/kv.resolve.0042"  # a DOI printed once is still its document's


def test_a_shared_doi_is_the_same_whether_one_document_or_all_are_resolved(make_library):
    entries = {f"entry{i}.pdf": article(title=f"Entry Number {i} of the Imaginary Encyclopedia of Things", authors=f"Author{i}") for i in range(3)}
    lib = make_library(entries)
    lib.resolve(accept_safe=True, document_ids=[lib.doc("entry0.pdf")])  # a filtered run still sees the whole library
    shape = lambda name: [(c["classification"], c["review"], c["evidence_json"]) for c in lib.candidates(name, "doi")]  # noqa: E731
    first = shape("entry0.pdf")
    assert first and first[0][:2] == ("ambiguous", "required") and "doi" not in lib.values("entry0.pdf")
    report = lib.resolve(accept_safe=True)
    assert shape("entry0.pdf") == first  # the full run reached the same verdict: nothing about entry0 moved
    assert all(shape(f"entry{i}.pdf")[0][:2] == ("ambiguous", "required") for i in (1, 2))
    assert not any("doi" in lib.values(f"entry{i}.pdf") for i in range(3)) and report["accepted"].get("doi") is None


def test_a_shared_doi_the_provider_confirms_exactly_is_accepted_because_the_provider_checked_this_document(make_library):
    copies = {f"copy{i}.pdf": article() for i in range(3)}  # three copies of one paper: the same DOI is legitimately shared
    lib = make_library(copies)
    lib.resolve(online=True, provider=FakeProvider({DOI: crossref_work()}), accept_safe=True)
    for name in copies:
        assert lib.values(name)["doi"] == DOI


def test_a_title_that_is_only_an_identifier_is_never_accepted_even_when_two_sources_repeat_it(make_library):
    lib = make_library({"a.pdf": article(), "b.pdf": article(title="Another Study Of Entirely Different Matters", doi=DOI2)})
    # Both the Info title and the XMP title of a.pdf say the DOI: two sources agreeing on junk.
    import pymupdf
    doc = pymupdf.open(lib.env.lib / "a.pdf")
    doc.set_metadata({"title": f"doi:{DOI}"})
    doc.saveIncr()
    doc.close()
    lib.env.scan(full=True)
    extract_library(lib.conn, lib.index, session=lib.session)
    lib.resolve(accept_safe=True)
    assert lib.values("a.pdf").get("title") != f"doi:{DOI}"
    assert not any(c["value"].lower().startswith("doi:") for c in lib.candidates("a.pdf", "title"))


@pytest.mark.parametrize("fail", [LookupState.TRANSIENT_ERROR, LookupState.OFFLINE, LookupState.PROVIDER_UNAVAILABLE])
def test_an_outage_alters_no_accepted_value_and_records_no_verdict(make_library, fail, monkeypatch):
    monkeypatch.setattr(resolve_metadata, "MAX_CONSECUTIVE_FAILURES", 2)
    lib = make_library({"a.pdf": article(), "b.pdf": article(title="Another Study Of Entirely Different Matters", doi=DOI2),
                        "c.pdf": article(title="Yet Another Unrelated Study Of Something Else Entirely", doi="10.5555/kv.resolve.0003")})
    lib.resolve(accept_safe=True)  # offline first: accepted values exist
    accepted = {name: lib.values(name) for name in ("a.pdf", "b.pdf", "c.pdf")}
    provider = FakeProvider(fail=fail)
    report = lib.resolve(online=True, provider=provider, accept_safe=True)
    assert {name: lib.values(name) for name in accepted} == accepted
    assert not any(r["source"] == "crossref" for r in lib.conn.execute("SELECT source FROM metadata_candidate"))
    assert report["online"]["states"] == {fail.value: len(provider.calls)}


def test_repeated_failures_stop_the_run_for_the_provider_but_not_the_local_work(make_library, monkeypatch):
    monkeypatch.setattr(resolve_metadata, "MAX_CONSECUTIVE_FAILURES", 2)
    names = {f"{i}.pdf": article(title=f"Study Number {i} of Imaginary Lattices and Related Matters", doi=f"10.5555/kv.resolve.{i:04d}") for i in range(4)}
    lib = make_library(names)
    provider = FakeProvider(fail=LookupState.TRANSIENT_ERROR)
    report = lib.resolve(online=True, provider=provider)
    assert len(provider.calls) == 2 and "in a row" in report["online"]["stopped"]
    assert report["documents"] == 4 and all(lib.candidates(n, "doi") for n in names)  # every document still got its local pass


def test_a_rate_limit_stops_the_provider_at_once_and_says_how_long_it_asked_for(make_library):
    lib = make_library({"a.pdf": article(), "b.pdf": article(title="Another Study Of Entirely Different Matters", doi=DOI2)})
    provider = FakeProvider(fail=LookupState.RATE_LIMITED, retry_after=3600)
    report = lib.resolve(online=True, provider=provider)
    assert len(provider.calls) == 1 and "3600" in report["online"]["stopped"]


def test_an_exhausted_budget_stops_the_provider(make_library):
    lib = make_library({"a.pdf": article(), "b.pdf": article(title="Another Study Of Entirely Different Matters", doi=DOI2)})
    report = lib.resolve(online=True, provider=FakeProvider(fail=LookupState.NOT_ATTEMPTED))
    assert report["online"]["stopped"] and report["online"]["states"] == {"not_attempted": 1}


def test_a_document_with_no_doi_is_found_by_title_and_a_strong_match_is_safe(make_library):
    lib = make_library({"a.pdf": article(doi=None)})
    provider = FakeProvider(searches={"*": [crossref_work(doi=DOI2)]})
    lib.resolve(online=True, provider=provider, accept_safe=True)
    assert provider.calls == [("title", TITLE)]
    found = lib.candidates("a.pdf", "doi", "crossref_title_search")[0]
    assert (found["value"], found["review"], found["classification"]) == (DOI2, "safe", "own")
    assert json.loads(found["evidence_json"])["query"] == TITLE
    assert lib.values("a.pdf")["doi"] == DOI2 and lib.values("a.pdf")["year"] == "2021"


def test_a_title_match_with_fewer_agreeing_authors_waits_for_a_person(make_library):
    lib = make_library({"a.pdf": article(doi=None, authors="A. Examplar")})
    match = crossref_work(doi=DOI2, families=("Examplar", "Notprinted", "Alsonotprinted"))
    lib.resolve(online=True, provider=FakeProvider(searches={"*": [match]}), accept_safe=True)
    found = lib.candidates("a.pdf", "doi", "crossref_title_search")[0]
    assert found["review"] == "required" and "doi" not in lib.values("a.pdf")


def test_two_equally_good_title_matches_are_ambiguous_and_nothing_is_chosen(make_library):
    lib = make_library({"a.pdf": article(doi=None)})
    editions = [crossref_work(doi=DOI2), crossref_work(doi="10.5555/kv.resolve.0009")]
    lib.resolve(online=True, provider=FakeProvider(searches={"*": editions}), accept_safe=True)
    found = lib.candidates("a.pdf", "doi", "crossref_title_search")
    assert len(found) == 2 and all(c["classification"] == "ambiguous" and c["review"] == "required" for c in found)
    assert "doi" not in lib.values("a.pdf")


def test_a_search_result_titled_differently_is_never_proposed(make_library):
    lib = make_library({"a.pdf": article(doi=None)})
    unrelated = crossref_work(doi=DOI2, title="Crystal Structures of Imaginary Perovskite Oxides at Pressure", families=("Differentperson",))
    lib.resolve(online=True, provider=FakeProvider(searches={"*": [unrelated]}), accept_safe=True)
    assert lib.candidates("a.pdf", "doi", "crossref_title_search") == []


def test_an_accepted_doi_whose_record_does_not_match_proposes_no_metadata_and_is_reported(make_library):
    lib = make_library({"a.pdf": article()})
    metadata.set_value(lib.conn, lib.doc("a.pdf"), "doi", "10.5555/kv.resolve.9999")
    wrong = crossref_work(doi="10.5555/kv.resolve.9999", title="Crystal Structures of Imaginary Perovskite Oxides at Pressure", families=("Differentperson",))
    report = lib.resolve(online=True, provider=FakeProvider({"10.5555/kv.resolve.9999": wrong}), accept_safe=True)
    assert report["online"].get("accepted_doi_contradicted") == 1 and report["problems"]
    assert not any(c["source"] == "crossref" for c in lib.candidates("a.pdf"))
    assert lib.values("a.pdf")["doi"] == "10.5555/kv.resolve.9999"  # a person's value is never undone by a run


def test_only_a_doi_or_a_title_is_ever_sent(make_library):
    lib = make_library({"secret-name-42.pdf": article(), "other.pdf": article(doi=None, title="Another Study Of Entirely Different Matters")})
    provider = FakeProvider({DOI: crossref_work()})
    lib.resolve(online=True, provider=provider)
    for kind, value in provider.calls:
        assert kind in ("doi", "title")
        assert value == DOI or value == "Another Study Of Entirely Different Matters"
        assert "secret" not in value and str(lib.env.lib) not in value


def test_a_killed_run_is_recorded_interrupted_and_the_rerun_completes(make_library):
    lib = make_library({"a.pdf": article()})

    class Boom(FakeProvider):
        def lookup_doi(self, doi):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        lib.resolve(online=True, provider=Boom())
    assert lib.env.one("SELECT status FROM resolve_run") == "interrupted"
    assert lib.candidates("a.pdf", "doi")  # the local pass for the document in flight was already committed
    lib.resolve(online=True, provider=FakeProvider({DOI: crossref_work()}), accept_safe=True)
    assert lib.values("a.pdf")["doi"] == DOI
    assert sorted(r[0] for r in lib.conn.execute("SELECT status FROM resolve_run")) == ["completed", "interrupted"]


def test_requests_can_be_listed_without_sending_or_writing_anything(make_library):
    lib = make_library({"a.pdf": article(), "b.pdf": article(doi=None, title="Another Study Of Entirely Different Matters")})
    lib.resolve()
    before, runs = lib.dump(), lib.env.one("SELECT COUNT(*) FROM resolve_run")
    plan = planned_requests(lib.conn, lib.cache)
    assert sorted((p["sends"], p["value"]) for p in plan) == [("doi", DOI), ("title", "Another Study Of Entirely Different Matters")]
    assert all(p["cached"] is False for p in plan)
    assert lib.dump() == before and lib.env.one("SELECT COUNT(*) FROM resolve_run") == runs
    metacache.put_response(lib.cache, f"crossref:1:doi:{DOI}", provider="crossref", kind="doi", request=DOI, state="no_match", payload=None, client_version="1")
    assert {p["value"]: p["cached"] for p in planned_requests(lib.conn, lib.cache)}[DOI] is True


def test_the_review_queue_after_a_run_lists_what_needs_a_person(make_library):
    lib = make_library({"a.pdf": article()})
    lib.resolve()  # no accept_safe: everything waits
    items, total = review.queue(lib.conn)
    assert {i["field"] for i in items} == {"doi", "title", "authors"} and total == 3
    safe, _ = review.queue(lib.conn, review="safe")
    assert {i["field"] for i in safe} == {"doi", "title"}
    assert titles.alnum_key(next(i for i in items if i["field"] == "title")["value"]) == titles.alnum_key(TITLE)


def test_a_printed_doi_the_provider_only_weakly_matches_sends_the_document_to_a_title_search(make_library):
    lib = make_library({"a.pdf": article()})
    weak = crossref_work(title=TITLE + " and Their Remarkable Aqueous Solubility Behaviour Under Pressure")
    provider = FakeProvider({DOI: weak}, searches={"*": [crossref_work(doi=DOI2)]})
    lib.resolve(online=True, provider=provider, accept_safe=True)
    assert provider.calls == [("doi", DOI), ("title", TITLE)]  # not usable, so the title is tried; a confirmed DOI never searches (test above)
    assert lib.candidates("a.pdf", "doi", "crossref_title_search")[0]["value"] == DOI2


def test_an_accepted_doi_the_provider_has_never_heard_of_is_reported_and_left_alone(make_library):
    """Accepted offline by local evidence, then asked about online: the provider's silence cannot undo it, but must be said."""
    lib = make_library({"a.pdf": article()})
    lib.resolve(accept_safe=True)
    assert lib.values("a.pdf")["doi"] == DOI
    report = lib.resolve(online=True, provider=FakeProvider({}), accept_safe=True)
    assert report["online"]["accepted_doi_unknown_to_provider"] == 1
    assert any(DOI in p["message"] and "not known to the provider" in p["message"] for p in report["problems"])
    assert lib.values("a.pdf")["doi"] == DOI  # reported, never undone


def test_a_rule_accepted_doi_whose_record_only_weakly_matches_is_reported_and_left_alone(make_library):
    lib = make_library({"a.pdf": article()})
    lib.resolve(accept_safe=True)
    weak = crossref_work(title=TITLE + " and Their Remarkable Aqueous Solubility Behaviour Under Pressure")
    report = lib.resolve(online=True, provider=FakeProvider({DOI: weak}), accept_safe=True)
    assert report["online"]["accepted_doi_weak"] == 1 and any("only weakly matches" in p["message"] for p in report["problems"])
    assert lib.values("a.pdf")["doi"] == DOI
    metadata.set_value(lib.conn, lib.doc("a.pdf"), "doi", DOI)  # a person's own statement is not second-guessed
    again = lib.resolve(online=True, provider=FakeProvider({DOI: weak}), accept_safe=True)
    assert "accepted_doi_weak" not in again["online"]
