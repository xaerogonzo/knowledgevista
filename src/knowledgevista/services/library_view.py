"""What the library window shows, as plain data: the document list, one document's detail, "why this value?", the review queue
with titles, the sidebar counts, and a page search that is ready to render.

The window is an adapter. It carries no SQL and no rules of its own (docs/ARCHITECTURE.md), so everything it would otherwise have
to decide is decided here, where it is tested without a display. Read-only: every function takes connections it may only read
with, and none of them writes. The mutations the window offers (accept, reject, collections, tags, roots) call the same services
the command line does.

Every string in these structures that came from outside Knowledge Vista (a title read from a PDF, a provider's record, a file
name) is UNTRUSTED TEXT. This module hands it over unchanged; the window must show it as text, never as markup.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

from knowledgevista.db.catalog import revision
from knowledgevista.domain import evidence_view as ev
from knowledgevista.domain import fields as fieldmod
from knowledgevista.domain.kinds import extension_kind
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.index.search import parse_query
from knowledgevista.services import explain as explain_service
from knowledgevista.services import organize, review
from knowledgevista.services import search_page as search_page_service

#: The order the inbox puts its reasons in: what needs a person first. A document with none of them sorts after, alphabetically.
INBOX_ORDER = ("unresolved", "ambiguous", "missing", "new")
_AFTER_INBOX = len(INBOX_ORDER)

#: What the sidebar lists, in this order, with the label a person reads. The names are `organize.VIEWS` keys.
SIDEBAR_VIEWS = (
    ("inbox", "Inbox"), ("unresolved", "Unresolved"), ("ambiguous", "Ambiguous"), ("missing", "Missing"),
    ("new", "New"), ("duplicates", "Duplicates"), ("untagged", "Untagged"), ("uncollected", "Uncollected"),
)

#: A search shows at most this many pages in the window; the rest are one "more" away, never silently dropped.
SEARCH_PAGE = 100

_LISTED_FIELDS = ("title", "authors", "year", "doi")


@dataclass(frozen=True)
class Row:
    """One line of the document table."""

    document_id: str
    title: str | None
    title_origin: str | None
    title_locked: bool
    authors: str | None
    year: str | None
    doi: str | None
    kind: str
    name: str  # the current path, else the start of the id: what to call it when nobody has titled it
    root: str | None
    size: int | None
    available: bool
    reason: str | None  # why it is in the inbox, or None
    reason_detail: str | None
    waiting: int  # distinct proposals waiting for a person
    copies: int  # current files carrying this document's bytes
    rank: int  # its place in the default order (inbox reasons first)

    @property
    def display_title(self) -> str:
        return self.title or self.name

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Listing:
    rows: list[Row]
    total_documents: int  # live documents in the library, whatever the scope
    scope: str
    revision: int  # the catalog revision this listing was read at

    def ids(self) -> list[str]:
        return [row.document_id for row in self.rows]


def parse_scope(scope: str) -> tuple[str, str | None]:
    """`all`, `view:<name>`, `collection:<id or name>` or `tag:<name>` -> (kind, name)."""
    kind, _, name = (scope or "all").partition(":")
    if kind == "all" and not name:
        return "all", None
    if kind in ("view", "collection", "tag") and name:
        return kind, name
    raise KvError(ErrorCode.INVALID_ARGUMENTS, f"{scope!r} is not a scope: use all, view:<name>, collection:<name> or tag:<name>.", {"scope": scope})


def _sort_key(row: Row) -> tuple[str, str]:
    return (row.display_title.casefold(), row.document_id)


def _accepted_values(conn: sqlite3.Connection) -> dict[str, dict[str, sqlite3.Row]]:
    values: dict[str, dict[str, sqlite3.Row]] = {}
    marks = ",".join("?" * len(_LISTED_FIELDS))
    for r in conn.execute(f"SELECT document_id, field, value, origin, locked FROM metadata_value WHERE field IN ({marks})", _LISTED_FIELDS):
        values.setdefault(r["document_id"], {})[r["field"]] = r
    return values


def _base_rows(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Every live document with the artifact the list describes (the canonical one) and where it is now."""
    base: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT d.document_id, a.artifact_id, a.size, a.content_kind FROM document d "
        "LEFT JOIN document_artifact da ON da.document_id = d.document_id AND da.canonical = 1 "
        "LEFT JOIN artifact a ON a.artifact_id = da.artifact_id WHERE d.retired_at IS NULL"
    ):
        base[r["document_id"]] = {"artifact_id": r["artifact_id"], "size": r["size"], "kind": r["content_kind"], "locations": []}
    # A live document with no canonical artifact is unusual but legal (a split before a pick); describe it by any one of its artifacts.
    for document_id, entry in base.items():
        if entry["artifact_id"] is None:
            other = conn.execute(
                "SELECT a.artifact_id, a.size, a.content_kind FROM document_artifact da JOIN artifact a ON a.artifact_id = da.artifact_id "
                "WHERE da.document_id = ? ORDER BY a.artifact_id LIMIT 1", (document_id,)).fetchone()
            if other is not None:
                entry.update(artifact_id=other["artifact_id"], size=other["size"], kind=other["content_kind"])
    for r in conn.execute(
        "SELECT da.document_id, l.state, l.relative_path, r.label, r.status FROM location l "
        "JOIN document_artifact da ON da.artifact_id = l.artifact_id JOIN root r ON r.root_id = l.root_id "
        "JOIN document d ON d.document_id = da.document_id WHERE l.ended_at IS NULL AND d.retired_at IS NULL "
        "ORDER BY da.document_id, l.state, l.path_key"
    ):
        base[r["document_id"]]["locations"].append((r["state"], r["relative_path"], r["label"], r["status"]))
    return base


def _waiting(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["document_id"]: r["n"] for r in conn.execute(
        "SELECT document_id, COUNT(*) AS n FROM (SELECT DISTINCT document_id, field, value FROM metadata_candidate WHERE status = 'proposed') GROUP BY document_id")}


def _inbox_groups(conn: sqlite3.Connection, index: sqlite3.Connection | None) -> dict[str, dict[str, str]]:
    """document -> {reason, detail}: the FIRST of unresolved / ambiguous / missing / new that holds, so a document is in one place.
    `reason` is the group (what sorts it), `detail` the specific cause that view reports ("a title was proposed and is waiting")."""
    groups: dict[str, dict[str, str]] = {}
    for name in INBOX_ORDER:
        for item in organize.run_view(conn, index, name):
            groups.setdefault(item["document_id"], {"reason": name, "detail": item["detail"]})
    return groups


def _in_scope(conn: sqlite3.Connection, index: sqlite3.Connection | None, scope: str) -> tuple[set[str] | None, dict[str, dict[str, str]] | None]:
    """(the documents a scope selects, or None for all; the reason each is shown with, or None to use the inbox's)."""
    kind, name = parse_scope(scope)
    if kind == "all":
        return None, None
    if kind == "view":
        items = organize.run_view(conn, index, name)
        selected = {i["document_id"] for i in items}
        if name == "inbox":
            return selected, None
        # Inside a view the reason is THAT view's, even for a document the inbox files under an earlier one.
        return selected, {i["document_id"]: {"reason": name, "detail": i["detail"]} for i in items}
    if kind == "collection":
        return set(organize.collection_members(conn, name)[1]), None
    key = organize.key_of(name)
    return {r[0] for r in conn.execute(
        "SELECT t.document_id FROM document_tag t JOIN document d ON d.document_id = t.document_id WHERE t.tag_key = ? AND d.retired_at IS NULL", (key,))}, None


def list_documents(conn: sqlite3.Connection, index: sqlite3.Connection | None, scope: str = "all") -> Listing:
    """The document table for a scope, in the default order: what needs a person first (unresolved, ambiguous, missing, new), then
    everything else alphabetically. A person re-sorts in the window; this is the order it starts in and returns to."""
    rev = revision(conn)
    selected, scoped_reasons = _in_scope(conn, index, scope)
    reasons = scoped_reasons if scoped_reasons is not None else _inbox_groups(conn, index)
    base, values, waiting = _base_rows(conn), _accepted_values(conn), _waiting(conn)
    rows: list[Row] = []
    for document_id, entry in base.items():
        if selected is not None and document_id not in selected:
            continue
        accepted = values.get(document_id, {})
        locs = entry["locations"]
        shown = locs[0] if locs else None
        # The table says why a row floats to the top.
        why = reasons.get(document_id)
        title = accepted.get("title")
        authors = accepted.get("authors")
        rows.append(Row(
            document_id=document_id,
            title=title["value"] if title else None, title_origin=title["origin"] if title else None, title_locked=bool(title["locked"]) if title else False,
            authors=fieldmod.display("authors", authors["value"]) if authors else None,
            year=accepted["year"]["value"] if "year" in accepted else None, doi=accepted["doi"]["value"] if "doi" in accepted else None,
            kind=entry["kind"] or extension_kind(shown[1] if shown else ""),
            name=shown[1] if shown else document_id[:12], root=shown[2] if shown else None, size=entry["size"],
            available=any(s == "active" and rs == "online" for s, _p, _r, rs in locs),
            reason=why["reason"] if why else None, reason_detail=why["detail"] if why else None,
            waiting=waiting.get(document_id, 0), copies=len(locs), rank=0,
        ))
    rows.sort(key=lambda r: (INBOX_ORDER.index(r.reason) if r.reason in INBOX_ORDER else _AFTER_INBOX, *_sort_key(r)))
    ranked = [Row(**{**r.as_dict(), "rank": number}) for number, r in enumerate(rows)]
    return Listing(ranked, len(base), scope, rev)


def sidebar(conn: sqlite3.Connection, index: sqlite3.Connection | None) -> dict[str, Any]:
    """The sidebar's counts: each view, each collection, each tag, and the roots. One read, so the counts agree with each other."""
    views = [{"scope": f"view:{name}", "label": label, "count": len(organize.run_view(conn, index, name))} for name, label in SIDEBAR_VIEWS]
    return {
        "revision": revision(conn),
        "documents": conn.execute("SELECT COUNT(*) FROM document WHERE retired_at IS NULL").fetchone()[0],
        "views": views,
        "collections": [{"scope": f"collection:{c['collection_id']}", "label": c["name"], "count": c["members"]} for c in organize.list_collections(conn)],
        "tags": [{"scope": f"tag:{t['tag']}", "label": t["tag"], "count": t["documents"]} for t in organize.list_tags(conn)],
        "roots": [dict(r) for r in conn.execute(
            "SELECT root_id, label, configured_path, status, enabled, allow_organize, last_scan_at FROM root ORDER BY created_at, root_id")],
    }


# ---------------------------------------------------------------------------------------------------------------- detail


def _field_state(conn: sqlite3.Connection, document_id: str) -> tuple[dict[str, sqlite3.Row], dict[str, list[dict[str, Any]]]]:
    values = {r["field"]: r for r in conn.execute("SELECT * FROM metadata_value WHERE document_id = ?", (document_id,))}
    candidates: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute("SELECT * FROM metadata_candidate WHERE document_id = ? ORDER BY field, priority, created_at", (document_id,)):
        candidates.setdefault(r["field"], []).append(dict(r))
    return values, candidates


def document_detail(conn: sqlite3.Connection, index: sqlite3.Connection | None, document_id: str) -> dict[str, Any]:
    """Everything the detail pane shows for one document. Raises KV_NOT_FOUND for an id the catalog does not have."""
    explained = explain_service.explain_document(conn, document_id)
    values, candidates = _field_state(conn, document_id)
    shown_fields = []
    for name in fieldmod.FIELDS:
        live = [c for c in candidates.get(name, []) if c["status"] == "proposed"]
        if name not in values and not live and name not in ev.CORE_FIELDS:
            continue
        accepted = values.get(name)
        entry: dict[str, Any] = {
            "field": name, "label": ev.field_label(name), "set": accepted is not None, "value": accepted["value"] if accepted else None,
            "display": fieldmod.display(name, accepted["value"]) if accepted else None,
            "origin": accepted["origin"] if accepted else None, "origin_label": ev.origin_label(accepted["origin"]) if accepted else None,
            "badge": ev.ORIGIN_BADGES.get(accepted["origin"], "?") if accepted else None,
            "source": accepted["source"] if accepted else None, "source_label": ev.source_label(accepted["source"]) if accepted else None,
            "locked": bool(accepted["locked"]) if accepted else False, "accepted_by": accepted["accepted_by"] if accepted else None,
            "waiting": len({c["value"] for c in live}),
            "agreement": ev.agreement(name, dict(accepted) if accepted else None, candidates.get(name, [])),
        }
        shown_fields.append(entry)
    document = conn.execute("SELECT retired_at, merged_into FROM document WHERE document_id = ?", (document_id,)).fetchone()
    primary = next((a for a in explained["artifacts"] if a["canonical"]), explained["artifacts"][0] if explained["artifacts"] else None)
    current = [loc for a in explained["artifacts"] for loc in a["locations"] if loc["ended_at"] is None]
    current.sort(key=lambda loc: (loc["state"], loc["path"]))
    title = values.get("title")
    return {
        "document_id": document_id, "revision": revision(conn),
        "title": title["value"] if title else None,
        "name": current[0]["path"] if current else document_id[:12],
        "retired": document["retired_at"] is not None, "merged_into": document["merged_into"],
        "kind": primary["content_kind"] if primary else None, "size": primary["size"] if primary else None,
        "available": any(a["available"] for a in explained["artifacts"]),
        "fields": shown_fields,
        "files": [{"root": loc["root"], "root_status": loc["root_status"], "path": loc["path"], "state": loc["state"]} for loc in current],
        "artifacts": [{"artifact_id": a["artifact_id"], "size": a["size"], "content_kind": a["content_kind"], "role": a["role"],
                       "canonical": a["canonical"], "available": a["available"]} for a in explained["artifacts"]],
        "collections": explained["organization"]["collections"], "tags": explained["organization"]["tags"],
        "relations": explained["relations"]["document"], "artifact_relations": explained["relations"]["artifact"],
        "proposals": explained["relations"]["proposals"], "waiting": sum(f["waiting"] for f in shown_fields),
        "warnings": explained["warnings"],
    }


def field_evidence(conn: sqlite3.Connection, document_id: str, field: str) -> dict[str, Any]:
    """"Why this value?" for one field: how the accepted value came to be, every other proposal with its evidence, and whether the
    sources agree. Proposals are shown even when rejected or set aside, so the answer to "why not that one?" is on the page."""
    if field not in fieldmod.FIELDS:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"{field!r} is not a metadata field.", {"field": field})
    if conn.execute("SELECT 1 FROM document WHERE document_id = ?", (document_id,)).fetchone() is None:
        raise KvError(ErrorCode.NOT_FOUND, f"No document {document_id}.", {"document_id": document_id})
    values, candidates = _field_state(conn, document_id)
    accepted_row = values.get(field)
    accepted = None
    if accepted_row is not None:
        accepted = {
            "value": accepted_row["value"], "display": fieldmod.display(field, accepted_row["value"]), "origin": accepted_row["origin"],
            "origin_label": ev.origin_label(accepted_row["origin"]), "source": accepted_row["source"],
            "source_label": ev.source_label(accepted_row["source"]), "locked": bool(accepted_row["locked"]),
            "accepted_by": accepted_row["accepted_by"], "accepted_at": accepted_row["accepted_at"],
            "evidence": ev.evidence_lines(json.loads(accepted_row["evidence_json"]) if accepted_row["evidence_json"] else None),
        }
        accepted["sentence"] = ev.provenance_sentence(accepted)
    key = fieldmod.agreement_key(field, accepted_row["value"]) if accepted_row else None
    proposals = []
    for c in candidates.get(field, []):
        relation = None if key is None else ("same" if fieldmod.agreement_key(field, c["value"]) == key else "differs")
        proposals.append({
            "candidate_id": c["candidate_id"], "value": c["value"], "display": fieldmod.display(field, c["value"]), "source": c["source"],
            "source_label": ev.source_label(c["source"]), "origin": c["origin"], "status": c["status"], "classification": c["classification"],
            "confidence": c["confidence"], "review": c["review"], "decided_by": c["decided_by"], "relation_to_accepted": relation,
            "evidence": ev.evidence_lines(json.loads(c["evidence_json"])),
        })
    state = ev.agreement(field, accepted_row and dict(accepted_row), candidates.get(field, []))
    history = [{"at": h["at"], "old": h["old_value"], "old_source": h["old_source"], "new": h["new_value"], "new_source": h["new_source"],
                "actor": h["actor"], "reason": h["reason"]}
               for h in conn.execute("SELECT * FROM metadata_history WHERE document_id = ? AND field = ? ORDER BY history_id DESC LIMIT 25", (document_id, field))]
    return {"document_id": document_id, "field": field, "label": ev.field_label(field), "accepted": accepted, "proposals": proposals,
            "agreement": state, "agreement_text": ev.AGREEMENT_TEXT[state], "history": history}


# ---------------------------------------------------------------------------------------------------------- review queue


def review_items(conn: sqlite3.Connection, *, only: str | None = None) -> list[dict[str, Any]]:
    """The review queue with each document's name, so a row says WHAT is proposed for WHICH paper. Most urgent first
    (`review.queue`'s order). `only` is `safe` or `required`."""
    items, _total = review.queue(conn, review=only, limit=1_000_000)
    names = {}
    for item in items:
        document_id = item["document_id"]
        if document_id not in names:
            title = conn.execute("SELECT value FROM metadata_value WHERE document_id = ? AND field = 'title'", (document_id,)).fetchone()
            path = conn.execute(
                "SELECT l.relative_path FROM location l JOIN document_artifact da ON da.artifact_id = l.artifact_id "
                "WHERE da.document_id = ? AND l.ended_at IS NULL ORDER BY l.state, l.path_key LIMIT 1", (document_id,)).fetchone()
            names[document_id] = (title[0] if title else None, path[0] if path else document_id[:12])
    return [{**item, "title": names[item["document_id"]][0], "name": names[item["document_id"]][1], "label": ev.field_label(item["field"]),
             "evidence_lines": ev.evidence_lines(item["evidence"]), "sources_text": ", ".join(ev.source_label(s) for s in item["sources"])}
            for item in items]


def decide_item(conn: sqlite3.Connection, item: dict[str, Any], accept: bool) -> dict[str, Any]:
    """Accept or reject one queue item (several candidates that say the same thing are one item and are decided together)."""
    from knowledgevista.services import metadata

    if accept:
        done = metadata.accept_candidate(conn, item["candidate_ids"][0], actor="user")
        return {"decision": "accepted", "field": done.field, "changed": done.changed}
    changed = sum(1 for candidate_id in item["candidate_ids"] if metadata.reject_candidate(conn, candidate_id))
    return {"decision": "rejected", "field": item["field"], "changed": bool(changed)}


# --------------------------------------------------------------------------------------------------------------- search


def search_pages(conn: sqlite3.Connection, index: sqlite3.Connection | None, text: str, *, limit: int | None = None, token: str | None = None) -> dict[str, Any]:
    """A page search ready to render: hits with the document's name, what the search could not see, and a cursor for more.
    A malformed query raises KV_QUERY_INVALID (the window shows the message); it never returns an empty list."""
    query = parse_query(text)
    result, next_token = search_page_service.search_page(conn, index, query, limit=SEARCH_PAGE if limit is None else limit, token=token, command="gui.search")
    hits = []
    for hit in result.hits:
        title = conn.execute("SELECT value FROM metadata_value WHERE document_id = ? AND field = 'title'", (hit["document_id"],)).fetchone() if hit["document_id"] else None
        where = hit["paths"][0]["path"] if hit["paths"] else hit["artifact_id"][:12]
        hits.append({
            "document_id": hit["document_id"], "artifact_id": hit["artifact_id"], "title": title[0] if title else None, "name": where,
            "pdf_page": hit["pdf_page"], "printed_label": hit["printed_label"], "snippet": " ".join(hit["snippet"].split()),
            "provisional": hit["extraction"]["provisional"], "available": hit["available"],
        })
    cov = result.coverage
    caveats = [f"{cov[key]} {label}" for key, label in (
        ("not_yet_extracted", "PDF(s) not extracted yet"), ("no_text_layer", "scan(s) with no text layer"),
        ("partially_indexed", "partially indexed"), ("extraction_failed", "failed extraction(s)")) if cov[key]]
    return {
        "query": text, "hits": hits, "truncated": result.truncated, "next_cursor": next_token, "coverage": cov, "scope": result.scope,
        "caveats": caveats, "searchable": cov["searchable"],
        "note": ("Not everything was searchable: " + ", ".join(caveats) + ". A missing hit is not proof of absence.") if caveats else "",
    }


# ---------------------------------------------------------------------------------------------------------------- health


def health_report(conn: sqlite3.Connection, index: sqlite3.Connection | None) -> dict[str, Any]:
    """Inventory (what the library holds) and health (what needs attention) kept as separate sections: "1,862 files" is not "7
    need attention". The window shows them side by side and never sums them."""
    from knowledgevista.services.stats import library_stats

    return library_stats(conn, index)
