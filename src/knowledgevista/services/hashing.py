"""Hash a file and say whether the answer can be trusted.

A file can change while it is being read (a download finishing, an editor saving). Hashing it anyway would commit
an identity for bytes that never existed together. So: stat BEFORE, read, stat AFTER, and compare. Anything that
differs means `changing`: nothing is committed and the next scan tries again. The stat taken before the read is the
one handed back for storage, so if the file changes right after this returns, the next scan sees a mismatch and
re-reads it (the freshness hint can only err toward re-reading, never toward trusting stale bytes).

`size + mtime` is a freshness hint that lets a scan SKIP hashing. It is never identity proof: this module is where
identity is actually established.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

BLOCK = 1 << 20
HEAD_BYTES = 4096


@dataclass(frozen=True)
class HashOutcome:
    kind: str  # "ok" | "changing" | "unreadable"
    sha256: str | None = None
    size: int | None = None
    mtime_ns: int | None = None
    head: bytes = b""
    seconds: float = 0.0
    error: str | None = None


def hash_file(path: str, *, mid_read: Callable[[], None] | None = None) -> HashOutcome:
    """`mid_read` is a test seam: called once after the first block is read, so a test can modify the file at the
    exact moment a real writer would."""
    started = time.perf_counter()
    try:
        before = os.stat(path)
        digest = hashlib.sha256()
        head = b""
        total = 0
        with open(path, "rb") as handle:
            first = True
            while True:
                block = handle.read(BLOCK)
                if not block:
                    break
                if first:
                    head = block[:HEAD_BYTES]
                    first = False
                    if mid_read is not None:
                        mid_read()
                digest.update(block)
                total += len(block)
        after = os.stat(path)
    except OSError as exc:
        return HashOutcome("unreadable", error=f"{type(exc).__name__}: {exc}"[:300], seconds=time.perf_counter() - started)
    elapsed = time.perf_counter() - started
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or total != before.st_size:
        return HashOutcome("changing", size=before.st_size, mtime_ns=before.st_mtime_ns, seconds=elapsed)
    return HashOutcome("ok", digest.hexdigest(), before.st_size, before.st_mtime_ns, head, elapsed)
