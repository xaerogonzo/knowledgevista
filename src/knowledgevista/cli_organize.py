"""The organizer's commands: `plan create/show`, `apply`, `undo`, `recover`, `history`, and `root allow-organize`.

The order a person follows is the order of the lifecycle (docs/ORGANIZER.md): allow a root, make a PLAN, read it, apply it, and undo it if it
was wrong. `plan` writes a file outside the library and changes no file in it; `apply`, `undo` and `recover` are the only commands here that
move one, and they are marked `filesystem` in `kv capabilities` so a caller can refuse to run them. Everything prints the same numbers in the
JSON envelope as on screen; a move that did not happen is an error entry, never a quiet absence.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from knowledgevista.cli_support import Outcome, catalog_path, list_page
from knowledgevista.db.catalog import open_catalog
from knowledgevista.domain import naming
from knowledgevista.domain import plan as planmod
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import cursor as cursormod
from knowledgevista.services import organizer, organizer_plan, roots

STATUS_CHOICES = ("planned", "blocked", "unchanged", "all")


def _item_line(i: planmod.PlanItem) -> str:
    if i.status == "unchanged":
        return f"  unchanged  {i.old_path}"
    arrow = f"{i.old_path}\n             -> {i.new_path}"
    return f"  {i.status:9} {i.operation:11} {i.risk:6} {arrow}" + (f"\n             blocked: {i.blocked_reason}" if i.blocked_reason else "")


def cmd_root_allow(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(catalog_path(args), create=False)
    try:
        root, changed = roots.set_allow_organize(conn, args.root, not args.off)
    finally:
        conn.close()
    verb = "may now" if root.allow_organize else "may no longer"
    return Outcome([{"type": "root", **root.as_dict(), "changed": changed}],
                   lines=[f"The organizer {verb} move files in {root.label} ({root.configured_path})" + ("" if changed else "  (it already was so)")])


def _plan_records(plan: planmod.Plan, items: list[planmod.PlanItem], total: int, offset: int, shown_status: str) -> list[dict[str, Any]]:
    return [{"type": "item", **i.as_dict()} for i in items]


def cmd_plan(args: argparse.Namespace) -> Outcome:
    action = args.plan_command
    conn = open_catalog(catalog_path(args), create=False, read_only=(action == "show"))
    try:
        if action == "create":
            selected = organizer_plan.organizable_roots(conn, args.root or None)
            documents = {_document(conn, d) for d in args.document} if args.document else None
            plan = organizer_plan.build_plan(conn, roots=selected, layout=args.layout, documents=documents, check_disk=not args.no_disk_check)
            path = organizer_plan.write_plan(conn, plan, Path(args.out) if args.out else None)
            reference = path
        else:
            plan, _ = organizer.load_registered_plan(conn, args.plan)
            reference = None
        wanted = [i for i in plan.items if args.status == "all" or i.status == args.status]
        shown, total, offset, next_token = list_page(
            conn, args, command="plan show", signature=cursormod.signature_of(plan.plan_id, args.status), default_limit=50, key=lambda i: i.item_id,
            fetch=lambda window: (wanted[:window], len(wanted)))
    finally:
        conn.close()
    summary = plan.summary()
    out = Outcome([{"type": "summary", "plan_id": plan.plan_id, "path": str(reference) if reference else None, "hash": planmod.hash_of(plan.body()), "naming_policy": plan.naming_policy,
                    "layout": plan.options.get("layout"), "created_at": plan.created_at, **summary, "shown": len(shown), "offset": offset}],
                  complete=offset + len(shown) >= total, next_cursor=next_token)
    out.records += _plan_records(plan, shown, total, offset, args.status)
    out.lines.append(f"plan {plan.plan_id[:12]}  naming {plan.naming_policy}  layout {plan.options.get('layout')}  {summary['items']} item(s): "
                     + ", ".join(f"{n} {k}" for k, n in summary["status"].items()))
    if reference:
        out.lines.append(f"  written to {reference}. Nothing has been moved. Read it, then: kv apply {reference} (or --dry-run first)")
    out.lines += [_item_line(i) for i in shown]
    if not out.complete:
        out.lines.append(f"  ({len(shown)} of {total} {args.status}; continue with --cursor {next_token})")
    blocked = summary["status"].get("blocked", 0)
    if blocked:
        out.warnings.append({"code": "KV_ITEMS_BLOCKED", "message": f"{blocked} item(s) are blocked (a destination is occupied or unusable) and will be skipped.", "details": {"blocked": blocked}})
    return out


def _document(conn, reference: str) -> str:
    from knowledgevista.services import resolve as resolve_service

    return resolve_service.resolve_one(conn, reference).document_id


def _report_outcome(report: organizer.Report, what: str) -> Outcome:
    records: list[dict[str, Any]] = [{"type": "summary", "kind": report.kind, "plan_id": report.plan_id, "operation_id": report.operation_id, "dry_run": report.dry_run,
                                      "already_done": report.already_done, "counts": report.counts(), "problems": report.problems, "left_by_the_plan": report.skipped_by_plan}]
    records += [{"type": "item", **i.as_dict()} for i in report.items]
    out = Outcome(records)
    for i in report.items:
        if i.state in ("failed", "uncertain"):
            out.errors.append({"code": i.code or ErrorCode.INTERNAL, "message": f"{i.old_path}: {i.message}", "details": {"item_id": i.item_id, "old_path": i.old_path, "new_path": i.new_path, "state": i.state}})
        elif i.state == "skipped":
            out.warnings.append({"code": "KV_ITEM_SKIPPED", "message": f"{i.old_path}: {i.message}", "details": {"item_id": i.item_id, "old_path": i.old_path, "reason_code": i.code}})
    head = {"apply": "applied", "undo": "undone", "recover": "recovered"}[report.kind]
    if report.already_done:
        out.lines.append(f"Nothing to do: {what}")
    out.lines.append(f"{'DRY RUN: ' if report.dry_run else ''}{head} {report.plan_id[:12] if report.plan_id else ''}"
                     + (f" (operation {report.operation_id[:12]})" if report.operation_id else "") + "  "
                     + (", ".join(f"{n} {k}" for k, n in report.counts().items()) or "no items"))
    for i in report.items:
        suffix = f"  [{i.code}] {i.message}" if i.state in ("failed", "uncertain", "skipped") and i.message else ""
        out.lines.append(f"  {i.state:15} {i.old_path} -> {i.new_path}{suffix}")
    return out


def cmd_apply(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(catalog_path(args), create=False)
    try:
        report = organizer.apply_plan(conn, args.plan, dry_run=args.dry_run, verify_hashes=args.verify_hashes)
    finally:
        conn.close()
    out = _report_outcome(report, "every move in that plan is already in place")
    if report.skipped_by_plan:
        out.lines.append("  left alone by the plan itself: " + ", ".join(f"{n} {k}" for k, n in report.skipped_by_plan.items()))
    return out


def cmd_undo(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(catalog_path(args), create=False)
    try:
        report = organizer.undo_operation(conn, args.operation, dry_run=args.dry_run, verify_hashes=args.verify_hashes)
    finally:
        conn.close()
    return _report_outcome(report, "every move in that operation was already undone")


def cmd_recover(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(catalog_path(args), create=False)
    try:
        report = organizer.recover(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    return _report_outcome(report, "no operation was left unfinished")


def cmd_history(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(catalog_path(args), create=False, read_only=True)
    try:
        if args.operation:
            operation, items = organizer.operation_items(conn, args.operation)
            out = Outcome([{"type": "operation", "operation_id": operation["operation_id"], "kind": operation["kind"], "status": operation["status"], "plan_id": operation["plan_id"],
                            "undoes": operation["undoes_operation_id"], "started_at": operation["started_at"], "finished_at": operation["finished_at"], "actor": operation["actor"],
                            "app_version": operation["app_version"], "operation_schema": operation["operation_schema"]}])
            for r in items:
                out.records.append({"type": "item", "seq": r["seq"], "item_id": r["item_id"], "state": r["state"], "old_path": r["old_path"], "new_path": r["new_path"],
                                    "code": r["error_code"], "message": r["message"], "temp_path": r["temp_path"]})
                out.lines.append(f"  {r['seq']:3} {r['state']:10} {r['old_path']} -> {r['new_path']}" + (f"  [{r['error_code']}]" if r["error_code"] else ""))
            out.lines.insert(0, f"{operation['kind']} {operation['operation_id'][:12]}  {operation['status']}  {operation['started_at']}")
            return out
        limit = cursormod.bounded_limit(args.limit, 20)
        operations = organizer.history(conn, limit=limit)
    finally:
        conn.close()
    out = Outcome([{"type": "operation", **o} for o in operations])
    out.lines = [f"{o['started_at']}  {o['kind']:7} {o['operation_id'][:12]}  {o['status']:24} " + ", ".join(f"{n} {k}" for k, n in o["items"].items()) for o in operations] \
        or ["No operations yet. Plan one: kv plan create"]
    return out


def add_parsers(sub, shared: argparse.ArgumentParser, make_parser) -> None:  # noqa: ANN001 - argparse's subparsers action
    plan = sub.add_parser("plan", parents=[shared], help="propose renames and moves from accepted metadata, as a frozen plan file (moves nothing)")
    plan_sub = plan.add_subparsers(dest="plan_command", metavar="action", required=True, parser_class=make_parser)
    create = plan_sub.add_parser("create", parents=[shared], help="make a plan for the roots that allow organizing, and write it outside the library")
    create.add_argument("--root", action="append", default=[], help="a root (id, label or path); repeatable. Default: every root that allows organizing")
    create.add_argument("--layout", choices=naming.LAYOUTS, default="in_place", help="in_place: rename in the same folder (default). by_year: also move into <year>/ folders")
    create.add_argument("--document", action="append", default=[], help="only this document (repeatable)")
    create.add_argument("--out", help="where to write the plan file (default: the app's plans folder). Never inside a root")
    create.add_argument("--no-disk-check", action="store_true", help="plan from the catalog alone; do not look for files already at the proposed names")
    for command in (create,):
        command.add_argument("--status", choices=STATUS_CHOICES, default="planned", help="which items to list (the file always holds all of them)")
        command.add_argument("--limit", type=int, default=50)
        command.add_argument("--cursor", help="continue from the `next_cursor` of the previous page")
    create.set_defaults(handler=cmd_plan)
    show = plan_sub.add_parser("show", parents=[shared], help="read a plan: the file or its id")
    show.add_argument("plan", help="a plan file, or a plan id (or unique prefix) this catalog made")
    show.add_argument("--status", choices=STATUS_CHOICES, default="planned")
    show.add_argument("--limit", type=int, default=50)
    show.add_argument("--cursor", help="continue from the `next_cursor` of the previous page")
    show.set_defaults(handler=cmd_plan)

    apply = sub.add_parser("apply", parents=[shared], help="MOVE FILES as a plan says: checks everything first, journals every move, refuses to overwrite")
    apply.add_argument("plan", help="a plan file, or a plan id this catalog made")
    apply.add_argument("--dry-run", action="store_true", help="run every check (including hashing each source) and move nothing")
    apply.add_argument("--verify-hashes", action="store_true", help="after each move, read the file again and compare its bytes (slower)")
    apply.set_defaults(handler=cmd_apply)
    undo = sub.add_parser("undo", parents=[shared], help="move files back as an apply moved them; refuses any file that changed since")
    undo.add_argument("--operation", help="an apply's operation id (or prefix); default: the latest one with moves still in place")
    undo.add_argument("--dry-run", action="store_true")
    undo.add_argument("--verify-hashes", action="store_true")
    undo.set_defaults(handler=cmd_undo)
    recover = sub.add_parser("recover", parents=[shared], help="reconcile an interrupted apply or undo with what is on disk (never retries, never moves a file forward)")
    recover.add_argument("--dry-run", action="store_true")
    recover.set_defaults(handler=cmd_recover)
    history = sub.add_parser("history", parents=[shared], help="the organizer's journal: operations, and with an id, its moves")
    history.add_argument("operation", nargs="?", help="an operation id (or prefix) to show its moves")
    history.add_argument("--limit", type=int, default=20)
    history.set_defaults(handler=cmd_history)
