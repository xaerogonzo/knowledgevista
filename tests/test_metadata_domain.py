"""The pure metadata logic: DOI parsing, title comparison, and the own/foreign DOI classifier.

All inputs are invented text; `10.5555` is Crossref's reserved test prefix. The classifier tests are written as the
false positives the plan names (DOI only in references, a foreign DOI in the body, a crowded first page) and each
asserts WHY, through the score components, not only the verdict.
"""

from __future__ import annotations

import pytest

from knowledgevista.domain import doi as doimod
from knowledgevista.domain import titles
from knowledgevista.domain.doi_evidence import MAX_PAGES_SCANNED, classify_dois

OWN = "10.5555/kv.own.0001"
OTHER = "10.5555/kv.other.0002"
THIRD = "10.5555/kv.third.0003"


# ---------------------------------------------------------------------------------------------------- DOI parsing


@pytest.mark.parametrize("raw", [
    "10.5555/KV.Own.0001", "doi:10.5555/kv.own.0001", "DOI: 10.5555/kv.own.0001", "https://doi.org/10.5555/kv.own.0001",
    "http://dx.doi.org/10.5555/kv.own.0001", " 10.5555/kv.own.0001. ", "10.5555/kv.own.0001);", "10.5555/kv.own.0001”",
])
def test_normalise_doi_reaches_one_spelling(raw):
    assert doimod.normalise_doi(raw) == "10.5555/kv.own.0001"


def test_normalise_doi_is_idempotent_and_rejects_non_dois():
    for raw in ("10.5555/ABC(1999)12-3", "10.1002/(SICI)1097-0126(1999)1:2<3::AID-X>3.0.CO;2-T"):
        once = doimod.normalise_doi(raw)
        assert once is not None and doimod.normalise_doi(once) == once
    for bad in ("", None, "11.5555/x", "10.55/x", "10.5555/", "no doi here", "10.5555/" + "x" * 300):
        assert doimod.normalise_doi(bad) is None


def test_balanced_closing_bracket_is_kept_unbalanced_is_not():
    assert doimod.normalise_doi("10.5555/abc(1999)") == "10.5555/abc(1999)"
    assert doimod.normalise_doi("10.5555/abc1999)") == "10.5555/abc1999"


def test_find_dois_reports_position_and_every_occurrence():
    text = f"Cite: https://doi.org/{OWN}.\nAlso {OTHER}, and again {OWN})"
    found = doimod.find_dois(text)
    assert [o.doi for o in found] == [OWN, OTHER, OWN]
    assert text[found[0].start:found[0].end].startswith("10.5555/kv.own.0001")
    assert not any(o.wrapped for o in found)


def test_a_doi_wrapped_across_lines_is_joined_and_flagged():
    text = "Cite this: J. Invented 2022, 1, 1.\nhttps://doi.org/10.5555/kv.wrapped.\n0042 Received: 1 May 2022"
    (found,) = doimod.find_dois(text)
    assert found.doi == "10.5555/kv.wrapped.0042"
    assert found.wrapped


def test_a_sentence_ending_in_a_doi_is_not_joined_to_the_next_sentence():
    # The oracle for the join rule: the same shape (DOI, full stop, newline) with a next word that has no digit.
    (found,) = doimod.find_dois("As shown in 10.5555/kv.own.0001.\nThe next sentence begins here.")
    assert found.doi == OWN
    assert not found.wrapped


def test_a_wrapped_doi_with_crlf_line_endings_joins():
    (found,) = doimod.find_dois("see 10.5555/kv.wrapped-\r\n0042 end")
    assert found.doi == "10.5555/kv.wrapped-0042"


# ---------------------------------------------------------------------------------------------------- titles

TITLE = "Solubility of Invented Compounds in Aqueous Media"


def test_alnum_key_ignores_what_extraction_does_to_a_title():
    mangled = "Solu-\nbility  of Invented\tCompounds in\nAqueous Media"
    assert titles.alnum_key(mangled) == titles.alnum_key(TITLE)
    assert titles.alnum_key("Café β-lactam H₂O") == titles.alnum_key("cafe beta lactam H2O".replace("beta", "β"))


def test_is_printed_finds_a_title_across_line_breaks_and_refuses_a_short_one():
    page = f"Journal of Invented Results\n{TITLE[:25]}\n{TITLE[25:]}\nA. Author"
    assert titles.is_printed(TITLE, page)
    assert not titles.is_printed("Introduction", "Introduction\nbody text")  # short titles prove nothing
    assert not titles.is_printed(TITLE, "A different paper about nothing in particular, entirely.")


def test_containment_and_similarity_tell_a_partial_title_from_a_different_one():
    assert titles.containment(TITLE, f"x {TITLE} y") == 1.0
    partial = titles.containment(TITLE, "solubility of compounds in media only")
    assert 0.3 < partial < 1.0
    assert titles.containment(TITLE, "") == 0.0 and titles.containment("", TITLE) == 0.0
    assert titles.similarity(TITLE, TITLE.upper()) == 1.0
    assert titles.similarity(TITLE, "A Comment on " + TITLE) < 0.9  # a "Comment on" is a different paper
    assert titles.similarity(TITLE, "") == 0.0


def test_clean_strips_markup_and_decodes_entities():
    assert titles.clean("Cu<sub>2</sub>O &amp; <i>ab initio</i>   study") == "Cu2O & ab initio study"


@pytest.mark.parametrize("junk", [
    "Microsoft Word - manuscript_final.doc", "untitled", "Untitled-1", "document1.pdf", "cm4c01978", "10.5555/kv.own.0001",
    "ab", "PowerPoint Presentation", "paper.tex", "0123456789abcdef0123456789abcdef",
    # Found on a real library: a Title field holding the paper's own identifier.
    "doi:10.1016/j.neulet.2008.07.048", "DOI 10.1021/jm030878b", "PII: S0009-2614(08)00123-4", "http://www.example.org/paper", "www.example.org",
    "S0009-2614(08)01234-5", "https://doi.org/10.5555/kv.x",
])
def test_junk_titles_are_recognised(junk):
    assert titles.is_junk_title(junk)


def test_a_real_title_is_not_junk_and_the_filename_itself_is():
    assert not titles.is_junk_title(TITLE)
    # Words that merely resemble the identifier patterns are not identifiers: the oracle that the filter is not a blanket refusal.
    for fine in ("Doing Chemistry With DOI Metadata: A Study", "Arxiv Papers and Their Citation Patterns", "Digital Object Identifiers in Practice"):
        assert not titles.is_junk_title(fine), fine
    assert titles.is_junk_title("kaya2022", filename_stem="kaya2022")
    assert titles.is_junk_title("Some Long Name Here", filename_stem="Some_Long_Name_Here")


def test_filename_hints():
    assert titles.filename_title_hint("Hansen solubility parameters handbook") == "Hansen solubility parameters handbook"
    assert titles.filename_title_hint("hansen_solubility-parameters_handbook") == "hansen solubility parameters handbook"
    assert titles.filename_title_hint("cm4c01978") is None and titles.filename_title_hint("kaya2022") is None
    assert titles.filename_author_year("kaya2022") == ("kaya", 2022)
    assert titles.filename_author_year("Smith-2019b_supp") == ("smith", 2019)
    assert titles.filename_author_year("cm4c01978") is None


# ---------------------------------------------------------------------------------------------------- classifier


def front(own: str = OWN, extra: str = "") -> str:
    return f"Journal of Invented Results\nCite This: J. Invented 2022, 1, 1-9\nhttps://doi.org/{own}\nReceived: 1 May 2022\n{extra}"


def references(*dois: str) -> str:
    return "REFERENCES\n" + "\n".join(f"({i}) Someone, A. Title. J. Elsewhere 2001, 1, 1. doi:{d}" for i, d in enumerate(dois, 1))


def verdicts(evidence) -> dict[str, str]:
    return {c.doi: c.classification for c in evidence.candidates}


def test_own_doi_printed_on_the_front_with_a_publisher_cue_is_own():
    evidence = classify_dois([(1, front()), (2, "Body text."), (3, references(OTHER, THIRD))])
    (candidate,) = evidence.candidates
    assert candidate.doi == OWN and candidate.classification == "own"
    assert candidate.components["on_first_page"] and candidate.components["publisher_cue_nearby"]
    assert {"received", "cite this"} <= set(candidate.cues)
    assert evidence.bibliography_page == 3 and evidence.in_bibliography == 2  # counted, never proposed


def test_a_doi_only_in_the_references_is_never_a_candidate():
    """The classic false positive: the paper prints no DOI of its own, and its bibliography cites another's."""
    evidence = classify_dois([(1, "A Paper With No DOI\nA. Author"), (2, "Body."), (3, references(OTHER))])
    assert evidence.candidates == [] and evidence.in_bibliography == 1 and evidence.bibliography_page == 3


def test_the_same_doi_before_the_heading_is_judged_by_where_it_is_printed_outside_it():
    evidence = classify_dois([(1, front()), (2, references(OWN))])
    assert verdicts(evidence) == {OWN: "own"}
    assert evidence.in_bibliography == 0


def test_a_foreign_doi_in_the_body_is_not_own():
    evidence = classify_dois([(1, front()), (2, f"As reported previously (doi:{OTHER}), the effect is small."), (3, "More.")])
    result = verdicts(evidence)
    assert result[OWN] == "own"
    assert result[OTHER] != "own"
    other = next(c for c in evidence.candidates if c.doi == OTHER)
    assert other.score < next(c for c in evidence.candidates if c.doi == OWN).score


def test_two_dois_on_the_first_page_the_one_with_the_cue_wins_and_the_other_is_set_aside_as_outranked():
    page = front() + f"\nSee also the companion paper {OTHER} for details."
    evidence = classify_dois([(1, page), (2, "Body.")])
    result = {c.doi: c for c in evidence.candidates}
    assert result[OWN].classification == "own"
    assert result[OTHER].classification == "foreign" and result[OTHER].outranked_by == OWN


def test_two_equally_plausible_dois_are_both_ambiguous():
    page = f"Received: 1 May 2022\n{OWN}\nReceived: 2 June 2022\n{OTHER}\n"
    evidence = classify_dois([(1, page), (2, "Body.")])
    assert verdicts(evidence) == {OWN: "ambiguous", OTHER: "ambiguous"}
    assert not evidence.own


def test_a_crowded_first_page_is_penalised_and_a_list_page_is_not_front_matter():
    crowd = "\n".join(f"10.5555/kv.item.{i:04d}" for i in range(7))  # a table of contents / volume index
    evidence = classify_dois([(1, crowd), (2, "Body.")])
    assert not evidence.own
    assert all(c.components.get("only_on_list_pages") for c in evidence.candidates)
    four = "\n".join(f"see 10.5555/kv.item.{i:04d} for details" for i in range(4))
    crowded = classify_dois([(1, four), (2, "Body.")])
    assert all(c.components.get("crowded_first_page") == -2 for c in crowded.candidates)


def test_a_header_repeated_across_pages_adds_to_the_score():
    header = f"Invented Results {OWN}\n"
    once = classify_dois([(1, header), (2, "x"), (3, "x")])
    repeated = classify_dois([(1, header), (2, header + "x"), (3, header + "x")])
    assert repeated.candidates[0].score == once.candidates[0].score + 1
    assert repeated.candidates[0].components["repeated_on_pages"] == 1


def test_a_doi_first_seen_late_without_support_is_not_own():
    evidence = classify_dois([(1, "Title"), (2, "x"), (3, "x"), (4, f"Supplementary data: {OTHER}")])
    assert not evidence.own
    assert verdicts(evidence)[OTHER] in ("foreign", "ambiguous")


def test_a_references_heading_on_page_one_counts_only_in_a_one_page_document():
    body = f"Title\n{OWN}\n"
    many = classify_dois([(1, body + references(OTHER)), (2, "x")])
    assert many.bibliography_page is None  # a longer paper's first page cannot START its bibliography
    single = classify_dois([(1, body + references(OTHER))], page_count=1)
    assert single.bibliography_page == 1 and verdicts(single) == {OWN: "own"} and single.in_bibliography == 1


def test_a_wrapped_doi_is_penalised_and_flagged():
    page = "Cite This: J. Invented 2022\nhttps://doi.org/10.5555/kv.wrapped.\n0042\nReceived: 1 May 2022"
    (candidate,) = classify_dois([(1, page), (2, "x")]).candidates
    assert candidate.doi == "10.5555/kv.wrapped.0042" and candidate.wrapped
    assert candidate.components["wrapped_across_lines"] == -1


def test_a_long_document_is_scanned_at_both_ends_only_and_says_so():
    pages = [(n, f"page {n}") for n in range(1, 401)]
    pages[0] = (1, front())
    pages[399] = (400, references(OTHER))
    evidence = classify_dois(pages)
    assert evidence.pages_scanned == MAX_PAGES_SCANNED and evidence.pages_total == 400
    assert verdicts(evidence) == {OWN: "own"} and evidence.in_bibliography == 1


def test_classification_is_deterministic_and_carries_an_excerpt_and_the_matcher_version():
    pages = [(1, front()), (2, "Body."), (3, references(OTHER))]
    first, second = classify_dois(pages), classify_dois(list(pages))
    assert [vars(c) for c in first.candidates] == [vars(c) for c in second.candidates]
    assert OWN in first.candidates[0].excerpt and "Cite This" in first.candidates[0].excerpt
    from knowledgevista.domain.doi_evidence import MATCHER_VERSION
    assert MATCHER_VERSION.startswith("doi-evidence-")


# ------------------------------------------------------------------------- shapes found on a real library (invented text)


def aip_cover(own: str = OWN) -> str:
    """A publisher cover page: the paper's own DOI on a 'Citation:' line, then a list of OTHER papers' DOIs."""
    others = "\n".join(f"J. Invented Phys. {i}, 1{i} (2012); 10.5555/kv.cover.{i:04d}" for i in range(6))
    return (f"Title of the Paper\nA. Author\nCitation: J. Invented Phys. 146, 124117 (2017); doi: {own}\n"
            f"View online: http://dx.doi.org/{own}\nPublished by the Institute\nArticles you may be interested in\n{others}")


def test_a_cover_page_listing_other_papers_still_finds_the_papers_own_doi():
    evidence = classify_dois([(1, aip_cover()), (2, "THE JOURNAL 146 (2017)\nBody.")])
    result = {c.doi: c for c in evidence.candidates}
    assert result[OWN].classification == "own"
    assert "citation" in result[OWN].cues and not result[OWN].components.get("only_on_list_pages")
    others = [c for c in evidence.candidates if c.doi != OWN]
    assert len(others) == 6 and all(c.classification == "foreign" for c in others)
    assert all(c.components.get("only_on_list_pages") == -3 for c in others)


def test_a_one_page_erratum_under_a_references_heading_keeps_its_own_doi_and_not_the_original_s():
    page = ("Erratum to 'A Paper' [J. Invented 1 (2007) 315]\nThe eigenvalues given in Table 1 are incorrect.\n"
            "References\n[1] J. Chen, J. Invented 1 (2007) 315.\n0000-0000/$ - see front matter 2008 Invented Press. All rights reserved.\n"
            f"doi:{OWN}\nDOI of original article: {OTHER}\n")
    evidence = classify_dois([(1, page)], page_count=1)
    result = {c.doi: c for c in evidence.candidates}
    assert result[OWN].classification == "own" and result[OWN].cues
    assert OTHER not in result and evidence.in_bibliography == 1  # the original's DOI is a citation, never a candidate
    assert evidence.bibliography_page == 1


def test_words_that_name_another_work_count_against_the_doi_on_their_own_line_only():
    page = f"Title\nReceived: 1 May 2022\n{OWN}\nThis comment on {OTHER} is brief.\nReceived: 3 May 2022\n"
    result = {c.doi: c for c in classify_dois([(1, page), (2, "Body.")]).candidates}
    assert result[OTHER].components["names_another_work"] == -3
    assert "names_another_work" not in result[OWN].components  # the line below it names another work; its own line does not
    assert result[OWN].classification == "own"


def test_a_reference_line_without_publisher_words_stays_in_the_bibliography_even_in_a_one_page_document():
    page = f"Short note\nBody.\nReferences\n[1] Other, A. J. Elsewhere 1 (2001) 1. doi:{OTHER}\n"
    evidence = classify_dois([(1, page)], page_count=1)
    assert evidence.candidates == [] and evidence.in_bibliography == 1


def test_a_control_character_after_a_doi_is_not_part_of_it():
    (found,) = doimod.find_dois(f"doi:{OWN}\x04 Received 1 May")
    assert found.doi == OWN
    (found,) = doimod.find_dois(f"{OWN}​ trailing")
    assert found.doi == OWN


# ------------------------------------------------------------------------- gaps the mutation sweep found


def test_a_second_strong_doi_is_ambiguous_not_foreign_when_the_first_is_clearly_better():
    header = f"Invented Results {OWN}\n"
    page1 = front() + f"\nCopyright 2021 Invented Press\nData set: {OTHER}\n"
    result = {c.doi: c for c in classify_dois([(1, page1), (2, header + "x"), (3, header + "x")]).candidates}
    assert result[OWN].classification == "own" and result[OWN].score - result[OTHER].score >= 2 and result[OTHER].score >= 4
    assert result[OTHER].classification == "ambiguous" and result[OTHER].outranked_by is None  # strong evidence is never called foreign


def test_a_doi_only_in_the_references_that_the_files_own_metadata_also_names_is_a_candidate_not_a_reference():
    pages = [(1, "Title"), (2, "Body."), (3, references(OTHER))]
    plain = classify_dois(pages)
    assert plain.candidates == [] and plain.in_bibliography == 1
    claimed = classify_dois(pages, metadata_dois=[OTHER.upper()])
    assert [(c.doi, c.classification, c.components) for c in claimed.candidates] == [(OTHER, "ambiguous", {"in_pdf_metadata": 2})]
    assert claimed.in_bibliography == 0  # it is not also counted as a reference-only DOI
