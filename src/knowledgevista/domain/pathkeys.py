"""Path comparison keys and Windows path handling: pure string logic, with the platform rules made explicit.

TWO REPRESENTATIONS, NEVER CONFUSED. A location keeps the path as the filesystem spelled it (what the user sees)
and, separately, a comparison KEY (what decides whether two paths are the same file). The key is never shown.

WHY `.lower()` AND NOT `.casefold()`. On a case-insensitive filesystem `Foo.pdf` and `foo.pdf` are one file, so the
key lower-cases. `casefold()` would also fold `ß` to `ss`, but NTFS keeps `ß.pdf` and `ss.pdf` as two different
files; a key that merged them would turn two real files into a UNIQUE-constraint crash. Where a key still collides
(Unicode corner cases), the scanner reports it and skips the second file rather than guessing.

NO Unicode NORMALISATION in the key. NTFS stores `é` (one code point) and `é` (e + combining accent) as different
names, so folding them would conflate files the filesystem keeps apart.
"""

from __future__ import annotations

import os
import sys

_EXTENDED = "\\\\?\\"
_EXTENDED_UNC = "\\\\?\\UNC\\"
#: Windows rejects ordinary paths of 260+ characters; switch to the extended-length form well before that.
_LONG_PATH_THRESHOLD = 240


def default_case_sensitive() -> bool:
    return not (sys.platform.startswith("win") or sys.platform == "darwin")


def path_key(relative: str, *, case_sensitive: bool | None = None) -> str:
    sensitive = default_case_sensitive() if case_sensitive is None else case_sensitive
    return relative if sensitive else relative.lower()


def strip_extended_prefix(path: str) -> str:
    """`\\\\?\\C:\\x` -> `C:\\x` and `\\\\?\\UNC\\srv\\share` -> `\\\\srv\\share`: the display form of a path."""
    if path.startswith(_EXTENDED_UNC):
        return "\\\\" + path[len(_EXTENDED_UNC):]
    if path.startswith(_EXTENDED):
        return path[len(_EXTENDED):]
    return path


def normalise_root(path: str, *, case_sensitive: bool | None = None) -> tuple[str, str]:
    """(display path, comparison key) for a configured root: absolute, no trailing separator, no extended prefix."""
    display = os.path.abspath(strip_extended_prefix(os.fspath(path)))
    sensitive = default_case_sensitive() if case_sensitive is None else case_sensitive
    key = display if sensitive else os.path.normcase(display)
    return display, key


def is_within(root_key: str, other_key: str) -> bool:
    """Whether `other_key` is `root_key` or below it, on a path boundary (so `C:\\lib` does not contain `C:\\library`)."""
    if other_key == root_key:
        return True
    return other_key.startswith(root_key.rstrip("\\/") + os.sep)


def overlaps(key_a: str, key_b: str) -> bool:
    return is_within(key_a, key_b) or is_within(key_b, key_a)


def fs_path(path: str) -> str:
    """The form to hand to the OS: on Windows a long absolute path gets the extended-length prefix, otherwise the
    open, stat and scandir calls fail for a legitimate deep path while the scanner claims to support it."""
    if os.name != "nt" or path.startswith(_EXTENDED) or len(path) < _LONG_PATH_THRESHOLD:
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\"):
        return _EXTENDED_UNC + absolute[2:]
    return _EXTENDED + absolute


def join_relative(root: str, relative: str) -> str:
    """Root plus a stored relative path (always '/'-separated) as an OS path."""
    return os.path.join(root, *relative.split("/")) if relative else root
