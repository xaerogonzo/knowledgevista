"""Read one extracted page: `kv show`.

There is no bare "page 164" anywhere in this API. A PDF's physical page position (`pdf_page`, 1-based) and the number
printed on the page (`printed_label`, a STRING such as `iii`, `A-1` or `164`) are different things that only coincide
by luck, and a research citation needs to say which it means. So the two are separate parameters and separate
failures: a label shared by several pages (every appendix restarting at "1") is `KV_AMBIGUOUS` with the candidates,
never a guess.

The text returned is EXTRACTED text: right for finding and orienting, wrong to quote a table from. The result says
so, and says where the real page is.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.extract.profile import IMPORTED_SOURCE
from knowledgevista.index.store import get_extraction, page_text
from knowledgevista.services.resolve import resolve_one
from knowledgevista.services.search import current_paths


def get_page(
    catalog: sqlite3.Connection, index: sqlite3.Connection | None, reference: str, *, pdf_page: int | None = None, label: str | None = None
) -> dict[str, Any]:
    if (pdf_page is None) == (label is None):
        raise KvError(ErrorCode.INVALID_ARGUMENTS, "Give exactly one of --pdf-page (physical position) or --label (printed page label).")
    match = resolve_one(catalog, reference)
    extraction = get_extraction(index, match.artifact_id) if index is not None else None
    if extraction is None:
        raise KvError(
            ErrorCode.NOT_EXTRACTED,
            "This document's text has not been extracted yet, so there is no page to show. Run: kv extract",
            {"document_id": match.document_id, "artifact_id": match.artifact_id},
        )
    if pdf_page is not None:
        row = index.execute("SELECT * FROM page WHERE extraction_id = ? AND pdf_page = ?", (extraction["extraction_id"], pdf_page)).fetchone()
        if row is None:
            raise KvError(
                ErrorCode.NOT_FOUND, f"The document has {extraction['page_count']} page(s); there is no pdf page {pdf_page}.",
                {"pdf_page": pdf_page, "page_count": extraction["page_count"]},
            )
    else:
        rows = index.execute("SELECT * FROM page WHERE extraction_id = ? AND printed_label = ? ORDER BY pdf_page", (extraction["extraction_id"], label)).fetchall()
        if not rows:
            raise KvError(ErrorCode.NOT_FOUND, f"No page carries the printed label {label!r}.", {"label": label})
        if len(rows) > 1:
            raise KvError(
                ErrorCode.AMBIGUOUS, f"{len(rows)} pages carry the printed label {label!r}; use --pdf-page.",
                {"label": label, "candidates": [{"pdf_page": r["pdf_page"], "printed_label": r["printed_label"]} for r in rows]},
            )
        row = rows[0]
    provisional = extraction["source"] == IMPORTED_SOURCE
    return {
        "document_id": match.document_id,
        "artifact_id": match.artifact_id,
        "anchor": {"artifact_id": match.artifact_id, "pdf_page": row["pdf_page"]},
        "pdf_page": row["pdf_page"],
        "printed_label": row["printed_label"],
        "page_count": extraction["page_count"],
        "text_state": row["text_state"],
        "text": page_text(index, row["page_id"]),
        "page_error": row["error"],
        "extraction": {"source": extraction["source"], "status": extraction["status"], "profile_id": extraction["profile_id"], "provisional": provisional},
        "paths": current_paths(catalog, match.artifact_id),
        "note": "Extracted text is a navigation aid, not a quotation: tables and layout do not survive extraction. "
                "Open the PDF at this page for the authoritative content.",
    }
