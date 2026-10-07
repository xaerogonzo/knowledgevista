"""Pagination cursors: an opaque token that says "continue THIS listing from HERE, if nothing has changed since".

A cursor carries four things (docs/CLI_CONTRACT.md, "Cursors"):

  * the catalog REVISION it was issued at. Any meaningful write moves the revision, and a cursor from before one is
    `KV_CURSOR_STALE`: the listing it points into may have gained, lost or reordered rows, so continuing it could skip or
    repeat items and call the result a continuation. Restarting costs one request; a silently wrong page costs trust.
  * the command and a SIGNATURE of the arguments that shaped the listing (the query, the filters, never the page size). A cursor
    from `relations list --kind related_to` handed to `search` is a mistake, not a stale listing, and says so.
  * the OFFSET to resume at and the KEY of the last item already returned. With the revision unchanged the offset alone is
    exact, so the key is a second, independent check that the item just before the offset is still the one that was last
    seen; if it is not, the cursor is stale whatever the revision says (a listing that depends on something the revision
    does not cover, such as which roots are online).

The token is base64url JSON with a short checksum, so a truncated or hand-edited one is `KV_INVALID_ARGUMENTS` rather than a
confusing parse error. It is not a secret and not signed: nothing is gained by forging one, since the worst a forged cursor
can do is page a listing the caller may already read.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from knowledgevista.errors import ErrorCode, KvError

CURSOR_VERSION = 1
#: Nobody pages through more than this in one listing; also bounds what a single request may ask a service to materialise.
MAX_WINDOW = 5000
MAX_LIMIT = 1000


@dataclass(frozen=True)
class Cursor:
    command: str
    signature: str
    revision: int
    offset: int
    after: str | None


def signature_of(*parts: Any) -> str:
    """A short, stable fingerprint of the arguments that decide what a listing contains."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=True, default=str).encode()).hexdigest()[:16]


def _checksum(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()[:8]


def encode(cursor: Cursor) -> str:
    body = json.dumps({"v": CURSOR_VERSION, "c": cursor.command, "s": cursor.signature, "r": cursor.revision, "o": cursor.offset, "a": cursor.after},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return base64.urlsafe_b64encode(f"{_checksum(body)}.{body}".encode()).decode().rstrip("=")


def decode(token: str) -> Cursor:
    def bad(why: str) -> KvError:
        return KvError(ErrorCode.INVALID_ARGUMENTS, f"That is not a cursor this program issued ({why}). Cursors come from `next_cursor`; do not edit them.")

    try:
        text = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")
        checksum, _, body = text.partition(".")
        if not body or checksum != _checksum(body):
            raise bad("damaged")
        data = json.loads(body)
    except KvError:
        raise
    except Exception as exc:  # noqa: BLE001 - any decoding failure is the same answer
        raise bad("unreadable") from exc
    if data.get("v") != CURSOR_VERSION:
        raise bad(f"cursor version {data.get('v')!r}, this program reads {CURSOR_VERSION}")
    try:
        offset, revision = int(data["o"]), int(data["r"])
        if offset < 0 or offset > MAX_WINDOW:
            raise ValueError
        return Cursor(str(data["c"]), str(data["s"]), revision, offset, data.get("a"))
    except (KeyError, TypeError, ValueError) as exc:
        raise bad("incomplete") from exc


def bounded_limit(value: int | None, default: int, *, maximum: int = MAX_LIMIT) -> int:
    """A page size, refused rather than clamped: an assistant asking for 10,000 should be told the cap, not given 1,000 and left
    to count."""
    limit = default if value is None else value
    if limit < 1 or limit > maximum:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"--limit must be between 1 and {maximum}.", {"limit": limit, "maximum": maximum})
    return limit


def start_offset(token: str | None, *, command: str, signature: str, revision: int, keys: Sequence[str | None] | None = None,
                 fallback_offset: int = 0) -> int:
    """Where to start. No cursor: `fallback_offset` (an explicit --offset, else 0). With one: its offset, after checking it
    belongs to this listing, was issued at this revision, and still has the same item just before the offset.

    `keys` is the full ordered list of item keys when the caller has it; pass None when only a window is available and the key
    check will be done by `check_anchor`."""
    if token is None:
        if fallback_offset < 0 or fallback_offset > MAX_WINDOW:
            raise KvError(ErrorCode.INVALID_ARGUMENTS, f"--offset must be between 0 and {MAX_WINDOW}.")
        return fallback_offset
    cursor = decode(token)
    if cursor.command != command or cursor.signature != signature:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, "That cursor belongs to a different command or different arguments; start the listing again without it.",
                      {"cursor_command": cursor.command, "command": command})
    if cursor.revision != revision:
        raise KvError(ErrorCode.CURSOR_STALE, "The catalog changed since this cursor was issued, so continuing could skip or repeat items. Run the command again without --cursor.",
                      {"cursor_revision": cursor.revision, "catalog_revision": revision})
    if keys is not None:
        check_anchor(cursor, keys)
    return cursor.offset


def check_anchor(cursor: Cursor, keys: Sequence[str | None]) -> None:
    """The item just before the offset must be the one the cursor says was last returned."""
    if cursor.offset == 0:
        return
    if cursor.offset > len(keys) or keys[cursor.offset - 1] != cursor.after:
        raise KvError(ErrorCode.CURSOR_STALE, "The listing is no longer in the order this cursor was issued in. Run the command again without --cursor.",
                      {"offset": cursor.offset})


def next_cursor(*, command: str, signature: str, revision: int, offset: int, shown: int, last_key: str | None, more: bool) -> str | None:
    """The token for the page after this one, or None when this was the last."""
    if not more or shown == 0 or offset + shown > MAX_WINDOW:
        return None  # past the window nobody should be paging: the caller narrows the query instead
    return encode(Cursor(command, signature, revision, offset + shown, last_key))
