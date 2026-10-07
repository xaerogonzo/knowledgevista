"""The filesystem primitives the organizer is allowed to use, each one a refusal waiting to happen.

Everything here exists to make ONE class of mistake impossible instead of unlikely:

  * `move_no_overwrite` never replaces a file. `os.rename` replaces silently on POSIX and fails on Windows; here both fail, because a
    "move" that destroys the thing at the destination is the one filesystem error this program may never make.
  * `safe_rename` owns the renames that look like no-ops to the filesystem (`Foo.pdf` -> `foo.pdf`, `e` + combining accent -> `e` with
    accent): they go through a temporary name in the same folder, which is journaled BEFORE the first step so an interruption can be undone.
  * `reparse_problem` / `contained` refuse anything that is not an ordinary file under the root: a symlink, a junction or any other
    reparse point on the path (it can point outside the root, and a move through it moves somewhere else), an alternate data stream.
  * `classify` turns an OSError into the five answers the executor acts on, so a locked PDF is "skipped and reported" and never a batch
    failure, and an unexpected error is `other` and never silently retried.

Every function takes plain strings and does one thing; the executor (organizer.py) decides what to do with the answer.
"""

from __future__ import annotations

import errno
import os
import stat
import time
import unicodedata
from collections.abc import Callable

from knowledgevista.domain.pathkeys import fs_path, join_relative, strip_extended_prefix
from knowledgevista.services.hashing import HashOutcome, hash_file

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
#: Windows sharing violation / lock violation: the file is open somewhere (a viewer, an antivirus scan, a sync client).
_LOCKED = {32, 33}
RETRY_DELAYS = (0.05, 0.15, 0.4)  # an antivirus scan holds a file for moments; a person holding it open does not let go in half a second


class Refused(Exception):
    """A primitive refused; `kind` is one of `exists`, `missing`, `locked`, `permission`, `other`."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def absolute(root_path: str, relative: str) -> str:
    return join_relative(root_path, relative)


def os_path(root_path: str, relative: str) -> str:
    """The form to hand to the OS (extended-length on Windows for a long path)."""
    return fs_path(absolute(root_path, relative))


def classify(exc: OSError) -> str:
    winerror = getattr(exc, "winerror", None)
    if winerror in _LOCKED:
        return "locked"
    if isinstance(exc, FileExistsError) or winerror in (80, 183) or exc.errno == errno.EEXIST:
        return "exists"
    if isinstance(exc, FileNotFoundError) or winerror in (2, 3) or exc.errno == errno.ENOENT:
        return "missing"
    if isinstance(exc, PermissionError) or winerror == 5 or exc.errno in (errno.EACCES, errno.EPERM):
        return "permission"
    return "other"


def is_reparse(path: str) -> bool:
    """A symlink, a junction or any reparse point, looked at WITHOUT following it."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def reparse_problem(root_path: str, relative: str) -> str | None:
    """The first component of `relative` (under the root) that is a link of any kind, else None. The root itself is the user's choice."""
    current = root_path
    for part in relative.split("/"):
        current = os.path.join(current, part)
        if is_reparse(fs_path(current)):
            return current
    return None


def contained(root_path: str, relative: str) -> bool:
    """Whether the path really lies under the root once everything is resolved (no `..`, no link that leaves)."""
    root = os.path.normcase(strip_extended_prefix(os.path.realpath(fs_path(root_path))))  # the extended form and the plain one are one path
    target = os.path.normcase(strip_extended_prefix(os.path.realpath(fs_path(absolute(root_path, relative)))))
    return target != root and os.path.commonpath([root, target]) == root


def lexists(path: str) -> bool:
    return os.path.lexists(path)


def same_object(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def same_name_ignoring_case_and_form(a: str, b: str) -> bool:
    """`Foo.pdf`/`foo.pdf`, or two spellings of one accented name: a rename the filesystem may treat as a no-op."""
    return unicodedata.normalize("NFC", a).lower() == unicodedata.normalize("NFC", b).lower()


def hash_path(path: str) -> HashOutcome:
    return hash_file(path)


def signature(path: str) -> tuple[int, int]:
    info = os.stat(path)
    return info.st_size, info.st_mtime_ns


def move_no_overwrite(src: str, dst: str, *, sleep: Callable[[float], None] = time.sleep) -> None:
    """Move `src` to `dst`, never replacing anything. Retries a locked file a few times, briefly (an antivirus scan), then raises Refused."""
    last: OSError | None = None
    for delay in (0.0, *RETRY_DELAYS):
        if delay:
            sleep(delay)
        try:
            if os.name == "nt":
                os.rename(src, dst)  # fails if dst exists
            else:
                os.link(src, dst)  # fails if dst exists; then the old name goes
                os.unlink(src)
            return
        except OSError as exc:
            last = exc
            if classify(exc) != "locked":
                break
    assert last is not None
    raise Refused(classify(last), f"{type(last).__name__}: {last}") from last


def temp_name(directory: str, token: str) -> str:
    """A name in the same folder that cannot collide with a real file: the token is the operation item's id, which is unique."""
    return os.path.join(directory, f".kv-moving-{token}")


def safe_rename(src: str, dst: str, *, token: str, before_step: Callable[[str], None] | None = None,
                sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """Rename `src` to `dst` in the same folder. If the two names are one file to the filesystem (case or Unicode form only), go through a
    temporary name. `before_step(path)` is called with each intermediate path BEFORE the step that creates it, so the caller can journal
    it. Returns the intermediate paths used (empty for an ordinary rename)."""
    directory, source_name = os.path.split(src)
    if os.path.split(dst)[0] == directory and same_name_ignoring_case_and_form(source_name, os.path.split(dst)[1]) and source_name != os.path.split(dst)[1]:
        temp = temp_name(directory, token)
        if before_step is not None:
            before_step(temp)
        move_no_overwrite(src, temp, sleep=sleep)
        move_no_overwrite(temp, dst, sleep=sleep)
        return [temp]
    move_no_overwrite(src, dst, sleep=sleep)
    return []


def make_directories(path: str, stop_at: str) -> list[str]:
    """Create `path` (and parents up to, not including, `stop_at`); returns the directories this call created, outermost first."""
    created: list[str] = []
    missing = []
    current = path
    while current and current != stop_at and not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        os.mkdir(directory)
        created.append(directory)
    return created


def remove_if_empty(path: str) -> bool:
    try:
        os.rmdir(path)
        return True
    except OSError:
        return False
