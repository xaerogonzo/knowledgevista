from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_repo_safe", Path(__file__).resolve().parent.parent / "tools" / "check_repo_safe.py"
)
tool = importlib.util.module_from_spec(_SPEC)
sys.modules["check_repo_safe"] = tool  # a dataclass resolves its own module through sys.modules
_SPEC.loader.exec_module(tool)

# Built from pieces, as in the tool, so this file never contains the strings the tool forbids.
WIN_PROFILE = "C" + ":\\" + "Users" + "\\" + "someone" + "\\" + "papers"
MAC_HOME = "/" + "Users" + "/" + "someone" + "/papers"


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    return tmp_path


def _rules(result):
    return {violation.rule for violation in result.violations}


def test_a_clean_repo_passes_and_reports_how_much_it_looked_at(tmp_path):
    root = _repo(tmp_path)
    (root / "README.md").write_text("Point it at your folder, e.g. D:\\Research.\n")
    result = tool.check(root)
    assert result.files_checked >= 2, "the oracle must be alive: a check that saw nothing proves nothing"
    assert result.violations == []


@pytest.mark.parametrize(
    ("name", "content", "rule"),
    [
        ("catalog.sqlite", b"x", "forbidden-file-type"),
        ("backups/catalog.kv-backup", b"x", "forbidden-file-type"),
        ("data.db", b"x", "forbidden-file-type"),
        (".env", b"KEY=1", "forbidden-file-name"),
        ("papers/real.pdf", b"%PDF-1.7\n", "pdf-outside-fixtures"),
        ("notes.md", ("see " + WIN_PROFILE).encode(), "personal-path"),
        ("notes2.md", ("see " + MAC_HOME).encode(), "personal-path"),
    ],
)
def test_each_forbidden_thing_is_caught_by_its_own_rule(tmp_path, name, content, rule):
    root = _repo(tmp_path)
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    assert rule in _rules(tool.check(root)), f"{name} was not flagged as {rule}"


def test_fixture_pdf_directory_is_allowed_until_it_gets_big(tmp_path):
    root = _repo(tmp_path)
    fixtures = root / "tests" / "fixtures" / "static"
    fixtures.mkdir(parents=True)
    (fixtures / "small.pdf").write_bytes(b"%PDF-1.7\n" + b"0" * 1000)
    assert tool.check(root).violations == []
    (fixtures / "big.pdf").write_bytes(b"%PDF-1.7\n" + b"0" * (tool.MAX_FIXTURE_PDF_BYTES + 1))
    assert _rules(tool.check(root)) == {"oversized-fixture-pdf"}


def test_oversized_non_pdf_file_is_caught(tmp_path):
    root = _repo(tmp_path)
    (root / "blob.bin").write_bytes(b"\0" * (tool.MAX_FILE_BYTES + 1))
    assert _rules(tool.check(root)) == {"oversized-file"}


def test_ignored_files_are_not_examined(tmp_path):
    root = _repo(tmp_path)
    (root / "ignored.txt").write_text("see " + WIN_PROFILE)
    assert tool.check(root).violations == []


def test_public_and_placeholder_paths_are_not_personal(tmp_path):
    root = _repo(tmp_path)
    placeholders = [
        "C" + ":\\" + "Users" + "\\" + "Public" + "\\docs",
        "C" + ":\\" + "Users" + "\\" + "<you>" + "\\docs",
        "/" + "home" + "/" + "runner" + "/work",
        "C" + ":\\" + "Users" + "\\" + "me" + "`",
        "C" + ":\\" + "Users" + "\\" + "..." + "`",
        "/" + "Users" + "/" + "username" + "/papers",
    ]
    (root / "docs.md").write_text("\n".join(placeholders))
    assert tool.check(root).violations == []


def test_maintainer_forbidden_strings_come_from_the_environment(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    (root / "a.md").write_text("mentions Private Nickname here")
    assert tool.check(root).violations == []
    monkeypatch.setenv("KV_SAFE_REPO_FORBID", "Private Nickname")
    assert _rules(tool.check(root)) == {"forbidden-string"}


def test_main_returns_nonzero_on_violation_and_on_an_empty_repo(tmp_path, capsys):
    root = _repo(tmp_path)
    (root / ".gitignore").unlink()  # an empty working tree: nothing was examined
    assert tool.main([str(root)]) == 2
    (root / "x.sqlite").write_bytes(b"x")
    assert tool.main([str(root)]) == 1
    capsys.readouterr()


def test_this_repository_is_itself_safe():
    root = Path(__file__).resolve().parent.parent
    result = tool.check(root)
    assert result.files_checked > 10
    assert result.violations == [], "\n".join(map(str, result.violations))
