"""The fixtures must be what their names claim. Read back with the engine production uses."""

from __future__ import annotations

import hashlib

import pdfbuilders as b
import pymupdf


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _texts(path):
    with pymupdf.open(path) as doc:
        return [page.get_text() for page in doc]


def test_native_pdf_has_title_doi_and_body_words(tmp_path):
    path = b.native_pdf(tmp_path / "n.pdf")
    first, *body = _texts(path)
    assert f"{b.TEST_DOI_PREFIX}/kv.fixture.0001" in first
    assert "Imaginary Lattices" in first
    assert body and all("aqueous solubility" in page for page in body)
    with pymupdf.open(path) as doc:
        assert doc.metadata["title"] == "A Synthetic Study of Imaginary Lattices"


def test_fixture_doi_uses_the_reserved_test_prefix(tmp_path):
    # Crossref reserves 10.5555 for testing; a fixture must never carry a DOI that could be a real paper's.
    assert b.TEST_DOI_PREFIX == "10.5555"
    assert b.native_pdf(tmp_path / "n.pdf") and "10.5555/" in _texts(tmp_path / "n.pdf")[0]


def test_doi_only_in_references_prints_no_doi_on_page_one(tmp_path):
    texts = _texts(b.doi_only_in_references_pdf(tmp_path / "x.pdf"))
    assert b.TEST_DOI_PREFIX not in texts[0]
    assert b.TEST_DOI_PREFIX in texts[-1], "the bibliography must really contain the foreign DOI"


def test_scanned_pdf_has_images_and_no_text_at_all(tmp_path):
    with pymupdf.open(b.scanned_pdf(tmp_path / "s.pdf", pages=3)) as doc:
        assert doc.page_count == 3
        assert [page.get_text().strip() for page in doc] == ["", "", ""]
        assert all(len(page.get_images()) == 1 for page in doc)


def test_hybrid_pdf_has_exactly_one_image_only_page_among_text_pages(tmp_path):
    with pymupdf.open(b.hybrid_pdf(tmp_path / "h.pdf")) as doc:
        has_text = [bool(page.get_text().strip()) for page in doc]
    assert has_text == [True, False, True]


def test_labelled_pdf_separates_pdf_page_from_printed_label(tmp_path):
    with pymupdf.open(b.labelled_pdf(tmp_path / "l.pdf", front_matter=4, body=6)) as doc:
        labels = [page.get_label() for page in doc]
        fifth_text = doc[4].get_text()
    assert labels[:5] == ["i", "ii", "iii", "iv", "1"]
    assert labels[-1] == "6"
    assert "PDFPAGE-5" in fifth_text, "PDF page 5 must carry printed label '1'"


def test_rotated_pdf_is_rotated_and_text_survives(tmp_path):
    with pymupdf.open(b.rotated_pdf(tmp_path / "r.pdf", rotation=90)) as doc:
        assert doc[0].rotation == 90
        assert "ROTATED-MARKER" in doc[0].get_text()


def test_same_paper_different_bytes_differ_in_hash_and_agree_in_text(tmp_path):
    a, c = b.same_paper_different_bytes(tmp_path / "a.pdf", tmp_path / "c.pdf")
    assert _sha(a) != _sha(c), "identical bytes would make this an exact duplicate, not a second manifestation"
    assert _texts(a) == _texts(c)
    # The two copies differ on purpose in how they were produced, not only by the per-save random file ID.
    with pymupdf.open(a) as first, pymupdf.open(c) as second:
        assert first.metadata["producer"] != second.metadata["producer"]


def test_two_builds_of_one_fixture_are_not_byte_identical(tmp_path):
    # Saving embeds a fresh file ID, so a test that needs a BYTE-identical copy must copy the file, not rebuild it.
    one = b.native_pdf(tmp_path / "one.pdf")
    two = b.native_pdf(tmp_path / "two.pdf")
    assert _sha(one) != _sha(two)


def test_malformed_pdf_yields_no_pages_or_an_error(tmp_path):
    path = b.malformed_pdf(tmp_path / "m.pdf")
    try:
        with pymupdf.open(path) as doc:
            assert doc.page_count == 0
    except Exception:  # noqa: BLE001 - either outcome means "unreadable", which is the property
        pass
    assert path.stat().st_size > 0


def test_long_book_page_count_and_size_stay_small(tmp_path):
    path = b.long_book(tmp_path / "b.pdf", pages=300)
    with pymupdf.open(path) as doc:
        assert doc.page_count == 300
    assert path.stat().st_size < 1_000_000, "a public fixture must stay small"


def test_unicode_pdf_extracts_the_exact_non_latin_and_chemistry_strings(tmp_path):
    texts = _texts(b.unicode_pdf(tmp_path / "u.pdf"))
    assert b.UNICODE_TITLE in texts[0] and "β" in texts[0]
    assert b.UNICODE_AUTHORS in texts[0]
    with pymupdf.open(tmp_path / "u.pdf") as doc:
        embedded = " ".join(font[3] for font in doc[0].get_fonts())
    assert "Droid" in embedded, f"the bundled full-coverage font was not used: {embedded!r}"
    assert "�" not in texts[0], "a replacement character means the font lacked the glyph: the fixture is ASCII in disguise"
    assert b.CHEMISTRY_LINE in texts[1]
