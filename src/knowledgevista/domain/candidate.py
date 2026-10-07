"""What a producer of metadata proposes, in a form that does not know about the catalog.

A `CandidateSpec` is a proposal before it is stored: which field, what value, from which source, on what evidence, how
sure and whether a rule may accept it unattended. `evidence_key` gives each distinct piece of evidence a stable identity,
which is what lets a second run recognise a proposal it has already made instead of making it again.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

#: Who may propose what. A name here is part of the data: `docs/METADATA.md`, "Sources", says what each means.
LOCAL_SOURCES = (
    "pdf_text_doi", "pdf_metadata_doi", "layout_title", "pdf_xmp_title", "pdf_info_title", "filename_hint",
    "pdf_xmp_authors", "pdf_info_authors", "isbn_text", "arxiv_text",
)
PROVIDER_SOURCES = ("crossref", "crossref_title_search")
#: Higher is preferred when two safe candidates compete (a ranking of proposals, never an automatic override).
SOURCE_RANK = {
    "crossref": 90, "crossref_title_search": 80, "pdf_text_doi": 70, "pdf_metadata_doi": 60, "layout_title": 50,
    "pdf_xmp_title": 45, "pdf_info_title": 40, "isbn_text": 40, "arxiv_text": 40, "pdf_xmp_authors": 30,
    "pdf_info_authors": 25, "filename_hint": 5,
}


@dataclass
class CandidateSpec:
    field: str
    value: str  # already normalised (domain/fields.normalise)
    origin: str  # observed | resolved | inferred
    source: str
    evidence_key: str
    evidence: dict[str, Any] = field(default_factory=dict)
    classification: str | None = None  # own | foreign | ambiguous, for a DOI
    confidence: str = "low"  # exact | high | medium | low | ambiguous
    review: str = "required"  # safe: a named rule may accept it in a batch; required: a person must
    risk: str = "identity"
    priority: int = 50
    status: str = "proposed"  # proposed | set_aside


def evidence_key(*parts: object) -> str:
    """A short stable identity for a piece of evidence. Two findings with the same key are the same proposal."""
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:20]
