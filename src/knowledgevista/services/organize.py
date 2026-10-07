"""Virtual organisation: collections, tags, saved searches and system views.

None of this touches a file. It is the part of "organising a library" that needs no risk: grouping, labelling and
re-finding documents without moving them, which covers most of what people want a tidy folder for (docs/ARCHITECTURE.md,
"Organizer"). The folder-reorganising organizer is a later, optional, reversible layer over the filesystem.

  * A collection is a named, ordered-by-name set of documents. Several documents can be in several collections.
  * A tag is a word on a document. Tags and collection names compare without regard to case or spacing.
  * A saved search stores the PARSED query and its language version, never a list of results, so it cannot silently change
    meaning when the parser is redesigned, and running it always asks the library as it is now.
  * A system view (Inbox, Unresolved, Missing, Duplicates, ...) is a QUERY, not stored state: nothing is recorded as "in the
    inbox", so a document leaves it the moment the reason is gone.

Nothing is deleted: a retired collection or saved search stays recorded.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from knowledgevista.db.catalog import bump_revision, transaction
from knowledgevista.domain.ids import new_id, utc_now
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.index.search import QUERY_LANGUAGE_VERSION, Query

MAX_NAME = 200


def key_of(name: str) -> str:
    """Case-, width- and spacing-insensitive comparison key; punctuation is kept, because `C` and `C++` are different tags."""
    key = " ".join(unicodedata.normalize("NFKC", name or "").casefold().split())
    if not key:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, "A name cannot be empty.")
    if len(key) > MAX_NAME:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"A name can be at most {MAX_NAME} characters.")
    return key


def _clean(name: str) -> str:
    return " ".join((name or "").split())


def _active(conn: sqlite3.Connection, document_id: str) -> None:
    row = conn.execute("SELECT retired_at FROM document WHERE document_id = ?", (document_id,)).fetchone()
    if row is None:
        raise KvError(ErrorCode.NOT_FOUND, f"No document {document_id}.", {"document_id": document_id})
    if row["retired_at"] is not None:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"Document {document_id[:12]} was merged into another and is retired; use the one it was merged into.")


# ------------------------------------------------------------------------------------------------ collections


def find_collection(conn: sqlite3.Connection, reference: str) -> sqlite3.Row:
    """A live collection by name (any case) or by an id prefix (8+ hex characters)."""
    key = key_of(reference)
    row = conn.execute("SELECT * FROM collection WHERE name_key = ? AND retired_at IS NULL", (key,)).fetchone()
    if row is not None:
        return row
    if len(key) >= 8 and all(c in "0123456789abcdef" for c in key):
        rows = conn.execute("SELECT * FROM collection WHERE collection_id LIKE ? AND retired_at IS NULL LIMIT 6", (key + "%",)).fetchall()
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            raise KvError(ErrorCode.AMBIGUOUS, f"{reference!r} matches more than one collection.", {"candidates": [r["name"] for r in rows]})
    raise KvError(ErrorCode.NOT_FOUND, f"No collection named {reference!r}. See: kv collection list", {"reference": reference})


def create_collection(conn: sqlite3.Connection, name: str, *, description: str | None = None, source_doi: str | None = None, actor: str = "user") -> str:
    """Caller owns the transaction."""
    key = key_of(name)
    if conn.execute("SELECT 1 FROM collection WHERE name_key = ? AND retired_at IS NULL", (key,)).fetchone():
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"A collection called {_clean(name)!r} already exists.", {"name": name})
    collection_id = new_id()
    conn.execute("INSERT INTO collection (collection_id, name, name_key, description, source_doi, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (collection_id, _clean(name), key, description, source_doi, utc_now(), actor))
    return collection_id


def _add_members(conn: sqlite3.Connection, collection_id: str, document_ids: list[str], actor: str) -> int:
    added = 0
    for document_id in document_ids:
        _active(conn, document_id)
        added += conn.execute("INSERT OR IGNORE INTO collection_member (collection_id, document_id, added_at, added_by) VALUES (?, ?, ?, ?)",
                              (collection_id, document_id, utc_now(), actor)).rowcount
    return added


def new_collection(conn: sqlite3.Connection, name: str, document_ids: list[str] | None = None, *, description: str | None = None, actor: str = "user") -> dict[str, Any]:
    with transaction(conn):
        collection_id = create_collection(conn, name, description=description, actor=actor)
        added = _add_members(conn, collection_id, document_ids or [], actor)
        bump_revision(conn)
        return {"collection_id": collection_id, "name": _clean(name), "added": added}


def add_to_collection(conn: sqlite3.Connection, reference: str, document_ids: list[str], *, actor: str = "user") -> dict[str, Any]:
    with transaction(conn):
        collection = find_collection(conn, reference)
        added = _add_members(conn, collection["collection_id"], document_ids, actor)
        if added:
            bump_revision(conn)
        return {"collection_id": collection["collection_id"], "name": collection["name"], "added": added, "already_members": len(document_ids) - added}


def remove_from_collection(conn: sqlite3.Connection, reference: str, document_ids: list[str]) -> dict[str, Any]:
    with transaction(conn):
        collection = find_collection(conn, reference)
        removed = sum(conn.execute("DELETE FROM collection_member WHERE collection_id = ? AND document_id = ?",
                                   (collection["collection_id"], d)).rowcount for d in document_ids)
        if removed:
            bump_revision(conn)
        return {"collection_id": collection["collection_id"], "name": collection["name"], "removed": removed}


def retire_collection(conn: sqlite3.Connection, reference: str) -> dict[str, Any]:
    """Delete a collection from view. Its members and history stay recorded."""
    with transaction(conn):
        collection = find_collection(conn, reference)
        conn.execute("UPDATE collection SET retired_at = ? WHERE collection_id = ?", (utc_now(), collection["collection_id"]))
        bump_revision(conn)
        return {"collection_id": collection["collection_id"], "name": collection["name"]}


def list_collections(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT c.collection_id, c.name, c.description, c.source_doi, c.created_at, "
        "(SELECT COUNT(*) FROM collection_member m JOIN document d ON d.document_id = m.document_id "
        " WHERE m.collection_id = c.collection_id AND d.retired_at IS NULL) AS members "
        "FROM collection c WHERE c.retired_at IS NULL ORDER BY c.name_key")]


def collection_members(conn: sqlite3.Connection, reference: str) -> tuple[sqlite3.Row, list[str]]:
    collection = find_collection(conn, reference)
    ids = [r[0] for r in conn.execute(
        "SELECT m.document_id FROM collection_member m JOIN document d ON d.document_id = m.document_id "
        "WHERE m.collection_id = ? AND d.retired_at IS NULL ORDER BY m.document_id", (collection["collection_id"],))]
    return collection, ids


def create_collection_from_group(conn: sqlite3.Connection, name: str, members: list[str], *, source_doi: str | None, actor: str) -> dict[str, Any]:
    """Accepting a 'these documents belong together' proposal. If a collection of that name exists, the members join it.
    Retired members (merged away since the proposal) are skipped. Caller owns the transaction."""
    live = [m for m in members if (conn.execute("SELECT retired_at FROM document WHERE document_id = ?", (m,)).fetchone() or ["gone"])[0] is None]
    existing = conn.execute("SELECT * FROM collection WHERE name_key = ? AND retired_at IS NULL", (key_of(name),)).fetchone()
    collection_id = existing["collection_id"] if existing else create_collection(conn, name, source_doi=source_doi, actor=actor)
    added = _add_members(conn, collection_id, live, actor)
    return {"collection_id": collection_id, "name": existing["name"] if existing else _clean(name), "added": added,
            "skipped_retired": len(members) - len(live), "reused_existing": existing is not None}


# ------------------------------------------------------------------------------------------------ tags


def add_tags(conn: sqlite3.Connection, document_ids: list[str], tags: list[str], *, actor: str = "user") -> dict[str, Any]:
    keyed: dict[str, str] = {}
    for tag in tags:
        keyed.setdefault(key_of(tag), _clean(tag))  # the spelling first given is the one kept
    with transaction(conn):
        added = 0
        for document_id in document_ids:
            _active(conn, document_id)
            for key, tag in keyed.items():
                added += conn.execute("INSERT OR IGNORE INTO document_tag (document_id, tag_key, tag, added_at, added_by) VALUES (?, ?, ?, ?, ?)",
                                      (document_id, key, tag, utc_now(), actor)).rowcount
        if added:
            bump_revision(conn)
        return {"added": added, "tags": sorted(keyed.values())}


def remove_tags(conn: sqlite3.Connection, document_ids: list[str], tags: list[str]) -> dict[str, Any]:
    keys = [key_of(t) for t in tags]
    with transaction(conn):
        removed = sum(conn.execute("DELETE FROM document_tag WHERE document_id = ? AND tag_key = ?", (d, k)).rowcount for d in document_ids for k in keys)
        if removed:
            bump_revision(conn)
        return {"removed": removed}


def list_tags(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT t.tag_key, MIN(t.tag) AS tag, COUNT(*) AS documents FROM document_tag t JOIN document d ON d.document_id = t.document_id "
        "WHERE d.retired_at IS NULL GROUP BY t.tag_key ORDER BY t.tag_key")]


def tags_of(conn: sqlite3.Connection, document_id: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT tag FROM document_tag WHERE document_id = ? ORDER BY tag_key", (document_id,))]


def collections_of(conn: sqlite3.Connection, document_id: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT c.name FROM collection_member m JOIN collection c ON c.collection_id = m.collection_id "
        "WHERE m.document_id = ? AND c.retired_at IS NULL ORDER BY c.name_key", (document_id,))]


# ------------------------------------------------------------------------------------------------ saved searches


def query_to_json(query: Query) -> str:
    data = asdict(query)
    data["filters"] = [list(f) for f in query.filters]
    return json.dumps(data, sort_keys=True, ensure_ascii=True)


def query_from_json(text: str, language_version: int) -> Query:
    """Rebuild a saved query. A version this program does not know is refused rather than reinterpreted."""
    if language_version > QUERY_LANGUAGE_VERSION:
        raise KvError(ErrorCode.QUERY_INVALID, f"That saved search uses query language {language_version}, newer than this program understands "
                      f"({QUERY_LANGUAGE_VERSION}). Update Knowledge Vista.", {"language_version": language_version})
    data = json.loads(text)
    return Query(
        alternatives=tuple(data["alternatives"]), near=tuple(data.get("near", ())), within=int(data.get("within", 30)), also=tuple(data.get("also", ())),
        path_contains=data.get("path_contains"), filters=tuple(tuple(f) for f in data.get("filters", ())), language_version=int(data.get("language_version", 1)),
    )


def save_search(conn: sqlite3.Connection, name: str, query: Query, *, replace: bool = False) -> dict[str, Any]:
    key = key_of(name)
    with transaction(conn):
        existing = conn.execute("SELECT search_id FROM saved_search WHERE name_key = ? AND retired_at IS NULL", (key,)).fetchone()
        now = utc_now()
        if existing and not replace:
            raise KvError(ErrorCode.INVALID_ARGUMENTS, f"A saved search called {_clean(name)!r} already exists. Use --replace to overwrite it.", {"name": name})
        if existing:
            conn.execute("UPDATE saved_search SET query_json = ?, language_version = ?, updated_at = ? WHERE search_id = ?",
                         (query_to_json(query), query.language_version, now, existing["search_id"]))
            search_id = existing["search_id"]
        else:
            search_id = new_id()
            conn.execute("INSERT INTO saved_search (search_id, name, name_key, query_json, language_version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (search_id, _clean(name), key, query_to_json(query), query.language_version, now, now))
        bump_revision(conn)
        return {"search_id": search_id, "name": _clean(name), "replaced": bool(existing)}


def get_saved_search(conn: sqlite3.Connection, name: str) -> tuple[sqlite3.Row, Query]:
    row = conn.execute("SELECT * FROM saved_search WHERE name_key = ? AND retired_at IS NULL", (key_of(name),)).fetchone()
    if row is None:
        raise KvError(ErrorCode.NOT_FOUND, f"No saved search called {name!r}. See: kv saved list", {"name": name})
    return row, query_from_json(row["query_json"], row["language_version"])


def list_saved_searches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [{"search_id": r["search_id"], "name": r["name"], "language_version": r["language_version"], "query": json.loads(r["query_json"]),
             "updated_at": r["updated_at"]} for r in conn.execute("SELECT * FROM saved_search WHERE retired_at IS NULL ORDER BY name_key")]


def retire_saved_search(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    with transaction(conn):
        row = conn.execute("SELECT * FROM saved_search WHERE name_key = ? AND retired_at IS NULL", (key_of(name),)).fetchone()
        if row is None:
            raise KvError(ErrorCode.NOT_FOUND, f"No saved search called {name!r}.", {"name": name})
        conn.execute("UPDATE saved_search SET retired_at = ? WHERE search_id = ?", (utc_now(), row["search_id"]))
        bump_revision(conn)
        return {"search_id": row["search_id"], "name": row["name"]}


# ------------------------------------------------------------------------------------------------ system views


def _live_documents(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT document_id FROM document WHERE retired_at IS NULL ORDER BY document_id")]


def _available(conn: sqlite3.Connection, document_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM document_artifact da JOIN location l ON l.artifact_id = da.artifact_id AND l.ended_at IS NULL AND l.state = 'active' "
        "JOIN root r ON r.root_id = l.root_id AND r.status = 'online' WHERE da.document_id = ? LIMIT 1", (document_id,)).fetchone() is not None


def view_unresolved(conn: sqlite3.Connection, index: sqlite3.Connection | None) -> list[dict[str, Any]]:
    from knowledgevista.services.metadata_report import TITLE_REASONS, title_states
    return [{"document_id": d, "reason": state, "detail": TITLE_REASONS[state]} for d, state in sorted(title_states(conn, index).items()) if state != "accepted"]


def view_ambiguous(conn: sqlite3.Connection, index: sqlite3.Connection | None) -> list[dict[str, Any]]:
    docs: dict[str, str] = {}
    for r in conn.execute("SELECT DISTINCT document_id FROM metadata_candidate WHERE status = 'proposed' AND (classification = 'ambiguous' OR confidence = 'ambiguous')"):
        docs[r[0]] = "a proposal that the evidence does not decide"
    for r in conn.execute("SELECT source_id, target_id FROM relation_candidate WHERE status = 'proposed' AND level <> 'group' AND confidence IN ('low', 'ambiguous')"):
        for end in (r[0], r[1]):
            if conn.execute("SELECT 1 FROM document WHERE document_id = ? AND retired_at IS NULL", (end,)).fetchone():
                docs.setdefault(end, "a relation proposal that the evidence does not decide")
    return [{"document_id": d, "reason": "ambiguous", "detail": text} for d, text in sorted(docs.items())]


def view_missing(conn: sqlite3.Connection, index: sqlite3.Connection | None) -> list[dict[str, Any]]:
    return [{"document_id": d, "reason": "missing", "detail": "no reachable copy right now (files missing, or the root is offline)"}
            for d in _live_documents(conn) if not _available(conn, d)]


def exact_copy_groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Level one of duplicates: ONE artifact (the same bytes) at several current paths. Not a relation: the library has one
    item; the files are copies of it."""
    groups = []
    for r in conn.execute("SELECT artifact_id, COUNT(*) AS n FROM location WHERE ended_at IS NULL AND state = 'active' AND artifact_id IS NOT NULL "
                          "GROUP BY artifact_id HAVING n > 1 ORDER BY artifact_id"):
        paths = [{"root": p["label"], "path": p["relative_path"]} for p in conn.execute(
            "SELECT r.label, l.relative_path FROM location l JOIN root r ON r.root_id = l.root_id WHERE l.artifact_id = ? AND l.ended_at IS NULL AND l.state = 'active' "
            "ORDER BY l.path_key", (r["artifact_id"],))]
        document = conn.execute("SELECT document_id FROM document_artifact WHERE artifact_id = ?", (r["artifact_id"],)).fetchone()
        groups.append({"artifact_id": r["artifact_id"], "document_id": document[0] if document else None, "copies": r["n"], "paths": paths})
    return groups


def view_duplicates(conn: sqlite3.Connection, index: sqlite3.Connection | None) -> list[dict[str, Any]]:
    docs: dict[str, str] = {}
    for g in exact_copy_groups(conn):
        if g["document_id"]:
            docs[g["document_id"]] = f"the same file at {g['copies']} paths"
    for r in conn.execute("SELECT source_id, target_id FROM relation_candidate WHERE status = 'proposed' AND kind = 'same_document'"):
        for end in (r[0], r[1]):
            docs.setdefault(end, "may be the same document as another (proposal)")
    return [{"document_id": d, "reason": "duplicates", "detail": text} for d, text in sorted(docs.items())]


def view_new(conn: sqlite3.Connection, index: sqlite3.Connection | None) -> list[dict[str, Any]]:
    """Documents `kv resolve` has not looked at yet: nothing proposed, nothing accepted."""
    seen = {r[0] for r in conn.execute("SELECT document_id FROM metadata_candidate UNION SELECT document_id FROM metadata_value")}
    return [{"document_id": d, "reason": "new", "detail": "not yet resolved (kv resolve)"} for d in _live_documents(conn) if d not in seen]


def view_untagged(conn: sqlite3.Connection, index: sqlite3.Connection | None) -> list[dict[str, Any]]:
    tagged = {r[0] for r in conn.execute("SELECT DISTINCT document_id FROM document_tag")}
    return [{"document_id": d, "reason": "untagged", "detail": "no tags"} for d in _live_documents(conn) if d not in tagged]


def view_uncollected(conn: sqlite3.Connection, index: sqlite3.Connection | None) -> list[dict[str, Any]]:
    member = {r[0] for r in conn.execute("SELECT DISTINCT m.document_id FROM collection_member m JOIN collection c ON c.collection_id = m.collection_id WHERE c.retired_at IS NULL")}
    return [{"document_id": d, "reason": "uncollected", "detail": "in no collection"} for d in _live_documents(conn) if d not in member]


def view_inbox(conn: sqlite3.Connection, index: sqlite3.Connection | None) -> list[dict[str, Any]]:
    """What needs a person first: unresolved, then ambiguous, then missing, then new. A document appears once, under the first reason."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for name in ("unresolved", "ambiguous", "missing", "new"):
        for item in VIEWS[name][1](conn, index):
            if item["document_id"] not in seen:
                seen.add(item["document_id"])
                out.append(item)
    return out


#: name -> (description, function). Order is the order shown.
VIEWS: dict[str, tuple[str, Callable[[sqlite3.Connection, sqlite3.Connection | None], list[dict[str, Any]]]]] = {
    "inbox": ("what needs a person first: unresolved, ambiguous, missing, then new", view_inbox),
    "unresolved": ("PDFs without an accepted title, with the reason", view_unresolved),
    "ambiguous": ("proposals the evidence does not decide", view_ambiguous),
    "missing": ("documents with no reachable copy right now", view_missing),
    "duplicates": ("the same file at several paths, or a proposed same-document merge", view_duplicates),
    "new": ("documents `kv resolve` has not looked at", view_new),
    "untagged": ("documents with no tag", view_untagged),
    "uncollected": ("documents in no collection", view_uncollected),
}


def run_view(conn: sqlite3.Connection, index: sqlite3.Connection | None, name: str) -> list[dict[str, Any]]:
    if name not in VIEWS:
        raise KvError(ErrorCode.NOT_FOUND, f"No view called {name!r}. Views: {', '.join(VIEWS)}", {"views": list(VIEWS)})
    return VIEWS[name][1](conn, index)
