"""`kv relate` over real generated PDFs: what is proposed, on what evidence, and what is deliberately NOT proposed.

Accepted metadata is stated directly (`metadata.set_value`) instead of going through `resolve`, because this suite is about
relations; the metadata pipeline has its own. The false-positive fixtures matter as much as the positives: a file name that
looks like a supplement, a DOI that many different papers print, the same title under two authors, a preprint that is not
the paper it resembles.
"""

from __future__ import annotations

import json
import os

import pdfbuilders as b
import pytest
from support import make_env, tree_hashes, write
from test_resolve_metadata import Library

from knowledgevista.services import metadata, organize, relate, relations

TITLE = "Aqueous Solubility of Invented Esters"  # short enough to fit the page at 20 pt: a clipped title is never printed
DOI = "10.5555/kv.rel.0001"
JOURNAL = "Cite This: J. Invented Results 2021, 12, 54-58"
#: Enough body text to fingerprint (a real paper has thousands of letters; the detector refuses to match near-empty text).
BODY = 8


def paper(**kw) -> dict:
    return {"title": TITLE, "authors": "A. Examplar and B. Placeholder", "journal": JOURNAL, "doi": DOI, "body_pages": BODY, **kw}


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


def accept(lib, name, **fields):
    for field, value in fields.items():
        metadata.set_value(lib.conn, lib.doc(name), field, value, lock=False)


def run(lib, cache=None) -> dict:
    return relate.detect(lib.conn, lib.index, cache)


def proposals(lib, kind=None):
    items, _ = relate.list_candidates(lib.conn, kind=kind, limit=500)
    return items


def pair(lib, kind, a, b):
    """The proposals of `kind` between two named files, as a list."""
    ends = {lib.doc(a), lib.doc(b)}
    return [p for p in proposals(lib, kind) if {p["source"], p["target"]} == ends]


# ------------------------------------------------------------------------------------------------ identical text


def test_the_same_publication_with_different_bytes_is_proposed_as_one_document_and_as_equivalent_artifacts(make_library):
    lib = make_library({"a.pdf": paper(producer="PublisherA"), "b.pdf": paper(producer="PublisherB")})
    assert lib.env.one("SELECT COUNT(DISTINCT artifact_id) FROM location") == 2  # genuinely different bytes
    report = run(lib)
    (same,) = pair(lib, "same_document", "a.pdf", "b.pdf")
    assert same["confidence"] == "exact" and same["evidence"]["text_fingerprint"]
    (artifacts,) = [p for p in proposals(lib, "equivalent_to")]
    assert artifacts["level"] == "artifact" and artifacts["evidence"]["same_document"] is False
    assert report["fingerprinted"] == 2 and report["outcomes"] == {"new": 2}


def test_accepting_the_merge_puts_both_artifacts_in_one_document_and_the_artifact_proposal_then_describes_one_document(make_library):
    lib = make_library({"a.pdf": paper(producer="PublisherA"), "b.pdf": paper(producer="PublisherB")})
    run(lib)
    (same,) = proposals(lib, "same_document")
    result = relations.accept_candidate(lib.conn, same["candidate_id"], actor="user")
    assert result["effect"] == "merged" and len(result["artifacts_moved"]) == 1
    survivor = lib.doc("a.pdf")
    assert lib.doc("b.pdf") == survivor  # one document, two paths
    assert lib.env.one("SELECT COUNT(*) FROM document_artifact WHERE document_id = ?", survivor) == 2
    again = run(lib)
    (equivalent,) = proposals(lib, "equivalent_to")
    assert equivalent["evidence"]["same_document"] is True  # now the question is only whether to RECORD that they are equivalent
    assert again["proposals"].get("same_document") is None  # nothing proposes merging a document with itself


def test_different_text_is_not_identical_text_even_when_the_pdf_looks_the_same(make_library):
    lib = make_library({"a.pdf": paper(), "b.pdf": paper(body_pages=BODY + 1)})  # one more page of body text
    run(lib)
    assert proposals(lib, "equivalent_to") == [] and all(not p["evidence"].get("text_fingerprint") for p in proposals(lib, "same_document"))


def test_a_scan_with_almost_no_text_is_never_declared_identical_to_another_scan(make_library):
    lib = make_library({"a.pdf": {"builder": b.scanned_pdf}, "b.pdf": {"builder": b.scanned_pdf, "pages": 3}})
    report = run(lib)
    assert report["fingerprinted"] == 0 and proposals(lib) == []  # equal emptiness proves nothing


# ------------------------------------------------------------------------------------------------ the same DOI


def test_two_documents_with_one_accepted_doi_and_the_same_title_are_proposed_as_the_same_document(make_library):
    lib = make_library({"a.pdf": paper(), "b.pdf": paper(body_pages=BODY + 1)})
    for name in ("a.pdf", "b.pdf"):
        accept(lib, name, doi=DOI, title=TITLE)
    run(lib)
    (same,) = pair(lib, "same_document", "a.pdf", "b.pdf")
    assert same["evidence"] == {"doi": DOI, "titles_agree": True, "identical_text": False} and same["confidence"] == "high"


def test_a_doi_many_different_papers_share_is_a_collection_not_a_pile_of_pairwise_merges(make_library):
    entries = {f"e{i}.pdf": paper(title=f"Entry Number {i} of the Imaginary Encyclopedia of Things", doi=DOI, authors=f"Author{i} Person") for i in range(4)}
    lib = make_library(entries)
    for i, name in enumerate(entries):
        accept(lib, name, doi=DOI, title=f"Entry Number {i} of the Imaginary Encyclopedia of Things")
    run(lib)
    assert proposals(lib, "same_document") == []  # four different papers are not copies of one
    (group,) = proposals(lib, "collection")
    assert group["level"] == "group" and len(group["members"]) == 4 and group["evidence"]["parent_doi"] == DOI
    assert "4 documents each print this DOI" in group["evidence"]["reason"]


def test_when_the_parent_book_is_in_the_library_its_entries_are_part_of_it_and_no_collection_is_proposed(make_library):
    entries = {f"e{i}.pdf": paper(title=f"Entry Number {i} of the Imaginary Encyclopedia of Things", doi=DOI, authors=f"Author{i} Person") for i in range(3)}
    lib = make_library({**entries, "book.pdf": paper(title="The Imaginary Encyclopedia of Things", doi=DOI, authors="Editor Person", body_pages=BODY + 3)})
    for i in range(3):
        accept(lib, f"e{i}.pdf", doi=DOI, title=f"Entry Number {i} of the Imaginary Encyclopedia of Things")
    accept(lib, "book.pdf", doi=DOI, title="The Imaginary Encyclopedia of Things", type="book")
    run(lib)
    parts = proposals(lib, "part_of")
    assert len(parts) == 3 and {p["target"] for p in parts} == {lib.doc("book.pdf")} and proposals(lib, "collection") == []


def test_accepting_the_collection_makes_it_and_records_the_parent_doi(make_library):
    entries = {f"e{i}.pdf": paper(title=f"Entry Number {i} of the Imaginary Encyclopedia of Things", doi=DOI, authors=f"Author{i} Person") for i in range(3)}
    lib = make_library(entries)
    for i, name in enumerate(entries):
        accept(lib, name, doi=DOI, title=f"Entry Number {i} of the Imaginary Encyclopedia of Things")
    run(lib)
    (group,) = proposals(lib, "collection")
    relations.accept_candidate(lib.conn, group["candidate_id"])
    (collection,) = organize.list_collections(lib.conn)
    assert collection["members"] == 3 and collection["source_doi"] == DOI and collection["name"].startswith("Documents that print the DOI")
    assert organize.collections_of(lib.conn, lib.doc("e1.pdf")) == [collection["name"]]


def test_chapters_of_one_book_are_grouped_by_their_container_and_chapters_of_different_books_are_not(make_library):
    pdfs = {f"c{i}.pdf": paper(title=f"Chapter {i} on a Quite Particular Imaginary Subject", doi=f"10.5555/kv.chap.{i}", authors=f"Writer{i} Person") for i in range(5)}
    lib = make_library(pdfs)
    for i in range(5):
        accept(lib, f"c{i}.pdf", doi=f"10.5555/kv.chap.{i}", title=f"Chapter {i} on a Quite Particular Imaginary Subject", type="book-chapter",
               container="Handbook of Imaginary Things" if i < 3 else "A Different Handbook Entirely")
    run(lib)
    groups = {g["evidence"]["name"]: g for g in proposals(lib, "collection")}
    assert set(groups) == {"Handbook of Imaginary Things", "A Different Handbook Entirely"}  # two books, two groups; never one merged group
    assert len(groups["Handbook of Imaginary Things"]["members"]) == 3 and groups["Handbook of Imaginary Things"]["evidence"]["reason"].startswith("3 chapters")
    assert len(groups["A Different Handbook Entirely"]["members"]) == 2
    assert not set(groups["Handbook of Imaginary Things"]["members"]) & set(groups["A Different Handbook Entirely"]["members"])


def test_a_single_chapter_of_a_book_is_not_a_group(make_library):
    lib = make_library({"c0.pdf": paper(title="Chapter 0 on a Quite Particular Subject", doi="10.5555/kv.chap.0", authors="Writer Person")})
    accept(lib, "c0.pdf", doi="10.5555/kv.chap.0", title="Chapter 0 on a Quite Particular Subject", type="book-chapter", container="Handbook of Imaginary Things")
    run(lib)
    assert proposals(lib, "collection") == []


# ------------------------------------------------------------------------------------------------ supplements


def si(main_title=TITLE, doi=DOI, **kw) -> dict:
    # The paper's title goes on the small line (a long title at 20 pt runs off the page and is clipped by the extractor).
    return {"title": "Supporting Information", "authors": "A. Examplar and B. Placeholder", "journal": f"Supporting Information for: {main_title}", "doi": doi,
            "body_pages": BODY, **kw}


def test_a_supplement_that_says_so_and_prints_its_papers_doi_is_proposed_as_a_supplement_not_a_duplicate(make_library):
    lib = make_library({"paper.pdf": paper(), "other.pdf": paper(title="A Completely Unrelated Paper About Something Else", doi="10.5555/kv.rel.0002", authors="C. Writer"),
                        "supp.pdf": si(body_pages=BODY + 2)})
    accept(lib, "paper.pdf", doi=DOI, title=TITLE)
    accept(lib, "supp.pdf", doi=DOI, title="Supporting Information")  # the supplement prints the paper's own DOI, so resolve accepts it too
    run(lib)
    (supplement,) = pair(lib, "supplement_of", "supp.pdf", "paper.pdf")
    assert supplement["source"] == lib.doc("supp.pdf") and supplement["target"] == lib.doc("paper.pdf")
    assert supplement["confidence"] == "high" and supplement["evidence"]["markers"]["says_so"] is True
    assert "its DOI is printed on the supplement's first pages" in supplement["evidence"]["signals"]
    assert pair(lib, "same_document", "supp.pdf", "paper.pdf") == []  # sharing a DOI is what supplements do; it does not make them one item


def test_a_supplement_can_find_its_paper_by_the_papers_printed_title_when_no_doi_is_shared(make_library):
    lib = make_library({"paper.pdf": paper(), "supp.pdf": {"title": "Supplementary Material", "authors": "A. Examplar", "journal": f"Supplementary Material for: {TITLE}", "doi": None}})
    accept(lib, "paper.pdf", doi=DOI, title=TITLE)
    run(lib)
    (supplement,) = pair(lib, "supplement_of", "supp.pdf", "paper.pdf")
    assert supplement["evidence"]["signals"] == ["its title is printed on the supplement's first pages"]


def test_a_file_name_that_looks_like_a_supplement_proposes_nothing_without_the_evidence(make_library):
    lib = make_library({"paper.pdf": paper(), "paper_si.pdf": paper(title="An Unrelated Paper That Merely Has A Suspicious File Name", doi="10.5555/kv.rel.0003", authors="C. Writer")})
    accept(lib, "paper.pdf", doi=DOI, title=TITLE)
    run(lib)
    assert proposals(lib, "supplement_of") == []  # the name nominated it; nothing on the page named the paper


def test_a_document_that_says_it_is_a_supplement_but_names_nothing_in_the_library_proposes_nothing(make_library):
    lib = make_library({"supp.pdf": si(main_title="A Paper This Library Does Not Hold At All", doi="10.5555/kv.elsewhere")})
    accept(lib, "supp.pdf", doi="10.5555/kv.elsewhere", title="Supporting Information")
    run(lib)
    assert proposals(lib) == []


def test_a_supplement_that_could_belong_to_two_papers_is_ambiguous_for_each(make_library):
    lib = make_library({"p1.pdf": paper(), "p2.pdf": paper(producer="Another"), "supp.pdf": si()})
    accept(lib, "p1.pdf", doi=DOI, title=TITLE)
    accept(lib, "p2.pdf", doi="10.5555/kv.rel.0002", title=TITLE)  # a second paper with the same title: the supplement cannot tell which
    run(lib)
    found = proposals(lib, "supplement_of")
    assert len(found) == 2 and {p["confidence"] for p in found} == {"ambiguous"}


# ------------------------------------------------------------------------------------------------ versions


def test_a_preprint_and_the_published_paper_with_one_title_are_a_version_not_a_duplicate(make_library):
    lib = make_library({"pre.pdf": paper(doi="10.5555/kv.pre.1", body_pages=BODY + 1), "pub.pdf": paper(doi=DOI)})
    accept(lib, "pre.pdf", doi="10.5555/kv.pre.1", title=TITLE, type="posted-content")
    accept(lib, "pub.pdf", doi=DOI, title=TITLE, type="journal-article")
    run(lib)
    (version,) = proposals(lib, "version_of")
    assert version["source"] == lib.doc("pre.pdf") and version["target"] == lib.doc("pub.pdf")  # the preprint is a version OF the published work
    assert version["evidence"] == {"title": TITLE, "preprint_doi": "10.5555/kv.pre.1", "published_doi": DOI}
    assert proposals(lib, "same_document") == []  # two DOIs are two publications


def test_the_same_title_under_two_dois_with_no_preprint_is_only_related(make_library):
    lib = make_library({"a.pdf": paper(doi="10.5555/kv.ed.1"), "b.pdf": paper(doi="10.5555/kv.ed.2", body_pages=BODY + 1)})
    accept(lib, "a.pdf", doi="10.5555/kv.ed.1", title=TITLE, type="journal-article")
    accept(lib, "b.pdf", doi="10.5555/kv.ed.2", title=TITLE, type="journal-article")
    run(lib)
    (related,) = proposals(lib, "related_to")
    assert related["confidence"] == "low" and proposals(lib, "version_of") == []


# ------------------------------------------------------------------------------------------------ title and author


def test_the_same_title_and_first_author_without_any_doi_is_proposed_as_the_same_document_with_medium_confidence(make_library):
    lib = make_library({"a.pdf": paper(doi=None), "b.pdf": paper(doi=None, body_pages=BODY + 1)})
    for name in ("a.pdf", "b.pdf"):
        accept(lib, name, title=TITLE, authors="Examplar; Placeholder")
    run(lib)
    (same,) = pair(lib, "same_document", "a.pdf", "b.pdf")
    assert same["confidence"] == "medium" and same["evidence"]["first_author"] == "examplar"


def test_the_same_title_by_a_different_first_author_is_not_the_same_document(make_library):
    lib = make_library({"a.pdf": paper(doi=None), "b.pdf": paper(doi=None, authors="Z. Elsewhere", body_pages=BODY + 1)})
    accept(lib, "a.pdf", title=TITLE, authors="Examplar; Placeholder")
    accept(lib, "b.pdf", title=TITLE, authors="Elsewhere; Other")
    run(lib)
    assert proposals(lib, "same_document") == []


def test_the_same_title_and_author_under_two_different_dois_is_two_publications(make_library):
    lib = make_library({"a.pdf": paper(doi="10.5555/kv.x.1"), "b.pdf": paper(doi="10.5555/kv.x.2", body_pages=BODY + 1)})
    for name, doi in (("a.pdf", "10.5555/kv.x.1"), ("b.pdf", "10.5555/kv.x.2")):
        accept(lib, name, doi=doi, title=TITLE, authors="Examplar; Placeholder")
    run(lib)
    assert proposals(lib, "same_document") == []


def test_a_short_generic_title_never_matches_anything(make_library):
    lib = make_library({"a.pdf": paper(title="Introduction", doi=None), "b.pdf": paper(title="Introduction", doi=None, body_pages=BODY + 1)})
    for name in ("a.pdf", "b.pdf"):
        accept(lib, name, title="Introduction", authors="Examplar; Placeholder")
    run(lib)
    assert proposals(lib, "same_document") == [] and proposals(lib, "related_to") == []


# ------------------------------------------------------------------------------------------------ replacement and copies


def test_bytes_replaced_at_the_same_path_are_proposed_as_a_replacement_of_the_older_artifact(make_library):
    lib = make_library({"a.pdf": paper()})
    old = lib.env.one("SELECT artifact_id FROM location WHERE relative_path = 'a.pdf' AND ended_at IS NULL")
    b.native_pdf(lib.env.lib / "a.pdf", title=TITLE, doi=DOI, journal=JOURNAL, producer="A Re-save", body_pages=BODY + 2)
    lib.env.scan()
    new = lib.env.one("SELECT artifact_id FROM location WHERE relative_path = 'a.pdf' AND ended_at IS NULL")
    assert new != old
    run(lib)
    (replaces,) = proposals(lib, "replaces")
    assert (replaces["level"], replaces["source"], replaces["target"]) == ("artifact", new, old) and replaces["evidence"]["path"] == "a.pdf"
    assert replaces["confidence"] == "medium" and replaces["evidence"]["same_doi"] is False  # no accepted DOI on either: not the 'high' of a shared one


def test_one_file_at_two_paths_is_reported_as_copies_and_proposes_no_relation(tmp_path):
    e = make_env(tmp_path, {"one/same.txt": "identical bytes", "two/same.txt": "identical bytes"})
    e.scan()
    report = relate.detect(e.conn, None)
    assert report["exact_copy_groups"] == 1 and relate.list_candidates(e.conn)[1] == 0
    assert len(relate.duplicates_report(e.conn)["exact_bytes"]) == 1


# ------------------------------------------------------------------------------------------------ the run itself


def test_a_rerun_with_nothing_new_changes_nothing(make_library):
    lib = make_library({"a.pdf": paper(producer="A"), "b.pdf": paper(producer="B"), "supp.pdf": si()})
    accept(lib, "a.pdf", doi=DOI, title=TITLE)
    accept(lib, "supp.pdf", doi=DOI, title="Supporting Information")
    run(lib)
    before = [tuple(r) for r in lib.env.rows("SELECT * FROM relation_candidate ORDER BY candidate_id")]
    revision = lib.env.revision()
    report = run(lib)
    assert [tuple(r) for r in lib.env.rows("SELECT * FROM relation_candidate ORDER BY candidate_id")] == before
    assert lib.env.revision() == revision and set(report["outcomes"]) == {"unchanged"}


def test_a_proposal_whose_evidence_goes_stale_and_a_decision_is_never_reopened(make_library):
    lib = make_library({"a.pdf": paper(doi="10.5555/kv.x.1"), "b.pdf": paper(doi="10.5555/kv.x.2", body_pages=BODY + 1)})
    accept(lib, "a.pdf", doi="10.5555/kv.x.1", title=TITLE)
    accept(lib, "b.pdf", doi="10.5555/kv.x.1", title=TITLE)
    run(lib)
    (same,) = proposals(lib, "same_document")
    accept(lib, "b.pdf", doi="10.5555/kv.x.2")  # a person fixes b's DOI: the shared-DOI evidence is gone
    ledger = run(lib)
    assert ledger["outcomes"].get("stale") == 1 and proposals(lib, "same_document") == []
    relations.reject_candidate(lib.conn, relate.list_candidates(lib.conn, kind="same_document", statuses=("stale",))[0][0]["candidate_id"])
    accept(lib, "b.pdf", doi="10.5555/kv.x.1")
    run(lib)
    assert proposals(lib, "same_document") == []  # the rejected proposal stays rejected when its evidence returns
    assert same["candidate_id"] in {r["candidate_id"] for r in lib.env.rows("SELECT candidate_id FROM relation_candidate WHERE status = 'rejected'")}


def test_detection_without_an_index_still_runs_and_says_it_read_no_text(make_library):
    lib = make_library({"a.pdf": paper(), "b.pdf": paper(), "c.pdf": paper(title="Another One Altogether", doi="10.5555/kv.rel.9", authors="C. W.")})
    accept(lib, "a.pdf", doi=DOI, title=TITLE)
    accept(lib, "b.pdf", doi=DOI, title=TITLE)
    report = relate.detect(lib.conn, None)
    assert report["fingerprinted"] == 0 and len(pair(lib, "same_document", "a.pdf", "b.pdf")) == 1  # the DOI evidence needs no text


def test_a_run_is_recorded_and_a_killed_one_is_closed_by_the_next(make_library):
    lib = make_library({"a.pdf": paper()})
    lib.conn.execute("INSERT INTO relation_run (run_id, started_at, status, matcher_version) VALUES ('old', '2026-01-01T00:00:00Z', 'running', 'x')")
    report = run(lib)
    statuses = {r["run_id"]: r["status"] for r in lib.env.rows("SELECT run_id, status FROM relation_run")}
    assert statuses["old"] == "interrupted" and statuses[report["run_id"]] == "completed"
    assert json.loads(lib.env.one("SELECT stats_json FROM relation_run WHERE run_id = ?", report["run_id"]))["documents"] == 1


def test_detection_never_modifies_the_sources_or_merges_anything(make_library):
    lib = make_library({"a.pdf": paper(producer="A"), "b.pdf": paper(producer="B")})
    before = tree_hashes(lib.env.lib)
    documents = lib.env.one("SELECT COUNT(*) FROM document WHERE retired_at IS NULL")
    run(lib)
    assert tree_hashes(lib.env.lib) == before and lib.env.one("SELECT COUNT(*) FROM document WHERE retired_at IS NULL") == documents
    assert lib.env.one("SELECT COUNT(*) FROM document_relation") == 0 and lib.env.one("SELECT COUNT(*) FROM artifact_relation") == 0


def test_duplicates_report_separates_exact_bytes_identical_text_same_publication_and_related_work(tmp_path):
    lib = Library(tmp_path, {"a.pdf": paper(producer="A"), "b.pdf": paper(producer="B"), "pre.pdf": paper(doi="10.5555/kv.pre.1", title="A Preprint Title Which Is Different Here", body_pages=BODY + 1),
                             "pub.pdf": paper(doi="10.5555/kv.pub.1", title="A Preprint Title Which Is Different Here", body_pages=BODY + 2, authors="Z. Other")})
    try:
        write(lib.env.lib / "copy1.txt", "same bytes")
        write(lib.env.lib / "copy2.txt", "same bytes")
        lib.env.scan()
        accept(lib, "pre.pdf", doi="10.5555/kv.pre.1", title="A Preprint Title Which Is Different Here", type="posted-content")
        accept(lib, "pub.pdf", doi="10.5555/kv.pub.1", title="A Preprint Title Which Is Different Here", type="journal-article")
        run(lib)
        report = relate.duplicates_report(lib.conn)
        assert len(report["exact_bytes"]) == 1 and report["exact_bytes"][0]["copies"] == 2
        assert {p["kind"] for p in report["identical_text"]} == {"same_document", "equivalent_to"}
        assert report["same_publication"] == [] and [p["kind"] for p in report["related_work"]] == ["version_of"]
        assert report["documents_with_several_artifacts"] == []
        relations.accept_candidate(lib.conn, next(p["candidate_id"] for p in report["identical_text"] if p["kind"] == "same_document"))
        assert len(relate.duplicates_report(lib.conn)["documents_with_several_artifacts"]) == 1
    finally:
        lib.close()


# ------------------------------------------------------------------------------------------------ found on the real library


def test_a_correction_that_mentions_the_supporting_information_is_related_to_its_paper_not_a_supplement_of_it(make_library):
    correction = {"title": "Correction", "authors": "A. Examplar and B. Placeholder", "doi": DOI, "body_pages": BODY,
                  "journal": f"Correction to: {TITLE}. The Supporting Information is available free of charge online."}
    lib = make_library({"paper.pdf": paper(), "fix.pdf": correction})
    accept(lib, "paper.pdf", doi=DOI, title=TITLE)
    accept(lib, "fix.pdf", doi=DOI, title="Correction")  # the correction prints the paper's own DOI, so resolve accepts it too
    run(lib)
    assert proposals(lib, "supplement_of") == []  # it SAYS "Supporting Information", and is still not one
    assert pair(lib, "same_document", "fix.pdf", "paper.pdf") == []  # sharing the paper's DOI does not make a correction the paper
    (related,) = pair(lib, "related_to", "fix.pdf", "paper.pdf")
    assert related["confidence"] == "high" and related["evidence"]["correction"] is True


def test_a_doi_many_documents_print_forms_a_collection_proposal_even_though_none_may_accept_it(make_library):
    entries = {f"e{i}.pdf": paper(title=f"Entry Number {i} of the Imaginary Encyclopedia of Things", doi=DOI, authors=f"Author{i} Person") for i in range(4)}
    lib = make_library(entries)
    lib.resolve()  # offline: proposes DOIs, accepts none; the shared-parent rule marks them
    assert not any(lib.values(n).get("doi") for n in entries)
    run(lib)
    (group,) = proposals(lib, "collection")
    assert len(group["members"]) == 4 and group["evidence"]["parent_doi"] == DOI and proposals(lib, "same_document") == []


def test_a_retired_document_still_resolves_by_its_id_so_its_history_can_be_read(make_library):
    from knowledgevista.services import resolve
    lib = make_library({"a.pdf": paper(producer="A"), "b.pdf": paper(producer="B")})
    kept, gone = lib.doc("a.pdf"), lib.doc("b.pdf")
    relations.merge(lib.conn, kept, gone)
    match = resolve.resolve_one(lib.conn, gone)
    assert match.document_id == gone and match.artifact_id == lib.env.one("SELECT artifact_id FROM location WHERE relative_path = 'b.pdf'")
