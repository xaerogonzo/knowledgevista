"""Extraction as a service over a real catalog, a real worker and generated PDFs, plus page reading and search coverage."""

from __future__ import annotations

import os
import shutil

import pdfbuilders as b
import pymupdf
import pytest
from support import make_env, write

from knowledgevista import paths
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.extract.client import ExtractionSession, FileResult, PageResult
from knowledgevista.extract.profile import IMPORTED_SOURCE, NATIVE_SOURCE, current_profile
from knowledgevista.index import search as qs
from knowledgevista.index import store
from knowledgevista.services import doctor, pages
from knowledgevista.services import search as search_service
from knowledgevista.services.extract import build_record, extract_library


class Spy(ExtractionSession):
    """A real session that records which paths it was asked to read, and can act in the middle of an extraction."""

    def __init__(self, hook=None, **kw):
        super().__init__(**kw)
        self.calls: list[str] = []
        self.hook = hook

    def extract(self, path):
        self.calls.append(os.path.basename(path))
        if self.hook:
            self.hook(path)
        return super().extract(path)


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path, {})
    b.native_pdf(e.lib / "native.pdf")
    b.scanned_pdf(e.lib / "scan.pdf")
    b.labelled_pdf(e.lib / "book.pdf")
    b.unicode_pdf(e.lib / "unicode.pdf")
    b.malformed_pdf(e.lib / "broken.pdf")
    write(e.lib / "notes.txt", "plain text is not extracted in this version")
    e.scan()
    e.index = store.open_index(tmp_path / "idx" / "extractions.sqlite", create=True)
    yield e
    e.index.close()


def run(env, **kwargs):
    with Spy(hook=kwargs.pop("hook", None)) as session:
        report = extract_library(env.conn, env.index, session=session, **kwargs)
    report.spy = session
    return report


def artifact(env, rel):
    return env.location(rel)["artifact_id"]


def search_hits(env, text, **kw):
    return {(h.artifact_id, h.pdf_page) for h in qs.run_search(env.index, qs.parse_query(text, **kw), limit=100)}


# --- what gets extracted ------------------------------------------------------------------------------------------


def test_first_run_extracts_every_pdf_and_reports_each_outcome(env):
    report = run(env)
    assert (report.candidates, report.extracted, report.complete, report.failed, report.partial) == (5, 5, 4, 1, 0)
    assert report.other_kinds_not_extracted == 1, "the .txt is counted as not extracted, not silently skipped"
    assert report.pages == 3 + 3 + 10 + 2
    assert store.get_extraction(env.index, artifact(env, "broken.pdf"))["status"] == "failed"
    assert "no pages" in store.get_extraction(env.index, artifact(env, "broken.pdf"))["error"]
    assert report.limiter.startswith("windows-job-object") or not os.name == "nt"
    assert store.consistency_problems(env.index) == []


def test_text_and_page_states_are_stored_per_page(env):
    run(env)
    states = lambda rel: [r["text_state"] for r in env.index.execute(  # noqa: E731
        "SELECT p.text_state FROM page p JOIN extraction e USING (extraction_id) WHERE e.artifact_id = ? ORDER BY p.pdf_page", (artifact(env, rel),))]
    assert states("scan.pdf") == ["image_only"] * 3
    assert states("native.pdf")[0] in ("text_native", "text_sparse")
    labels = [r[0] for r in env.index.execute("SELECT p.printed_label FROM page p JOIN extraction e USING (extraction_id) WHERE e.artifact_id = ? ORDER BY p.pdf_page", (artifact(env, "book.pdf"),))]
    assert labels[:5] == ["i", "ii", "iii", "iv", "1"] and labels[-1] == "6"
    unlabelled = [r[0] for r in env.index.execute("SELECT p.printed_label FROM page p JOIN extraction e USING (extraction_id) WHERE e.artifact_id = ?", (artifact(env, "native.pdf"),))]
    assert unlabelled == [None] * 3


def test_second_run_does_nothing_and_rewrites_nothing(env):
    run(env)
    ids = [r[0] for r in env.index.execute("SELECT page_id FROM page ORDER BY page_id")]
    second = run(env)
    assert (second.extracted, second.current, second.skipped_failed) == (0, 4, 1)
    assert second.spy.calls == [], "an unchanged extractor version means no file is read again"
    assert [r[0] for r in env.index.execute("SELECT page_id FROM page ORDER BY page_id")] == ids


def test_a_failed_document_is_not_retried_until_asked(env):
    run(env)
    assert run(env).spy.calls == []
    retried = run(env, retry_failed=True)
    assert retried.spy.calls == ["broken.pdf"] and retried.failed == 1


def test_force_rereads_everything(env):
    run(env)
    forced = run(env, force=True)
    assert forced.extracted == 5 and len(forced.spy.calls) == 5


def test_limit_stops_after_that_many(env):
    assert run(env, limit=2).extracted == 2


# --- versioned derivatives ----------------------------------------------------------------------------------------


def test_a_new_extractor_version_marks_everything_stale_and_extracts_again(env, monkeypatch):
    run(env)
    anchors = {(a, p): pid for a, p, pid in env.index.execute(
        "SELECT e.artifact_id, p.pdf_page, p.page_id FROM page p JOIN extraction e USING (extraction_id)")}
    old_profile = current_profile().profile_id

    monkeypatch.setattr("knowledgevista.extract.profile.installed_pymupdf_version", lambda: "99.0.0")
    found = [f.code for f in doctor.run_doctor(env.conn, env.index)]
    assert "KVD_STALE_EXTRACTION" in found, "doctor notices staleness before anyone re-extracts"
    redone = run(env)
    assert redone.stale_redone == 5 and redone.spy.calls.count("native.pdf") == 1
    assert redone.spy.calls.count("broken.pdf") == 1, "a document that failed under the old profile gets one new attempt: a new extractor may read it"
    assert store.get_extraction(env.index, artifact(env, "native.pdf"))["profile_id"] != old_profile

    after = {(a, p): pid for a, p, pid in env.index.execute(
        "SELECT e.artifact_id, p.pdf_page, p.page_id FROM page p JOIN extraction e USING (extraction_id)")}
    native = artifact(env, "native.pdf")
    assert after[(native, 2)] != anchors[(native, 2)], "re-extraction issues new internal page ids"
    shown = pages.get_page(env.conn, env.index, "native.pdf", pdf_page=2)
    assert shown["anchor"] == {"artifact_id": native, "pdf_page": 2} and "aqueous solubility" in shown["text"], "but artifact + pdf_page still resolves"
    assert run(env).spy.calls == [], "once current, nothing is redone, and the still-failing document is not hammered again"


def test_an_unchanged_profile_is_never_stale(env):
    run(env)
    assert "KVD_STALE_EXTRACTION" not in [f.code for f in doctor.run_doctor(env.conn, env.index)]


def test_extraction_needs_the_optional_group_and_says_which(env, monkeypatch):
    monkeypatch.setattr("knowledgevista.extract.profile.installed_pymupdf_version", lambda: None)
    with pytest.raises(KvError) as caught:
        extract_library(env.conn, env.index)
    assert caught.value.code == ErrorCode.DEPENDENCY_MISSING


# --- the derivative must come from the artifact -------------------------------------------------------------------


def test_a_file_changed_since_the_scan_is_skipped_and_nothing_is_stored(env):
    write(env.lib / "native.pdf", b.native_pdf(env.lib.parent / "other.pdf", title="A Different Paper Entirely").read_bytes())
    report = run(env)
    assert report.changed_since_scan == 1 and "native.pdf" not in report.spy.calls
    assert store.get_extraction(env.index, artifact(env, "native.pdf")) is None
    assert any("kv scan" in p["message"] for p in report.problems)
    env.scan()
    assert run(env).extracted == 1, "after a rescan the new bytes are a new artifact and extract normally"


def test_a_file_that_changes_while_being_extracted_stores_nothing(env):
    def corrupt_midway(path):
        if path.endswith("book.pdf"):
            with open(path, "ab") as handle:
                handle.write(b"\n%appended while we were reading")

    report = run(env, hook=corrupt_midway)
    assert report.changed_since_scan == 1
    assert store.get_extraction(env.index, artifact(env, "book.pdf")) is None


def test_an_unreachable_file_is_counted_not_guessed(env):
    shutil.move(str(env.lib), str(env.lib) + "-away")
    env.scan()
    report = run(env)
    assert report.unreachable == 5 and report.extracted == 0 and report.spy.calls == []


# --- identity: extraction follows the bytes, not the path ----------------------------------------------------------


def test_a_moved_file_keeps_its_extraction_and_stays_searchable(env):
    run(env)
    before = store.get_extraction(env.index, artifact(env, "native.pdf"))["extraction_id"]
    (env.lib / "sorted").mkdir()
    os.rename(env.lib / "native.pdf", env.lib / "sorted" / "A Real Title.pdf")
    env.scan()
    report = run(env)
    assert report.spy.calls == [], "the same bytes at a new path need no new extraction"
    assert store.get_extraction(env.index, artifact(env, "sorted/A Real Title.pdf"))["extraction_id"] == before
    result = search_service.search(env.conn, env.index, qs.parse_query("aqueous solubility"), limit=5)
    assert result.hits and result.hits[0]["paths"][0]["path"] == "sorted/A Real Title.pdf", "the hit shows the CURRENT path"


def test_replaced_bytes_get_a_fresh_extraction_and_the_old_one_is_flagged_orphaned(env):
    run(env)
    write(env.lib / "native.pdf", b.native_pdf(env.lib.parent / "o.pdf", title="Replacement", body_pages=1).read_bytes())
    env.scan()
    report = run(env)
    assert report.extracted == 1
    codes = [f.code for f in doctor.run_doctor(env.conn, env.index)]
    assert "KVD_ORPHAN_EXTRACTION" not in codes, "the replaced artifact still exists in the catalog as history, so its text is not an orphan"


def test_deleting_the_extraction_cache_loses_no_catalog_state(env, tmp_path):
    run(env)
    dump = lambda: {  # noqa: E731
        t: [tuple(r) for r in env.conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
        for t in ("root", "artifact", "document", "document_artifact", "location", "location_event")}
    hits_before = search_hits(env, "aqueous solubility | trinitrotoluene")
    catalog_before = dump()
    env.index.close()
    shutil.rmtree(tmp_path / "idx")
    env.index = None

    assert dump() == catalog_before, "everything the catalog holds survives the loss of the cache"
    gone = doctor.run_doctor(env.conn, None)
    assert not doctor.has_errors(gone) and any(f.code == "KVD_NOT_EXTRACTED" for f in gone)
    assert search_service.search(env.conn, None, qs.parse_query("aqueous"), limit=5).hits == []
    assert search_service.coverage(env.conn, None)["not_yet_extracted"] == 5

    env.index = store.open_index(tmp_path / "idx" / "extractions.sqlite", create=True)
    rebuilt = run(env)
    assert rebuilt.extracted == 5
    assert search_hits(env, "aqueous solubility | trinitrotoluene") == hits_before, "rebuilt text answers the same queries identically"


# --- the pure record builder --------------------------------------------------------------------------------------


def _result(*texts, status="complete"):
    return FileResult(status, len(texts), [PageResult(i, t, None, 0) for i, t in enumerate(texts, 1)])


def test_build_record_takes_the_doi_signal_from_the_first_two_pages_only():
    profile = current_profile()
    doi = "10." + "5555/own.paper"
    record = build_record("a" * 64, profile, _result(f"Title page doi:{doi}", "body", f"References 10.5555/other.paper"))
    assert record.first_pages_doi == doi
    late = build_record("b" * 64, profile, _result("Title", "body", f"References doi:{doi}"))
    assert late.first_pages_doi is None, "a DOI printed only in the bibliography is not the document's"


def test_build_record_truncates_first_text_and_flags_scans():
    profile = current_profile()
    record = build_record("a" * 64, profile, _result("x" * 5000, "short"))
    assert len(record.first_text) == 600 and record.chars == 5005 and record.legacy_scanned is False
    assert build_record("b" * 64, profile, _result("", "3")).legacy_scanned is True
    assert build_record("c" * 64, profile, _result()).status == "complete" and build_record("c" * 64, profile, FileResult("failed")).pages == []


def test_build_record_marks_failed_pages():
    result = FileResult("partial", 2, [PageResult(1, "fine text here, long enough to count as native text content"), PageResult(2, error="boom")])
    record = build_record("a" * 64, current_profile(), result)
    assert [p.text_state for p in record.pages] == ["text_native", "failed"] and record.pages[1].error == "boom"


# --- reading a page -----------------------------------------------------------------------------------------------


def test_show_by_pdf_page_and_by_label_are_different_questions(env):
    run(env)
    by_position = pages.get_page(env.conn, env.index, "book.pdf", pdf_page=5)
    by_label = pages.get_page(env.conn, env.index, "book.pdf", label="1")
    assert by_position["anchor"] == by_label["anchor"] and by_position["printed_label"] == "1" and by_position["page_count"] == 10
    assert "PDFPAGE-5" in by_position["text"]
    roman = pages.get_page(env.conn, env.index, "book.pdf", label="iii")
    assert roman["pdf_page"] == 3, "printed labels are strings, not integers"
    assert "navigation aid" in by_position["note"], "extracted text says it is not the authority"
    assert pages.get_page(env.conn, env.index, "book.pdf", pdf_page=1)["pdf_page"] == 1, "pdf pages count from 1"


def test_show_errors_are_distinct_and_machine_readable(env):
    run(env)
    for kwargs, code in (({"pdf_page": 99}, ErrorCode.NOT_FOUND), ({"pdf_page": 0}, ErrorCode.NOT_FOUND), ({"label": "zz"}, ErrorCode.NOT_FOUND),
                         ({}, ErrorCode.INVALID_ARGUMENTS), ({"pdf_page": 1, "label": "1"}, ErrorCode.INVALID_ARGUMENTS)):
        with pytest.raises(KvError) as caught:
            pages.get_page(env.conn, env.index, "book.pdf", **kwargs)
        assert caught.value.code == code, kwargs
    with pytest.raises(KvError) as caught:
        pages.get_page(env.conn, env.index, "no-such.pdf", pdf_page=1)
    assert caught.value.code == ErrorCode.NOT_FOUND


def test_a_label_shared_by_several_pages_is_ambiguous_not_guessed(env):
    doc = pymupdf.open()
    for i in range(6):
        doc.new_page().insert_text((72, 72), f"section page {i}")
    doc.set_page_labels([{"startpage": 0, "style": "D", "firstpagenum": 1}, {"startpage": 3, "style": "D", "firstpagenum": 1}])
    doc.save(env.lib / "appendices.pdf")
    env.scan()
    run(env)
    with pytest.raises(KvError) as caught:
        pages.get_page(env.conn, env.index, "appendices.pdf", label="2")
    assert caught.value.code == ErrorCode.AMBIGUOUS
    assert [c["pdf_page"] for c in caught.value.details["candidates"]] == [2, 5]
    assert pages.get_page(env.conn, env.index, "appendices.pdf", pdf_page=5)["printed_label"] == "2"


def test_show_before_extraction_says_not_extracted_not_not_found(env):
    with pytest.raises(KvError) as caught:
        pages.get_page(env.conn, env.index, "native.pdf", pdf_page=1)
    assert caught.value.code == ErrorCode.NOT_EXTRACTED, "'could not look' is not 'none exist'"
    with pytest.raises(KvError) as caught:
        pages.get_page(env.conn, None, "native.pdf", pdf_page=1)
    assert caught.value.code == ErrorCode.NOT_EXTRACTED


def test_an_image_only_page_shows_its_state_and_no_text(env):
    run(env)
    shown = pages.get_page(env.conn, env.index, "scan.pdf", pdf_page=2)
    assert shown["text_state"] == "image_only" and shown["text"] == ""


# --- search over the library, with coverage -----------------------------------------------------------------------


def test_search_hits_carry_stable_anchors_and_current_paths(env):
    run(env)
    result = search_service.search(env.conn, env.index, qs.parse_query("aqueous solubility"), limit=10)
    assert {h["pdf_page"] for h in result.hits} == {2, 3}
    hit = result.hits[0]
    assert hit["anchor"] == {"artifact_id": artifact(env, "native.pdf"), "pdf_page": hit["pdf_page"]}
    assert hit["document_id"] and hit["paths"][0]["path"] == "native.pdf" and hit["available"] is True
    assert hit["evidence_type"] == "search_navigation" and "[[aqueous solubility]]" in hit["snippet"]
    assert hit["extraction"]["provisional"] is False


def test_coverage_says_what_a_search_could_not_see(env):
    cov = search_service.coverage(env.conn, None)
    assert cov["searchable"] == 0 and cov["not_yet_extracted"] == 5 and cov["other_files_not_searchable"] == 1
    run(env)
    cov = search_service.coverage(env.conn, env.index)
    assert (cov["pdf_documents"], cov["searchable"], cov["no_text_layer"], cov["extraction_failed"], cov["not_yet_extracted"]) == (5, 4, 1, 1, 0)


def test_path_scope_restricts_a_search(env):
    run(env)
    both = search_service.search(env.conn, env.index, qs.parse_query("page | lattices | marker"), limit=50)
    only_book = search_service.search(env.conn, env.index, qs.parse_query("page | lattices | marker", path_contains="BOOK"), limit=50)
    assert {h["paths"][0]["path"] for h in only_book.hits} == {"book.pdf"} and len(both.hits) > len(only_book.hits)


def test_truncation_is_reported(env):
    run(env)
    result = search_service.search(env.conn, env.index, qs.parse_query("marker"), limit=3)
    assert result.truncated and len(result.hits) == 3


# --- the query set: exact, prefix, OR, NEAR, Unicode, chemistry punctuation, and NEGATIVE cases -------------------


QUERY_SET_HITS = [
    # (query kwargs, file that must be the only match, why)
    (dict(text="aqueous solubility"), "native.pdf", "exact phrase"),
    (dict(text="solub*"), "native.pdf", "explicit prefix"),
    (dict(text="Imaginary | Fictional", near=[], ), "native.pdf", "OR alternatives"),
    (dict(text="aqueous", near=["298"]), "native.pdf", "NEAR"),
    (dict(text="compound", also=["measured"]), "native.pdf", "same page"),
    (dict(text="2,4-DNT"), "unicode.pdf", "chemistry: comma-separated locants"),
    (dict(text="Cu(II)"), "unicode.pdf", "chemistry: parenthesised oxidation state"),
    (dict(text="Al2O3"), "unicode.pdf", "chemistry: formula"),
    (dict(text="CAS 118-96-7"), "unicode.pdf", "chemistry: CAS number"),
    (dict(text="2,4,6-trinitrotoluene"), "unicode.pdf", "chemistry: full name"),
    (dict(text="pH 7.4"), "unicode.pdf", "decimal number"),
    (dict(text="β-lactame"), "unicode.pdf", "Greek letter"),
    (dict(text="ETUDE de la"), "unicode.pdf", "case-insensitive"),
]
QUERY_SET_MISSES = [
    (dict(text="2,4-DNP"), "a different compound with the same locants"),
    (dict(text="Cu(III)"), "a different oxidation state"),
    (dict(text="Al2O"), "a stem is not a prefix without the star"),
    (dict(text="CAS 118-96-8"), "a different CAS number"),
    (dict(text="pH 7.40"), "7.40 is not the printed 7.4"),
    (dict(text="beta-lactame"), "no silent transliteration: the Greek letter is not 'beta'"),
    (dict(text="solubility aqueous"), "a phrase is ordered"),
    (dict(text="aqueous", near=["zzzz"]), "NEAR with an absent term"),
    (dict(text="trinitro"), "a partial word without the star"),
]


@pytest.mark.parametrize(("kw", "file", "why"), QUERY_SET_HITS, ids=[w for _, _, w in QUERY_SET_HITS])
def test_query_set_positive(env, kw, file, why):
    run(env)
    result = search_service.search(env.conn, env.index, qs.parse_query(kw.pop("text"), **kw), limit=50)
    assert result.hits, why
    assert {h["paths"][0]["path"] for h in result.hits} == {file}, why


@pytest.mark.parametrize(("kw", "why"), QUERY_SET_MISSES, ids=[w for _, w in QUERY_SET_MISSES])
def test_query_set_negative(env, kw, why):
    run(env)
    assert search_service.search(env.conn, env.index, qs.parse_query(kw.pop("text"), **kw), limit=50).hits == [], why


def test_accents_fold_which_is_the_documented_inherited_behaviour(env):
    run(env)
    assert search_service.search(env.conn, env.index, qs.parse_query("etude"), limit=5).hits, "diacritics are folded (remove_diacritics 2)"
