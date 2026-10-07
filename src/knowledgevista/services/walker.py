"""Walk a root and list what is there, without reading any file's content.

Two things this must get right, because getting them wrong looks like data loss:

  * A directory that cannot be listed is NOT empty. Everything below it is unseen, not gone, so the walker returns
    the directory in `unreadable_dirs` and the scan leaves those locations alone (marks them inaccessible, never missing).
  * Symlinks and junctions are not followed. Following one can walk out of the configured root, or loop. Other reparse
    points (OneDrive placeholders, for example) are NOT skipped: they are ordinary files and directories to the user.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from knowledgevista.domain.pathkeys import fs_path, path_key

# Windows reparse tags for the two kinds that redirect the walk elsewhere.
_IO_REPARSE_TAG_SYMLINK = 0xA000000C
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


@dataclass(frozen=True)
class Entry:
    relative_path: str  # '/'-separated, as the filesystem spelled it
    key: str
    size: int
    mtime_ns: int


@dataclass
class WalkResult:
    entries: list[Entry] = field(default_factory=list)
    unreadable_dirs: list[str] = field(default_factory=list)  # relative, '/'-separated, '' for the root itself
    skipped_links: list[str] = field(default_factory=list)
    key_collisions: list[str] = field(default_factory=list)


def _is_redirecting_link(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    tag = getattr(info, "st_reparse_tag", 0)
    return tag in (_IO_REPARSE_TAG_SYMLINK, _IO_REPARSE_TAG_MOUNT_POINT)


def walk_tree(
    root: str,
    *,
    scandir: Callable[[str], Iterator[os.DirEntry]] = os.scandir,
    case_sensitive: bool | None = None,
) -> WalkResult:
    """List every regular file under `root`. `scandir` is injectable so a test can make one directory unreadable
    (a real permission error is hard to create portably on Windows)."""
    result = WalkResult()
    seen_keys: set[str] = set()
    stack: list[str] = [""]  # relative directory paths, '' = root
    while stack:
        relative_dir = stack.pop()
        directory = os.path.join(root, *relative_dir.split("/")) if relative_dir else root
        try:
            with scandir(fs_path(directory)) as iterator:
                children = sorted(iterator, key=lambda child: child.name)
        except OSError:
            result.unreadable_dirs.append(relative_dir)
            continue
        for child in children:
            relative = f"{relative_dir}/{child.name}" if relative_dir else child.name
            try:
                info = child.stat(follow_symlinks=False)
            except OSError:
                # We saw the name but could not stat it: treat the PARENT as partly unseen rather than guessing.
                result.unreadable_dirs.append(relative_dir)
                continue
            if _is_redirecting_link(info):
                result.skipped_links.append(relative)
                continue
            if stat.S_ISDIR(info.st_mode):
                stack.append(relative)
            elif stat.S_ISREG(info.st_mode):
                key = path_key(relative, case_sensitive=case_sensitive)
                if key in seen_keys:
                    result.key_collisions.append(relative)
                    continue
                seen_keys.add(key)
                result.entries.append(Entry(relative, key, info.st_size, info.st_mtime_ns))
    result.entries.sort(key=lambda entry: entry.key)
    return result
