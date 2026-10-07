"""The commands for relations, duplicates and virtual organisation: `relate`, `dupes`, `related`, `relations`, `document`,
`collection`, `tag`, `saved` and `view`. Thin adapters over `services/relate.py`, `relations.py` and `organize.py`; no SQL and
no decisions live here (docs/RELATIONS.md has the policy, docs/CLI_CONTRACT.md the contract).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from knowledgevista import paths
from knowledgevista.cli_support import Outcome, catalog_path, index_for, label_of, search_outcome
from knowledgevista.db.catalog import open_catalog
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.index.metacache import open_metacache
from knowledgevista.services import explain as explain_service
from knowledgevista.services import organize, relate, relations
from knowledgevista.services import resolve as resolve_service


def _document(conn, reference: str) -> str:
    return resolve_service.resolve_one(conn, reference).document_id


def _documents(conn, references: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for reference in references:
        seen.setdefault(_document(conn, reference))
    return list(seen)


def _artifact_of(conn, document_id: str, reference: str) -> str:
    """An artifact of THIS document by sha256 (or an 8+ character prefix) or by one of its paths."""
    ref = (reference or "").strip().lower()
    rows = conn.execute("SELECT artifact_id FROM document_artifact WHERE document_id = ?", (document_id,)).fetchall()
    matches = [r[0] for r in rows if len(ref) >= 8 and r[0].startswith(ref)]
    if not matches:
        matches = [r[0] for r in rows if conn.execute(
            "SELECT 1 FROM location WHERE artifact_id = ? AND lower(relative_path) LIKE ?", (r[0], "%" + ref.replace("\\", "/"))).fetchone()]
    if not matches:
        raise KvError(ErrorCode.NOT_FOUND, f"{reference!r} is not an artifact of that document (give its sha256, 8+ characters, or one of its paths).",
                      {"artifacts": [r[0] for r in rows]})
    if len(matches) > 1:
        raise KvError(ErrorCode.AMBIGUOUS, f"{reference!r} matches more than one artifact of that document.", {"candidates": matches})
    return matches[0]


# --- relate / dupes / related -----------------------------------------------------------------------------------


def cmd_relate(args: argparse.Namespace) -> Outcome:
    from knowledgevista.cli_support import say

    conn = open_catalog(catalog_path(args), create=False)
    index = index_for(args, create=False, read_only=True)
    cache = open_metacache(paths.metacache_path(catalog_path(args)), create=False, read_only=True)
    try:
        report = relate.detect(conn, index, cache, progress=say)
        waiting = relate.list_candidates(conn, limit=0)[1]
    finally:
        conn.close()
        if index is not None:
            index.close()
        if cache is not None:
            cache.close()
    out = Outcome([{"type": "summary", "proposals_waiting": waiting, **report}])
    found = ", ".join(f"{n} {kind}" for kind, n in sorted(report["proposals"].items())) or "nothing"
    out.lines = [f"Looked across {report['documents']} document(s) ({report['fingerprinted']} read for identical text): {found}.",
                 "Changes since last time: " + (", ".join(f"{n} {k}" for k, n in sorted(report["outcomes"].items())) or "none") + ".",
                 f"{report['exact_copy_groups']} file(s) exist at more than one path (not relations: kv dupes). {waiting} proposal(s) wait: kv relations list"]
    if index is None:
        out.warnings.append({"code": "KV_NO_TEXT_INDEX", "message": "Nothing has been extracted, so identical-text and supplement evidence could not be looked for. Run: kv extract"})
        out.lines.append("note: " + out.warnings[0]["message"])
    return out


def cmd_dupes(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(catalog_path(args), create=False, read_only=True)
    try:
        report = relate.duplicates_report(conn)
        labels = {}
        for group in report["exact_bytes"]:
            labels[group["artifact_id"]] = group["paths"]
    finally:
        conn.close()
    out = Outcome([{"type": "summary", **{level: len(items) for level, items in report.items()}}])
    out.records += [{"type": level, **item} for level, items in report.items() for item in items]
    out.lines.append(f"1. same bytes at several paths (nothing to decide; one item): {len(report['exact_bytes'])} group(s)")
    for group in report["exact_bytes"]:
        out.lines.append(f"     {group['copies']}x  " + "  |  ".join(p["path"] for p in group["paths"][:4]))
    out.lines.append(f"2. byte-different files with identical text (almost always one publication): {len(report['identical_text'])} proposal(s)")
    out.lines.append(f"3. one DOI, or title and first author, under several documents: {len(report['same_publication'])} proposal(s)")
    out.lines.append(f"4. a preprint and its published version, or one title under two DOIs: {len(report['related_work'])} proposal(s)")
    out.lines.append(f"   already merged (several artifacts under one document): {len(report['documents_with_several_artifacts'])}")
    return out


def cmd_related(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(catalog_path(args), create=False, read_only=True)
    try:
        document_id = _document(conn, args.reference)
        data = explain_service.explain_document(conn, document_id)["relations"]
        names = {}
        for item in data["document"]:
            names[item["other_document"]] = label_of(conn, item["other_document"])
        for item in data["proposals"]:
            for end in (item["source"], item["target"]):
                if len(end) == 32:
                    names.setdefault(end, label_of(conn, end))
        this = label_of(conn, document_id)
    finally:
        conn.close()
    out = Outcome([{"type": "relations", "document_id": document_id, **data}])
    out.lines.append(f"{this}  ({document_id})")
    if data["retired_at"]:
        out.lines.append(f"  merged into {data['merged_into']} on {data['retired_at']}")
    for item in data["document"]:
        out.lines.append(f"  {item['kind']:14} {item['direction']:9} {names[item['other_document']]}   [{item['relation_id'][:10]}]")
    for item in data["artifact"]:
        out.lines.append(f"  artifact {item['kind']:14} {item['direction']:9} {item['other_artifact'][:12]}   [{item['relation_id'][:10]}]")
    for item in data["proposals"]:
        out.lines.append(f"  proposed {item['kind']:14} {item['confidence']:9} {item['candidate_id'][:10]}  ({', '.join(item['evidence'].get('signals', [])) or item['evidence'].get('doi') or '...'})")
    for item in data["group_proposals"]:
        out.lines.append(f"  proposed {item['kind']} (a group)  {item['confidence']}  {item['candidate_id'][:10]}")
    if len(out.lines) == 1:
        out.lines.append("  no relations and no proposals")
    return out


# --- relations list / accept / reject / add / remove -------------------------------------------------------------


def cmd_relations(args: argparse.Namespace) -> Outcome:
    action = args.relations_command
    conn = open_catalog(catalog_path(args), create=False, read_only=(action == "list"))
    out = Outcome()
    try:
        if action == "list":
            statuses = ("proposed", "stale", "rejected", "accepted") if args.status == "all" else (args.status,)
            items, total = relate.list_candidates(conn, kind=args.kind, statuses=statuses, limit=args.limit, offset=args.offset)
            out.complete = args.offset + len(items) >= total
            out.records.append({"type": "summary", "total": total, "shown": len(items), "offset": args.offset})
            for item in items:
                ends = item["members"] if item["members"] is not None else [item["source"], item["target"]]
                item["labels"] = [label_of(conn, e) if len(e) == 32 else e[:12] for e in ends[:3]]
                out.records.append({"type": "proposal", **item})
                what = (f"{len(item['members'])} documents: " if item["members"] is not None else "") + " -> ".join(item["labels"]) + (" ..." if item["members"] and len(item["members"]) > 3 else "")
                why = item["evidence"].get("reason") or ", ".join(item["evidence"].get("signals", [])) or item["evidence"].get("doi") or item["evidence"].get("text_fingerprint", "")
                out.lines.append(f"{item['candidate_id'][:10]}  {item['kind']:14} {item['confidence']:9} {item['level']:8} {what}\n             {why}")
            out.lines.append(f"{len(items)} of {total} proposal(s)" + ("" if out.complete else f" (use --offset {args.offset + len(items)} for more)"))
        elif action == "accept":
            for reference in args.candidates:
                candidate_id = relations.resolve_candidate_id(conn, reference)
                try:
                    result = relations.accept_candidate(conn, candidate_id, actor="user", keep=_document(conn, args.keep) if args.keep else None)
                except KvError as exc:
                    if exc.code != ErrorCode.INVALID_ARGUMENTS or "stale" not in exc.message or len(args.candidates) == 1:
                        raise
                    # In a batch, an earlier merge can make a later proposal redundant (two copies already became one). Say so, go on.
                    out.warnings.append({"code": "KV_PROPOSAL_STALE", "message": f"skipped {candidate_id[:10]}: {exc.message}", "details": {"candidate_id": candidate_id}})
                    continue
                out.records.append({"type": "accepted", **result})
                out.lines.append(f"{result['kind']}: {result['effect']}" + (f" (kept {label_of(conn, result['kept'])}, absorbed {result['absorbed'][:12]})" if result["effect"] == "merged" else ""))
        elif action == "reject":
            for reference in args.candidates:
                candidate_id = relations.resolve_candidate_id(conn, reference)
                changed = relations.reject_candidate(conn, candidate_id)
                out.records.append({"type": "rejected", "candidate_id": candidate_id, "changed": changed})
                out.lines.append(("rejected " if changed else "already rejected ") + candidate_id[:10])
        elif action == "add":
            if args.artifacts:
                source, target = resolve_service.resolve_one(conn, args.source).artifact_id, resolve_service.resolve_one(conn, args.target).artifact_id
                level = "artifact"
            else:
                source, target, level = _document(conn, args.source), _document(conn, args.target), "document"
            relation_id, created = relations.add_relation_by_user(conn, level, args.kind, source, target, note=args.note, position=args.position)
            out.records.append({"type": "relation", "relation_id": relation_id, "created": created, "level": level, "kind": args.kind})
            out.lines.append(f"{args.kind} recorded [{relation_id[:10]}]" if created else f"{args.kind} was already recorded [{relation_id[:10]}]")
        else:
            changed = relations.retract_relation(conn, args.relation)
            out.records.append({"type": "retracted", "relation_id": args.relation, "changed": changed})
            out.lines.append("retracted (kept in the record)" if changed else "was already retracted")
    finally:
        conn.close()
    return out


# --- document merge / split / canonical --------------------------------------------------------------------------


def cmd_document(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(catalog_path(args), create=False)
    try:
        action = args.document_command
        if action == "merge":
            result = relations.merge(conn, _document(conn, args.keep), _document(conn, args.absorb), reason=args.reason)
            line = (f"merged: {len(result['artifacts_moved'])} artifact(s) moved into {label_of(conn, result['kept'])}; "
                    f"the other document is retired, not deleted (undo: kv document split)")
            record = {"type": "merged", **result}
        elif action == "split":
            document_id = _document(conn, args.document)
            result = relations.split_document(conn, document_id, _artifact_of(conn, document_id, args.artifact))
            line = f"split: {label_of(conn, result['document_id'])} is its own document again" + (" (the original document, revived)" if result["revived"] else "")
            record = {"type": "split", **result}
        else:
            document_id = _document(conn, args.document)
            artifact_id = _artifact_of(conn, document_id, args.artifact)
            changed = relations.set_canonical(conn, document_id, artifact_id, reason=args.reason)
            line = "canonical artifact changed" if changed else "that artifact was already the canonical one"
            record = {"type": "canonical", "document_id": document_id, "artifact_id": artifact_id, "changed": changed}
    finally:
        conn.close()
    return Outcome([record], lines=[line])


# --- collections, tags, saved searches, views --------------------------------------------------------------------


def cmd_collection(args: argparse.Namespace) -> Outcome:
    action = args.collection_command
    conn = open_catalog(catalog_path(args), create=False, read_only=(action in ("list", "show")))
    try:
        if action == "create":
            result = organize.new_collection(conn, args.name, _documents(conn, args.documents), description=args.description)
            return Outcome([{"type": "collection", **result}], lines=[f"collection {result['name']!r} created with {result['added']} document(s)"])
        if action == "add":
            result = organize.add_to_collection(conn, args.name, _documents(conn, args.documents))
            return Outcome([{"type": "collection", **result}], lines=[f"{result['added']} added to {result['name']!r} ({result['already_members']} already there)"])
        if action == "remove":
            result = organize.remove_from_collection(conn, args.name, _documents(conn, args.documents))
            return Outcome([{"type": "collection", **result}], lines=[f"{result['removed']} removed from {result['name']!r} (the documents themselves are untouched)"])
        if action == "delete":
            result = organize.retire_collection(conn, args.name)
            return Outcome([{"type": "collection", **result}], lines=[f"collection {result['name']!r} deleted from view (its record is kept)"])
        if action == "list":
            listed = organize.list_collections(conn)
            out = Outcome([{"type": "collection", **c} for c in listed])
            out.lines = [f"{c['members']:5}  {c['name']}" + (f"   (parent DOI {c['source_doi']})" if c["source_doi"] else "") for c in listed] or ["No collections yet. Create one: kv collection create <name> [documents]"]
            return out
        collection, ids = organize.collection_members(conn, args.name)
        out = Outcome([{"type": "collection", "collection_id": collection["collection_id"], "name": collection["name"], "members": len(ids)}])
        for document_id in ids[: args.limit]:
            out.records.append({"type": "member", "document_id": document_id, "label": label_of(conn, document_id)})
            out.lines.append(label_of(conn, document_id))
        out.complete = len(ids) <= args.limit
        out.lines.append(f"{min(len(ids), args.limit)} of {len(ids)} member(s) of {collection['name']!r}")
        return out
    finally:
        conn.close()


def cmd_tag(args: argparse.Namespace) -> Outcome:
    action = args.tag_command
    conn = open_catalog(catalog_path(args), create=False, read_only=(action == "list"))
    try:
        if action == "list":
            tags = organize.list_tags(conn)
            return Outcome([{"type": "tag", **t} for t in tags], lines=[f"{t['documents']:5}  {t['tag']}" for t in tags] or ["No tags yet. Add one: kv tag add <tag> <document>..."])
        documents = _documents(conn, args.documents)
        if action == "add":
            result = organize.add_tags(conn, documents, [args.tag])
            return Outcome([{"type": "tags", **result}], lines=[f"tagged {len(documents)} document(s) {args.tag!r} ({result['added']} new)"])
        result = organize.remove_tags(conn, documents, [args.tag])
        return Outcome([{"type": "tags", **result}], lines=[f"{result['removed']} tag(s) removed"])
    finally:
        conn.close()


def cmd_saved(args: argparse.Namespace) -> Outcome:
    action = args.saved_command
    conn = open_catalog(catalog_path(args), create=False, read_only=(action in ("list", "run")))
    try:
        if action == "list":
            saved = organize.list_saved_searches(conn)
            return Outcome([{"type": "saved", **s} for s in saved], lines=[f"{s['name']}   {' | '.join(s['query']['alternatives'])} {' '.join(f'{n}:{v}' for n, v in s['query'].get('filters', []))}".rstrip() for s in saved]
                           or ["No saved searches. Save one: kv search <query> --save <name>"])
        if action == "delete":
            result = organize.retire_saved_search(conn, args.name)
            return Outcome([{"type": "saved", **result}], lines=[f"saved search {result['name']!r} deleted from view (its record is kept)"])
        _, query = organize.get_saved_search(conn, args.name)
    finally:
        conn.close()
    return search_outcome(args, query)


def cmd_view(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(catalog_path(args), create=False, read_only=True)
    index = index_for(args, create=False, read_only=True)
    try:
        if not args.name:
            return Outcome([{"type": "view", "name": n, "description": d} for n, (d, _) in organize.VIEWS.items()],
                           lines=[f"{n:12} {d}" for n, (d, _) in organize.VIEWS.items()])
        items = organize.run_view(conn, index, args.name)
        shown = items[: args.limit]
        for item in shown:
            item["label"] = label_of(conn, item["document_id"])
    finally:
        conn.close()
        if index is not None:
            index.close()
    out = Outcome([{"type": "summary", "view": args.name, "total": len(items), "shown": len(shown)}], complete=len(items) <= args.limit)
    out.records += [{"type": "item", **i} for i in shown]
    out.lines = [f"{i['label']}   [{i['reason']}] {i['detail']}" for i in shown]
    out.lines.append(f"{len(shown)} of {len(items)} in {args.name}" + ("" if out.complete else f" (limit {args.limit})"))
    return out


# --- parsers ----------------------------------------------------------------------------------------------------


def add_parsers(sub: Any, shared: argparse.ArgumentParser, make_parser: Callable[..., argparse.ArgumentParser]) -> None:
    sub.add_parser("relate", parents=[shared], help="look across the library for the same document twice, supplements, chapters and versions (proposes only)").set_defaults(handler=cmd_relate)
    sub.add_parser("dupes", parents=[shared], help="the four levels of 'the same thing twice', kept apart").set_defaults(handler=cmd_dupes)
    related = sub.add_parser("related", parents=[shared], help="the relations, proposals and merge history of one document")
    related.add_argument("reference")
    related.set_defaults(handler=cmd_related)

    rel = sub.add_parser("relations", parents=[shared], help="relation proposals, and the relations you accept")
    rel_sub = rel.add_subparsers(dest="relations_command", metavar="action", required=True, parser_class=make_parser)
    lst = rel_sub.add_parser("list", parents=[shared], help="list proposals, best evidence first")
    lst.add_argument("--kind", choices=relations.ARTIFACT_KINDS + relations.DOCUMENT_KINDS + ("same_document", "collection"))
    lst.add_argument("--status", choices=("proposed", "stale", "rejected", "accepted", "all"), default="proposed")
    lst.add_argument("--limit", type=int, default=25)
    lst.add_argument("--offset", type=int, default=0)
    lst.set_defaults(handler=cmd_relations)
    acc = rel_sub.add_parser("accept", parents=[shared], help="accept proposals (a same_document proposal MERGES the two documents)")
    acc.add_argument("candidates", nargs="+")
    acc.add_argument("--keep", help="for a merge: which document survives (default: the one with more accepted metadata)")
    acc.set_defaults(handler=cmd_relations)
    rej = rel_sub.add_parser("reject", parents=[shared], help="decide a proposal is wrong (kept, and not proposed again)")
    rej.add_argument("candidates", nargs="+")
    rej.set_defaults(handler=cmd_relations)
    add = rel_sub.add_parser("add", parents=[shared], help="state a relation yourself")
    add.add_argument("kind", choices=relations.ARTIFACT_KINDS + relations.DOCUMENT_KINDS)
    add.add_argument("source", help="the document (or, with --artifacts, the file) the relation starts from")
    add.add_argument("target")
    add.add_argument("--artifacts", action="store_true", help="relate two ARTIFACTS (bytes): duplicate_of, derivative_of, replaces, equivalent_to")
    add.add_argument("--note")
    add.add_argument("--position", help="for part_of: where the part sits (a chapter number, 'iv')")
    add.set_defaults(handler=cmd_relations)
    rem = rel_sub.add_parser("remove", parents=[shared], help="take a relation back (it stays recorded as retracted)")
    rem.add_argument("relation", help="a relation id (8+ characters)")
    rem.set_defaults(handler=cmd_relations)

    doc = sub.add_parser("document", parents=[shared], help="merge two documents into one, split one back out, choose the canonical artifact")
    doc_sub = doc.add_subparsers(dest="document_command", metavar="action", required=True, parser_class=make_parser)
    merge = doc_sub.add_parser("merge", parents=[shared], help="make two documents one: artifacts move, the other is retired (nothing is deleted)")
    merge.add_argument("keep")
    merge.add_argument("absorb")
    merge.add_argument("--reason")
    merge.set_defaults(handler=cmd_document)
    split = doc_sub.add_parser("split", parents=[shared], help="take one artifact out into its own document (the original is revived if there was one)")
    split.add_argument("document")
    split.add_argument("artifact", help="its sha256 (8+ characters) or one of its paths")
    split.set_defaults(handler=cmd_document)
    canonical = doc_sub.add_parser("canonical", parents=[shared], help="choose which artifact is read for text and metadata")
    canonical.add_argument("document")
    canonical.add_argument("artifact")
    canonical.add_argument("--reason")
    canonical.set_defaults(handler=cmd_document)

    col = sub.add_parser("collection", parents=[shared], help="named groups of documents (nothing is moved)")
    col_sub = col.add_subparsers(dest="collection_command", metavar="action", required=True, parser_class=make_parser)
    create = col_sub.add_parser("create", parents=[shared], help="make a collection, optionally with documents")
    create.add_argument("name")
    create.add_argument("documents", nargs="*")
    create.add_argument("--description")
    create.set_defaults(handler=cmd_collection)
    for action, text in (("add", "add documents to a collection"), ("remove", "take documents out of a collection (they are untouched)")):
        one = col_sub.add_parser(action, parents=[shared], help=text)
        one.add_argument("name")
        one.add_argument("documents", nargs="+")
        one.set_defaults(handler=cmd_collection)
    col_sub.add_parser("list", parents=[shared], help="every collection and its size").set_defaults(handler=cmd_collection)
    show = col_sub.add_parser("show", parents=[shared], help="the members of a collection")
    show.add_argument("name")
    show.add_argument("--limit", type=int, default=50)
    show.set_defaults(handler=cmd_collection)
    delete = col_sub.add_parser("delete", parents=[shared], help="delete a collection from view (the record is kept)")
    delete.add_argument("name")
    delete.set_defaults(handler=cmd_collection)

    tag = sub.add_parser("tag", parents=[shared], help="words on documents (nothing is moved)")
    tag_sub = tag.add_subparsers(dest="tag_command", metavar="action", required=True, parser_class=make_parser)
    for action, text in (("add", "tag documents"), ("remove", "remove a tag from documents")):
        one = tag_sub.add_parser(action, parents=[shared], help=text)
        one.add_argument("tag")
        one.add_argument("documents", nargs="+")
        one.set_defaults(handler=cmd_tag)
    tag_sub.add_parser("list", parents=[shared], help="every tag and how many documents carry it").set_defaults(handler=cmd_tag)

    saved = sub.add_parser("saved", parents=[shared], help="searches you saved (the parsed query, never a result list)")
    saved_sub = saved.add_subparsers(dest="saved_command", metavar="action", required=True, parser_class=make_parser)
    saved_sub.add_parser("list", parents=[shared], help="every saved search").set_defaults(handler=cmd_saved)
    run = saved_sub.add_parser("run", parents=[shared], help="run a saved search against the library as it is now")
    run.add_argument("name")
    run.add_argument("--limit", type=int, default=20)
    run.set_defaults(handler=cmd_saved)
    drop = saved_sub.add_parser("delete", parents=[shared], help="delete a saved search from view (the record is kept)")
    drop.add_argument("name")
    drop.set_defaults(handler=cmd_saved)

    view = sub.add_parser("view", parents=[shared], help="system views (inbox, unresolved, missing, duplicates, ...): queries, not stored state")
    view.add_argument("name", nargs="?", help="a view name; omit to list them")
    view.add_argument("--limit", type=int, default=50)
    view.set_defaults(handler=cmd_view)
