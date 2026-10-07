"""Which of the DOIs printed in a document is the document's OWN?

The question matters because a PDF prints many DOIs and only one names the paper: its bibliography cites dozens, the
first page can list "articles you may be interested in", and an erratum prints the DOI of the paper it corrects. "The
first DOI in the first two pages" (what the OpenChem index recorded, and what `extraction.first_pages_doi` still is)
answers a simpler question and is therefore a SIGNAL, not metadata. This module replaces the signal with classified
candidates:

    own        printed as the paper's own identifier
    foreign    printed, but as somebody else's (a citation, a list, "DOI of original article")
    ambiguous  the evidence does not decide

Position is one feature among several, never the rule. Each candidate carries the score and every component of it, so
`kv explain` and the review queue can show WHY ("page 1, next to 'Cite This', repeated on 6 pages") instead of a bare
confidence; a classification nobody can inspect is the one that gets blamed for a wrong title.

Publisher WORDS beside a DOI ("Citation:", "Received", "(c) ... All rights reserved") are the strongest local evidence,
and they overrule the structural penalties: an AIP cover page lists five neighbouring papers' DOIs under the paper's own,
and a one-page erratum has a "References" heading above its own DOI. Words that point the other way ("DOI of original
article", "Erratum to") count against the DOI they are beside.

The score is additive and deliberately coarse. `MATCHER_VERSION` changes whenever a number here does, so a stored
candidate says which rules produced it and goes stale when they change (docs/METADATA.md).

Pure: text in, candidates out. No I/O, no catalog.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from knowledgevista.domain.doi import DoiOccurrence, find_dois, normalise_doi

MATCHER_VERSION = "doi-evidence-4"

#: Own-identifier thresholds. A DOI needs OWN_SCORE and a margin over every other contender to be called `own`.
OWN_SCORE = 4
OWN_MARGIN = 2
AMBIGUOUS_FLOOR = 1
#: A page with this many distinct DOIs is a list (references, a table of contents), not a paper's front matter.
DENSE_PAGE_DOIS = 6
#: A first page with this many distinct DOIs is crowded: position alone no longer says which one is the paper's.
CROWDED_FRONT_DOIS = 4
#: Beyond this many pages only the first and the last stretch are read: a DOI is on the front, or in a header repeated
#: throughout, or in the bibliography at the end.
MAX_PAGES_SCANNED = 200
EXCERPT_CHARS = 80
MAX_LISTED_PAGES = 8
#: Pages whose publisher words count (the front matter).
FRONT_PAGES = 2

_BIBLIOGRAPHY_HEADING = re.compile(
    r"^[ \t]*[^\w\n]{0,3}[ \t]*(?:\d{1,2}[.)]?[ \t]+)?"
    r"(?:references?(?:[ \t]+and[ \t]+notes|[ \t]+cited)?|notes[ \t]+and[ \t]+references|bibliography|literature[ \t]+cited"
    r"|works[ \t]+cited|cited[ \t]+references|reference[ \t]+list)[ \t]*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
#: Words a publisher prints around a paper's OWN identifier: dates of receipt and publication, the citation line,
#: the copyright line, the publisher's site.
_OWN_CUES = re.compile(
    r"\b(received|accepted|published|cite\s+this|cite\s+as|to\s+cite|copyright|downloaded|available\s+online|"
    r"article\s+history|revised|journal\s+homepage|online\s+version|citation|all\s+rights\s+reserved|front\s+matter)\b|©|"
    r"pubs\.acs\.org|sciencedirect\.com|onlinelibrary\.wiley\.com|link\.springer\.com|iopscience\.iop\.org|pubs\.rsc\.org|"
    r"aip\.org|nature\.com|tandfonline\.com|mdpi\.com|scitation\.org",
    re.IGNORECASE,
)
#: Words that say the DOI beside them names ANOTHER work: the thing an erratum, comment or reply is about.
_FOREIGN_CUES = re.compile(
    r"\b(original\s+(?:article|paper|letter|work|publication|study)|corrigendum\s+to|erratum\s+to|correction\s+to|"
    r"comment\s+on|reply\s+to|addendum\s+to|related\s+article|see\s+also)\b",
    re.IGNORECASE,
)


@dataclass
class DoiCandidate:
    doi: str
    classification: str  # own | foreign | ambiguous
    score: int
    components: dict[str, int]
    pages: list[int]  # the distinct pages it is printed on outside the bibliography (at most MAX_LISTED_PAGES)
    pages_total: int
    first_page: int
    region: str  # front | body
    excerpt: str
    wrapped: bool
    cues: list[str] = field(default_factory=list)
    foreign_cues: list[str] = field(default_factory=list)
    outranked_by: str | None = None


@dataclass
class DoiEvidence:
    candidates: list[DoiCandidate]
    bibliography_page: int | None  # where a references heading was found, or None
    in_bibliography: int  # distinct DOIs seen only after it: counted, never proposed
    pages_scanned: int
    pages_total: int

    @property
    def own(self) -> list[DoiCandidate]:
        return [c for c in self.candidates if c.classification == "own"]


@dataclass
class _Place:
    page: int
    occurrence: DoiOccurrence
    cues: list[str]
    foreign_cues: list[str]
    line_cues: list[str]  # the publisher words on the DOI's OWN line


def _bibliography_start(pages: list[tuple[int, str]], page_count: int) -> tuple[int, int] | None:
    """(pdf_page, offset) of the first references heading that can be a bibliography, else None. A heading on page 1
    counts only in a one-page document: a longer paper's first page can mention its references but never starts them."""
    for number, text in pages:
        if number == 1 and page_count > 1:
            continue
        match = _BIBLIOGRAPHY_HEADING.search(text or "")
        if match:
            return number, match.start()
    return None


def _scanned_pages(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    if len(pages) <= MAX_PAGES_SCANNED:
        return pages
    half = MAX_PAGES_SCANNED // 2
    return pages[:half] + pages[-half:]


def _excerpt(text: str, occurrence: DoiOccurrence) -> str:
    low, high = max(0, occurrence.start - EXCERPT_CHARS), min(len(text), occurrence.end + EXCERPT_CHARS)
    return " ".join(text[low:high].split())


def _words_near(text: str, occurrence: DoiOccurrence, pattern: re.Pattern[str], *, above: int, below: int) -> list[str]:
    """Matches of `pattern` on the DOI's own line and `above`/`below` lines around it. Publisher words may sit a line
    away; a wider window (measured: 250 characters) lets a DOI in an abstract inherit the 'Received' line of the header,
    and 'DOI of original article' on the NEXT line would make the paper's own DOI look foreign."""
    low = text.rfind("\n", 0, occurrence.start) + 1  # start of the DOI's line
    for _ in range(above):
        low = text.rfind("\n", 0, max(0, low - 1)) + 1 if low > 0 else 0
    high = text.find("\n", occurrence.end)
    for _ in range(below):
        high = text.find("\n", high + 1) if high != -1 else -1
    window = text[low: high if high != -1 else len(text)]
    return sorted({" ".join(m.group(0).lower().split()) for m in pattern.finditer(window)})


def classify_dois(pages: list[tuple[int, str]], page_count: int | None = None, metadata_dois: Sequence[str] = ()) -> DoiEvidence:
    """Classify every DOI printed in `pages` (ordered `(pdf_page, text)` pairs, 1-based).

    `metadata_dois` are DOIs the file's own Info/XMP names: a publisher-written claim, untrusted but informative. One that is
    also printed scores higher; one that is only in the metadata becomes an ambiguous candidate (`first_page` 0), so a scan
    with no text layer can still be offered its DOI for a person or a provider to confirm."""
    page_count = page_count or (pages[-1][0] if pages else 0)
    scanned = _scanned_pages(pages)
    bibliography = _bibliography_start(scanned, page_count)
    texts = dict(scanned)

    found: dict[int, list[_Place]] = {}
    for number, text in scanned:
        found[number] = [
            _Place(number, o, _words_near(text, o, _OWN_CUES, above=1, below=1), _words_near(text, o, _FOREIGN_CUES, above=0, below=0),
                   _words_near(text, o, _OWN_CUES, above=0, below=0))
            for o in find_dois(text)
        ]
    dense = {n for n, places in found.items() if len({p.occurrence.doi for p in places}) >= DENSE_PAGE_DOIS}

    def in_references(place: _Place) -> bool:
        """After the heading, and not carrying the publisher's own words: an erratum's DOI sits under its one-page
        'References' heading, and a reference line has no 'All rights reserved' beside it."""
        return bibliography is not None and (place.page, place.occurrence.start) > bibliography and not place.cues

    outside: dict[str, list[_Place]] = defaultdict(list)
    in_bibliography: set[str] = set()
    for places in found.values():
        for place in places:
            (in_bibliography.add(place.occurrence.doi) if in_references(place) else outside[place.occurrence.doi].append(place))
    in_bibliography -= set(outside)  # printed outside it too: it is judged by where it is printed OUTSIDE

    on_first_page = sorted((p for p in found.get(1, []) if not in_references(p)), key=lambda p: p.occurrence.start)
    first_page_dois = {p.occurrence.doi for p in on_first_page}
    top_doi = on_first_page[0].occurrence.doi if on_first_page else None

    scored: list[DoiCandidate] = []
    for doi, places in outside.items():
        places.sort(key=lambda p: (p.page, p.occurrence.start))
        # A place on a list page counts as front matter only if the publisher's words are on ITS OWN line: the line above
        # a list's first entry is the list's heading ('Published by ...'), which says nothing about that entry.
        real_pages = sorted({p.page for p in places if p.page not in dense or p.line_cues})
        all_pages = sorted({p.page for p in places})
        first = places[0]
        early = [p for p in places if p.page <= FRONT_PAGES]
        cue_names = sorted({c for p in early for c in p.cues})
        foreign_names = sorted({c for p in early for c in p.foreign_cues})
        components: dict[str, int] = {}
        if 1 in real_pages:
            components["on_first_page"] = 3
        elif real_pages and real_pages[0] == 2:
            components["on_second_page"] = 1
        if cue_names:
            components["publisher_cue_nearby"] = 2
        if foreign_names:
            components["names_another_work"] = -3
        if len(real_pages) >= 3:
            components["repeated_on_pages"] = 1
        if doi == top_doi and 1 in real_pages:
            components["first_on_page"] = 1
        if doi in first_page_dois and len(first_page_dois) >= CROWDED_FRONT_DOIS and not cue_names:
            components["crowded_first_page"] = -2
        if not real_pages:
            components["only_on_list_pages"] = -3
        wrapped = any(p.occurrence.wrapped for p in places)
        if wrapped:
            components["wrapped_across_lines"] = -1
        scored.append(DoiCandidate(
            doi=doi, classification="ambiguous", score=sum(components.values()), components=components,
            pages=all_pages[:MAX_LISTED_PAGES], pages_total=len(all_pages), first_page=first.page,
            region="front" if first.page <= FRONT_PAGES else "body", excerpt=_excerpt(texts[first.page], first.occurrence),
            wrapped=wrapped, cues=cue_names, foreign_cues=foreign_names,
        ))

    claimed = [d for d in dict.fromkeys(normalise_doi(m) for m in metadata_dois) if d]
    by_doi = {c.doi: c for c in scored}
    for doi in claimed:
        candidate = by_doi.get(doi)
        if candidate is None:
            candidate = DoiCandidate(doi=doi, classification="ambiguous", score=0, components={}, pages=[], pages_total=0, first_page=0,
                                     region="metadata", excerpt="", wrapped=False)
            scored.append(candidate)
        candidate.components["in_pdf_metadata"] = 2
        candidate.score = sum(candidate.components.values())
    in_bibliography -= set(claimed)

    scored.sort(key=lambda c: (-c.score, c.first_page or 10**9, c.doi))
    best = scored[0] if scored else None
    confident = best is not None and best.score >= OWN_SCORE and (len(scored) == 1 or best.score - scored[1].score >= OWN_MARGIN)
    for candidate in scored:
        if candidate is best and confident:
            candidate.classification = "own"
        elif confident and candidate.score < OWN_SCORE:
            candidate.classification, candidate.outranked_by = "foreign", best.doi
        elif candidate.score >= AMBIGUOUS_FLOOR:
            candidate.classification = "ambiguous"
        else:
            candidate.classification = "foreign"
    return DoiEvidence(scored, bibliography[0] if bibliography else None, len(in_bibliography), len(scanned), page_count)
