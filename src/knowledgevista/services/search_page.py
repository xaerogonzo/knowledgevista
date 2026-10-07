"""One page of a search, continued from a cursor: the single implementation `kv search`, `kv saved run` and the MCP `search_pages`
tool all use, so a hit list means the same thing whoever asks and a cursor from one is understood by the same code.

Search ranks deterministically (relevance, then artifact id, then page), so with the catalog unchanged the Nth hit is always the
same hit. A cursor therefore stores an offset and the last hit's anchor; the service is asked for `offset + limit` hits and the
first `offset` are dropped. The window is capped (services/cursor.py), which bounds what one request can make the index read.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from knowledgevista.db.catalog import revision
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.index.search import Query
from knowledgevista.services import cursor as cursormod
from knowledgevista.services import search as search_service


def query_signature(query: Query) -> str:
    return cursormod.signature_of(list(query.alternatives), list(query.near), query.within, list(query.also), query.path_contains,
                                  [list(f) for f in query.filters], query.language_version)


def hit_key(hit: dict[str, Any]) -> str:
    return f"{hit['artifact_id']}:{hit['pdf_page']}"


def search_page(catalog: sqlite3.Connection, index: sqlite3.Connection | None, query: Query, *, limit: int, token: str | None = None,
                command: str = "search") -> tuple[search_service.SearchResult, str | None]:
    """(the result, narrowed to this page, and the cursor for the next page or None)."""
    limit = cursormod.bounded_limit(limit, 20)
    rev, signature = revision(catalog), query_signature(query)
    offset = cursormod.start_offset(token, command=command, signature=signature, revision=rev)
    if offset + limit > cursormod.MAX_WINDOW:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"A search cannot be paged past {cursormod.MAX_WINDOW} hits; narrow the query.", {"offset": offset, "limit": limit})
    result = search_service.search(catalog, index, query, limit=offset + limit)
    if token is not None:
        cursormod.check_anchor(cursormod.decode(token), [hit_key(h) for h in result.hits])
    more = result.truncated
    result.hits = result.hits[offset:]
    result.truncated = more
    last = hit_key(result.hits[-1]) if result.hits else None
    return result, cursormod.next_cursor(command=command, signature=signature, revision=rev, offset=offset, shown=len(result.hits), last_key=last, more=more)
