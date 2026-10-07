"""Candidates a document supports FROM ITSELF: its printed DOI, the title on its first page, what its metadata claims.

Everything here is a proposal with the evidence that produced it. Nothing is a fact, and nothing here talks to a
provider; provider verdicts arrive only as the `checks` mapping (DOI -> what an earlier online run concluded), so that a
rerun of the local pass keeps a DOI the provider contradicted set aside instead of proposing it again.

A candidate is `safe` (a named rule may accept it unattended) only when the evidence is independent enough that being
wrong needs two coincidences:

  DOI    printed on the front with a publisher's own words beside it, no competing own DOI, not wrapped across lines,
         and not one a provider failed to find (`SAFE_LOCAL_DOI_SCORE`, chosen from the real corpus: docs/METADATA.md)
  title  printed on the first pages AND read from two independent places (the layout and the file's own metadata)
  arXiv  exactly one identifier, printed on the first page

Authors and ISBNs are never safe locally: a hardback, a paperback and an e-book have three ISBNs, and a name list from a
file's metadata is as often the uploader as the author.

Pure: text and parsed front matter in, specs out.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from knowledgevista.domain import fields as fieldmod
from knowledgevista.domain import titles
from knowledgevista.domain.candidate import CandidateSpec, evidence_key
from knowledgevista.domain.doi_evidence import MATCHER_VERSION as DOI_MATCHER_VERSION
from knowledgevista.domain.doi_evidence import DoiEvidence, classify_dois
from knowledgevista.domain.frontmatter import FrontFacts

MATCHER_VERSION = f"local-2+{DOI_MATCHER_VERSION}"
#: A DOI that this many documents each print as their own names something they share, not any one of them.
SHARED_DOI_DOCUMENTS = 3
#: A printed DOI with a publisher cue (3 front page + 2 cue + 1 first on page) and nothing against it.
SAFE_LOCAL_DOI_SCORE = 6
#: How many leading pages are read for the title, authors and year.
FRONT_TEXT_PAGES = 2
#: Pages searched for an ISBN or arXiv identifier (a copyright page is not page one).
IDENTIFIER_PAGES = 10
TITLE_AGREEMENT = 0.9

_ISBN = re.compile(r"ISBN(?:-1[03])?\s*[:#]?\s*((?:97[89][\s-]?)?\d(?:[\s-]?\d){8,11}[\s-]?[\dXx])\b", re.IGNORECASE)
_ARXIV = re.compile(r"\barXiv:\s*([a-z\-]+(?:\.[A-Za-z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)


@dataclass
class LocalResult:
    specs: list[CandidateSpec] = field(default_factory=list)
    dois: DoiEvidence | None = None
    notes: list[str] = field(default_factory=list)


def front_text(pages: Sequence[tuple[int, str]]) -> str:
    return "\n".join(text for number, text in pages if number <= FRONT_TEXT_PAGES)


def _confidence_for_doi(classification: str, score: int) -> str:
    if classification == "own":
        return "high" if score >= SAFE_LOCAL_DOI_SCORE else "medium"
    return "ambiguous" if classification == "ambiguous" else "low"


def shared_dois(evidences: Sequence[DoiEvidence]) -> dict[str, int]:
    """DOIs that `SHARED_DOI_DOCUMENTS` or more documents of the library each call their OWN: doi -> how many.

    One document's own DOI is one document's. A DOI that many documents print as theirs names something they share: the
    book an encyclopedia entry is part of (measured: 160 entries each printing the encyclopedia's DOI), the issue a
    set of offprints came from. Such a DOI is never evidence of any one document's identity, however well it scores
    on that document's page. Computed over the whole library, so it does not depend on the order documents are read."""
    counts: dict[str, int] = {}
    for evidence in evidences:
        for candidate in evidence.own:
            counts[candidate.doi] = counts.get(candidate.doi, 0) + 1
    return {doi: n for doi, n in counts.items() if n >= SHARED_DOI_DOCUMENTS}


def _doi_specs(evidence: DoiEvidence, checks: Mapping[str, str], shared: Mapping[str, int]) -> list[CandidateSpec]:
    specs = []
    effective = {}
    for candidate in evidence.candidates:
        verdict = checks.get(candidate.doi)
        classification = candidate.classification
        if verdict == "contradicted":
            classification = "foreign"
        elif classification == "own" and candidate.doi in shared:
            classification = "ambiguous"
        effective[candidate.doi] = classification
    # At most one DOI per document is ever classified own (it needs a margin over every other), so "the only own DOI" needs no
    # separate test here: two plausible DOIs are both ambiguous, and an ambiguous one is never safe.
    for candidate in evidence.candidates:
        verdict = checks.get(candidate.doi)
        classification = effective[candidate.doi]
        status = "proposed" if classification in ("own", "ambiguous") else "set_aside"
        if status == "set_aside" and (candidate.components.get("only_on_list_pages") or candidate.first_page > 2):
            continue  # a cover page's list of neighbours, or something deep in the body: not worth a row
        notes = []
        if verdict == "contradicted":
            notes.append("a provider's record for this DOI does not match the document")
        if verdict == "no_match":
            notes.append("the provider has no record of this DOI")
        if verdict in ("weak", "unverifiable"):
            notes.append("a provider's record for this DOI only weakly matches the document")
        if candidate.doi in shared:
            notes.append(f"{shared[candidate.doi]} documents in this library print this DOI as their own, so it probably names a parent "
                         "work (a book, an issue), not this document")
        safe = (classification == "own" and candidate.score >= SAFE_LOCAL_DOI_SCORE and not candidate.wrapped
                and verdict not in ("no_match", "weak", "unverifiable"))
        source = "pdf_text_doi" if candidate.first_page else "pdf_metadata_doi"
        specs.append(CandidateSpec(
            field="doi", value=candidate.doi, origin="observed", source=source, evidence_key=evidence_key("doi", candidate.doi, source),
            evidence={
                "first_page": candidate.first_page, "pages": candidate.pages, "pages_total": candidate.pages_total,
                "region": candidate.region, "excerpt": candidate.excerpt, "cues": candidate.cues, "foreign_cues": candidate.foreign_cues,
                "score": candidate.score, "components": candidate.components, "wrapped": candidate.wrapped,
                "outranked_by": candidate.outranked_by, "references_heading_page": evidence.bibliography_page,
                "dois_only_in_references": evidence.in_bibliography, "pages_read": evidence.pages_scanned, "notes": notes,
                "shared_by_documents": shared.get(candidate.doi),
            },
            classification=classification, confidence=_confidence_for_doi(classification, candidate.score),
            review="safe" if safe else "required", priority=10 if classification == "own" else 30, status=status,
        ))
    return specs


def _title_specs(facts: FrontFacts, text: str, filename_stem: str | None) -> list[CandidateSpec]:
    found: list[tuple[str, str, dict]] = []
    if facts.layout_title:
        guess = facts.layout_title
        found.append(("layout_title", titles.clean(guess.text),
                      {"font_size": guess.size, "body_font_size": guess.body_size, "lines": guess.lines}))
    if facts.xmp_title:
        found.append(("pdf_xmp_title", facts.xmp_title, {}))
    if facts.info_title:
        found.append(("pdf_info_title", facts.info_title, {}))
    hint = titles.filename_title_hint(filename_stem) if filename_stem else None
    if hint:
        found.append(("filename_hint", hint, {}))
    specs = []
    for source, value, extra in found:
        agreeing = sorted({other for other, other_value, _ in found
                           if other == source or titles.similarity(value, other_value) >= TITLE_AGREEMENT})
        independent = [s for s in agreeing if s != "filename_hint"]
        printed = titles.is_printed(value, text)
        if source == "filename_hint":
            confidence = "low"
        elif printed and len(independent) >= 2:
            confidence = "high"
        else:
            confidence = "medium" if printed else "low"
        specs.append(CandidateSpec(
            field="title", value=value, origin="observed", source=source, evidence_key=evidence_key("title", source, titles.alnum_key(value)),
            evidence={**extra, "printed_on_first_pages": printed, "agreeing_sources": agreeing, "dropped": facts.dropped},
            # Safe needs the page's own layout among the agreeing sources: a file's Title and XMP are often one value copied from
            # one place, and two copies of junk (measured: both `doi:10.1016/...`) are not two witnesses.
            confidence=confidence, review="safe" if printed and len(independent) >= 2 and "layout_title" in independent else "required",
            priority=20,
        ))
    return specs


def _author_specs(facts: FrontFacts, text: str) -> list[CandidateSpec]:
    specs = []
    key = titles.alnum_key(text)
    for source, names in (("pdf_xmp_authors", facts.xmp_authors), ("pdf_info_authors", facts.info_authors)):
        if not names:
            continue
        surnames = [titles.alnum_key(re.split(r"[ ,]+", n.strip())[0] if "," in n else n.split()[-1]) for n in names]
        printed = sum(len(s) >= 3 and s in key for s in surnames)
        try:
            value = fieldmod.normalise("authors", [{"name": n} for n in names])
        except ValueError:
            continue
        specs.append(CandidateSpec(
            field="authors", value=value, origin="observed", source=source, evidence_key=evidence_key("authors", source, value),
            evidence={"names": len(names), "surnames_printed_on_first_pages": printed}, confidence="medium" if printed * 2 >= len(names) else "low",
            review="required", priority=40,
        ))
    return specs


def _identifier_specs(pages: Sequence[tuple[int, str]]) -> list[CandidateSpec]:
    specs, seen_isbn, arxiv_ids = [], set(), []
    for number, text in pages:
        if number > IDENTIFIER_PAGES:
            break
        for match in _ISBN.finditer(text):
            digits = re.sub(r"[\s-]", "", match.group(1)).upper()
            if fieldmod.isbn_ok(digits) and digits not in seen_isbn:
                seen_isbn.add(digits)
                specs.append(CandidateSpec(
                    field="isbn", value=digits, origin="observed", source="isbn_text", evidence_key=evidence_key("isbn", digits),
                    evidence={"page": number, "excerpt": " ".join(text[max(0, match.start() - 40): match.end() + 40].split())},
                    confidence="high", review="required", priority=30,
                ))
        if number == 1:
            for match in _ARXIV.finditer(text):
                identifier = match.group(1).lower()
                if identifier not in arxiv_ids:
                    arxiv_ids.append(identifier)
    for identifier in arxiv_ids:
        specs.append(CandidateSpec(
            field="arxiv", value=identifier, origin="observed", source="arxiv_text", evidence_key=evidence_key("arxiv", identifier),
            evidence={"page": 1, "identifiers_on_first_page": len(arxiv_ids)}, confidence="high" if len(arxiv_ids) == 1 else "ambiguous",
            review="safe" if len(arxiv_ids) == 1 else "required", priority=30,
        ))
    return specs


def local_specs(
    pages: Sequence[tuple[int, str]],
    page_count: int,
    facts: FrontFacts | None,
    filename_stem: str | None = None,
    checks: Mapping[str, str] | None = None,
    shared: Mapping[str, int] | None = None,
) -> LocalResult:
    """Everything the document itself supports. `facts` is None when the file's front matter could not be read: the
    text-based candidates (DOI, ISBN, arXiv) are still produced, and the result says what was missing."""
    result = LocalResult()
    text = front_text(pages)
    result.dois = classify_dois(list(pages), page_count, facts.metadata_dois if facts else ())
    result.specs += _doi_specs(result.dois, checks or {}, shared or {})
    result.specs += _identifier_specs(pages)
    if facts is None:
        result.notes.append("front matter was not read: no layout title, file-metadata title or authors")
    else:
        result.specs += _title_specs(facts, text, filename_stem)
        result.specs += _author_specs(facts, text)
    if not any(t.strip() for _, t in pages):
        result.notes.append("the document has no text layer; only its file metadata can propose anything")
    return result
