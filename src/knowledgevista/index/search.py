"""The search query: parse text into a versioned AST, then compile it to an FTS5 expression with every term quoted.

PORTED from OpenChem's `tools/library_index.py` (`_phrase`, `fts_query`), whose behaviour is pinned by its tests, with
one structural change: the user's text becomes a `Query` object first, and SQL is built from that object only. That is
the injection boundary, and it is what lets the CLI, MCP and GUI (and later saved searches) share one meaning. A saved
search stores the AST and its `query_language_version`, never a result list, so it cannot silently change meaning
when the parser is redesigned.

WHAT SEARCH MEANS (docs/SEARCH.md is the readable version):
  * Every term is a quoted phrase: `2,4-DNT`, `NEAR(a b)`, `x AND y` are searched, not parsed as operators.
  * A trailing `*` is the ONLY operator a term can carry: an explicit prefix match (`solub*`).
  * No stemming and no typo correction, ever: in chemistry a "correction" changes the compound.
  * Case-insensitive; accents are folded (`remove_diacritics 2`, the OpenChem index's tokenizer, kept for parity).
  * Punctuation separates tokens, so `2,4-DNT` is the phrase `2 4 dnt`, and does NOT match `2,4,6-trinitrotoluene`.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from knowledgevista.domain.doi import normalise_doi
from knowledgevista.errors import ErrorCode, KvError

#: 2 added metadata filters (`tag:`, `collection:`, `doi:`, `year:`, `author:`, `kind:`). A version-1 saved search parses unchanged.
QUERY_LANGUAGE_VERSION = 2
DEFAULT_WITHIN = 30
FILTER_FIELDS = ("author", "year", "doi", "kind", "tag", "collection")
_FILTER = re.compile(r'(?<!\S)(' + "|".join(FILTER_FIELDS) + r'):(?:"([^"]*)"|(\S*))')
_YEARS = re.compile(r"^(\d{4})?(?:-(\d{4})?)?$")


@dataclass(frozen=True)
class Query:
    """What to find. `alternatives` are synonyms (OR); each `near` term must be within `within` tokens of the
    alternative; each `also` term must appear anywhere on the same page; `path_contains` scopes to files whose path
    contains the text."""

    alternatives: tuple[str, ...]
    near: tuple[str, ...] = ()
    within: int = DEFAULT_WITHIN
    also: tuple[str, ...] = ()
    path_contains: str | None = None
    #: (field, value) pairs that narrow the search to documents with that metadata; ALL must hold.
    filters: tuple[tuple[str, str], ...] = ()
    language_version: int = QUERY_LANGUAGE_VERSION


def extract_filters(text: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Pull `field:value` / `field:"a value"` filters out of the query text. ONLY the six known field names at the start of a
    word are filters, so chemistry like `Cu(II):` or `pH:7.4` stays search text; quote a phrase to search for `"year:2020"`
    literally. A filter with no value is KV_QUERY_INVALID, never a search that quietly ignores it."""
    found: list[tuple[str, str]] = []

    def take(match: re.Match[str]) -> str:
        name, value = match.group(1), (match.group(2) if match.group(2) is not None else match.group(3)).strip()
        if not value:
            raise KvError(ErrorCode.QUERY_INVALID, f"The filter {name}: has no value.", {"filter": name})
        if name == "year" and not (_YEARS.match(value) and any(re.findall(r"\d{4}", value))):
            raise KvError(ErrorCode.QUERY_INVALID, f"year: wants a year (2020), a range (2015-2020) or an open range (2015- / -2020), not {value!r}.", {"filter": name})
        if name == "doi" and not normalise_doi(value):
            raise KvError(ErrorCode.QUERY_INVALID, f"doi: wants a DOI such as 10.1234/abc, not {value!r}.", {"filter": name})
        found.append((name, value))
        return " "

    return " ".join(_FILTER.sub(take, text or "").split()), tuple(found)


def parse_query(
    text: str, *, near: Iterable[str] = (), within: int = DEFAULT_WITHIN, also: Iterable[str] = (), path_contains: str | None = None
) -> Query:
    """`text` is ONE phrase, or alternatives separated by `|`, plus optional `field:value` filters. Raises KV_QUERY_INVALID
    rather than returning a query that would silently match nothing."""
    text, filters = extract_filters(text)
    alternatives = tuple(part for part in (p.strip() for p in (text or "").split("|")) if part)
    if not alternatives:
        raise KvError(ErrorCode.QUERY_INVALID, "The query is empty: give a word or phrase to search for.", {"query": text})
    for term in alternatives:
        if not term.rstrip("*").strip():
            raise KvError(ErrorCode.QUERY_INVALID, f"{term!r} has nothing to search for before the '*'.", {"term": term})
    if within < 1:
        raise KvError(ErrorCode.QUERY_INVALID, "--within must be at least 1.", {"within": within})
    return Query(
        alternatives, tuple(t.strip() for t in near if t and t.strip()), within,
        tuple(t.strip() for t in also if t and t.strip()), path_contains or None, filters,
    )


def _phrase(term: str) -> str:
    """One FTS5 phrase with the query syntax defused. A TRAILING `*` is kept as a prefix match; without it a stem
    matches nothing at all, and zero hits reads as "not in the library"."""
    term = term.strip()
    prefix = term.endswith("*")
    quoted = '"' + term.rstrip("*").strip().replace('"', '""') + '"'
    return quoted + ("*" if prefix else "")


def compile_fts(query: Query) -> str:
    parts = []
    for alternative in query.alternatives:
        if query.near:
            terms = " ".join([_phrase(alternative), *(_phrase(t) for t in query.near)])
            parts.append(f"NEAR({terms}, {int(query.within)})")
        else:
            parts.append(_phrase(alternative))
    expression = " OR ".join(parts)
    if query.also:
        expression = f"({expression}) AND " + " AND ".join(_phrase(t) for t in query.also)
    return expression


@dataclass(frozen=True)
class RawHit:
    page_id: int
    artifact_id: str
    pdf_page: int
    printed_label: str | None
    source: str
    status: str
    snippet: str
    rank: float


def run_search(
    connection: sqlite3.Connection, query: Query, *, limit: int, artifact_scope: set[str] | None = None
) -> list[RawHit]:
    """Matching pages, best first, ties broken by artifact then page so the order is the same every time.

    Returns up to `limit + 1` rows: the extra row is how the caller knows the result was truncated."""
    expression = compile_fts(query)
    sql = (
        "SELECT p.page_id, e.artifact_id, p.pdf_page, p.printed_label, e.source, e.status, "
        "snippet(page_fts, 0, '[[', ']]', ' ... ', 24) AS snippet, page_fts.rank AS rank "
        "FROM page_fts JOIN page p ON p.page_id = page_fts.rowid JOIN extraction e ON e.extraction_id = p.extraction_id "
        "WHERE page_fts MATCH ? "
    )
    arguments: list = [expression]
    if artifact_scope is not None:
        connection.execute("CREATE TEMP TABLE IF NOT EXISTS _scope (artifact_id TEXT PRIMARY KEY)")
        connection.execute("DELETE FROM _scope")
        connection.executemany("INSERT OR IGNORE INTO _scope VALUES (?)", [(a,) for a in artifact_scope])
        sql += "AND e.artifact_id IN (SELECT artifact_id FROM _scope) "
    sql += "ORDER BY page_fts.rank, e.artifact_id, p.pdf_page LIMIT ?"
    arguments.append(limit + 1)
    try:
        rows = connection.execute(sql, arguments).fetchall()
    except sqlite3.OperationalError as exc:
        raise KvError(ErrorCode.QUERY_INVALID, f"The search engine could not parse this query: {exc}", {"expression": expression}) from exc
    return [RawHit(r["page_id"], r["artifact_id"], r["pdf_page"], r["printed_label"], r["source"], r["status"], r["snippet"], r["rank"]) for r in rows]
