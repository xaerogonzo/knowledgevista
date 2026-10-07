"""What a PDF says about itself, weighed as the untrusted claim it is.

Three sources, none of them authoritative:

  * the Info dictionary and XMP: written by whatever tool produced the file, so as often a template ("Microsoft Word -
    manuscript_v3.doc") or the uploader's name as a title. Publishers' own files carry good values, which is why they
    are candidates at all;
  * the layout: the largest text on the first page is usually the title, unless it is the journal's masthead, which
    repeats at the top of page two (the test used to tell them apart: a title is not repeated as a running header).

The output feeds CANDIDATES (`services/resolve_metadata.py`); nothing here is metadata, and nothing here is stored as a
fact. Pure: parsed worker output in, guesses out.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from knowledgevista.domain import titles
from knowledgevista.domain.doi import find_dois, normalise_doi

#: A first-page line must be this much bigger than the body text (ratio and absolute points) to be title-sized.
TITLE_SIZE_RATIO = 1.2
TITLE_SIZE_MARGIN = 1.5
#: Title-sized lines closer than this many line-heights belong to the same title.
GROUP_GAP_LINES = 2.2
#: The title is in the upper part of the page.
TITLE_ZONE = 0.7
#: Running headers/footers live in these bands of page two.
HEADER_BAND, FOOTER_BAND = 0.2, 0.9
MIN_TITLE_KEY = 12

_BANNER = re.compile(
    r"^(?:research\s+article|review\s+article|article|review|letter|letters|communication|communications|note|perspective|"
    r"paper|open\s+access|short\s+communication|original\s+article|original\s+paper|full\s+paper|special\s+issue|editorial|"
    r"regular\s+article|feature\s+article|critical\s+review|mini[- ]review|accounts?|case\s+reports?|brief\s+reports?|short\s+reports?|"
    r"technical\s+notes?|original\s+research|original\s+contribution|original\s+investigation|research\s+paper|data\s+paper|software)$",
    re.IGNORECASE,
)
_NOT_A_TITLE_LINE = re.compile(r"https?://|www\.|@|doi\.org|©|\ball\s+rights\s+reserved\b", re.IGNORECASE)
#: A table-of-contents entry ("20.1 Introduction 20.2 Background", "Inorganic Molecules ........ 4") is large text, not a title.
_CONTENTS_ENTRY = re.compile(r"\.{4,}|(?:\s\.){4,}|^\s*\d+(?:\.\d+)+\s")
_XMP_TITLE = re.compile(r"<dc:title\b[^>]*>(.*?)</dc:title>", re.DOTALL | re.IGNORECASE)
_XMP_CREATOR = re.compile(r"<dc:creator\b[^>]*>(.*?)</dc:creator>", re.DOTALL | re.IGNORECASE)
_XMP_ITEM = re.compile(r"<rdf:li\b[^>]*>(.*?)</rdf:li>", re.DOTALL | re.IGNORECASE)
_XMP_DOI = re.compile(r"<(?:prism|pdfx):doi\b[^>]*>(.*?)</(?:prism|pdfx):doi>", re.DOTALL | re.IGNORECASE)
_XMP_IDENTIFIER = re.compile(r"<dc:identifier\b[^>]*>(.*?)</dc:identifier>", re.DOTALL | re.IGNORECASE)
_JUNK_AUTHOR = re.compile(r"\b(?:admin|administrator|user|owner|unknown|microsoft|windows|office|author|pc|laptop|desktop|guest)\b|\.(?:docx?|pdf)\b", re.IGNORECASE)
_AUTHOR_SPLIT = re.compile(r"\s*;\s*|\s+and\s+|\s*&\s*", re.IGNORECASE)
MAX_AUTHORS = 60


@dataclass
class TitleGuess:
    text: str
    size: float
    body_size: float
    lines: int


@dataclass
class FrontFacts:
    info_title: str | None = None
    xmp_title: str | None = None
    info_authors: list[str] = field(default_factory=list)
    xmp_authors: list[str] = field(default_factory=list)
    layout_title: TitleGuess | None = None
    metadata_dois: list[str] = field(default_factory=list)  # DOIs the file's own metadata names (untrusted)
    dropped: dict[str, str] = field(default_factory=dict)  # field -> why a claimed value was thrown away


def _strip_xml(text: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]{1,200}>", " ", text)).split())


def _first_item(block: str) -> str:
    item = _XMP_ITEM.search(block)
    return _strip_xml(item.group(1) if item else block)


def xmp_fields(xmp: str) -> dict[str, object]:
    """Title, creators and DOIs from an XMP packet by pattern, not by an XML parser: the packet is untrusted input, a
    parser would honour entity declarations, and three fields are all that is wanted."""
    out: dict[str, object] = {}
    title = _XMP_TITLE.search(xmp or "")
    if title:
        out["title"] = _first_item(title.group(1))
    creator = _XMP_CREATOR.search(xmp or "")
    if creator:
        out["authors"] = [name for name in (_strip_xml(m.group(1)) for m in _XMP_ITEM.finditer(creator.group(1))) if name]
    dois = [m.group(1) for m in _XMP_DOI.finditer(xmp or "")]
    dois += [m.group(1) for m in _XMP_IDENTIFIER.finditer(xmp or "") if "10." in m.group(1)]
    out["dois"] = dois
    return out


def split_authors(text: str | None) -> list[str]:
    """Names from an Info `Author` string. Commas are not separators: `Smith, J.` is one name."""
    names = [n.strip() for n in _AUTHOR_SPLIT.split(text or "") if n.strip()]
    return names[:MAX_AUTHORS]


def _plausible_author(name: str) -> bool:
    letters = sum(ch.isalpha() for ch in name)
    return letters >= 3 and not _JUNK_AUTHOR.search(name) and letters >= 0.6 * len(name.replace(" ", ""))


def _weighted_median_size(lines: list[dict]) -> float:
    weighted = sorted((float(l["s"]), max(1, len(l["t"]))) for l in lines)
    half, running = sum(w for _, w in weighted) / 2, 0.0
    for size, weight in weighted:
        running += weight
        if running >= half:
            return size
    return weighted[-1][0]


def layout_title(lines: list[dict], page_size: list[float] | None) -> TitleGuess | None:
    """The largest title-sized text group on page one that is not a masthead, banner or running header."""
    first = [l for l in lines if l.get("p") == 1 and l.get("t", "").strip()]
    if not first:
        return None
    height = float(page_size[1]) if page_size and len(page_size) > 1 else 842.0
    body = _weighted_median_size(first)
    threshold = max(body * TITLE_SIZE_RATIO, body + TITLE_SIZE_MARGIN)
    # Text that recurs at the top or bottom of page two is a masthead or running header, however large it is.
    repeated = titles.alnum_key(" ".join(
        l["t"] for l in lines if l.get("p") == 2 and (float(l["y"]) < HEADER_BAND * height or float(l["y"]) > FOOTER_BAND * height)))

    sized = sorted(
        (l for l in first if float(l["s"]) >= threshold and float(l["y"]) < TITLE_ZONE * height
         and not _NOT_A_TITLE_LINE.search(l["t"]) and len(titles.alnum_key(l["t"])) >= 3),
        key=lambda l: float(l["y"]),
    )
    groups: list[list[dict]] = []
    for line in sized:
        last = groups[-1][-1] if groups else None
        if last is not None and abs(float(line["s"]) - float(last["s"])) <= 0.75 and float(line["y"]) - float(last["y"]) <= GROUP_GAP_LINES * float(last["s"]):
            groups[-1].append(line)
        else:
            groups.append([line])

    best: tuple[float, float, TitleGuess] | None = None
    for group in groups:
        # A label set in the title's own size ("Case Report", "Research Article") is not part of the title.
        group = [l for l in group if not _BANNER.match(l["t"].strip())]
        if not group:
            continue
        text = _join_lines([l["t"] for l in group])
        key = titles.alnum_key(text)
        if len(key) < MIN_TITLE_KEY or _BANNER.match(text.strip()) or _CONTENTS_ENTRY.search(text) or (repeated and key in repeated):
            continue
        size = max(float(l["s"]) for l in group)
        rank = (round(size * 2) / 2, -float(group[0]["y"]))  # bigger first, then higher on the page
        if best is None or rank > best[:2]:
            best = (rank[0], rank[1], TitleGuess(text, size, body, len(group)))
    return best[2] if best else None


def _join_lines(parts: list[str]) -> str:
    """Lines of one title as one string: a hyphen at a line end joins the word, otherwise a space does."""
    text = ""
    for part in (p.strip() for p in parts if p.strip()):
        if text.endswith("-") and part[:1].islower():
            text = text[:-1] + part
        else:
            text = f"{text} {part}".strip()
    return " ".join(text.split())


def read_front(info: dict[str, str], xmp: str, lines: list[dict], page_size: list[float] | None, filename_stem: str | None = None) -> FrontFacts:
    """Everything the file claims about itself, junk removed and the reason for each removal kept."""
    facts = FrontFacts()
    parsed = xmp_fields(xmp)
    for source, raw in (("info_title", info.get("title")), ("xmp_title", parsed.get("title"))):
        text = titles.clean(str(raw or ""))
        if not text:
            continue
        if titles.is_junk_title(text, filename_stem):
            facts.dropped[source] = f"not a title: {text[:60]!r}"
        else:
            setattr(facts, source, text)
    facts.info_authors = [n for n in split_authors(info.get("author")) if _plausible_author(n)]
    facts.xmp_authors = [n for n in (parsed.get("authors") or []) if _plausible_author(n)][:MAX_AUTHORS]
    if info.get("author") and not facts.info_authors:
        facts.dropped["info_author"] = f"not author names: {info['author'][:60]!r}"
    facts.layout_title = layout_title(lines, page_size)
    seen: list[str] = []
    claims = [str(parsed.get("dois") and " ".join(map(str, parsed["dois"])) or ""), info.get("subject", ""), info.get("keywords", "")]
    for blob in claims:
        for occurrence in find_dois(blob):
            if occurrence.doi not in seen:
                seen.append(occurrence.doi)
        direct = normalise_doi(blob.strip())
        if direct and direct not in seen:
            seen.append(direct)
    facts.metadata_dois = seen
    return facts
