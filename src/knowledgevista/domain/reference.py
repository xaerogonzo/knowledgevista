"""`knowledgevista://` references: a stable, portable way for another program (OpenChem's literature files, a coding assistant,
a note) to name a document, an artifact or a page. Pure: no catalog, no file, no network.

    knowledgevista://document/<document id>
    knowledgevista://document/<document id>/page/<pdf page>        physical position, counted from 1
    knowledgevista://document/<document id>/label/<printed label>  the number printed on the page (percent-encoded)
    knowledgevista://artifact/<sha256>
    knowledgevista://artifact/<sha256>/page/<pdf page>
    knowledgevista://artifact/<sha256>/label/<printed label>

The grammar is strict on purpose. An id is the exact lowercase hex the catalog stores (a document id is 32 characters, an
artifact is its SHA-256), so a reference can never be a prefix or a file name: guessing belongs to `resolve_reference`, not
to an address. A page is `page` OR `label`, never a bare number, because a physical position and a printed label only
coincide by luck (docs/SEARCH.md). There is no query string and no fragment, so nothing can be smuggled after the address,
and the same address has exactly one spelling (`format(parse(text))` is canonical), which is what lets a stored reference be
compared as text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

SCHEME = "knowledgevista"
MAX_LABEL = 64
MAX_PAGE = 1_000_000
_DOCUMENT_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PAGE = re.compile(r"^[1-9][0-9]{0,6}$")


class ReferenceError(ValueError):
    """The text is not a Knowledge Vista reference; the message says which part is wrong."""


@dataclass(frozen=True)
class Reference:
    kind: str  # "document" | "artifact"
    id: str
    pdf_page: int | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("document", "artifact"):
            raise ReferenceError(f"unknown reference kind {self.kind!r}")
        if not (_DOCUMENT_ID if self.kind == "document" else _SHA256).match(self.id):
            raise ReferenceError(f"{self.id!r} is not a {'document id (32 lowercase hex characters)' if self.kind == 'document' else 'SHA-256 (64 lowercase hex characters)'}")
        if self.pdf_page is not None and self.label is not None:
            raise ReferenceError("a reference names a pdf page OR a printed label, never both")
        if self.pdf_page is not None and not 1 <= self.pdf_page <= MAX_PAGE:
            raise ReferenceError(f"pdf page must be between 1 and {MAX_PAGE}")
        if self.label is not None and not _label_ok(self.label):
            raise ReferenceError(f"a printed label must be 1 to {MAX_LABEL} characters with no control characters")

    def format(self) -> str:
        base = f"{SCHEME}://{self.kind}/{self.id}"
        if self.pdf_page is not None:
            return f"{base}/page/{self.pdf_page}"
        if self.label is not None:
            return f"{base}/label/{quote(self.label, safe='')}"
        return base

    def without_page(self) -> Reference:
        return Reference(self.kind, self.id)


def _label_ok(label: str) -> bool:
    return 1 <= len(label) <= MAX_LABEL and not any(ord(c) < 32 or ord(c) == 127 for c in label) and label == label.strip()


def is_reference(text: str) -> bool:
    """Whether the text is written as a reference at all (its scheme), without saying whether it is a valid one."""
    return (text or "").strip().lower().startswith(SCHEME + ":")


def parse(text: str) -> Reference:
    """The reference `text` names, or `ReferenceError`. The scheme is case-insensitive; ids are accepted in any case and
    canonicalised to lowercase; everything else must already be exact."""
    raw = (text or "").strip()
    prefix = SCHEME + "://"
    if not raw.lower().startswith(prefix):
        raise ReferenceError(f"a reference starts with {prefix}")
    if "?" in raw or "#" in raw:
        raise ReferenceError("a reference has no query string or fragment")
    parts = raw[len(prefix):].split("/")
    if len(parts) < 2 or parts[0] not in ("document", "artifact"):
        raise ReferenceError("expected knowledgevista://document/<id> or knowledgevista://artifact/<sha256>")
    kind, identifier = parts[0], parts[1].lower()
    rest = parts[2:]
    if not rest:
        return Reference(kind, identifier)
    if len(rest) != 2 or rest[0] not in ("page", "label"):
        raise ReferenceError("after the id, expected /page/<pdf page> or /label/<printed label>")
    if rest[0] == "page":
        if not _PAGE.match(rest[1]):
            raise ReferenceError(f"{rest[1]!r} is not a pdf page number (counted from 1, no leading zeros)")
        return Reference(kind, identifier, pdf_page=int(rest[1]))
    try:
        label = unquote(rest[1], errors="strict")
    except UnicodeDecodeError as exc:
        raise ReferenceError("the label is not valid UTF-8 once decoded") from exc
    if quote(label, safe="").lower() != rest[1].lower():  # %2f and %2F are the same spelling; a stray % or an unescaped / is not
        raise ReferenceError("the label is not percent-encoded (a / # ? % or space inside it must be written as %XX)")
    return Reference(kind, identifier, label=label)


def format_reference(kind: str, identifier: str, *, pdf_page: int | None = None, label: str | None = None) -> str:
    return Reference(kind, identifier, pdf_page, label).format()
