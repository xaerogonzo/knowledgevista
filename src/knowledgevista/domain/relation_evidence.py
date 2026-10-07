"""The evidence that two items in a library are related, as pure functions.

Four things are measured, each by what the documents say about THEMSELVES, never by their file names:

  * IDENTICAL TEXT. Two artifacts with different bytes (a re-download, a re-save, another PDF producer) whose extracted text is
    the same letter for letter are the same publication. `text_fingerprint` reduces a document's whole text to its letters and
    digits and hashes it, so line breaks, hyphenation and spacing, which differ between producers, do not.
  * A SUPPLEMENT announces itself on its first page ("Supporting Information", "Supplementary Material") and then names its
    paper, by that paper's DOI or its title. A file name like `_si` is only a hint: it can nominate a document to be examined,
    never decide a relation.
  * A BOOK'S CHAPTERS share a container title (and often a DOI prefix or a whole DOI).
  * A PREPRINT and its published version share a title but not a DOI.

`MATCHER_VERSION` changes with any rule here, so a stored proposal says which rules made it and goes stale when they change.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

MATCHER_VERSION = "relations-1"

#: Fewer letters and digits than this and a text proves nothing by being equal (a scan's stray page number, a cover sheet).
MIN_FINGERPRINT_CHARS = 400
#: A group larger than this is linked to its first member rather than pairwise (n*(n-1)/2 proposals help nobody).
MAX_PAIRWISE = 6
#: Record types that make a document a chapter-like part of a larger work.
PART_TYPES = frozenset({"book-chapter", "reference-entry", "book-section", "book-part"})
#: Record types of a whole book.
WHOLE_TYPES = frozenset({"book", "edited-book", "reference-book", "monograph", "book-set"})
PREPRINT_TYPES = frozenset({"posted-content", "preprint"})

_NOT_ALNUM = re.compile(r"[\W_]+", re.UNICODE)
_SUPPLEMENT_TEXT = re.compile(
    r"\b(supporting\s+information|supplementary\s+(?:material|information|data|file|materials)|supplemental\s+(?:material|information|data)|"
    r"electronic\s+supplementary\s+material|online\s+supplement)\b",
    re.IGNORECASE,
)
_SUPPLEMENT_NAME = re.compile(r"(?:^|[\s_\-.])(?:si|supp|suppl|supplement|supplementary|supporting|esi|moesm\d*)(?:$|[\s_\-.\d])", re.IGNORECASE)


def text_fingerprint(pages: Sequence[tuple[int, str]]) -> str | None:
    """A hash of a document's whole text reduced to letters and digits, or None if there is too little text to mean anything."""
    digest, count = hashlib.sha256(), 0
    for _, text in sorted(pages):
        folded = _NOT_ALNUM.sub("", (text or "").casefold())
        count += len(folded)
        digest.update(folded.encode("utf-8"))
    return digest.hexdigest() if count >= MIN_FINGERPRINT_CHARS else None


_CORRECTION = re.compile(r"^\W*(?:correction|corrigendum|corrigenda|erratum|errata|addendum|retraction|comment|reply|response)\b", re.IGNORECASE)


def is_correction(title: str | None, first_pages_text: str) -> bool:
    """Whether a document is a correction, comment or reply ABOUT another work: its accepted title, or the start of its first page,
    begins that way. Such a document often mentions its paper's "Supporting Information", which is why it is told apart from a
    supplement before a supplement is looked for."""
    return bool(_CORRECTION.match(title or "") or _CORRECTION.match((first_pages_text or "")[:200]))


def supplement_markers(first_pages_text: str, filename_stem: str | None) -> dict[str, bool]:
    """Whether a document SAYS it is a supplement (on its first page) and whether its file name hints at one."""
    return {"says_so": bool(_SUPPLEMENT_TEXT.search((first_pages_text or "")[:1500])),
            "file_name_hint": bool(filename_stem and _SUPPLEMENT_NAME.search(filename_stem))}


def pairs(items: Sequence[str], cap: int = MAX_PAIRWISE) -> list[tuple[str, str]]:
    """Every pair of a small group; for a large one, each member paired with the first (a star), in a stable order."""
    ordered = sorted(items)
    if len(ordered) <= cap:
        return [(a, b) for i, a in enumerate(ordered) for b in ordered[i + 1:]]
    return [(ordered[0], other) for other in ordered[1:]]
