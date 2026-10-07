"""What a metadata provider is: one protocol, a normalised work record, and the result of asking.

A provider turns a DOI or a title into a `WORK`: the same dictionary shape whatever the source, so the matcher and the
review queue never learn which provider spoke. A provider never decides whether its answer is the right paper: it
reports what it was told, and `domain/match.py` compares that with the PDF.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from knowledgevista.network.policy import LookupState

#: The keys of a normalised work. Absent information is None or an empty list, never an empty string.
WORK_KEYS = ("provider", "doi", "title", "subtitle", "authors", "year", "years", "container", "publisher", "type",
             "volume", "issue", "pages", "preprint", "score")


@dataclass
class ProviderResult:
    state: LookupState
    work: dict[str, Any] | None = None  # a DOI lookup's answer
    items: list[dict[str, Any]] = field(default_factory=list)  # a title search's candidates, best first
    detail: str = ""
    from_cache: bool = False
    #: False when the answer was produced without asking anyone (a malformed DOI, a title too short to search for).
    sent: bool = True
    retry_after: float | None = None


class MetadataProvider(Protocol):
    name: str
    #: Bumped when the way this provider is queried or its answer is read changes: part of every cache key.
    client_version: str

    def lookup_doi(self, doi: str) -> ProviderResult: ...

    def search_title(self, title: str, *, rows: int = 5) -> ProviderResult: ...
