"""The `kv` command: a thin adapter. Parse arguments, call a service, render the result. No SQL and no
business logic lives here (docs/ARCHITECTURE.md, "Layering"); the contract is docs/CLI_CONTRACT.md.

With `--json`, stdout carries exactly one JSON envelope and nothing else; progress and diagnostics go to stderr, so a
caller can parse stdout without filtering. Argument errors are also envelopes, with exit code 2.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from knowledgevista import __version__, cli_gui, cli_integration, cli_organize, cli_relations, paths
from knowledgevista.cli_support import Outcome
from knowledgevista.cli_support import catalog_path as _catalog_path
from knowledgevista.cli_support import index_for as _index
from knowledgevista.cli_support import label_of as _label
from knowledgevista.cli_support import list_page
from knowledgevista.cli_support import say as _say
from knowledgevista.cli_support import search_outcome
from knowledgevista.db.catalog import open_catalog
from knowledgevista.db.migrations import SchemaTooNew
from knowledgevista.domain import fields as fieldmod
from knowledgevista.envelope import Envelope
from knowledgevista.errors import EXIT_FAILURE, EXIT_INVALID, EXIT_OK, ErrorCode, KvError
from knowledgevista.extract.client import ExtractionSession
from knowledgevista.index.importer import import_openchem_index
from knowledgevista.index.metacache import open_metacache
from knowledgevista.index.search import parse_query
from knowledgevista.index.store import open_index
from knowledgevista.network.policy import Fetcher, NetworkPolicy
from knowledgevista.providers.crossref import CrossrefProvider
from knowledgevista.services import cursor as cursormod
from knowledgevista.services import doctor as doctor_service
from knowledgevista.services import metadata as metadata_service
from knowledgevista.services import organize as organize_service
from knowledgevista.services import resolve_metadata as resolve_metadata_service
from knowledgevista.services import review as review_service
from knowledgevista.services import settings as settings_service
from knowledgevista.services import extract as extract_service
from knowledgevista.services import pages as pages_service
from knowledgevista.services import search as search_service
from knowledgevista.services import explain as explain_service
from knowledgevista.services import resolve as resolve_service
from knowledgevista.services import roots as roots_service
from knowledgevista.services import scan as scan_service
from knowledgevista.services import stats as stats_service
from knowledgevista.services import verify as verify_service


class _Parser(argparse.ArgumentParser):
    """In `--json` mode an argument error is an envelope (code KV_INVALID_ARGUMENTS), never a usage dump on stderr."""

    json_mode = False

    def error(self, message: str):  # noqa: D102 - argparse's hook
        if self.json_mode:
            raise KvError(ErrorCode.INVALID_ARGUMENTS, message)
        super().error(message)


# --- commands ---------------------------------------------------------------------------------------------------


def cmd_root_add(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(_catalog_path(args), create=True)
    try:
        root = roots_service.add_root(
            conn, args.path, label=args.label, allow_organize=args.allow_organize, catalog_path=str(_catalog_path(args))
        )
    finally:
        conn.close()
    return Outcome([root.as_dict()], lines=[f"Added root {root.label}: {root.configured_path}", f"  root id {root.root_id}"])


def cmd_root_list(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(_catalog_path(args), create=False, read_only=True)
    try:
        listed = roots_service.list_roots(conn)
    finally:
        conn.close()
    lines = [f"{r.label:20} {r.status:18} {r.configured_path}" for r in listed] or ["No roots yet. Add one: kv root add <path>"]
    return Outcome([r.as_dict() for r in listed], lines=lines)


def cmd_scan(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(_catalog_path(args), create=False)
    out = Outcome()
    try:
        selected = roots_service.find_roots(conn, args.root)
        if not selected:
            raise KvError(ErrorCode.NOT_FOUND, "No enabled roots to scan. Add one: kv root add <path>")
        skipped = 0
        for root in selected:
            report = scan_service.scan_root(conn, root.root_id, full=args.full, progress=_say)
            record = {"root": root.label, **report.as_dict()}
            out.records.append(record)
            out.warnings += [{**w, "root": root.label} for w in report.warnings]
            if report.status != "completed":
                skipped += 1
                out.lines.append(f"{root.label}: skipped, {report.root_status}. Nothing was changed.")
                continue
            out.lines.append(
                f"{root.label}: {report.files_seen} files; {report.unchanged} unchanged, {report.hashed} read "
                f"({report.new_artifacts} new artifacts, {report.moved} moved, {report.replaced} replaced, "
                f"{report.went_missing} missing, {report.reappeared} back) in {report.seconds:.1f}s"
            )
        if skipped == len(selected):
            out.errors.append(KvError(ErrorCode.ROOT_UNAVAILABLE, "Every selected root was unavailable; nothing was scanned.").as_dict())
    finally:
        conn.close()
    return out


def cmd_stats(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(_catalog_path(args), create=False, read_only=True)
    index = _index(args, create=False, read_only=True)
    try:
        stats = stats_service.library_stats(conn, index)
    finally:
        conn.close()
        if index is not None:
            index.close()
    inv, health = stats["inventory"], stats["health"]
    lines = [
        f"Inventory: {inv['documents']} documents, {inv['artifacts']} artifacts, {inv['locations_current']} current files "
        f"({inv['total_bytes'] / 1e6:.1f} MB) in {inv['roots']} root(s)",
        "  by type: " + ", ".join(f"{k} {v}" for k, v in inv["by_extension_kind"].items()),
        f"Health: {health['locations_missing']} missing, {health['locations_inaccessible']} inaccessible, "
        f"{health['exact_duplicate_groups']} exact-duplicate group(s), {health['extension_content_mismatches']} name/content mismatch(es)",
        f"Search: {stats['search']['searchable']} of {stats['search']['pdf_documents']} PDFs searchable "
        f"({stats['search']['not_yet_extracted']} not extracted yet, {stats['search']['no_text_layer']} scans with no text layer, "
        f"{stats['search']['partially_indexed']} partial, {stats['search']['extraction_failed']} failed)",
    ]
    meta = stats["metadata"]
    if meta["pdf_documents"]:
        pct = lambda value: "n/a" if value is None else f"{value:.0%}"  # noqa: E731
        lines.append(f"Metadata: of {meta['pdf_documents']} PDFs, {meta['accepted'].get('doi', 0)} have an accepted DOI ({pct(meta['doi_coverage'])}), "
                     f"{meta['accepted'].get('title', 0)} a title ({pct(meta['title_coverage'])}); "
                     f"{meta['review_queue']['proposed_items']} proposal(s) wait for review ({meta['review_queue']['safe']} safe for the batch rule)")
        for reason, count in sorted(meta["titles_without"].items(), key=lambda item: -item[1]):
            lines.append(f"  no title: {count} - {meta['title_reasons'].get(reason, reason)}")
    return Outcome([stats], lines=lines)


def cmd_explain(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(_catalog_path(args), create=False, read_only=True)
    try:
        match = resolve_service.resolve_one(conn, args.reference)
        doc = explain_service.explain_document(conn, match.document_id)
    finally:
        conn.close()
    out = Outcome([doc], warnings=list(doc["warnings"]))
    if match.historical:
        out.warnings.append({"code": "KVD_HISTORICAL_PATH",
                             "message": f"{args.reference!r} matched only a PAST location of this document (it was moved or replaced)."})
    out.lines.append(f"Document {doc['document_id']}")
    for art in doc["artifacts"]:
        out.lines.append(f"  artifact {art['artifact_id'][:16]}  {art['size']} bytes  {art['content_kind']}  "
                         f"{'available' if art['available'] else 'NOT AVAILABLE'}")
        for loc in art["locations"]:
            ended = f"  ended ({loc['end_reason']})" if loc["ended_at"] else ""
            out.lines.append(f"    {loc['state']:12} {loc['root']}/{loc['path']}{ended}")
    meta = doc["metadata"]
    for value in meta["values"]:
        out.lines.append(f"  {value['field']:9} {value['display'][:90]}   ({value['origin']} from {value['source']}, accepted by {value['accepted_by']}"
                         f"{', LOCKED' if value['locked'] else ''})")
    waiting = [c for c in meta["candidates"] if c["status"] == "proposed"]
    if waiting:
        out.lines.append(f"  {len(waiting)} proposal(s) waiting: kv review list")
    out.lines += [f"  warning: {w['message']}" for w in out.warnings]
    return out


def cmd_doctor(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(_catalog_path(args), create=False, read_only=True)
    index = _index(args, create=False, read_only=True)
    try:
        findings = doctor_service.run_doctor(conn, index)
    finally:
        conn.close()
        if index is not None:
            index.close()
    errors = sum(f.severity == "error" for f in findings)
    warns = sum(f.severity == "warning" for f in findings)
    out = Outcome([{"type": "summary", "categories_checked": doctor_service.CHECKED_CATEGORIES, "errors": errors, "warnings": warns},
                   *[{"type": "finding", **f.as_dict()} for f in findings]])
    out.lines = [f"[{f.severity}] {f.category}: {f.message}" for f in findings] or ["No problems found."]
    out.lines.append(f"Checked: {', '.join(doctor_service.CHECKED_CATEGORIES)} ({errors} error(s), {warns} warning(s))")
    if errors:
        out.errors.append(KvError(ErrorCode.DOCTOR_FOUND_PROBLEMS, f"{errors} catalog error(s) found.", {"errors": errors}).as_dict())
    return out


def cmd_verify(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(_catalog_path(args), create=False, read_only=True)
    try:
        selected = [r.root_id for r in roots_service.find_roots(conn, args.root)]
        report = verify_service.verify_hashes(conn, selected, progress=_say)
    finally:
        conn.close()
    out = Outcome([{"type": "summary", **{k: v for k, v in report.as_dict().items() if k not in ("mismatched", "missing", "unreadable", "changing")},
                    "mismatched": len(report.mismatched), "missing": len(report.missing),
                    "unreadable": len(report.unreadable), "changing": len(report.changing)}])
    out.records += [{"type": "mismatch", **m} for m in report.mismatched]
    out.lines = [f"Verified {report.checked} file(s): {report.ok} match, {len(report.mismatched)} CHANGED, "
                 f"{len(report.missing)} missing, {len(report.unreadable)} unreadable."]
    out.lines += [f"  changed: {m['path']}" for m in report.mismatched]
    if report.mismatched:
        out.errors.append(KvError(ErrorCode.FILE_CHANGED, f"{len(report.mismatched)} file(s) no longer match their recorded bytes. "
                                  "Run: kv scan --full", {"paths": [m["path"] for m in report.mismatched][:50]}).as_dict())
    if report.missing:
        out.errors.append(KvError(ErrorCode.FILE_MISSING, f"{len(report.missing)} active file(s) are gone. Run: kv scan",
                                  {"paths": report.missing[:50]}).as_dict())
    out.warnings += [{"code": "KV_ROOT_SKIPPED", "message": f"{s['root']} was not verified ({s['status']})."} for s in report.skipped_roots]
    return out


def cmd_extract(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(_catalog_path(args), create=False)
    index = _index(args, create=True)
    out = Outcome()
    try:
        selected = [r.root_id for r in roots_service.find_roots(conn, args.root)] if args.root else None
        session = ExtractionSession(memory_limit_mb=args.memory_limit_mb, page_timeout=args.page_timeout)
        with session:
            report = extract_service.extract_library(
                conn, index, root_ids=selected, force=args.force, retry_failed=args.retry_failed,
                rebuild_imported=args.rebuild_imported, limit=args.limit, session=session, progress=_say,
            )
    finally:
        conn.close()
        index.close()
    out.records.append({"type": "summary", **report.as_dict()})
    out.lines.append(
        f"{report.extracted} extracted ({report.complete} complete, {report.partial} partial, {report.failed} failed; "
        f"{report.pages} pages) of {report.candidates} PDFs in {report.seconds:.1f}s; {report.current} already current, "
        f"{report.provisional_imported} provisional imports kept, {report.unreachable} unreachable, "
        f"{report.changed_since_scan} changed since scan, {report.stale_redone} stale redone"
    )
    out.lines += [f"  {p['artifact_id'][:12]}: {p['message']}" for p in report.problems]
    if report.limiter and "NOT ENFORCED" in report.limiter:
        out.warnings.append({"code": "KV_MEMORY_LIMIT_NOT_ENFORCED", "message": report.limiter})
    if report.changed_since_scan:
        out.warnings.append({"code": "KV_CHANGED_SINCE_SCAN",
                             "message": f"{report.changed_since_scan} file(s) changed since the last scan and were skipped. Run: kv scan"})
    if report.other_kinds_not_extracted:
        out.warnings.append({"code": "KV_NON_PDF_NOT_EXTRACTED",
                             "message": f"{report.other_kinds_not_extracted} non-PDF artifact(s) are not extracted in this version."})
    if report.extracted and report.failed == report.extracted:
        out.errors.append(KvError(ErrorCode.EXTRACTION_FAILED, "Every document that was tried failed to extract.",
                                  {"failed": report.failed}).as_dict())
    return out


def cmd_search(args: argparse.Namespace) -> Outcome:
    query = parse_query(args.query, near=args.near, within=args.within, also=args.also, path_contains=args.file)
    out = search_outcome(args, query)
    if args.save:
        conn = open_catalog(_catalog_path(args), create=False)
        try:
            saved = organize_service.save_search(conn, args.save, query, replace=args.replace)
        finally:
            conn.close()
        out.records.append({"type": "saved", **saved})
        out.lines.append(f"saved as {saved['name']!r}" + (" (replaced the earlier one)" if saved["replaced"] else "") + ": kv saved run " + repr(saved["name"]))
    return out


def cmd_show(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(_catalog_path(args), create=False, read_only=True)
    index = _index(args, create=False, read_only=True)
    try:
        page = pages_service.get_page(conn, index, args.reference, pdf_page=args.pdf_page, label=args.label)
    finally:
        conn.close()
        if index is not None:
            index.close()
    shown = f"pdf page {page['pdf_page']} of {page['page_count']}"
    if page["printed_label"]:
        shown += f" (printed label {page['printed_label']})"
    out = Outcome([page])
    out.lines = [f"{shown}  [{page['text_state']}]", page["text"] or "(no extracted text on this page)", "", f"-- {page['note']}"]
    if page["extraction"]["provisional"]:
        out.warnings.append({"code": "KVD_PROVISIONAL_TEXT",
                             "message": "This text was imported from the OpenChem index and has no page labels."})
    return out


def cmd_import_index(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(_catalog_path(args), create=False)
    index = _index(args, create=True)
    try:
        report = import_openchem_index(conn, index, args.path, progress=_say)
    finally:
        conn.close()
        index.close()
    out = Outcome([{"type": "summary", **report.as_dict()}])
    out.lines = [f"Imported {report.imported} document(s), {report.pages} pages, from {report.legacy_files} legacy row(s). "
                 f"Unmatched (hash not in the catalog): {report.unmatched}; kept native: {report.skipped_native_exists}; "
                 f"already imported: {report.skipped_already_imported}; duplicate hash: {report.skipped_duplicate_hash}."]
    if report.unmatched:
        out.warnings.append({"code": "KV_IMPORT_UNMATCHED", "details": {"examples": report.unmatched_examples},
                             "message": f"{report.unmatched} legacy row(s) match no artifact (not scanned yet, or the file changed). "
                                        "Run kv scan, then import again."})
    return out


def cmd_resolve(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(_catalog_path(args), create=False)
    index = _index(args, create=False, read_only=True)
    cache = open_metacache(paths.metacache_path(_catalog_path(args)), create=True)
    out = Outcome()
    try:
        if args.list_requests:
            plan = resolve_metadata_service.planned_requests(conn, cache)
            out.records = [{"type": "summary", "requests": len(plan), "dois": sum(p["sends"] == "doi" for p in plan),
                            "titles": sum(p["sends"] == "title" for p in plan), "already_cached": sum(p["cached"] for p in plan)}]
            out.records += [{"type": "request", **p} for p in plan]
            out.lines = [f"{p['sends']:5} {p['value']}{'   (cached: not sent)' if p['cached'] else ''}" for p in plan]
            out.lines.append(f"{len(plan)} lookup(s) would be considered ({out.records[0]['already_cached']} already cached). Nothing was sent. "
                             "Only a DOI or a title leaves the machine.")
            return out
        if index is None:
            raise KvError(ErrorCode.NOT_EXTRACTED, "Nothing has been extracted yet, so there is no text to read DOIs and titles from. Run: kv extract")
        settings = settings_service.load()
        provider = None
        if args.online:
            if not settings["online_lookup"]:
                raise KvError(ErrorCode.NETWORK_DISABLED,
                              "Online lookups are switched off. They send a DOI or a title to a metadata provider (Crossref) and nothing else. "
                              "To allow them: kv config set online_lookup true", {"setting": "online_lookup"})
            policy = NetworkPolicy(online=True, mailto=settings["mailto"], request_budget=args.max_requests or settings["request_budget"])
            provider = CrossrefProvider(Fetcher(policy), cache=cache)
            _say(f"online: DOIs and titles will be sent to api.crossref.org (at most {policy.request_budget} requests)"
                 + ("" if policy.mailto else "; no contact address is set (kv config set mailto you@example.org)"))
        document_ids = [resolve_service.resolve_one(conn, args.document).document_id] if args.document else None
        root_ids = [r.root_id for r in roots_service.find_roots(conn, args.root)] if args.root else None
        session = None if args.no_front else ExtractionSession(memory_limit_mb=args.memory_limit_mb, page_timeout=args.page_timeout)
        try:
            report = resolve_metadata_service.resolve_library(
                conn, index, cache, provider=provider, online=args.online, accept_safe=args.accept_safe, session=session,
                read_front=not args.no_front, force_front=args.refresh_front, document_ids=document_ids, root_ids=root_ids,
                limit=args.limit, progress=_say)
        finally:
            if session is not None:
                session.close()
        queue_items = review_service.queue(conn, limit=0)[1]
    finally:
        conn.close()
        cache.close()
        if index is not None:
            index.close()
    local, found, accepted, online = report["local"], report["doi_classes"], report["accepted"], report["online"]
    stopped = online.get("stopped")
    out.complete = not (args.online and stopped)
    out.records.append({"type": "summary", "review_queue_items": queue_items, **report})
    out.lines.append(f"Resolved {report['documents']} document(s) ({report['not_extracted']} not extracted yet): printed DOI found for "
                     f"{found.get('own', 0)}, undecided for {found.get('ambiguous', 0)}, none for {found.get('none', 0)}.")
    out.lines.append("Proposals: " + ", ".join(f"{local.get(k, 0)} {k}" for k in ("new", "updated", "unchanged", "stale", "decided") if local.get(k)) + "." if local else "Proposals: none.")
    if args.online:
        out.lines.append(f"Online: {online['requests']} request(s), {online['cache_hits']} answered from the cache; "
                         f"answers: {', '.join(f'{k} {v}' for k, v in sorted(online['states'].items())) or 'none'}"
                         + (f"; checked {online.get('dois_checked', 0)} DOI(s), {online.get('title_searches', 0)} title search(es)" if online.get('dois_checked') or online.get('title_searches') else "") + ".")
    if args.accept_safe:
        out.lines.append(f"Accepted by the safe rule: {accepted['total']}" + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(accepted.items()) if k != "total") + ")" if accepted["total"] else "") + ".")
    out.lines.append(f"{queue_items} item(s) wait for review: kv review list")
    if stopped:
        out.warnings.append({"code": "KV_PROVIDER_STOPPED", "message": f"Online lookups stopped early: {stopped}. Nothing accepted was changed; run again later and it resumes from the cache.",
                             "details": {"reason": stopped}})
    for skipped in report["skipped_by_rule"]:
        out.warnings.append({"code": "KV_SAFE_RULE_SKIPPED", "message": f"The safe rule left the {skipped['field']} alone: {skipped['reason']}.", "details": skipped})
    for problem in report["problems"]:
        out.warnings.append({"code": "KV_RESOLVE_PROBLEM", "message": problem["message"], "details": problem})
    if report["front"].get("unavailable") and not args.no_front:
        out.warnings.append({"code": "KV_FRONT_MATTER_UNAVAILABLE", "message": f"The front matter of {report['front']['unavailable']} file(s) was not read "
                             "(PyMuPDF is not installed, or the file is not reachable); titles from the layout and the file's metadata are missing for them."})
    out.lines += [f"  note: {w['message']}" for w in out.warnings]
    return out


def cmd_review_list(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(_catalog_path(args), create=False, read_only=True)
    try:
        include = ("proposed", "set_aside", "stale", "rejected") if args.status == "all" else (args.status,)
        items, total, offset, next_token = list_page(
            conn, args, command="review list", signature=cursormod.signature_of(args.field, args.review, args.status), default_limit=25,
            key=lambda i: i["candidate_ids"][0],
            fetch=lambda window: review_service.queue(conn, field_=args.field, review=args.review, include=include, limit=window, offset=0))
        for item in items:
            item["label"] = _label(conn, item["document_id"])
    finally:
        conn.close()
    out = Outcome([{"type": "summary", "total": total, "shown": len(items), "offset": offset}], complete=offset + len(items) >= total, next_cursor=next_token)
    out.records += [{"type": "item", **i} for i in items]
    for i in items:
        hint = f"   [differs from accepted: {i['differs_from_accepted']}]" if i["differs_from_accepted"] else ""
        out.lines.append(f"{i['candidate_ids'][0][:10]}  {i['field']:9} {i['confidence']:9} {i['review']:8} {i['display'][:70]}{hint}\n"
                         f"             {i['label']}  <- {', '.join(i['sources'])}")
    out.lines.append(f"{len(items)} of {total} item(s)" + ("" if out.complete else f" (continue with --cursor {next_token})"))
    return out


def cmd_review_accept(args: argparse.Namespace) -> Outcome:
    if not args.safe and not args.candidates:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, "Name the candidate(s) to accept (ids from: kv review list), or use --safe for the batch rule.")
    conn = open_catalog(_catalog_path(args), create=False)
    out = Outcome()
    try:
        if args.safe:
            batch = review_service.accept_safe(conn)
            accepted, skipped = batch.accepted, batch.skipped
        else:
            accepted, skipped = [metadata_service.accept_candidate(conn, review_service.resolve_candidate_id(conn, c), actor="user") for c in args.candidates], []
        for item in accepted:
            out.records.append({"type": "accepted", "candidate_id": item.candidate_id, "document_id": item.document_id, "field": item.field,
                                "value": item.value, "changed": item.changed, "replaced": item.replaced})
            out.lines.append(f"accepted {item.field}: {item.value[:80]}  ({_label(conn, item.document_id)})")
        for item in skipped:
            out.warnings.append({"code": "KV_SAFE_RULE_SKIPPED", "message": f"left the {item['field']} alone: {item['reason']}", "details": item})
    finally:
        conn.close()
    out.lines.append(f"{sum(1 for r in out.records if r['changed'])} value(s) changed, {len(out.records)} proposal(s) accepted."
                     + (f" {len(out.warnings)} left for a person." if out.warnings else ""))
    return out


def cmd_review_reject(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(_catalog_path(args), create=False)
    out = Outcome()
    try:
        for reference in args.candidates:
            candidate_id = review_service.resolve_candidate_id(conn, reference)
            changed = metadata_service.reject_candidate(conn, candidate_id)
            out.records.append({"type": "rejected", "candidate_id": candidate_id, "changed": changed})
            out.lines.append(f"{'rejected' if changed else 'already rejected'}: {candidate_id[:10]}")
    finally:
        conn.close()
    return out


def cmd_metadata(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(_catalog_path(args), create=False)
    try:
        document_id = resolve_service.resolve_one(conn, args.reference).document_id
        action = args.metadata_command
        if action == "set":
            changed = metadata_service.set_value(conn, document_id, args.field, args.value, lock=not args.no_lock)
            line = f"{args.field} set{'' if args.no_lock else ' and locked'}" if changed else f"{args.field} already has that value"
        elif action == "clear":
            changed = metadata_service.clear_value(conn, document_id, args.field)
            line = f"{args.field} cleared (back to unknown)" if changed else f"{args.field} had no value"
        else:
            changed = metadata_service.set_lock(conn, document_id, args.field, action == "lock")
            line = f"{args.field} {action}ed" if changed else f"{args.field} was already {action}ed"
        values = metadata_service.get_values(conn, document_id)
        current = values.get(args.field)
    finally:
        conn.close()
    return Outcome([{"type": "metadata", "document_id": document_id, "field": args.field, "changed": changed,
                     "value": current["value"] if current else None, "locked": bool(current["locked"]) if current else None}], lines=[line])


def cmd_config(args: argparse.Namespace) -> Outcome:
    action = args.config_command
    if action == "set":
        value = settings_service.set_value(args.key, args.value)
        return Outcome([{"type": "setting", "key": args.key, "value": value, "default": settings_service.DEFAULTS[args.key]}],
                       lines=[f"{args.key} = {value}"])
    current = settings_service.load()
    keys = [args.key] if action == "get" else list(settings_service.DEFAULTS)
    if action == "get" and args.key not in settings_service.DEFAULTS:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"Unknown setting {args.key!r}. Settings: {', '.join(settings_service.DEFAULTS)}")
    out = Outcome([{"type": "setting", "key": k, "value": current[k], "default": settings_service.DEFAULTS[k], "help": settings_service.HELP[k]} for k in keys])
    out.lines = [f"{k} = {current[k]}" + (f"    # {settings_service.HELP[k]}" if action == "list" else "") for k in keys]
    if current.problem:
        out.warnings.append({"code": "KV_SETTINGS_IGNORED", "message": current.problem})
        out.lines.append(f"note: {current.problem}")
    return out


# --- parser and entry point -------------------------------------------------------------------------------------


def build_parser(json_mode: bool = False) -> _Parser:
    parser = _Parser(prog="kv", description="Knowledge Vista: a local-first document library.")
    parser.json_mode = json_mode
    # Global options are accepted before OR after the command (SUPPRESS keeps the later parse from resetting them).
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print one JSON envelope on stdout")
    shared.add_argument("--catalog", default=argparse.SUPPRESS, help="the catalog file (default: the app-data location)")
    parser.add_argument("--json", action="store_true", help="print one JSON envelope on stdout; progress goes to stderr")
    parser.add_argument("--catalog", help="the catalog file (default: the app-data location)")
    parser.add_argument("--version", action="version", version=f"KnowledgeVista {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command", parser_class=lambda **kw: _Sub(json_mode, **kw))

    root = sub.add_parser("root", parents=[shared], help="manage library roots")
    root_sub = root.add_subparsers(dest="root_command", metavar="action", required=True, parser_class=lambda **kw: _Sub(json_mode, **kw))
    add = root_sub.add_parser("add", parents=[shared], help="add a folder to scan")
    add.add_argument("path")
    add.add_argument("--label")
    add.add_argument("--allow-organize", action="store_true", help="let a future rename/move plan touch this root (default: no)")
    add.set_defaults(handler=cmd_root_add)
    root_sub.add_parser("list", parents=[shared], help="list roots").set_defaults(handler=cmd_root_list)
    allow = root_sub.add_parser("allow-organize", parents=[shared], help="let the organizer move files in this root (or, with --off, stop it); the default is no")
    allow.add_argument("root", help="a root id, label or path")
    allow.add_argument("--off", action="store_true", help="take the permission away")
    allow.set_defaults(handler=cli_organize.cmd_root_allow)

    scan = sub.add_parser("scan", parents=[shared], help="find files, hash what changed, reconcile moves and removals")
    scan.add_argument("--root", help="a root id, label or path (default: every enabled root)")
    scan.add_argument("--full", action="store_true", help="re-read every file instead of trusting size+mtime")
    scan.set_defaults(handler=cmd_scan)
    sub.add_parser("stats", parents=[shared], help="inventory and health").set_defaults(handler=cmd_stats)
    explain = sub.add_parser("explain", parents=[shared], help="everything known about one document")
    explain.add_argument("reference", help="a document id, artifact sha256 (or 8+ char prefix), or a path/file name")
    explain.set_defaults(handler=cmd_explain)
    sub.add_parser("doctor", parents=[shared], help="structural health check (read-only)").set_defaults(handler=cmd_doctor)
    verify = sub.add_parser("verify", parents=[shared], help="re-read every file and check its bytes (slow, read-only)")
    verify.add_argument("--root")
    verify.set_defaults(handler=cmd_verify)
    extract = sub.add_parser("extract", parents=[shared], help="read the text of PDFs into the search index (needs the extract group)")
    extract.add_argument("--root")
    extract.add_argument("--force", action="store_true", help="re-extract everything")
    extract.add_argument("--retry-failed", action="store_true", help="try again the documents that failed or were partial")
    extract.add_argument("--rebuild-imported", action="store_true",
                         help="replace text imported from the OpenChem index with native extraction")
    extract.add_argument("--limit", type=int, help="stop after this many documents")
    extract.add_argument("--memory-limit-mb", type=int, default=2048, help="memory cap for the extraction worker (default 2048)")
    extract.add_argument("--page-timeout", type=float, default=60.0, help="seconds one page may take before the worker is replaced")
    extract.set_defaults(handler=cmd_extract)
    search = sub.add_parser("search", parents=[shared], help="find pages by their words")
    search.add_argument("query", help="a phrase; `a | b` for alternatives; a trailing * is a prefix")
    search.add_argument("--near", action="append", default=[], help="also within --within words of it (repeatable)")
    search.add_argument("--within", type=int, default=30)
    search.add_argument("--also", action="append", default=[], help="also somewhere on the same page (repeatable)")
    search.add_argument("--file", help="only files whose path contains this")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--cursor", help="continue from the `next_cursor` of the previous page")
    search.add_argument("--save", metavar="NAME", help="also save this query (the parsed query, never its results) under a name: kv saved run NAME")
    search.add_argument("--replace", action="store_true", help="with --save: overwrite a saved search of that name")
    search.set_defaults(handler=cmd_search)
    show = sub.add_parser("show", parents=[shared], help="print one extracted page")
    show.add_argument("reference", help="a document id, sha256 or path/file name")
    which = show.add_mutually_exclusive_group(required=True)
    which.add_argument("--pdf-page", type=int, help="the physical position in the PDF, counted from 1")
    which.add_argument("--label", help="the page number as PRINTED on the page (a string: iii, A-1, 164)")
    show.set_defaults(handler=cmd_show)
    imp = sub.add_parser("import", parents=[shared], help="import data from another tool")
    imp_sub = imp.add_subparsers(dest="import_command", metavar="source", required=True,
                                 parser_class=lambda **kw: _Sub(json_mode, **kw))
    oc = imp_sub.add_parser("openchem-index", parents=[shared],
                            help="use a '<library>.index.sqlite' from OpenChem as provisional search text")
    oc.add_argument("path")
    oc.set_defaults(handler=cmd_import_index)

    resolve = sub.add_parser("resolve", parents=[shared], help="propose titles, DOIs and authors from the files (and, if allowed, a provider)")
    resolve.add_argument("--online", action="store_true",
                         help="also ask a metadata provider (needs: kv config set online_lookup true). Only a DOI or a title is sent")
    resolve.add_argument("--list-requests", action="store_true", help="show what --online would send and send nothing")
    resolve.add_argument("--accept-safe", action="store_true", help="accept the proposals that earned `safe` (the named batch rule); nothing else")
    resolve.add_argument("--document", help="only this document (id, sha256 prefix or path)")
    resolve.add_argument("--root", help="only documents reachable under this root")
    resolve.add_argument("--limit", type=int, help="stop after this many documents")
    resolve.add_argument("--max-requests", type=int, help="the most requests this run may make (default: the request_budget setting)")
    resolve.add_argument("--no-front", action="store_true", help="do not open the PDFs for their layout, Info and XMP (text only)")
    resolve.add_argument("--refresh-front", action="store_true", help="read the PDFs' front matter again even if it is cached")
    resolve.add_argument("--memory-limit-mb", type=int, default=2048)
    resolve.add_argument("--page-timeout", type=float, default=60.0)
    resolve.set_defaults(handler=cmd_resolve)

    rev = sub.add_parser("review", parents=[shared], help="the queue of proposals waiting for a person")
    rev_sub = rev.add_subparsers(dest="review_command", metavar="action", required=True, parser_class=lambda **kw: _Sub(json_mode, **kw))
    rev_list = rev_sub.add_parser("list", parents=[shared], help="list proposals, most urgent first")
    rev_list.add_argument("--field", choices=fieldmod.FIELDS)
    rev_list.add_argument("--review", choices=("safe", "required"), help="only proposals the batch rule may accept / only those a person must")
    rev_list.add_argument("--status", choices=("proposed", "set_aside", "stale", "rejected", "all"), default="proposed")
    rev_list.add_argument("--limit", type=int, default=25)
    rev_list.add_argument("--offset", type=int, default=0, help="skip this many items (prefer --cursor: it notices when the queue changed)")
    rev_list.add_argument("--cursor", help="continue from the `next_cursor` of the previous page")
    rev_list.set_defaults(handler=cmd_review_list)
    rev_accept = rev_sub.add_parser("accept", parents=[shared], help="accept proposals (ids from `review list`), or --safe for the batch rule")
    rev_accept.add_argument("candidates", nargs="*", help="candidate ids (8+ characters)")
    rev_accept.add_argument("--safe", action="store_true", help="apply the safe batch rule to every document")
    rev_accept.set_defaults(handler=cmd_review_accept)
    rev_reject = rev_sub.add_parser("reject", parents=[shared], help="decide a proposal is wrong (it is kept, and not proposed again)")
    rev_reject.add_argument("candidates", nargs="+")
    rev_reject.set_defaults(handler=cmd_review_reject)

    meta = sub.add_parser("metadata", parents=[shared], help="state, clear or lock a document's metadata by hand")
    meta_sub = meta.add_subparsers(dest="metadata_command", metavar="action", required=True, parser_class=lambda **kw: _Sub(json_mode, **kw))
    for action, text in (("set", "state a value (locked unless --no-lock)"), ("clear", "back to unknown"), ("lock", "protect the value from every resolver"),
                         ("unlock", "let resolvers propose a different value again")):
        one = meta_sub.add_parser(action, parents=[shared], help=text)
        one.add_argument("reference", help="a document id, sha256 prefix or path")
        one.add_argument("field", choices=fieldmod.FIELDS)
        if action == "set":
            one.add_argument("value")
            one.add_argument("--no-lock", action="store_true")
        one.set_defaults(handler=cmd_metadata)

    cfg = sub.add_parser("config", parents=[shared], help="app-wide settings (online lookups are off by default)")
    cfg_sub = cfg.add_subparsers(dest="config_command", metavar="action", required=True, parser_class=lambda **kw: _Sub(json_mode, **kw))
    cfg_sub.add_parser("list", parents=[shared], help="show every setting").set_defaults(handler=cmd_config)
    cfg_get = cfg_sub.add_parser("get", parents=[shared], help="show one setting")
    cfg_get.add_argument("key")
    cfg_get.set_defaults(handler=cmd_config)
    cfg_set = cfg_sub.add_parser("set", parents=[shared], help="change a setting")
    cfg_set.add_argument("key")
    cfg_set.add_argument("value")
    cfg_set.set_defaults(handler=cmd_config)

    cli_relations.add_parsers(sub, shared, lambda **kw: _Sub(json_mode, **kw))
    cli_integration.add_parsers(sub, shared, lambda **kw: _Sub(json_mode, **kw))
    cli_organize.add_parsers(sub, shared, lambda **kw: _Sub(json_mode, **kw))
    cli_gui.add_parsers(sub, shared, lambda **kw: _Sub(json_mode, **kw))
    return parser


class _Sub(_Parser):
    def __init__(self, json_mode: bool, **kwargs: Any):
        super().__init__(**kwargs)
        self.json_mode = json_mode


def _emit(envelope: Envelope, json_mode: bool, lines: list[str]) -> None:
    if json_mode:
        sys.stdout.write(envelope.to_json() + "\n")
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        for line in lines:
            print(line)
        for error in envelope.errors:
            print(f"error {error['code']}: {error['message']}", file=sys.stderr)
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in args_in
    command = "kv"
    try:
        args = build_parser(json_mode).parse_args(args_in)
        handler: Callable[[argparse.Namespace], Outcome] | None = getattr(args, "handler", None)
        if handler is None:
            raise KvError(ErrorCode.INVALID_ARGUMENTS, "No command given. Try: kv --help")
        command = args.command
        if command in ("root", "import", "review", "metadata", "config", "relations", "document", "collection", "tag", "saved", "plan"):
            command = f"{command} {getattr(args, command + '_command')}"
        outcome = handler(args)
    except KvError as exc:
        _emit(Envelope(command, ok=False, errors=[exc.as_dict()]), json_mode, [])
        return exc.exit_code
    except SchemaTooNew as exc:
        error = KvError(ErrorCode.CATALOG_TOO_NEW, str(exc))
        _emit(Envelope(command, ok=False, errors=[error.as_dict()]), json_mode, [])
        return EXIT_FAILURE
    except Exception as exc:  # noqa: BLE001 - the contract is "an envelope, never a traceback", so convert and report
        error = KvError(ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}")
        _emit(Envelope(command, ok=False, errors=[error.as_dict()]), json_mode, [])
        return EXIT_FAILURE
    if outcome.silent:
        return EXIT_OK
    envelope = Envelope(command, ok=outcome.ok, records=outcome.records, warnings=outcome.warnings, errors=outcome.errors, complete=outcome.complete,
                        next_cursor=outcome.next_cursor)
    _emit(envelope, json_mode, outcome.lines)
    if outcome.ok:
        return EXIT_OK
    return EXIT_INVALID if any(e["code"] == ErrorCode.INVALID_ARGUMENTS for e in outcome.errors) else EXIT_FAILURE
