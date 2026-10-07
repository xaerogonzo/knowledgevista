"""Turn something a person typed into exactly one document, or say why it cannot.

Accepts a document ID, an artifact SHA-256 (or a unique prefix of 8+ hex characters), or a path: absolute, relative
to a root, or just a file name. Resolution NEVER guesses: more than one document is `KV_AMBIGUOUS` with every
candidate listed, and a name that only ever matched a PAST location is answered from location history, so
"what happened to kaya2022.pdf" works after the file was moved or replaced.

Rule for paths: if any CURRENT location matches (active, missing or inaccessible), only those are considered; the
history (ended locations) is consulted only when nothing current matches. Otherwise a stale name could make a
current file ambiguous against its own past.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass

from knowledgevista.domain.ids import is_uuid_hex, normalise_sha256
from knowledgevista.domain.pathkeys import is_within, normalise_root, path_key
from knowledgevista.errors import ErrorCode, KvError

_HEX_PREFIX = re.compile(r"^[0-9a-fA-F]{8,63}$")


@dataclass(frozen=True)
class Match:
    document_id: str
    artifact_id: str
    location_id: str | None
    path: str | None
    root_label: str | None
    historical: bool
    how: str  # document_id | artifact | artifact_prefix | path | file_name

    def as_dict(self) -> dict:
        return {
            "document_id": self.document_id, "artifact_id": self.artifact_id, "location_id": self.location_id,
            "path": self.path, "root": self.root_label, "historical": self.historical, "matched_by": self.how,
        }


def _document_of(conn: sqlite3.Connection, artifact_id: str) -> str | None:
    row = conn.execute("SELECT document_id FROM document_artifact WHERE artifact_id = ?", (artifact_id,)).fetchone()
    return row[0] if row else None


def _by_artifact(conn: sqlite3.Connection, artifact_id: str, how: str) -> list[Match]:
    document_id = _document_of(conn, artifact_id)
    return [Match(document_id, artifact_id, None, None, None, False, how)] if document_id else []


def _location_matches(conn: sqlite3.Connection, text: str, how_default: str) -> list[Match]:
    absolute = os.path.isabs(text)
    rows: list[sqlite3.Row] = []
    query = (
        "SELECT l.location_id, l.artifact_id, l.relative_path, l.ended_at, r.label, r.root_key "
        "FROM location l JOIN root r ON r.root_id = l.root_id WHERE l.artifact_id IS NOT NULL AND "
    )
    if absolute:
        _, key = normalise_root(text)
        for root in conn.execute("SELECT root_id, root_key FROM root"):
            if is_within(root["root_key"], key) and key != root["root_key"]:
                relative = path_key(os.path.relpath(key, root["root_key"]).replace("\\", "/"))
                rows += conn.execute(query + "l.root_id = ? AND l.path_key = ?", (root["root_id"], relative)).fetchall()
        how = "path"
    else:
        wanted = path_key(text.replace("\\", "/").strip("/"))
        escaped = wanted.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = conn.execute(
            query + "(l.path_key = ? OR l.path_key LIKE ? ESCAPE '\\')", (wanted, "%/" + escaped)
        ).fetchall()
        how = "path" if "/" in wanted else "file_name"
    current = [row for row in rows if row["ended_at"] is None]
    chosen, historical = (current, False) if current else (rows, True)
    matches = []
    for row in chosen:
        document_id = _document_of(conn, row["artifact_id"])
        if document_id:
            matches.append(Match(document_id, row["artifact_id"], row["location_id"], row["relative_path"],
                                 row["label"], historical, how or how_default))
    return matches


def candidates(conn: sqlite3.Connection, text: str) -> list[Match]:
    text = (text or "").strip()
    if not text:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, "Nothing to resolve: the reference is empty.")
    lowered = text.lower()
    if is_uuid_hex(lowered):
        row = conn.execute("SELECT artifact_id FROM document_artifact WHERE document_id = ? AND canonical = 1", (lowered,)).fetchone()
        if row:
            return [Match(lowered, row[0], None, None, None, False, "document_id")]
    sha = normalise_sha256(text)
    if sha:
        found = _by_artifact(conn, sha, "artifact")
        if found:
            return found
    if _HEX_PREFIX.match(text):
        rows = conn.execute("SELECT artifact_id FROM artifact WHERE artifact_id LIKE ? ORDER BY artifact_id", (lowered + "%",)).fetchall()
        if rows:
            return [m for row in rows for m in _by_artifact(conn, row[0], "artifact_prefix")]
    return _location_matches(conn, text, "path")


def resolve_one(conn: sqlite3.Connection, text: str) -> Match:
    """Exactly one document, or KV_NOT_FOUND / KV_AMBIGUOUS. Several locations of ONE document are not ambiguity."""
    found = candidates(conn, text)
    documents = {match.document_id for match in found}
    if not found:
        raise KvError(ErrorCode.NOT_FOUND, f"Nothing in the catalog matches {text!r}.", {"reference": text})
    if len(documents) > 1:
        raise KvError(
            ErrorCode.AMBIGUOUS, f"{text!r} matches {len(documents)} different documents; use a document id.",
            {"reference": text, "candidates": [match.as_dict() for match in found]},
        )
    return found[0]
