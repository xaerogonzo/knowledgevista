"""Crossref (https://api.crossref.org): a DOI to its work record, and a title to candidate works.

What was checked against the live API while writing this (2026-10-07, one request for a public DOI): the work is at
`/works/<doi>` with `status: ok` and `message-type: work`; `title` and `container-title` are lists; `author` entries carry
`given`/`family`/`sequence`; dates are `date-parts`; and the response headers carry `x-rate-limit-limit` /
`x-rate-limit-interval` / `x-concurrency-limit`, which the fetcher uses for pacing instead of a number written here.
An unknown DOI is HTTP 404. A work's response includes its whole reference list, so the response-size cap matters.

Only a DOI, or a title, is ever put in a request (and the contact address the user chose, if any). Nothing from the file
and no path is sent.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from urllib.parse import quote, urlencode

from knowledgevista.domain import titles
from knowledgevista.domain.doi import normalise_doi
from knowledgevista.index import metacache
from knowledgevista.network.policy import Fetcher, LookupState
from knowledgevista.providers.base import ProviderResult

BASE_URL = "https://api.crossref.org"
CLIENT_VERSION = "1"
_SEARCH_FIELDS = "DOI,title,subtitle,author,issued,published-print,published-online,container-title,publisher,type,volume,issue,page,score"
MAX_QUERY_TITLE = 300


def _first(value: Any) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    text = titles.clean(str(value)) if value else ""
    return text or None


def _year(block: Any) -> int | None:
    try:
        return int(block["date-parts"][0][0])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def normalise_work(message: dict[str, Any]) -> dict[str, Any] | None:
    """A Crossref `work` as the normalised record, or None if it has no usable DOI. Titles lose their JATS markup."""
    doi = normalise_doi(str(message.get("DOI") or ""))
    if not doi:
        return None
    authors = []
    for entry in message.get("author") or []:
        if not isinstance(entry, dict):
            continue
        family, given, name = _first(entry.get("family")), _first(entry.get("given")), _first(entry.get("name"))
        if family or name:
            authors.append({"family": family, "given": given, "name": name})
    years = {"issued": _year(message.get("issued")), "print": _year(message.get("published-print")),
             "online": _year(message.get("published-online"))}
    kind = _first(message.get("type"))
    return {
        "provider": "crossref", "doi": doi, "title": _first(message.get("title")), "subtitle": _first(message.get("subtitle")),
        "authors": authors, "year": years["issued"] or years["print"] or years["online"], "years": years,
        "container": _first(message.get("container-title")), "publisher": _first(message.get("publisher")), "type": kind,
        "volume": _first(message.get("volume")), "issue": _first(message.get("issue")), "pages": _first(message.get("page")),
        "preprint": kind == "posted-content", "score": message.get("score"),
    }


class CrossrefProvider:
    name = "crossref"
    client_version = CLIENT_VERSION

    def __init__(self, fetcher: Fetcher, *, cache: sqlite3.Connection | None = None, base_url: str = BASE_URL):
        self.fetcher, self.cache, self.base_url = fetcher, cache, base_url.rstrip("/")

    # -- DOI -> work

    def lookup_doi(self, doi: str) -> ProviderResult:
        canonical = normalise_doi(doi)
        if not canonical:
            return ProviderResult(LookupState.NO_MATCH, detail=f"not a DOI: {doi!r}", sent=False)
        key = f"{self.name}:{CLIENT_VERSION}:doi:{canonical}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        outcome = self.fetcher.get(f"{self.base_url}/works/{quote(canonical, safe='/:;()')}{self._mailto('?')}")
        if outcome.state == LookupState.NO_MATCH:
            return self._remember(key, "doi", canonical, ProviderResult(LookupState.NO_MATCH, detail=outcome.detail), None)
        if outcome.state != LookupState.SUCCESS:
            return ProviderResult(outcome.state, detail=outcome.detail, retry_after=outcome.retry_after)
        message = self._message(outcome.body, "work")
        work = normalise_work(message) if message is not None else None
        if work is None:
            return ProviderResult(LookupState.PROVIDER_UNAVAILABLE, detail="the response was not a usable work record")
        return self._remember(key, "doi", canonical, ProviderResult(LookupState.SUCCESS, work=work), json.dumps(work))

    # -- title -> candidate works

    def search_title(self, title: str, *, rows: int = 5) -> ProviderResult:
        query = titles.clean(title)[:MAX_QUERY_TITLE]
        if len(titles.alnum_key(query)) < titles.MIN_DECISIVE_LENGTH:
            return ProviderResult(LookupState.NO_MATCH, detail="the title is too short to search for", sent=False)
        key = f"{self.name}:{CLIENT_VERSION}:title:{rows}:{titles.alnum_key(query)[:200]}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        params = urlencode({"query.bibliographic": query, "rows": rows, "select": _SEARCH_FIELDS})
        outcome = self.fetcher.get(f"{self.base_url}/works?{params}{self._mailto('&')}")
        if outcome.state != LookupState.SUCCESS:
            if outcome.state == LookupState.NO_MATCH:
                return self._remember(key, "title", query, ProviderResult(LookupState.NO_MATCH, detail=outcome.detail), None)
            return ProviderResult(outcome.state, detail=outcome.detail, retry_after=outcome.retry_after)
        message = self._message(outcome.body, "work-list")
        if message is None:
            return ProviderResult(LookupState.PROVIDER_UNAVAILABLE, detail="the response was not a work list")
        items = [w for w in (normalise_work(i) for i in message.get("items") or [] if isinstance(i, dict)) if w]
        if not items:
            return self._remember(key, "title", query, ProviderResult(LookupState.NO_MATCH, detail="no results"), None)
        return self._remember(key, "title", query, ProviderResult(LookupState.SUCCESS, items=items), json.dumps(items))

    # -- plumbing

    def _mailto(self, joiner: str) -> str:
        mailto = self.fetcher.policy.mailto
        return f"{joiner}{urlencode({'mailto': mailto})}" if mailto else ""

    @staticmethod
    def _message(body: bytes, expected: str) -> dict[str, Any] | None:
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("status") != "ok" or data.get("message-type") != expected:
            return None
        message = data.get("message")
        return message if isinstance(message, dict) else None

    def _cached(self, key: str) -> ProviderResult | None:
        if self.cache is None:
            return None
        row = metacache.get_response(self.cache, key)
        if row is None:
            return None
        if row["state"] == "no_match":
            return ProviderResult(LookupState.NO_MATCH, detail="cached: no match", from_cache=True)
        payload = json.loads(row["payload"])
        if row["kind"] == "doi":
            return ProviderResult(LookupState.SUCCESS, work=payload, from_cache=True)
        return ProviderResult(LookupState.SUCCESS, items=payload, from_cache=True)

    def _remember(self, key: str, kind: str, request: str, result: ProviderResult, payload: str | None) -> ProviderResult:
        if self.cache is not None:
            metacache.put_response(self.cache, key, provider=self.name, kind=kind, request=request,
                                   state="success" if result.state == LookupState.SUCCESS else "no_match",
                                   payload=payload, client_version=CLIENT_VERSION)
        return result
