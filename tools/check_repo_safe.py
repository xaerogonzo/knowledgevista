"""Fail if the repository contains something a public repository must never carry.

A catalog is a person's reading list, a corpus is other people's copyrighted work, and a path with a username in it
is personal information. None of that belongs in git, and ".gitignore covers it" is not a check: a rule can be
overridden by `git add -f`, and a fixture is easy to commit by accident. This runs in CI over every file git would
commit (tracked plus untracked-and-not-ignored).

    python tools/check_repo_safe.py
    KV_SAFE_REPO_FORBID="Alexs Folder,my-nickname" python tools/check_repo_safe.py   # maintainer-only extra strings

The extra strings come from the environment on purpose: writing a private name into this file would publish it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_BYTES = 1_000_000
MAX_FIXTURE_PDF_BYTES = 200_000
STATIC_FIXTURE_DIR = "tests/fixtures/static/"

#: Never committed, whatever their size: a catalog, a catalog backup, an SQLite sidecar.
FORBIDDEN_SUFFIXES = (".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal", ".db", ".kv-backup")
FORBIDDEN_NAMES = (".env",)

# Built from pieces so this file does not contain what it forbids and so cannot flag itself.
_SEP = r"[\\/]"
#: Names that are obviously placeholders in documentation, never a real person's profile folder.
_PLACEHOLDER = r"(?!<|%|\.\.\.|(?:me|you|user|username|name|yourname|youruser)\b)"
_NAME = r"[^\s\\/\"'`<>]+"
_PERSONAL_PATHS = {
    "windows user profile": re.compile(
        r"\b[A-Za-z]:" + _SEP + "Users" + _SEP + r"(?!Public\b|Default\b)" + _PLACEHOLDER + _NAME
    ),
    "macOS home directory": re.compile(r"/" + "Users" + r"/(?!Shared\b)" + _PLACEHOLDER + _NAME),
    "linux home directory": re.compile(r"/" + "home" + r"/(?!runner\b)" + _PLACEHOLDER + _NAME),
}


@dataclass(frozen=True)
class Violation:
    path: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}: {self.rule}: {self.detail}"


@dataclass(frozen=True)
class Result:
    files_checked: int
    violations: list[Violation]


def candidate_files(root: Path) -> list[str]:
    """Every file git would commit: tracked, plus untracked and not ignored."""
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root, capture_output=True, check=True,
    ).stdout.decode("utf-8", errors="replace")
    return sorted({name for name in output.split("\0") if name and (root / name).is_file()})


def _extra_forbidden() -> list[str]:
    return [item.strip() for item in os.environ.get("KV_SAFE_REPO_FORBID", "").split(",") if item.strip()]


def check(root: Path, files: list[str] | None = None) -> Result:
    root = Path(root)
    names = files if files is not None else candidate_files(root)
    extra = _extra_forbidden()
    found: list[Violation] = []
    for name in names:
        path = root / name
        lowered = name.lower().replace("\\", "/")
        size = path.stat().st_size

        if lowered.endswith(FORBIDDEN_SUFFIXES):
            found.append(Violation(name, "forbidden-file-type", "a catalog, backup or database must never be committed"))
        if lowered.rsplit("/", 1)[-1] in FORBIDDEN_NAMES:
            found.append(Violation(name, "forbidden-file-name", "local secrets and configuration are not committed"))
        if lowered.endswith(".pdf"):
            if not lowered.startswith(STATIC_FIXTURE_DIR):
                found.append(Violation(name, "pdf-outside-fixtures", f"PDFs are only allowed under {STATIC_FIXTURE_DIR}"))
            elif size > MAX_FIXTURE_PDF_BYTES:
                found.append(Violation(name, "oversized-fixture-pdf", f"{size} bytes > {MAX_FIXTURE_PDF_BYTES}"))
        elif size > MAX_FILE_BYTES:
            found.append(Violation(name, "oversized-file", f"{size} bytes > {MAX_FILE_BYTES}"))

        data = path.read_bytes() if size <= MAX_FILE_BYTES else b""
        if b"\0" in data:
            continue  # binary: no text to scan
        text = data.decode("utf-8", errors="replace")
        for label, pattern in _PERSONAL_PATHS.items():
            match = pattern.search(text)
            if match:
                found.append(Violation(name, "personal-path", f"{label}: {match.group(0)!r}"))
        for needle in extra:
            if needle in text or needle in name:
                found.append(Violation(name, "forbidden-string", "contains a maintainer-configured forbidden string"))
    return Result(len(names), found)


def main(argv: list[str] | None = None) -> int:
    root = Path(argv[0]) if argv else Path(__file__).resolve().parent.parent
    result = check(root)
    if result.files_checked == 0:
        # A check that looked at nothing is not a pass (tests-that-pass-without-testing.md, section 1).
        print("check_repo_safe: no files were examined; is this a git repository?", file=sys.stderr)
        return 2
    for violation in result.violations:
        print(violation)
    print(f"check_repo_safe: {result.files_checked} files examined, {len(result.violations)} violation(s)")
    return 1 if result.violations else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
