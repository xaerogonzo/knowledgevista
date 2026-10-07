"""Comparing titles with text, and deciding that a "title" is not one.

Two comparisons, used for different questions:

  * `alnum_key` reduces text to its letters and digits only (case-folded, accents and compatibility forms removed). It
    answers "is this exact title printed here?" and is immune to everything extraction does to a title: line breaks,
    hyphenation at a line end, doubled spaces, a different dash, a superscript digit.
  * `tokens` gives the words (three letters or more, no stop words). It answers "how much of this title is here?",
    which survives a title the extractor mangled in the middle.

Neither is a fuzzy matcher: there is no stemming, transliteration or typo correction, for the reason docs/SEARCH.md gives
for search. A title that is not printed does not become "close enough" by being forgiven.
"""

from __future__ import annotations

import html
import re
import unicodedata
from difflib import SequenceMatcher

_NOT_ALNUM = re.compile(r"[\W_]+", re.UNICODE)
_WORD = re.compile(r"[^\W\d_]{3,}|\d{2,}", re.UNICODE)
_TAGS = re.compile(r"<[^>]{1,200}>")
_SPACES = re.compile(r"\s+")
STOP_WORDS = frozenset(
    "the and for with from that this are was were into onto over under between among about via their its our "
    "using use used based study studies analysis new one two".split()
)
#: Below this many letters and digits a title is too short to prove anything by being found in a page.
MIN_DECISIVE_LENGTH = 20

_JUNK_FRAGMENTS = (
    "microsoft word", "microsoft powerpoint", "microsoft excel", "untitled", "document1", "adobe acrobat",
    "pdf document", "powerpoint presentation", "scanned by", "camscanner", "acrobat distiller", "tex output",
    "latex with hyperref", "title goes here", "paper title", "no title", "default title", "unknown",
)
#: A Title field holding an identifier (measured on a real library: two publishers' files had `doi:10.1016/...` as the XMP
#: title, and the Info title said the same, so two "independent" sources agreed on junk).
_IDENTIFIER_PREFIX = re.compile(r"^\s*(?:(?:doi|pii|isbn|issn|arxiv|url)\b\s*:?\s*\S*\d|https?://|www\.)", re.IGNORECASE)
_IDENTIFIER_INSIDE = re.compile(r"\b10\.\d{4,9}/\S|\bS\d{4}-\d{3}[\dX]")
_JUNK_EXTENSION = re.compile(r"\.(?:docx?|pdf|tex|dvi|ps|indd|qxd|rtf|txt|pmd|odt)\s*$", re.IGNORECASE)
_LOOKS_LIKE_ID = re.compile(r"^(?:[a-z]{0,6}\d[\w.\-]*|[\d.\-_/ ]+|10\.\d{4,9}/\S+|[0-9a-f]{16,})$", re.IGNORECASE)


def clean(text: str) -> str:
    """Whitespace collapsed, markup tags removed (Crossref titles carry JATS such as `<i>`/`<sub>`), entities decoded."""
    return _SPACES.sub(" ", html.unescape(_TAGS.sub("", text or ""))).strip()


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def alnum_key(text: str) -> str:
    return _NOT_ALNUM.sub("", _fold(text))


def tokens(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for word in _WORD.findall(_fold(text)):
        if word not in STOP_WORDS:
            seen.setdefault(word)
    return list(seen)


def is_printed(title: str, text: str) -> bool:
    """Whether `title` appears in `text` once both are reduced to letters and digits. A title shorter than
    MIN_DECISIVE_LENGTH is never "printed" in this sense: `Introduction` is on every page of every book."""
    key = alnum_key(title)
    return len(key) >= MIN_DECISIVE_LENGTH and key in alnum_key(text)


def containment(title: str, text: str) -> float:
    """The fraction of the title's words that occur in `text`, 0.0 when the title has no usable words."""
    wanted = tokens(title)
    if not wanted:
        return 0.0
    available = set(tokens(text))
    return sum(word in available for word in wanted) / len(wanted)


def similarity(a: str, b: str) -> float:
    """How alike two titles are, 0..1, by their letters and digits. Used to ask whether a provider's title IS the
    title we searched for, never to forgive a different one."""
    ka, kb = alnum_key(a), alnum_key(b)
    if not ka or not kb:
        return 0.0
    return SequenceMatcher(None, ka, kb, autojunk=False).ratio()


def is_junk_title(title: str | None, filename_stem: str | None = None) -> bool:
    """A string PDF software puts in the Title field that is not a title: an application name, a file name, an ID."""
    text = clean(title or "")
    if len(alnum_key(text)) < 6:
        return True
    lowered = text.casefold()
    if any(fragment in lowered for fragment in _JUNK_FRAGMENTS) or _JUNK_EXTENSION.search(text):
        return True
    if _LOOKS_LIKE_ID.match(text) and " " not in text:
        return True
    if _IDENTIFIER_PREFIX.match(text) or _IDENTIFIER_INSIDE.search(text):
        return True
    if filename_stem and alnum_key(filename_stem) == alnum_key(text):
        return True
    return False


#: A run of this many one-character words is letter-spacing ("T H E E F F E C T"), the signature of a scanned page's text layer.
GARBLED_RUN = 4


def looks_garbled(text: str) -> bool:
    """Whether a title reads like letter-spaced scan output rather than words. A title can have single letters ("Vitamin B 12",
    "Phase A and B"), but not a run of four or more in a row; such a string is a transcription of the print, not a title."""
    run = longest = 0
    for token in (text or "").split():
        run = run + 1 if len(token) == 1 and token.isalnum() else 0
        longest = max(longest, run)
    return longest >= GARBLED_RUN


def filename_title_hint(stem: str) -> str | None:
    """A name that reads like a title (three or more alphabetic words), else None. `cm4c01978` and `kaya2022` are
    locators, not titles; `Hansen solubility parameters handbook` is a hint. Always labelled `filename_hint`."""
    spaced = _SPACES.sub(" ", re.sub(r"[_\-+.]+", " ", stem)).strip()
    words = [w for w in spaced.split(" ") if w]
    alphabetic = [w for w in words if re.fullmatch(r"[^\W\d_]{2,}", w, re.UNICODE)]
    return spaced if len(alphabetic) >= 3 and len(alphabetic) >= 0.7 * len(words) else None


_AUTHOR_YEAR = re.compile(r"^(?P<author>[^\W\d_]{3,}?)[ _\-]*(?P<year>(?:19|20)\d{2})[a-z]?(?:[ _\-].*)?$", re.UNICODE | re.IGNORECASE)


def filename_author_year(stem: str) -> tuple[str, int] | None:
    """`kaya2022` -> ('kaya', 2022). A hint that can support a match, never create one."""
    match = _AUTHOR_YEAR.match(stem.strip())
    return (match.group("author").casefold(), int(match.group("year"))) if match else None
