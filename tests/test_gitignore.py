"""The repository's REAL .gitignore, asked about realistic local files with `git check-ignore`.

A rule list that reads well proves nothing: a wrong pattern, or a negation that re-includes too much, only shows
up when git is asked about an actual path. So this copies the real file into a scratch repository and asks.
Paths are built to match what this project really produces (a catalog next to a library, a WAL sidecar, a backup,
a per-checkout Claude worktree), including the case-variants Windows makes likely.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

MUST_BE_IGNORED = [
    "catalog.sqlite",
    "catalog.sqlite-wal",
    "catalog.sqlite-shm",
    "catalog.sqlite-journal",
    "Sci Downloads.index.sqlite",
    "data/catalog.db",
    "backups/catalog.v1-to-v2.20261006T120000.kv-backup",
    "papers/some paper.pdf",
    "papers/SHOUTING.PDF",
    "tests/fixtures/generated/big.pdf",  # only tests/fixtures/static/ may hold a committed PDF
    ".env",
    ".env.local",
    ".kv/anything",
    "kv-cache/extractions/a.json",
    "kv-private/manifest.json",
    ".tokensave/tokensave.db",
    ".codegraph/x",
    ".venv/Scripts/python.exe",
    "src/knowledgevista/__pycache__/x.cpython-313.pyc",
    "debug.log",
    "scratch/notes.txt",
    ".claude/worktrees/some-branch/file.py",
    ".claude/settings.local.json",
    ".mcp.json",
    ".idea/workspace.xml",
    ".vscode/settings.json",
    "kv.code-workspace",
    "dist/kv.exe",
    "build/lib/x.py",
    "Thumbs.db",
]

MUST_NOT_BE_IGNORED = [
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "uv.lock",
    "src/knowledgevista/cli.py",
    "src/knowledgevista/db/schema/0001_library.sql",
    "tests/pdfbuilders.py",
    "tests/fixtures/static/tiny.pdf",
    "docs/ARCHITECTURE.md",
    ".github/workflows/ci.yml",
    ".gitignore",
    "CLAUDE.md",
    "BASIC_INSTRUCTIONS.md",
    ".claude/commands/shared-command.md",  # project-level, shareable Claude files are not blanket-ignored
]


@pytest.fixture(scope="module")
def scratch_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("ignore-check")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    shutil.copy2(ROOT / ".gitignore", repo / ".gitignore")
    return repo


def _ignored(repo: Path, relative: str) -> bool:
    """True if git would ignore the path. check-ignore exits 0 for ignored, 1 for not ignored, 128 on error."""
    done = subprocess.run(["git", "check-ignore", "-q", "--no-index", relative], cwd=repo, capture_output=True)
    assert done.returncode in (0, 1), done.stderr.decode(errors="replace")
    return done.returncode == 0


def test_the_oracle_is_alive_a_normal_file_is_not_ignored(scratch_repo):
    # If git failed in a way that made everything look ignored, the lists below would pass vacuously.
    assert not _ignored(scratch_repo, "README.md")


@pytest.mark.parametrize("relative", MUST_BE_IGNORED)
def test_local_and_private_files_are_ignored(scratch_repo, relative):
    assert _ignored(scratch_repo, relative), f"{relative} would be committed"


@pytest.mark.parametrize("relative", MUST_NOT_BE_IGNORED)
def test_project_files_are_not_ignored(scratch_repo, relative):
    assert not _ignored(scratch_repo, relative), f"{relative} is ignored and would be silently left out of the repo"
