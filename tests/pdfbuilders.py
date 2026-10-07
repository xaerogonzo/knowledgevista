"""Synthetic PDF fixtures, generated in code.

WHY GENERATED. A public repository must carry no one's publications (README, "Your files and your rights"), and a
15 MB real paper is a worse CI fixture than a 20 KB one that isolates the single property under test. Every title,
author and DOI here is invented. `10.5555` is the DOI prefix Crossref reserves for testing, so a fixture DOI can
never be mistaken for, or resolve to, a real paper.

Each builder makes ONE property true and `tests/test_pdfbuilders.py` verifies that it is, by reading the file back
with the same engine production uses. A fixture that is not what its name says makes every test built on it
vacuous (docs/gotchas/tests-that-pass-without-testing.md).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pymupdf

TEST_DOI_PREFIX = "10.5555"
PAGE = pymupdf.paper_rect("a4")


def _write_lines(page: pymupdf.Page, lines: Sequence[str], *, top: float = 72, size: float = 11) -> None:
    y = top
    for line in lines:
        page.insert_text((72, y), line, fontsize=size)
        y += size * 1.6


def _front_page(doc: pymupdf.Document, title: str, authors: str, journal: str, doi: str | None) -> None:
    page = doc.new_page(width=PAGE.width, height=PAGE.height)
    page.insert_text((72, 80), journal, fontsize=9)
    if doi:
        page.insert_text((72, 94), f"https://doi.org/{doi}", fontsize=9)
    page.insert_text((72, 140), title, fontsize=20)  # the largest text on the page: what a title heuristic keys on
    page.insert_text((72, 180), authors, fontsize=11)


def native_pdf(
    path: Path,
    *,
    title: str = "A Synthetic Study of Imaginary Lattices",
    authors: str = "A. Examplar and B. Placeholder",
    journal: str = "Journal of Invented Results 12 (2020) 1-9",
    doi: str | None = f"{TEST_DOI_PREFIX}/kv.fixture.0001",
    body_pages: int = 2,
    producer: str = "KVFixture",
) -> Path:
    """An ordinary text-layer article: DOI and title on page 1, a body of distinctive words after it."""
    doc = pymupdf.open()
    _front_page(doc, title, authors, journal, doi)
    for number in range(1, body_pages + 1):
        page = doc.new_page(width=PAGE.width, height=PAGE.height)
        _write_lines(page, [f"Section {number}", f"The aqueous solubility of compound {number} was measured at 298 K."])
    doc.set_metadata({"title": title, "author": authors, "producer": producer, "creator": producer})
    doc.save(path)
    doc.close()
    return path


def doi_only_in_references_pdf(
    path: Path,
    *,
    own_title: str = "A Paper With No Printed DOI of Its Own",
    foreign_doi: str = f"{TEST_DOI_PREFIX}/kv.someone.else.0002",
) -> Path:
    """The classic false positive: page 1 prints no DOI, and the bibliography cites ANOTHER paper's."""
    doc = pymupdf.open()
    _front_page(doc, own_title, "C. Author", "Journal of Invented Results 13 (2021) 10-19", None)
    page = doc.new_page(width=PAGE.width, height=PAGE.height)
    _write_lines(page, ["Body text of the paper."])
    page = doc.new_page(width=PAGE.width, height=PAGE.height)
    _write_lines(page, ["References", f"[1] D. Other, Some Other Paper, J. Elsewhere 1 (2001) 1. doi:{foreign_doi}"])
    doc.save(path)
    doc.close()
    return path


def _image_only_page(doc: pymupdf.Document, text: str) -> None:
    """A page that LOOKS like text but carries none: the text is rasterised to an image, the way a scan is."""
    scratch = pymupdf.open()
    source = scratch.new_page(width=PAGE.width, height=PAGE.height)
    _write_lines(source, [text], size=14)
    pixmap = source.get_pixmap(dpi=100)
    scratch.close()
    page = doc.new_page(width=PAGE.width, height=PAGE.height)
    page.insert_image(page.rect, pixmap=pixmap)


def scanned_pdf(path: Path, *, pages: int = 3) -> Path:
    """No text layer at all: every page is an image."""
    doc = pymupdf.open()
    for number in range(1, pages + 1):
        _image_only_page(doc, f"Scanned page {number}: invented words about nothing in particular.")
    doc.save(path)
    doc.close()
    return path


def hybrid_pdf(path: Path) -> Path:
    """Text pages with one image-only page between them: a document-level 'has text' hides a page-level gap."""
    doc = pymupdf.open()
    for number in (1, 3):
        page = doc.new_page(width=PAGE.width, height=PAGE.height)
        _write_lines(page, [f"Native text page {number}.", "Enough ordinary words to count as a text layer " * 3])
        if number == 1:
            _image_only_page(doc, "This page is a picture of text.")
    doc.save(path)
    doc.close()
    return path


def rotated_pdf(path: Path, *, rotation: int = 90) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE.width, height=PAGE.height)
    _write_lines(page, ["Rotated page with a known string: ROTATED-MARKER"])
    page.set_rotation(rotation)
    doc.save(path)
    doc.close()
    return path


def labelled_pdf(path: Path, *, front_matter: int = 4, body: int = 6) -> Path:
    """A book with roman front matter (i..iv) then arabic pages restarting at 1.

    The 5th PDF page is printed page "1": exactly the case where `pdf_page` and `printed_label` differ, and where
    an API taking a bare `page=` would be ambiguous.
    """
    doc = pymupdf.open()
    for index in range(front_matter + body):
        page = doc.new_page(width=PAGE.width, height=PAGE.height)
        _write_lines(page, [f"Book page marker PDFPAGE-{index + 1}"])
    doc.set_page_labels(
        [
            {"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1},
            {"startpage": front_matter, "prefix": "", "style": "D", "firstpagenum": 1},
        ]
    )
    doc.save(path)
    doc.close()
    return path


def same_paper_different_bytes(path_a: Path, path_b: Path) -> tuple[Path, Path]:
    """The SAME synthetic publication as two distinct byte objects (a re-download, a re-save): same title, authors,
    DOI and text, different producer metadata, therefore a different SHA-256. Not 'two similar titles'."""
    native_pdf(path_a, producer="PublisherSystemA")
    native_pdf(path_b, producer="PublisherSystemB")
    return path_a, path_b


def malformed_pdf(path: Path) -> Path:
    """A PDF whose structure is damaged beyond what the parser can repair: the header survives, the body is junk."""
    path.write_bytes(b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog /Pages 99 0 R >>\nendobj\n" + b"\x00\xff" * 200)
    return path


def long_book(path: Path, *, pages: int = 300) -> Path:
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page(width=PAGE.width, height=PAGE.height)
        _write_lines(page, [f"Long book page {index + 1}", "Filler sentence about invented chemistry. " * 4])
    doc.save(path, deflate=True)
    doc.close()
    return path


def _unicode_line(page: pymupdf.Page, point: tuple[float, float], text: str, size: float = 11) -> None:
    """Unicode text that survives. `page.insert_text` with PyMuPDF's default Base-14 fonts turns Greek, Cyrillic and
    CJK into replacement characters (measured), which would make a 'Unicode' fixture ASCII in disguise. A TextWriter
    with the bundled `cjk` font (Droid Sans Fallback, shipped with PyMuPDF, so CI needs no system font) keeps them.
    Measured too: TextWriter falls back to that same font for any glyph the requested font lacks, so swapping the
    requested font does not change the output; what matters is not using `insert_text` here."""
    writer = pymupdf.TextWriter(page.rect)
    writer.append(point, text, font=pymupdf.Font("cjk"), fontsize=size)
    writer.write_text(page)


UNICODE_TITLE = "Etude de la β-lactame et du 2,4-DNT"
UNICODE_AUTHORS = "田中太郎, Иванов А."
CHEMISTRY_LINE = "Cu(II) and Al2O3 at pH 7.4; 2,4,6-trinitrotoluene; CAS 118-96-7."


def unicode_pdf(path: Path) -> Path:
    """Non-Latin author names, a Greek letter and chemistry punctuation: strings a normaliser must never alter."""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE.width, height=PAGE.height)
    _unicode_line(page, (72, 140), UNICODE_TITLE, 20)
    _unicode_line(page, (72, 180), UNICODE_AUTHORS)
    page = doc.new_page(width=PAGE.width, height=PAGE.height)
    _unicode_line(page, (72, 100), CHEMISTRY_LINE)
    doc.save(path)
    doc.close()
    return path
