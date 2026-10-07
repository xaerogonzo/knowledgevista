"""Front matter: the worker's `front` request, and the pure reading of what it returns.

The worker tests use real PDFs built in code; the layout tests use invented line lists, so a heuristic is exercised on
shapes a generated PDF cannot easily make (a masthead that repeats on page two, a title wrapped over three lines).
"""

from __future__ import annotations

import sys

import pymupdf
import pytest
from pdfbuilders import PAGE, malformed_pdf, native_pdf, scanned_pdf

from knowledgevista.domain import frontmatter
from knowledgevista.extract.client import ExtractionSession

TITLE = "A Synthetic Study of Imaginary Lattices"


def line(text: str, size: float, y: float, page: int = 1, x: float = 72, bold: bool = False) -> dict:
    return {"p": page, "t": text, "s": size, "b": bold, "x": x, "y": y}


BODY = [line(f"Body sentence number {i} of the abstract about invented chemistry.", 10, 300 + 14 * i) for i in range(8)]


# ---------------------------------------------------------------------------------------------------- the worker


def test_front_matter_reports_info_layout_and_page_size(tmp_path):
    path = native_pdf(tmp_path / "a.pdf")
    with ExtractionSession() as session:
        result = session.front_matter(str(path))
    assert result.ok and result.page_count == 3
    assert result.info["title"] == TITLE
    biggest = max(result.lines, key=lambda l: l["s"])
    assert biggest["t"] == TITLE and biggest["s"] == 20.0 and biggest["p"] == 1
    assert result.page_size == [round(PAGE.width, 1), round(PAGE.height, 1)]
    facts = frontmatter.read_front(result.info, result.xmp, result.lines, result.page_size, "a")
    assert facts.layout_title and facts.layout_title.text == TITLE and facts.info_title == TITLE


def test_front_matter_reads_an_xmp_packet(tmp_path):
    path = native_pdf(tmp_path / "x.pdf")
    doc = pymupdf.open(path)
    doc.set_xml_metadata(
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">'
        '<dc:title><rdf:Alt><rdf:li xml:lang="x-default">Cu&lt;sub&gt;2&lt;/sub&gt;O &amp; Friends: An Invented Study</rdf:li></rdf:Alt></dc:title>'
        '<dc:creator><rdf:Seq><rdf:li>Alexandra Examplar</rdf:li><rdf:li>Boris Placeholder</rdf:li></rdf:Seq></dc:creator>'
        '<prism:doi>10.5555/kv.xmp.0001</prism:doi></rdf:Description></rdf:RDF></x:xmpmeta>'
    )
    doc.saveIncr()
    doc.close()
    with ExtractionSession() as session:
        result = session.front_matter(str(path))
    facts = frontmatter.read_front(result.info, result.xmp, result.lines, result.page_size, "x")
    assert facts.xmp_title == "Cu2O & Friends: An Invented Study"
    assert facts.xmp_authors == ["Alexandra Examplar", "Boris Placeholder"]
    assert "10.5555/kv.xmp.0001" in facts.metadata_dois


def test_front_matter_of_a_file_that_will_not_open_is_a_result_not_an_exception(tmp_path):
    with ExtractionSession() as session:
        bad = session.front_matter(str(malformed_pdf(tmp_path / "bad.pdf")))
        assert not bad.ok and bad.error
        missing = session.front_matter(str(tmp_path / "nope.pdf"))
        assert not missing.ok and missing.error
        # The same worker is still usable after a failure.
        assert session.front_matter(str(native_pdf(tmp_path / "ok.pdf"))).ok


def test_front_matter_of_a_scan_has_no_text_lines_but_still_a_result(tmp_path):
    with ExtractionSession() as session:
        result = session.front_matter(str(scanned_pdf(tmp_path / "scan.pdf")))
    assert result.ok and result.lines == []
    assert frontmatter.read_front(result.info, result.xmp, result.lines, result.page_size).layout_title is None


def test_a_worker_that_hangs_is_replaced_and_reported(tmp_path):
    hang = [sys.executable, "-c", "import time; time.sleep(60)"]
    with ExtractionSession(command=hang, open_timeout=0.3, page_timeout=0.3) as session:
        result = session.front_matter(str(tmp_path / "any.pdf"))
    assert not result.ok and "timed out" in result.error


def test_a_worker_that_dies_is_reported_with_its_reason(tmp_path):
    die = [sys.executable, "-c", "import sys; sys.stderr.write('boom from the worker'); sys.exit(7)"]
    with ExtractionSession(command=die, open_timeout=5, page_timeout=5) as session:
        result = session.front_matter(str(tmp_path / "any.pdf"))
    assert not result.ok and "worker exited" in result.error


# ---------------------------------------------------------------------------------------------------- the layout rule


def test_the_title_is_the_largest_text_that_is_not_repeated_on_page_two():
    lines = [line("THE JOURNAL OF INVENTED RESULTS", 24, 30), line(TITLE, 18, 100), *BODY,
             line("THE JOURNAL OF INVENTED RESULTS 12, 1 (2020)", 9, 20, page=2)]
    guess = frontmatter.layout_title(lines, [595, 842])
    assert guess and guess.text == TITLE  # the masthead is bigger but recurs as page two's running header
    without_repeat = frontmatter.layout_title([l for l in lines if l["p"] == 1], [595, 842])
    assert without_repeat.text == "THE JOURNAL OF INVENTED RESULTS"  # the oracle: with nothing on page two, the bigger text wins


def test_a_title_wrapped_over_lines_is_joined_and_a_hyphen_at_a_line_end_joins_the_word():
    lines = [line("A Synthetic Study of Imag-", 18, 100), line("inary Lattices and Their", 18, 122), line("Aqueous Solubility", 18, 144), *BODY]
    guess = frontmatter.layout_title(lines, [595, 842])
    assert guess.text == "A Synthetic Study of Imaginary Lattices and Their Aqueous Solubility" and guess.lines == 3


def test_banners_urls_and_body_sized_text_are_never_titles():
    lines = [line("RESEARCH ARTICLE", 22, 40), line("www.invented-journal.org/lattices", 20, 60), *BODY]
    assert frontmatter.layout_title(lines, [595, 842]) is None
    assert frontmatter.layout_title(BODY, [595, 842]) is None
    assert frontmatter.layout_title([], None) is None


@pytest.mark.parametrize("heading", ["20.1 Introduction 20.2 Background", "Inorganic Molecules ............ 41", "3.2.1 Methods and Materials Used"])
def test_a_table_of_contents_entry_is_never_a_title(heading):
    assert frontmatter.layout_title([line(heading, 20, 100), *BODY], [595, 842]) is None
    real = frontmatter.layout_title([line(heading, 20, 100), line("A Synthetic Study of Imaginary Lattices", 18, 160), *BODY], [595, 842])
    assert real.text == "A Synthetic Study of Imaginary Lattices"  # the next candidate is used instead of nothing


def test_a_label_set_in_the_titles_own_size_is_not_part_of_the_title():
    lines = [line("Case Report", 18, 80), line("Quartz as a Subject of Careful Study", 18, 102), *BODY]
    assert frontmatter.layout_title(lines, [595, 842]).text == "Quartz as a Subject of Careful Study"


def test_text_in_the_lower_part_of_the_page_is_not_a_title():
    lines = [line("A Large Heading Far Down The Page", 20, 700), *BODY]
    assert frontmatter.layout_title(lines, [595, 842]) is None


def test_two_title_sized_groups_the_bigger_one_wins_and_a_tie_goes_to_the_higher():
    lines = [line("The Second Heading Of Some Kind", 16, 300), line("The First Heading Of Some Kind", 16, 100), *BODY]
    assert frontmatter.layout_title(lines, [595, 842]).text == "The First Heading Of Some Kind"


# ---------------------------------------------------------------------------------------------------- claims weighed


@pytest.mark.parametrize("claimed", ["Microsoft Word - final_v3.doc", "untitled", "kaya2022", "", "a.pdf"])
def test_junk_info_titles_are_dropped_with_a_reason(claimed):
    facts = frontmatter.read_front({"title": claimed}, "", [], None, "kaya2022")
    assert facts.info_title is None
    if claimed.strip():
        assert "info_title" in facts.dropped


def test_info_authors_are_split_filtered_and_junk_is_dropped():
    facts = frontmatter.read_front({"author": "Alexandra Examplar; Boris Placeholder and C. Writer"}, "", [], None)
    assert facts.info_authors == ["Alexandra Examplar", "Boris Placeholder", "C. Writer"]
    junk = frontmatter.read_front({"author": "Administrator"}, "", [], None)
    assert junk.info_authors == [] and "info_author" in junk.dropped
    comma = frontmatter.split_authors("Examplar, Alexandra")
    assert comma == ["Examplar, Alexandra"]  # a comma is not a separator: it is `family, given`


def test_dois_named_by_the_files_own_metadata_are_collected_in_canonical_form():
    facts = frontmatter.read_front({"subject": "J. Invented 2022, 1, 1. DOI: 10.5555/KV.SUBJECT.0001"}, "", [], None)
    assert facts.metadata_dois == ["10.5555/kv.subject.0001"]
    assert frontmatter.read_front({"subject": "no identifier here"}, "", [], None).metadata_dois == []


def test_xmp_is_read_by_pattern_so_a_hostile_packet_cannot_expand_entities():
    bomb = '<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]><x><dc:title><rdf:li>&b;&b;</rdf:li></dc:title></x>'
    parsed = frontmatter.xmp_fields(bomb)
    assert len(str(parsed.get("title", ""))) < 100  # unknown entities stay text; nothing is expanded
