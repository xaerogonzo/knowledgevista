"""DOIs: finding the ones a document prints, and the single spelling they are compared in.

A DOI is case-insensitive, so the canonical form is lower case; everything stored or compared uses `normalise_doi`.

Two facts about text extracted from a PDF drive the rest of this module:

  * A DOI printed near a line end is often wrapped (`10.1021/acs.jcim.` / `2c01234`). Taking only the first half
    produces a SHORTER, still well-formed DOI, and a truncated DOI can be a different, valid one. A wrapped DOI is
    therefore joined only in the clear case (the line ends in a DOI separator and the next line starts with a token
    that contains a digit) and the occurrence is flagged `wrapped`, which lowers the confidence in it.
  * A DOI is followed by sentence punctuation more often than it is followed by a space. Trailing punctuation is
    trimmed, but a closing bracket is kept when it balances one inside the DOI (`10.1002/(SICI)1097-0126(1999)...`).

Known limit, stated rather than hidden: the pattern stops at `<` and `>` (as the OpenChem index it replaces did), so an
old Wiley "SICI" DOI that contains them is cut short. A truncated DOI is checked against the provider's title before
it can be accepted, so this costs a missed match, never a wrong one.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# A control character (measured: a stray \x04 ended one real DOI) or an invisible one ends a DOI like a space does.
_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>\x00-\x1f\x7f­​-‍⁠﻿]+)", re.IGNORECASE)
_PREFIX = re.compile(r"^\s*(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)", re.IGNORECASE)
_VALID = re.compile(r"^10\.\d{4,9}/\S+$")
#: Sentence punctuation that follows a DOI. The typographic forms are here because a PDF's metadata and text carry them: found by the library window,
#: which showed a real DOI with a trailing U+201A (a comma look-alike) as a second, DIFFERENT proposal beside the right one.
_TRAILING_PUNCTUATION = ".,;:'\"”’‚„“‘…»"
_BRACKETS = {")": "(", "]": "[", "}": "{"}
#: A wrapped DOI's first line ends in one of these, because a line is broken after a separator.
_WRAP_SEPARATORS = ".-/_"
_NEXT_TOKEN = re.compile(r"[ \t]*([A-Za-z0-9][A-Za-z0-9._\-;:()/]*)")
MAX_LENGTH = 200


@dataclass(frozen=True)
class DoiOccurrence:
    doi: str  # canonical (lower case)
    start: int  # offset of the first character in the text
    end: int
    wrapped: bool  # joined across a line break: less trustworthy


def normalise_doi(raw: str | None) -> str | None:
    """The canonical spelling of a DOI, or None if `raw` is not one. Idempotent: `f(f(x)) == f(x)`."""
    if not raw:
        return None
    # PDF text carries typographic ligatures (measured: `j.ﬂuid` for `j.fluid`, which no provider knows); NFKC folds them and
    # the full-width and compatibility forms to the ASCII a DOI is registered in.
    text = _PREFIX.sub("", unicodedata.normalize("NFKC", raw).strip())
    changed = True
    while changed:
        changed = False
        text = text.rstrip(_TRAILING_PUNCTUATION)
        while text and text[-1] in _BRACKETS and text.count(_BRACKETS[text[-1]]) < text.count(text[-1]):
            text, changed = text[:-1], True
        if text.endswith(">"):  # the pattern stops before it, but a pasted value may carry one
            text, changed = text[:-1], True
    text = text.lower()
    return text if len(text) <= MAX_LENGTH and _VALID.match(text) else None


def find_dois(text: str) -> list[DoiOccurrence]:
    """Every DOI printed in `text`, in order, each with where it is."""
    found: list[DoiOccurrence] = []
    for match in _DOI.finditer(text or ""):
        raw, end = match.group(1), match.end()
        wrapped = False
        if end < len(text) and text[end] in "\r\n" and raw[-1] in _WRAP_SEPARATORS:
            tail = _NEXT_TOKEN.match(text, end + (2 if text.startswith("\r\n", end) else 1))
            if tail and any(ch.isdigit() for ch in tail.group(1)):
                raw, end, wrapped = raw + tail.group(1), tail.end(), True
        doi = normalise_doi(raw)
        if doi:
            found.append(DoiOccurrence(doi, match.start(), end, wrapped))
    return found


def doi_url(doi: str) -> str:
    """Where a DOI resolves; used only to show the user a link, never fetched by Knowledge Vista."""
    return f"https://doi.org/{doi}"
