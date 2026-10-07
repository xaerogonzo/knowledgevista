"""The `kv` command: a thin adapter. Parse arguments, call a service, render the result. No SQL and no
business logic lives here (docs/ARCHITECTURE.md, "Layering"); the contract is docs/CLI_CONTRACT.md.

With `--json`, stdout carries exactly one JSON envelope and nothing else; progress and diagnostics go to stderr, so a
caller can parse stdout without filtering. Argument errors are also envelopes, with exit code 2.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from knowledgevista import __version__, paths
from knowledgevista.db.catalog import open_catalog
from knowledgevista.db.migrations import SchemaTooNew
from knowledgevista.envelope import Envelope
from knowledgevista.errors import EXIT_FAILURE, EXIT_INVALID, EXIT_OK, ErrorCode, KvError
from knowledgevista.extract.client import ExtractionSession
from knowledgevista.index.importer import import_openchem_index
from knowledgevista.index.search import parse_query
from knowledgevista.index.store import open_index
from knowledgevista.services import doctor as doctor_service
from knowledgevista.services import extract as extract_service
from knowledgevista.services import pages as pages_service
from knowledgevista.services import search as search_service
from knowledgevista.services import explain as explain_service
from knowledgevista.services import resolve as resolve_service
from knowledgevista.services import roots as roots_service
from knowledgevista.services import scan as scan_service
from knowledgevista.services import stats as stats_service
from knowledgevista.services import verify as verify_service


@dataclass
class Outcome:
    records: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)  # the human rendering
    complete: bool = True  # False when the records are a truncated view of a larger result

    @property
    def ok(self) -> bool:
        return not self.errors


class _Parser(argparse.ArgumentParser):
    """In `--json` mode an argument error is an envelope (code KV_INVALID_ARGUMENTS), never a usage dump on stderr."""

    json_mode = False

    def error(self, message: str):  # noqa: D102 - argparse's hook
        if self.json_mode:
            raise KvError(ErrorCode.INVALID_ARGUMENTS, message)
        super().error(message)


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --- commands ---------------------------------------------------------------------------------------------------


def _catalog_path(args: argparse.Namespace) -> Path:
    return Path(args.catalog) if getattr(args, "catalog", None) else paths.catalog_path()


def _index(args: argparse.Namespace, *, create: bool, read_only: bool = False):
    """The extraction store beside this catalog, or None if there is none (or it is from another schema version)."""
    return open_index(paths.index_path(_catalog_path(args)), create=create, read_only=read_only)


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
    conn = open_catalog(_catalog_path(args), create=False, read_only=True)
    index = _index(args, create=False, read_only=True)
    try:
        result = search_service.search(conn, index, query, limit=args.limit)
    finally:
        conn.close()
        if index is not None:
            index.close()
    cov = result.coverage
    out = Outcome(complete=not result.truncated)
    out.records.append({
        "type": "summary",
        "query": {"alternatives": list(query.alternatives), "near": list(query.near), "within": query.within,
                  "also": list(query.also), "path_contains": query.path_contains, "language_version": query.language_version},
        "hits": len(result.hits), "truncated": result.truncated, "coverage": cov,
    })
    out.records += [{"type": "hit", **hit} for hit in result.hits]
    if cov["searchable"] == 0:
        out.errors.append(KvError(ErrorCode.NOTHING_SEARCHABLE,
                                  "No document has extracted text, so this search could not have found anything. Run: kv extract",
                                  {"coverage": cov}).as_dict())
    else:
        caveats = [f"{cov[key]} {label}" for key, label in (
            ("not_yet_extracted", "PDF(s) not extracted yet"), ("no_text_layer", "scan(s) with no text layer"),
            ("partially_indexed", "partially indexed"), ("extraction_failed", "failed extraction(s)")) if cov[key]]
        if caveats:
            out.warnings.append({"code": "KV_SEARCH_COVERAGE", "details": cov,
                                 "message": "Not everything was searchable: " + ", ".join(caveats) + ". A missing hit is not proof of absence."})
    for hit in result.hits:
        where = hit["paths"][0]["path"] if hit["paths"] else hit["artifact_id"][:12]
        label = f" [{hit['printed_label']}]" if hit["printed_label"] else ""
        provisional = "  (provisional)" if hit["extraction"]["provisional"] else ""
        out.lines.append(f"{where}  p{hit['pdf_page']}{label}{provisional}\n    {' '.join(hit['snippet'].split())}")
    out.lines.append(f"{len(result.hits)} page(s)" + (f" (limit {args.limit}; more exist)" if result.truncated else ""))
    out.lines += [f"  note: {w['message']}" for w in out.warnings]
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
        if command in ("root", "import"):
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
    envelope = Envelope(command, ok=outcome.ok, records=outcome.records, warnings=outcome.warnings, errors=outcome.errors, complete=outcome.complete)
    _emit(envelope, json_mode, outcome.lines)
    if outcome.ok:
        return EXIT_OK
    return EXIT_INVALID if any(e["code"] == ErrorCode.INVALID_ARGUMENTS for e in outcome.errors) else EXIT_FAILURE
