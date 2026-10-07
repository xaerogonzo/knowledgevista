"""Where is it NOW: `kv locate`, the call other programs make with a hash, a document id or a `knowledgevista://` reference.

This is the answer to the question the whole design exists for (docs/INVARIANTS.md, "filenames are locators"): a caller that
remembers WHICH bytes it meant, not where they were, asks and is told the current path, whether the file is really there, and what
the library knows about it. The caller (OpenChem's literature checks) must be able to rely on three things:

  * Exactly one answer or a refusal. A name that matches several documents is `KV_AMBIGUOUS` with every candidate; a hash prefix
    that matches two is the same. Nothing is guessed.
  * Reality beats the catalog. Each current location is stat'ed, because a catalog scanned yesterday can name a file someone
    renamed this morning; `on_disk: false` on an `active` location says the catalog is behind, rather than leaving the caller to
    open a path that is not there. A root that is offline is `on_disk: null` (not looked at), never `false`.
  * An id that a merge retired still resolves, to the document that now holds its artifacts, and says so
    (`requested_document_id`, `retired_document: true`), so a stored `kv_document_id` survives a merge.

It reads the catalog and calls `stat`; it opens no file and writes nothing.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from knowledgevista.domain import reference as refmod
from knowledgevista.domain.pathkeys import fs_path, join_relative
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import resolve as resolve_service


def _document_of(conn: sqlite3.Connection, artifact_id: str) -> str | None:
    row = conn.execute("SELECT document_id FROM document_artifact WHERE artifact_id = ?", (artifact_id,)).fetchone()
    return row[0] if row else None


def _locations(conn: sqlite3.Connection, artifact_id: str, check_disk: bool) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute(
        "SELECT l.relative_path, l.state, r.root_id, r.label, r.status, r.configured_path FROM location l JOIN root r ON r.root_id = l.root_id "
        "WHERE l.artifact_id = ? AND l.ended_at IS NULL ORDER BY l.path_key", (artifact_id,)):
        absolute = join_relative(r["configured_path"], r["relative_path"])
        on_disk: bool | None = None
        if check_disk and r["status"] == "online":
            try:
                on_disk = os.path.isfile(fs_path(absolute))
            except OSError:
                on_disk = False
        out.append({"root": r["label"], "root_id": r["root_id"], "root_status": r["status"], "root_path": r["configured_path"],
                    "relative_path": r["relative_path"], "absolute_path": absolute, "state": r["state"], "on_disk": on_disk})
    return out


def _status(locations: list[dict[str, Any]]) -> str:
    if not locations:
        return "unlocated"
    if any(loc["state"] == "active" and loc["root_status"] == "online" and loc["on_disk"] is not False for loc in locations):
        return "available"
    if any(loc["state"] == "active" and loc["root_status"] != "online" for loc in locations):
        return "root_offline"
    return "missing"


def _accepted(conn: sqlite3.Connection, document_id: str) -> dict[str, str]:
    return {r["field"]: r["value"] for r in conn.execute(
        "SELECT field, value FROM metadata_value WHERE document_id = ? AND field IN ('title', 'doi', 'year')", (document_id,))}


def _resolve(conn: sqlite3.Connection, text: str) -> tuple[resolve_service.Match, refmod.Reference | None]:
    if refmod.is_reference(text):
        try:
            ref = refmod.parse(text)
        except refmod.ReferenceError as exc:
            raise KvError(ErrorCode.INVALID_ARGUMENTS, f"Not a knowledgevista:// reference: {exc}", {"reference": text}) from exc
        if ref.kind == "artifact":
            found = resolve_service._by_artifact(conn, ref.id, "artifact")
            if not found:
                raise KvError(ErrorCode.NOT_FOUND, f"No artifact {ref.id[:16]} in this catalog.", {"reference": text})
            return found[0], ref
        return resolve_service.resolve_one(conn, ref.id), ref
    return resolve_service.resolve_one(conn, text), None


def locate(conn: sqlite3.Connection, text: str, *, check_disk: bool = True) -> dict[str, Any]:
    match, ref = _resolve(conn, (text or "").strip())
    artifact_id = match.artifact_id
    document_id = _document_of(conn, artifact_id) or match.document_id
    retired = document_id != match.document_id
    artifacts = []
    for a in conn.execute(
        "SELECT da.artifact_id, da.canonical, da.role, a.size, a.content_kind FROM document_artifact da JOIN artifact a ON a.artifact_id = da.artifact_id "
        "WHERE da.document_id = ? ORDER BY da.canonical DESC, da.artifact_id", (document_id,)):
        locations = _locations(conn, a["artifact_id"], check_disk)
        artifacts.append({"artifact_id": a["artifact_id"], "artifact_uri": refmod.format_reference("artifact", a["artifact_id"]),
                          "canonical": bool(a["canonical"]), "role": a["role"], "size": a["size"], "content_kind": a["content_kind"],
                          "status": _status(locations), "locations": locations})
    chosen = next((a for a in artifacts if a["artifact_id"] == artifact_id), None)
    if chosen is None:  # a retired id whose artifact was split away again, or an inconsistent catalog: say what is known
        chosen = {"artifact_id": artifact_id, "artifact_uri": refmod.format_reference("artifact", artifact_id), "status": "unlocated", "locations": []}
    page: dict[str, Any] | None = None
    uri = refmod.format_reference("document", document_id)
    if ref is not None and (ref.pdf_page is not None or ref.label is not None):
        page = {"pdf_page": ref.pdf_page, "printed_label": ref.label}
        uri = refmod.format_reference("document", document_id, pdf_page=ref.pdf_page, label=ref.label)
    accepted = _accepted(conn, document_id)
    record: dict[str, Any] = {
        "type": "location", "reference": text, "matched_by": "uri" if ref is not None else match.how, "historical": match.historical,
        "document_id": document_id, "uri": uri, "artifact_id": artifact_id, "artifact_uri": chosen["artifact_uri"],
        "status": chosen["status"], "available": chosen["status"] == "available",
        "document_available": any(a["status"] == "available" for a in artifacts),
        "locations": chosen["locations"], "artifacts": artifacts,
        "title": accepted.get("title"), "doi": accepted.get("doi"), "year": accepted.get("year"),
    }
    if retired:
        record["requested_document_id"] = match.document_id
        record["retired_document"] = True
    if page is not None:
        record["page"] = page
    return record
