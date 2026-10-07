"""`knowledgevista://` references: one spelling per address, exact ids, and nothing smuggled after the address."""

from __future__ import annotations

import pytest

from knowledgevista.domain import reference as r

DOC = "0123456789abcdef0123456789abcdef"
SHA = "a" * 64


@pytest.mark.parametrize("label", ["iii", "A-1", "164", "S12", "ⅳ", "page 5", "a/b", "100%", "x#y", "q?r", "tabé", "~tilde", "(1)", "a+b", "日本語"])
def test_a_reference_round_trips_whatever_the_label_contains(label):
    for kind, identifier in (("document", DOC), ("artifact", SHA)):
        text = r.format_reference(kind, identifier, label=label)
        parsed = r.parse(text)
        assert (parsed.kind, parsed.id, parsed.label, parsed.pdf_page) == (kind, identifier, label, None)
        assert parsed.format() == text  # canonical: parsing and formatting again changes nothing
        assert "?" not in text and "#" not in text and " " not in text.split("://", 1)[1]


def test_every_documented_form_parses_and_formats_to_itself():
    for text in (f"knowledgevista://document/{DOC}", f"knowledgevista://document/{DOC}/page/12", f"knowledgevista://document/{DOC}/label/iii",
                 f"knowledgevista://artifact/{SHA}", f"knowledgevista://artifact/{SHA}/page/1", f"knowledgevista://artifact/{SHA}/label/A-1"):
        assert r.parse(text).format() == text


def test_the_scheme_and_the_ids_are_case_insensitive_but_the_output_is_canonical():
    parsed = r.parse(f"KnowledgeVista://DOCUMENT/{DOC.upper()}".replace("DOCUMENT", "document"))
    assert parsed.format() == f"knowledgevista://document/{DOC}"
    assert r.parse(f"knowledgevista://artifact/{SHA.upper()}/page/3").format() == f"knowledgevista://artifact/{SHA}/page/3"
    assert r.parse(f"knowledgevista://document/{DOC}/label/a%2fb").label == "a/b"  # %2f and %2F are one spelling


@pytest.mark.parametrize("text", [
    "", "http://document/" + DOC, "knowledgevista:/document/" + DOC, "knowledgevista://", "knowledgevista://document", "knowledgevista://document/",
    "knowledgevista://folder/" + DOC, f"knowledgevista://document/{DOC[:-1]}", f"knowledgevista://document/{DOC}0", f"knowledgevista://document/{'g' * 32}",
    f"knowledgevista://artifact/{SHA[:-1]}", f"knowledgevista://artifact/{DOC}",  # a document id is not an artifact id
    f"knowledgevista://document/{DOC}?x=1", f"knowledgevista://document/{DOC}#top", f"knowledgevista://document/{DOC}/",
    f"knowledgevista://document/{DOC}/page", f"knowledgevista://document/{DOC}/page/0", f"knowledgevista://document/{DOC}/page/-1",
    f"knowledgevista://document/{DOC}/page/007", f"knowledgevista://document/{DOC}/page/1.5", f"knowledgevista://document/{DOC}/page/1000001",
    f"knowledgevista://document/{DOC}/page/3/extra", f"knowledgevista://document/{DOC}/section/3", f"knowledgevista://document/{DOC}/label/",
    f"knowledgevista://document/{DOC}/label/a/b", f"knowledgevista://document/{DOC}/label/a b", f"knowledgevista://document/{DOC}/label/100%",
    f"knowledgevista://document/{DOC}/label/%ZZ", f"knowledgevista://document/{DOC}/label/%C3", f"knowledgevista://document/{DOC}/label/%00",
    f"knowledgevista://document/{DOC}/label/{'x' * 65}", f"knowledgevista://document/{DOC}/label/%20x",
])
def test_anything_else_is_refused_with_a_reason(text):
    with pytest.raises(r.ReferenceError) as caught:
        r.parse(text)
    assert str(caught.value)


def test_the_reason_names_the_real_problem():
    for text, fragment in ((f"knowledgevista://document/{DOC}?x=1", "no query string or fragment"), (f"knowledgevista://document/{DOC}#top", "no query string or fragment"),
                           (f"knowledgevista://folder/{DOC}", "expected knowledgevista://document/<id>"), ("knowledgevista://", "expected knowledgevista://document/<id>"),
                           (f"knowledgevista://document/{DOC}/page/0", "not a pdf page number"), (f"knowledgevista://document/{DOC}/label/a/b", "after the id")):
        with pytest.raises(r.ReferenceError, match=fragment):
            r.parse(text)


def test_a_reference_cannot_name_a_page_and_a_label_at_once_or_a_bad_page():
    with pytest.raises(r.ReferenceError):
        r.Reference("document", DOC, pdf_page=2, label="iii")
    for page in (0, -3, r.MAX_PAGE + 1):
        with pytest.raises(r.ReferenceError):
            r.Reference("document", DOC, pdf_page=page)
    with pytest.raises(r.ReferenceError):
        r.Reference("book", DOC)
    assert r.Reference("document", DOC, pdf_page=r.MAX_PAGE).format().endswith(f"/page/{r.MAX_PAGE}")


def test_is_reference_looks_only_at_the_scheme():
    assert r.is_reference("knowledgevista://garbage") and r.is_reference("  KnowledgeVista:whatever")
    assert not r.is_reference("kaya2022.pdf") and not r.is_reference("") and not r.is_reference("https://knowledgevista")


def test_labels_are_limited_and_have_no_control_characters_or_edge_spaces():
    for bad in ("", " x", "x ", "a\nb", "a\x00b", "a\x7fb", "x" * 65):
        with pytest.raises(r.ReferenceError):
            r.Reference("document", DOC, label=bad)
    assert r.Reference("document", DOC, label="x" * 64).label == "x" * 64


def test_without_page_drops_the_page_only():
    ref = r.Reference("artifact", SHA, pdf_page=9)
    assert ref.without_page().format() == f"knowledgevista://artifact/{SHA}"
