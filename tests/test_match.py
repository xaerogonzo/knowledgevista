"""Does a provider's record describe THIS PDF? Each test is a false positive the plan names, plus the positive that
proves the matcher is not simply refusing everything (an oracle that accepts something must exist)."""

from __future__ import annotations

from knowledgevista.domain.match import choose_search_match, verify_work

TITLE = "Aqueous Solubility of Invented Nitrate Esters at Elevated Temperature"
PAGE = (
    f"Journal of Invented Results 12 (2021) 54-58\n{TITLE}\nAlexandra Examplar, Boris Placeholder\n"
    "Received 3 March 2021; accepted 9 June 2021; published online 31 July 2021\nAbstract. We measured the solubility."
)


def work(**changes) -> dict:
    base = {"provider": "crossref", "doi": "10.5555/kv.match.0001", "title": TITLE, "subtitle": None,
            "authors": [{"family": "Examplar", "given": "Alexandra", "name": None}, {"family": "Placeholder", "given": "Boris", "name": None}],
            "year": 2021, "years": {"issued": 2021, "print": 2021, "online": 2021}, "container": "Journal of Invented Results",
            "publisher": "Invented Press", "type": "journal-article", "volume": "12", "issue": None, "pages": "54-58",
            "preprint": False, "score": 60.0}
    base.update(changes)
    return base


def test_the_right_record_is_exact_and_says_why():
    verdict = verify_work(work(), PAGE, [TITLE])
    assert verdict.level == "exact" and verdict.confirms
    assert verdict.components["title_printed"] and verdict.components["authors_found"] == 2
    assert verdict.components["title_equals_layout_title"] is True and not verdict.components["year_conflict"]


def test_a_title_printed_in_a_longer_title_is_not_exact_a_correction_is_not_its_original():
    correction_page = f"Correction to “{TITLE}”\nAlexandra Examplar, Boris Placeholder\nPublished: September 24, 2021\n"
    local = [f"Correction to “{TITLE}”"]
    original = verify_work(work(), correction_page, local)
    assert original.level == "strong" and not original.level == "exact"
    assert any("different title" in r for r in original.reasons)
    correction = verify_work(work(doi="10.5555/kv.match.0002", title=f"Correction to “{TITLE}”"), correction_page, local)
    assert correction.level == "exact"


def test_the_oracle_a_matcher_without_the_layout_title_cannot_tell_them_apart():
    correction_page = f"Correction to “{TITLE}”\nAlexandra Examplar, Boris Placeholder\nPublished: 2021\n"
    assert verify_work(work(), correction_page, None).level == "exact"  # why the layout title is consulted at all


def test_a_wrong_year_stops_a_record_short_of_exact():
    verdict = verify_work(work(year=2008, years={"issued": 2008, "print": None, "online": None}), PAGE, [TITLE])
    assert verdict.level == "strong" and verdict.components["year_conflict"] and any("years" in r for r in verdict.reasons)


def test_a_year_within_one_of_any_known_date_is_not_a_conflict():
    verdict = verify_work(work(year=2022, years={"issued": 2022, "print": 2022, "online": 2021}), PAGE, [TITLE])
    assert verdict.level == "exact"


def test_a_page_with_no_years_cannot_contradict_the_year():
    page = PAGE.replace("2021", "").replace("Received 3 March ; accepted 9 June ; published online 31 July ", "")
    assert not verify_work(work(), page, [TITLE]).components["year_conflict"]


def test_the_title_without_any_author_is_weak_not_exact():
    verdict = verify_work(work(authors=[{"family": "Nobodyknown", "given": "N.", "name": None}]), PAGE, [TITLE])
    assert verdict.level == "weak" and any("no author" in r for r in verdict.reasons)


def test_a_record_about_something_else_is_contradicted():
    other = work(title="Crystal Structures of Imaginary Perovskite Oxides", authors=[{"family": "Differentperson", "given": "D.", "name": None}])
    verdict = verify_work(other, PAGE, [TITLE])
    assert verdict.level == "contradicted" and not verdict.confirms


def test_ocr_garbage_never_verifies():
    garbage = "Aqu3ous S0lub1lity of Inv3nted N1trate Est3rs\nAlex@ndra Ex@mplar, B0ris Pl@ceholder\n\x0c~~ ,, ;;"
    assert verify_work(work(), garbage, ["Aqu3ous S0lub1lity of Inv3nted N1trate Est3rs"]).level in ("weak", "contradicted")


def test_a_short_generic_title_proves_nothing_even_if_printed():
    short = work(title="Introduction")
    verdict = verify_work(short, "Introduction\nAlexandra Examplar\n", ["Introduction"])
    assert verdict.level != "exact"


def test_authors_with_particles_and_a_consortium_are_handled():
    particle = work(authors=[{"family": "van der Waals", "given": "J.", "name": None}, {"family": None, "given": None, "name": "The Consortium"}])
    verdict = verify_work(particle, PAGE.replace("Alexandra Examplar", "J. van der Waals"), [TITLE])
    assert verdict.components["authors_found"] == 1 and verdict.components["authors_checked"] == 1
    assert verdict.level == "exact"


def test_a_record_with_nothing_to_compare_is_unverifiable():
    assert verify_work(work(title=None, authors=[]), PAGE, [TITLE]).level == "unverifiable"


def test_a_preprint_record_is_marked_in_the_components():
    assert verify_work(work(preprint=True, type="posted-content"), PAGE, [TITLE]).components["record_is_preprint"]


# --------------------------------------------------------------------------------------------------- title search


def test_the_published_version_is_chosen_over_its_preprint_and_the_preprint_is_reported():
    published, preprint = work(), work(doi="10.5555/kv.match.pre", preprint=True, type="posted-content", score=70.0)
    match = choose_search_match([preprint, published], TITLE, PAGE, [TITLE])
    assert match.chosen["doi"] == "10.5555/kv.match.0001" and [w["doi"] for w in match.others] == ["10.5555/kv.match.pre"]
    assert not match.ambiguous and match.verdict.level == "exact"


def test_only_a_preprint_verifying_proposes_the_preprint():
    match = choose_search_match([work(doi="10.5555/kv.match.pre", preprint=True)], TITLE, PAGE, [TITLE])
    assert match.chosen["doi"] == "10.5555/kv.match.pre"


def test_two_published_works_that_verify_equally_are_ambiguous_and_nothing_is_chosen():
    editions = [work(doi="10.5555/kv.match.ed1"), work(doi="10.5555/kv.match.ed2")]
    match = choose_search_match(editions, TITLE, PAGE, [TITLE])
    assert match.chosen is None and match.ambiguous and len(match.others) == 2


def test_similar_editions_the_year_separates_them():
    old = work(doi="10.5555/kv.match.ed1", year=2008, years={"issued": 2008, "print": None, "online": None})
    match = choose_search_match([old, work(doi="10.5555/kv.match.ed2")], TITLE, PAGE, [TITLE])
    assert match.chosen["doi"] == "10.5555/kv.match.ed2" and not match.ambiguous


def test_a_search_result_titled_differently_is_never_a_candidate_however_it_ranks():
    different = work(doi="10.5555/kv.match.diff", title="Aqueous Solubility of Invented Nitrate Esters: A Review of Methods and Models")
    match = choose_search_match([different], TITLE, PAGE, [TITLE])
    assert match.chosen is None and not match.ambiguous and match.reasons


def test_a_search_result_titled_alike_but_not_verified_by_the_pages_is_not_a_candidate():
    stranger = work(doi="10.5555/kv.match.stranger", authors=[{"family": "Differentperson", "given": "D.", "name": None}])
    assert choose_search_match([stranger], TITLE, PAGE, [TITLE]).chosen is None


# ------------------------------------------------------------------------- found by the full run on a real library


BOOK = work(title="Encyclopedia of the Imaginary Sciences", authors=[], year=2011, years={"issued": 2011, "print": None, "online": None},
            type="reference-book")
ENTRY_PAGE = "T Scores\nGRANT IVERSON\nUniversity of Nowhere\nJohn Doe, Jane Roe (eds.), Encyclopedia of the Imaginary Sciences, DOI 10.5555/kv.book\n2011\n"


def test_a_book_record_with_no_authors_is_not_exact_for_an_entry_whose_footer_prints_the_books_title():
    """Ten documents were 'confirmed' as the whole encyclopedia: no authors to check, no layout title to disagree with."""
    no_layout = verify_work(BOOK, ENTRY_PAGE, [])
    assert no_layout.level != "exact" and any("no authors" in r for r in no_layout.reasons)
    short_layout = verify_work(BOOK, ENTRY_PAGE, ["T Scores"])  # the entry's own title: not the book's
    assert short_layout.level != "exact"


def test_the_same_book_record_is_exact_for_the_book_itself_because_the_layout_title_confirms_it():
    page = "Encyclopedia of the Imaginary Sciences\nJohn Doe, Jane Roe (eds.)\n2011\n"
    assert verify_work(BOOK, page, ["Encyclopedia of the Imaginary Sciences"]).level == "exact"


def test_an_author_on_the_page_still_backs_a_match_when_no_layout_title_was_found():
    assert verify_work(work(), PAGE, []).level == "exact"
