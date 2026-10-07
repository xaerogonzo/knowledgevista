"""The metadata cache: provider answers (positive AND negative) and the front matter read from each file.

Like the extraction store it is derived data in the cache directory, deleted and rebuilt on a schema mismatch, and
deleting it loses no catalog state: accepted metadata, candidates and history are in the catalog. What it buys:

  * Resumable resolution. A run that is killed, or stopped by a provider outage, repeats no request that already got an
    answer, because the answer (a work, or "no such DOI") is here.
  * Negative caching. "No match" is cached for a shorter time than a match (a work can be registered tomorrow), so a
    library full of DOIs the provider does not know does not ask again on every run.
  * NOT cached: a transient error, a rate limit, an outage. Those say nothing about the work and must be retried.

The cache key names the provider, its client version and the normalised request, so a change to how a provider is queried
or read cannot serve an answer shaped by the old way.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from knowledgevista.db.catalog import transaction
from knowledgevista.domain.ids import utc_now
from knowledgevista.index.store import _connect, _remove, _split, _version_of

SCHEMA_VERSION = 1
SUCCESS_TTL = timedelta(days=90)
NO_MATCH_TTL = timedelta(days=14)

_SCHEMA = """
CREATE TABLE response (
    cache_key       TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    kind            TEXT NOT NULL,
    request         TEXT NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('success', 'no_match')),
    payload         TEXT,
    client_version  TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);
CREATE INDEX response_expiry ON response (expires_at);
CREATE TABLE front (
    artifact_id        TEXT PRIMARY KEY,
    extractor_version  TEXT NOT NULL,
    ok                 INTEGER NOT NULL CHECK (ok IN (0, 1)),
    payload            TEXT,
    error              TEXT,
    created_at         TEXT NOT NULL
);
"""


def open_metacache(path: Path | str, *, create: bool, read_only: bool = False) -> sqlite3.Connection | None:
    """Open the cache; None if it does not exist and `create` is False (or, read-only, if it is another schema)."""
    target = Path(path)
    if target.exists():
        if _version_of(target) != SCHEMA_VERSION:
            if read_only or not create:
                return None
            _remove(target)
    if not target.exists():
        if not create:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        connection = _connect(target, read_only=False)
        try:
            with transaction(connection):
                for statement in _split(_SCHEMA):
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except BaseException:
            connection.close()
            _remove(target)
            raise
        if read_only:
            connection.close()
            return _connect(target, read_only=True)
        return connection
    return _connect(target, read_only=read_only)


def _later(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def get_response(cache: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    """A cached answer that has not expired, else None."""
    return cache.execute("SELECT * FROM response WHERE cache_key = ? AND expires_at > ?", (key, utc_now())).fetchone()


def put_response(
    cache: sqlite3.Connection, key: str, *, provider: str, kind: str, request: str, state: str, payload: str | None, client_version: str
) -> None:
    """Remember an answer. Only `success` and `no_match` are ever stored; anything else is a bug in the caller."""
    if state not in ("success", "no_match"):
        raise ValueError(f"only a definite answer is cached, not {state!r}")
    ttl = SUCCESS_TTL if state == "success" else NO_MATCH_TTL
    with transaction(cache):
        cache.execute(
            "INSERT OR REPLACE INTO response (cache_key, provider, kind, request, state, payload, client_version, fetched_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, provider, kind, request, state, payload, client_version, utc_now(), _later(ttl)),
        )


def get_front(cache: sqlite3.Connection, artifact_id: str, extractor_version: str) -> sqlite3.Row | None:
    """The stored front matter for these bytes, read by this extractor version, else None."""
    return cache.execute("SELECT * FROM front WHERE artifact_id = ? AND extractor_version = ?", (artifact_id, extractor_version)).fetchone()


def put_front(cache: sqlite3.Connection, artifact_id: str, extractor_version: str, *, ok: bool, payload: str | None, error: str | None) -> None:
    with transaction(cache):
        cache.execute(
            "INSERT OR REPLACE INTO front (artifact_id, extractor_version, ok, payload, error, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (artifact_id, extractor_version, int(ok), payload, error, utc_now()),
        )


def purge_expired(cache: sqlite3.Connection) -> int:
    with transaction(cache):
        return cache.execute("DELETE FROM response WHERE expires_at <= ?", (utc_now(),)).rowcount
