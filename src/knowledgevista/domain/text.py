"""Pure text logic for extraction: a naive first-pages DOI signal, scan detection, per-page text state.

The DOI function is ported from OpenChem's `tools/library_index.py` so the imported index and native extraction
agree. It is a SIGNAL, not metadata: "the first DOI printed in the first two pages", which the metadata milestone
replaces with classified candidates (own / foreign / ambiguous). Until then nothing may present it as the
document's DOI.
"""

from __future__ import annotations

import re

#: Fewer characters per page than this and the document is treated as having no usable text layer. A real article
#: page carries thousands; a scan with a stray page number carries a few. (Same threshold as the OpenChem index.)
SCANNED_CHARS_PER_PAGE = 100
#: Per-page boundary between "text" and "a few stray characters".
NATIVE_TEXT_MIN_CHARS = 50
#: Leading pages read for the printed DOI: a paper prints its own on page one, and a bibliography further in holds
#: OTHER papers' DOIs, which is the trap.
DOI_PAGES = 2
FIRST_TEXT_CHARS = 600

_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)

PAGE_STATES = ("text_native", "text_sparse", "image_only", "blank", "unknown", "failed")


def find_doi(text: str) -> str:
    """The first DOI in `text`, trimmed of the punctuation a sentence puts after it, or ''. Only for a file's FIRST
    pages: a DOI found deeper is usually a reference's, which is a claim about another paper."""
    match = _DOI.search(text or "")
    if not match:
        return ""
    return match.group(1).rstrip(".,;:)]}'”’")


def is_scanned(chars: int, pages: int) -> bool:
    """Whether a document has no usable text layer. An unreadable file (0 pages) is an error, not a scan."""
    return pages > 0 and chars / pages < SCANNED_CHARS_PER_PAGE


def page_state(chars: int, images: int, *, error: bool = False) -> str:
    """What a page's text layer is, from how much text it has and whether it carries pictures.

    `chars` is the stripped text length. Zero text with images is the signature of a scan; zero text with none is
    simply blank, and the two must not be confused (one is unsearchable content, the other is nothing).
    """
    if error:
        return "failed"
    if chars >= NATIVE_TEXT_MIN_CHARS:
        return "text_native"
    if chars > 0:
        return "text_sparse"
    return "image_only" if images > 0 else "blank"


def imported_page_state(chars: int) -> str:
    """The OpenChem index kept text only, so an empty page cannot be told apart as blank or image-only."""
    if chars >= NATIVE_TEXT_MIN_CHARS:
        return "text_native"
    return "text_sparse" if chars > 0 else "unknown"
