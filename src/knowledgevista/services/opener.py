"""Handing a document's file to the operating system's viewer: the one implementation `kv open` and the library window share.

Resolution is `locate`'s (disk beats catalog), so a file that was renamed since the last scan still opens. The viewer is told the
file, not the page: an external program cannot be steered to one, so the page is reported and never claimed as targeted.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from knowledgevista.domain import reference as refmod
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import locate as locate_service


def launch(path: str) -> None:
    """Hand a file to the operating system's default program. Separate so tests can replace it."""
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606 - the point of the command
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])  # noqa: S603,S607
    else:
        subprocess.Popen(["xdg-open", path])  # noqa: S603,S607


def check_page(pdf_page: int | None, label: str | None) -> None:
    if pdf_page is not None and label is not None:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, "Give --pdf-page (physical position) or --label (printed label), not both.")
    if pdf_page is not None and not 1 <= pdf_page <= refmod.MAX_PAGE:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"--pdf-page must be between 1 and {refmod.MAX_PAGE}.")


def open_document(conn: sqlite3.Connection, reference: str, *, pdf_page: int | None = None, label: str | None = None,
                  do_launch: bool = True, launcher: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Resolve `reference` to a reachable copy and open it. Raises FILE_MISSING / ROOT_UNAVAILABLE when there is none to open."""
    check_page(pdf_page, label)
    found = locate_service.locate(conn, reference)
    here = [loc for loc in found["locations"] if loc["state"] == "active" and loc["root_status"] == "online" and loc["on_disk"] is not False]
    if not here:
        code = ErrorCode.ROOT_UNAVAILABLE if found["status"] == "root_offline" else ErrorCode.FILE_MISSING
        raise KvError(code, f"There is no reachable copy of this document to open right now ({found['status']}).", {"locate": found})
    path = here[0]["absolute_path"]
    page = {"pdf_page": pdf_page, "printed_label": label} if (pdf_page is not None or label is not None) else found.get("page")
    launched = False
    if do_launch:
        (launcher or launch)(path)
        launched = True
    return {"type": "open", "document_id": found["document_id"], "artifact_id": found["artifact_id"], "path": path, "launched": launched,
            "requested_page": page, "page_targeted": False,
            "note": "The file was handed to the operating system's default program, which does not take a page from us. Go to the page named in requested_page; "
                    "the built-in reader (a later milestone) will open at it."}
