"""The commands other programs call: `capabilities`, `locate`, `open`, and `mcp` (the stdio server).

They are the integration surface (docs/INTEGRATION.md): small, read-only, and stable. `capabilities` needs no catalog; `locate` is
the question OpenChem asks of a hash it remembers; `open` hands a file to the operating system's viewer; `mcp` serves the read-only
Model Context Protocol tools on stdin/stdout.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable

from knowledgevista.cli_support import Outcome, catalog_path
from knowledgevista.db.catalog import open_catalog
from knowledgevista.domain import reference as refmod
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import capabilities as capabilities_service
from knowledgevista.services import locate as locate_service


def cmd_capabilities(args: argparse.Namespace) -> Outcome:
    data = capabilities_service.capabilities(catalog_path(args))
    reads = sum(1 for c in data["commands"] if c["read_only"])
    return Outcome([data], lines=[
        f"Knowledge Vista {data['app_version']}  protocol {data['protocol_version']}  json schema {data['json_schema_version']}  "
        f"catalog schema {data['catalog_schema_version']}  query language {data['query_language_version']}",
        f"{len(data['commands'])} commands ({reads} read-only); MCP tools: {', '.join(data['mcp']['tools'])}",
        f"catalog: {data['catalog']}",
    ])


_DISK_NOTE = {True: "", False: "   (NOT on disk: the catalog is behind; run kv scan)", None: "   (not checked)"}


def cmd_locate(args: argparse.Namespace) -> Outcome:
    conn = open_catalog(catalog_path(args), create=False, read_only=True)
    try:
        found = locate_service.locate(conn, args.reference, check_disk=not args.no_disk_check)
    finally:
        conn.close()
    out = Outcome([found])
    title = f"  {found['title']}" if found["title"] else ""
    out.lines.append(f"{found['status']}{title}")
    out.lines.append(f"  {found['uri']}")
    out.lines += [f"  {loc['absolute_path']}{_DISK_NOTE[loc['on_disk']]}" for loc in found["locations"]]
    if found["historical"]:
        out.warnings.append({"code": "KV_HISTORICAL_MATCH", "message": "That name only matched where the file USED to be; the locations shown are where it is now."})
    if found.get("retired_document"):
        out.warnings.append({"code": "KV_DOCUMENT_MERGED", "message": f"{found['requested_document_id']} was merged into {found['document_id']}; use that id from now on.",
                             "details": {"requested_document_id": found["requested_document_id"], "document_id": found["document_id"]}})
    out.lines += [f"  note: {w['message']}" for w in out.warnings]
    return out


def _launch(path: str) -> None:
    """Hand a file to the operating system's default program. Separate so tests can replace it."""
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606 - the point of the command
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])  # noqa: S603,S607
    else:
        subprocess.Popen(["xdg-open", path])  # noqa: S603,S607


def cmd_open(args: argparse.Namespace, launch: Callable[[str], None] | None = None) -> Outcome:
    if args.pdf_page is not None and args.label is not None:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, "Give --pdf-page (physical position) or --label (printed label), not both.")
    if args.pdf_page is not None and not 1 <= args.pdf_page <= refmod.MAX_PAGE:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"--pdf-page must be between 1 and {refmod.MAX_PAGE}.")
    conn = open_catalog(catalog_path(args), create=False, read_only=True)
    try:
        found = locate_service.locate(conn, args.reference)
    finally:
        conn.close()
    here = [loc for loc in found["locations"] if loc["state"] == "active" and loc["root_status"] == "online" and loc["on_disk"] is not False]
    if not here:
        code = ErrorCode.ROOT_UNAVAILABLE if found["status"] == "root_offline" else ErrorCode.FILE_MISSING
        raise KvError(code, f"There is no reachable copy of this document to open right now ({found['status']}).", {"locate": found})
    path = here[0]["absolute_path"]
    page = {"pdf_page": args.pdf_page, "printed_label": args.label} if (args.pdf_page is not None or args.label is not None) else found.get("page")
    launched = False
    if not args.no_launch:
        (launch or _launch)(path)
        launched = True
    record = {"type": "open", "document_id": found["document_id"], "artifact_id": found["artifact_id"], "path": path, "launched": launched,
              "requested_page": page, "page_targeted": False,
              "note": "The file was handed to the operating system's default program, which does not take a page from us. Go to the page named in requested_page; "
                      "the built-in reader (a later milestone) will open at it."}
    where = f"  (page {page['pdf_page'] or page['printed_label']} is not targeted by an external viewer)" if page else ""
    return Outcome([record], lines=[f"{'opened' if launched else 'would open'} {path}{where}"])


def cmd_mcp(args: argparse.Namespace) -> Outcome:
    from knowledgevista.mcp.stdio import serve

    serve(catalog_path(args))
    return Outcome(silent=True)


def add_parsers(sub, shared: argparse.ArgumentParser, make_parser) -> None:  # noqa: ANN001 - argparse's subparsers action
    sub.add_parser("capabilities", parents=[shared], help="what this installation can do (versions, commands, tools); needs no catalog").set_defaults(handler=cmd_capabilities)
    loc = sub.add_parser("locate", parents=[shared], help="where a document or file is NOW, from an id, hash or knowledgevista:// reference")
    loc.add_argument("reference", help="a document id, artifact sha256 (or 8+ char prefix), path/file name, or a knowledgevista:// reference")
    loc.add_argument("--no-disk-check", action="store_true", help="answer from the catalog alone; do not check that each path exists")
    loc.set_defaults(handler=cmd_locate)
    opn = sub.add_parser("open", parents=[shared], help="open a document's file in the operating system's viewer")
    opn.add_argument("reference")
    opn.add_argument("--pdf-page", type=int, help="the physical page (reported; an external viewer cannot be told to go there)")
    opn.add_argument("--label", help="the printed page label (reported, likewise)")
    opn.add_argument("--no-launch", action="store_true", help="resolve and report the path, but start nothing")
    opn.set_defaults(handler=cmd_open)
    sub.add_parser("mcp", parents=[shared], help="serve the read-only Model Context Protocol tools on stdin/stdout").set_defaults(handler=cmd_mcp)
