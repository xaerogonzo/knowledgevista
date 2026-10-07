"""Candidates a document supports from itself: which are safe for a batch rule, which wait for a person, and why."""

from __future__ import annotations

import pytest

from knowledgevista.domain.frontmatter import FrontFacts, TitleGuess
from knowledgevista.domain.local_evidence import SAFE_LOCAL_DOI_SCORE, local_specs

DOI = "10.5555/kv.local.0001"
TITLE = "Aqueous Solubility of Invented Nitrate Esters at Elevated Temperature"
PAGE1 = (
    f"Journal of Invented Results\nCite This: J. Invented 2021, 12, 54-58\nhttps://doi.org/{DOI}\n{TITLE}\n"
    "Alexandra Examplar and Boris Placeholder\nReceived: 3 March 2021\n"
)
PAGES = [(1, PAGE1), (2, "Body of the paper."), (3, "More body.")]


def facts(**changes) -> FrontFacts:
    base = dict(layout_title=TitleGuess(TITLE, 20.0, 10.0, 1), info_title=TITLE)
    base.update(changes)
    return FrontFacts(**base)


def by(result, field, source=None):
    return [s for s in result.specs if s.field == field and (source is None or s.source == source)]


def test_a_clean_article_yields_a_safe_doi_and_a_safe_title_and_unsafe_authors():
    result = local_specs(PAGES, 3, facts(info_authors=["Alexandra Examplar", "Boris Placeholder"]), "cm4c01978")
    (doi,) = by(result, "doi")
    assert (doi.value, doi.classification, doi.review, doi.confidence) == (DOI, "own", "safe", "high")
    assert doi.evidence["score"] >= SAFE_LOCAL_DOI_SCORE and doi.evidence["cues"]
    titles_ = by(result, "title")
    assert {t.source for t in titles_} == {"layout_title", "pdf_info_title"}
    assert all(t.review == "safe" and t.confidence == "high" and t.evidence["printed_on_first_pages"] for t in titles_)
    (authors,) = by(result, "authors")
    assert authors.review == "required" and authors.confidence == "medium"  # surnames are printed, but never safe locally


def test_a_title_from_one_source_alone_is_never_safe():
    result = local_specs(PAGES, 3, facts(info_title=None), "x")
    (title,) = by(result, "title")
    assert title.source == "layout_title" and title.review == "required" and title.confidence == "medium"


def test_a_file_metadata_title_that_is_not_printed_on_the_first_pages_is_low_confidence_and_not_safe():
    other = "A Completely Different Title About Something Else Entirely"
    result = local_specs(PAGES, 3, facts(layout_title=None, info_title=other), "x")
    (title,) = by(result, "title")
    assert title.confidence == "low" and title.review == "required" and not title.evidence["printed_on_first_pages"]


def test_two_sources_that_disagree_do_not_make_each_other_safe():
    result = local_specs(PAGES, 3, facts(info_title="Another Title Which Also Appears Nowhere At All"), "x")
    assert all(t.review == "required" for t in by(result, "title"))


def test_two_copies_of_the_files_own_metadata_are_not_two_witnesses_the_layout_must_be_one():
    """Measured: two publishers' files carried the same junk in the Info and XMP titles, and 'two sources agree' accepted it."""
    result = local_specs(PAGES, 3, facts(layout_title=None, info_title=TITLE, xmp_title=TITLE), "x")
    titles_ = by(result, "title")
    assert {t.source for t in titles_} == {"pdf_info_title", "pdf_xmp_title"} and all(t.review == "required" for t in titles_)
    assert all(t.confidence == "high" for t in titles_)  # they do agree and are printed; confidence is not the same as safety
    with_layout = local_specs(PAGES, 3, facts(info_title=None, xmp_title=TITLE), "x")
    assert all(t.review == "safe" for t in by(with_layout, "title"))


def test_a_doi_many_documents_call_their_own_is_ambiguous_and_never_safe():
    from knowledgevista.domain.doi_evidence import classify_dois
    from knowledgevista.domain.local_evidence import SHARED_DOI_DOCUMENTS, shared_dois
    book = classify_dois(PAGES, 3)
    assert shared_dois([book] * (SHARED_DOI_DOCUMENTS - 1)) == {}
    shared = shared_dois([book] * SHARED_DOI_DOCUMENTS)
    assert shared == {DOI: SHARED_DOI_DOCUMENTS}
    (doi,) = by(local_specs(PAGES, 3, None, shared=shared), "doi")
    assert (doi.classification, doi.review, doi.confidence) == ("ambiguous", "required", "ambiguous")
    assert doi.evidence["shared_by_documents"] == SHARED_DOI_DOCUMENTS and "parent work" in doi.evidence["notes"][0]
    (unshared,) = by(local_specs(PAGES, 3, None, shared={}), "doi")
    assert unshared.review == "safe" and unshared.evidence["shared_by_documents"] is None


def test_only_own_dois_count_toward_being_shared():
    from knowledgevista.domain.doi_evidence import classify_dois
    from knowledgevista.domain.local_evidence import shared_dois
    cited = classify_dois([(1, "Title"), (2, "See 10.5555/kv.cited.0001 for details."), (3, "More text.")], 3)
    assert not cited.own and shared_dois([cited] * 5) == {}


@pytest.mark.parametrize("verdict", ["weak", "unverifiable"])
def test_a_provider_that_only_weakly_matches_vetoes_the_safe_flag(verdict):
    (doi,) = by(local_specs(PAGES, 3, None, checks={DOI: verdict}), "doi")
    assert doi.review == "required" and doi.status == "proposed" and any("weakly" in n for n in doi.evidence["notes"])
    for confirms in ("exact", "strong"):
        assert by(local_specs(PAGES, 3, None, checks={DOI: confirms}), "doi")[0].review == "safe"


def test_without_front_matter_the_text_candidates_are_still_produced_and_the_gap_is_said():
    result = local_specs(PAGES, 3, None, "x")
    assert by(result, "doi") and not by(result, "title") and not by(result, "authors")
    assert any("front matter was not read" in n for n in result.notes)


def test_a_wrapped_doi_is_never_safe():
    page = "Cite This: J. Invented 2021\nhttps://doi.org/10.5555/kv.wrapped.\n0042\nReceived: 3 March 2021\n"
    (doi,) = by(local_specs([(1, page), (2, "x")], 2, None), "doi")
    assert doi.value == "10.5555/kv.wrapped.0042" and doi.review == "required"


def test_two_own_looking_dois_are_never_safe():
    page = f"Received: 1 May 2021\n{DOI}\nReceived: 2 June 2021\n10.5555/kv.local.0002\n"
    specs = by(local_specs([(1, page), (2, "x")], 2, None), "doi")
    assert len(specs) == 2 and all(s.review == "required" and s.classification == "ambiguous" for s in specs)


def test_an_earlier_provider_verdict_is_honoured_by_the_next_local_pass():
    contradicted = by(local_specs(PAGES, 3, None, checks={DOI: "contradicted"}), "doi")[0]
    assert contradicted.classification == "foreign" and contradicted.status == "set_aside" and contradicted.review == "required"
    assert any("does not match" in n for n in contradicted.evidence["notes"])
    unknown = by(local_specs(PAGES, 3, None, checks={DOI: "no_match"}), "doi")[0]
    assert unknown.status == "proposed" and unknown.review == "required"  # a printed DOI the provider has never heard of
    assert any("no record" in n for n in unknown.evidence["notes"])


def test_a_cover_page_list_of_other_papers_is_not_stored_as_set_aside_rows():
    others = "\n".join(f"J. Invented Phys. {i} (2012); 10.5555/kv.cover.{i:04d}" for i in range(6))
    page = f"Title\nCitation: J. Invented 146 (2017); doi: {DOI}\nPublished by the Institute\n{others}"
    result = local_specs([(1, page), (2, "x")], 2, None)
    assert [s.value for s in result.specs if s.field == "doi"] == [DOI]


def test_a_foreign_doi_near_the_front_is_kept_as_set_aside_so_the_question_why_not_this_one_has_an_answer():
    page = f"{PAGE1}\nSee also the companion paper 10.5555/kv.local.0002 for details.\n"
    specs = by(local_specs([(1, page), (2, "x")], 2, None), "doi")
    assert {s.value: (s.status, s.classification) for s in specs} == {DOI: ("proposed", "own"), "10.5555/kv.local.0002": ("set_aside", "foreign")}


def test_a_doi_only_the_file_metadata_names_is_offered_as_ambiguous():
    scan = [(1, ""), (2, "")]
    result = local_specs(scan, 2, FrontFacts(metadata_dois=[DOI]))
    (doi,) = by(result, "doi")
    assert doi.source == "pdf_metadata_doi" and doi.review == "required" and doi.classification == "ambiguous"
    assert any("no text layer" in n for n in result.notes)


def test_isbns_are_validated_listed_each_and_never_safe():
    page = "Copyright page.\nISBN 978-3-16-148410-0 (hardback)\nISBN-10: 0-306-40615-2\nISBN 978-3-16-148410-1 (wrong check digit)\n"
    specs = by(local_specs([(1, "Title"), (4, page)], 4, None), "isbn")
    assert sorted(s.value for s in specs) == ["0306406152", "9783161484100"]
    assert all(s.review == "required" for s in specs)


def test_isbns_beyond_the_first_pages_are_not_read():
    page = "ISBN 978-3-16-148410-0"
    assert by(local_specs([(1, "x"), (30, page)], 30, None), "isbn") == []


def test_one_arxiv_identifier_on_the_first_page_is_safe_and_two_are_not():
    one = by(local_specs([(1, "arXiv:2105.12345v2 [physics.chem-ph] 4 Jun 2021\nTitle"), (2, "arXiv:2201.00001")], 2, None), "arxiv")
    assert [(s.value, s.review) for s in one] == [("2105.12345", "safe")]
    two = by(local_specs([(1, "arXiv:2105.12345v2\nsee arXiv:2201.00001")], 1, None), "arxiv")
    assert len(two) == 2 and all(s.review == "required" and s.confidence == "ambiguous" for s in two)
    old = by(local_specs([(1, "arXiv:hep-th/9901001v3")], 1, None), "arxiv")
    assert [s.value for s in old] == ["hep-th/9901001"]


def test_a_title_like_filename_is_a_low_confidence_hint_and_an_id_like_one_is_nothing():
    hint = by(local_specs(PAGES, 3, FrontFacts(), "Hansen solubility parameters handbook"), "title", "filename_hint")
    assert len(hint) == 1 and hint[0].confidence == "low" and hint[0].review == "required"
    assert by(local_specs(PAGES, 3, FrontFacts(), "cm4c01978"), "title") == []


def test_authors_whose_surnames_are_not_printed_are_low_confidence():
    result = local_specs(PAGES, 3, FrontFacts(info_authors=["Zed Nobodyknown"]), "x")
    (authors,) = by(result, "authors")
    assert authors.confidence == "low" and authors.evidence["surnames_printed_on_first_pages"] == 0


def test_output_is_deterministic_and_keys_do_not_depend_on_dict_order():
    first = local_specs(PAGES, 3, facts(), "x")
    second = local_specs(list(PAGES), 3, facts(), "x")
    assert [(s.field, s.evidence_key, s.value, s.review) for s in first.specs] == [(s.field, s.evidence_key, s.value, s.review) for s in second.specs]
    assert len({(s.field, s.evidence_key) for s in first.specs}) == len(first.specs)  # no two proposals share an identity


def test_a_wrapped_doi_is_not_safe_even_when_it_would_score_enough_anyway():
    header = "Cite This: J. Invented 2021\nhttps://doi.org/10.5555/kv.wrapped.\n0042\nReceived: 3 March 2021\n"
    (doi,) = by(local_specs([(1, header), (2, header + "x"), (3, header + "x")], 3, None), "doi")
    assert doi.evidence["score"] >= SAFE_LOCAL_DOI_SCORE and doi.evidence["wrapped"]  # the oracle: the score alone would allow it
    assert doi.review == "required"


def test_a_wrong_isbn_10_check_digit_is_ignored_like_a_wrong_isbn_13():
    page = "ISBN-10: 0-306-40615-2\nISBN 0-306-40615-3 (typo)\n"
    assert [s.value for s in by(local_specs([(1, "Title"), (4, page)], 4, None), "isbn")] == ["0306406152"]
