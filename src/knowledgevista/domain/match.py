"""Does a provider's record describe THIS PDF? The comparison that keeps a wrong title out of the library.

A provider answers "what is the work with this DOI" or "which works have a title like this". Neither answers "is it the
paper in my hand", and the failures are quiet: a correction whose title contains the original's, a preprint and its
published version, a second edition, a paper that merely cites the DOI. So the record is compared with what the PDF
itself prints, and the comparison is reported as a level with every component, not as a number:

    exact         the title is printed (and equals the layout title, if one was found), and an author of the record
                  appears on the first pages, and the year does not contradict
    strong        the title is (nearly) all there and an author appears, but something stops short of exact: a
                  title printed inside a longer one ("Correction to ..."), a year that disagrees
    weak          some of the title, or authors, but not enough to say
    contradicted  neither the title nor any author appears: the record is about something else
    unverifiable  the record has nothing to compare (no title, no authors)

Only `exact` is ever eligible for a batch rule; everything below it waits for a person. Comparing against the LAYOUT
title as well as the text is what separates a correction from the paper it corrects: the original's title is a substring
of the correction's, but it is not equal to the title printed at the top.

Pure: dictionaries and strings in, a verdict out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from knowledgevista.domain import titles

MATCHER_VERSION = "match-1"

#: A printed title equal to the layout title within this similarity counts as the same title.
LAYOUT_SIMILARITY = 0.9
#: A provider title must be this similar to the title that was searched for to be a candidate for a title search.
SEARCH_SIMILARITY = 0.9
STRONG_CONTAINMENT = 0.85
WEAK_CONTAINMENT = 0.6
MAX_AUTHORS_CHECKED = 6
MIN_FAMILY_LETTERS = 3
YEAR_SLACK = 1  # online/print/issued dates and a printed "received" year differ by a year or so

_YEAR = re.compile(r"(?<!\d)(19\d\d|20\d\d)(?!\d)")


@dataclass
class Verdict:
    level: str  # exact | strong | weak | contradicted | unverifiable
    components: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def confirms(self) -> bool:
        return self.level in ("exact", "strong")


def _authors_found(work: dict[str, Any], text_key: str) -> tuple[int, int]:
    families = []
    for author in (work.get("authors") or [])[:MAX_AUTHORS_CHECKED]:
        family = titles.alnum_key(author.get("family") or "")
        if len(family) >= MIN_FAMILY_LETTERS:
            families.append(family)
    return sum(f in text_key for f in families), len(families)


#: A title printed INSIDE a longer one by at least this many letters is a different title ("Correction to ...").
EMBEDDED_EXTRA = 6


def same_title(work: dict[str, Any], layout_title: str) -> bool:
    """Whether the title printed at the top of the document IS the record's title.

    Similarity alone is the wrong test: a short prefix on a long title ("Correction to <title>") scores above 0.9. A
    record whose title sits inside a longer printed one, by more than a few letters, is the paper the longer title is ABOUT,
    not the paper itself."""
    printed = titles.alnum_key(layout_title)
    for candidate in (work.get("title"), f"{work.get('title') or ''} {work.get('subtitle') or ''}"):
        key = titles.alnum_key(candidate or "")
        if not key:
            continue
        if key == printed:
            return True
        if key in printed and len(printed) - len(key) >= EMBEDDED_EXTRA:
            continue
        if titles.similarity(candidate, layout_title) >= LAYOUT_SIMILARITY:
            return True
    return False


def _year_conflict(work: dict[str, Any], text: str) -> bool:
    """True when the pages print years and none is within a year of any date the record has."""
    printed = {int(y) for y in _YEAR.findall(text)}
    known = {int(y) for y in (work.get("years") or {}).values() if y} | ({int(work["year"])} if work.get("year") else set())
    return bool(printed and known and not any(abs(p - k) <= YEAR_SLACK for p in printed for k in known))


def verify_work(work: dict[str, Any], text: str, local_titles: list[str] | None = None) -> Verdict:
    """Compare a normalised provider `work` with `text` (the first pages, joined) and the titles read from the file."""
    title = work.get("title")
    text_key = titles.alnum_key(text)
    found, total = _authors_found(work, text_key)
    components: dict[str, Any] = {"authors_found": found, "authors_checked": total}
    if work.get("preprint"):
        components["record_is_preprint"] = True
    if not title and total == 0:
        return Verdict("unverifiable", components, ["the record has no title and no authors to compare"])

    printed = bool(title) and titles.is_printed(title, text)
    contained = titles.containment(title, text) if title else 0.0
    candidates = [t for t in (local_titles or []) if t]
    layout = max((titles.similarity(title, t) for t in candidates), default=None) if title else None
    layout_same = (any(same_title(work, t) for t in candidates) if candidates else None) if title else None
    conflict = _year_conflict(work, text)
    components.update({"title_printed": printed, "title_words_present": round(contained, 2),
                       "title_equals_layout_title": layout_same, "title_similarity_to_layout": None if layout is None else round(layout, 2),
                       "year_conflict": conflict})
    reasons: list[str] = []
    if conflict:
        reasons.append(f"the pages print years, none near the record's {work.get('year')}")
    if printed and layout_same is False:
        reasons.append("the record's title is printed, but inside a different title (a correction, comment or reply?)")
    if printed and total and not found:
        reasons.append("the title is printed but no author of the record appears")

    title_equal = printed and layout_same is not False
    if title_equal and (found >= 1 or total == 0) and not conflict:
        return Verdict("exact", components, reasons)
    if (printed or contained >= STRONG_CONTAINMENT) and found >= 1:
        return Verdict("strong", components, reasons)
    if contained >= WEAK_CONTAINMENT or found >= 2 or printed:
        return Verdict("weak", components, reasons)
    return Verdict("contradicted", components, reasons + ["neither the record's title nor its authors appear on the first pages"])


@dataclass
class SearchMatch:
    chosen: dict[str, Any] | None
    verdict: Verdict | None
    others: list[dict[str, Any]] = field(default_factory=list)  # other works that matched as well as the chosen one
    ambiguous: bool = False
    reasons: list[str] = field(default_factory=list)


def choose_search_match(items: list[dict[str, Any]], query_title: str, text: str, local_titles: list[str] | None = None) -> SearchMatch:
    """The one work a title search should propose, or none, or "ambiguous".

    A result counts only if its title is (nearly) the title searched for AND it verifies against the pages: a search ranks
    by relevance, which is how a different paper with a similar title comes first. Among results that verify, a published
    version beats its preprint (both are reported); two published versions that verify equally are ambiguous."""
    verified = []
    for item in items:
        if not item.get("title") or titles.similarity(item["title"], query_title) < SEARCH_SIMILARITY:
            continue
        verdict = verify_work(item, text, local_titles)
        if verdict.level == "exact":
            verified.append((item, verdict))
    if not verified:
        return SearchMatch(None, None, reasons=["no search result is both titled like the document and verified by its first pages"])
    published = [(w, v) for w, v in verified if not w.get("preprint")]
    pool = published or verified
    if len(pool) > 1:
        return SearchMatch(None, None, others=[w for w, _ in pool], ambiguous=True,
                           reasons=[f"{len(pool)} different works match equally well"])
    chosen, verdict = pool[0]
    others = [w for w, _ in verified if w is not chosen]
    reasons = ["the preprint of the same title also matched"] if others and not chosen.get("preprint") else []
    return SearchMatch(chosen, verdict, others=others, reasons=reasons)
