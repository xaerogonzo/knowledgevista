"""The filesystem primitives: each is a refusal, so each test is an attempt to make it do the one thing it must never do."""

from __future__ import annotations

import errno
import os
import sys

import pytest

from knowledgevista.services import organizer_fs as fs

WINDOWS = sys.platform.startswith("win")


def test_a_move_never_replaces_what_is_at_the_destination(tmp_path):
    (tmp_path / "src").write_text("mine")
    (tmp_path / "dst").write_text("somebody else's work")
    with pytest.raises(fs.Refused) as caught:
        fs.move_no_overwrite(str(tmp_path / "src"), str(tmp_path / "dst"))
    assert caught.value.kind == "exists"
    assert (tmp_path / "dst").read_text() == "somebody else's work" and (tmp_path / "src").read_text() == "mine"


def test_a_move_of_a_missing_file_says_missing_and_a_good_move_moves_exactly_one_file(tmp_path):
    with pytest.raises(fs.Refused) as caught:
        fs.move_no_overwrite(str(tmp_path / "nothing"), str(tmp_path / "dst"))
    assert caught.value.kind == "missing"
    (tmp_path / "src").write_text("x")
    fs.move_no_overwrite(str(tmp_path / "src"), str(tmp_path / "dst"))
    assert sorted(os.listdir(tmp_path)) == ["dst"]


def test_a_locked_file_is_retried_with_the_documented_delays_then_reported_locked(tmp_path, monkeypatch):
    delays, attempts = [], []

    def locked(src, dst):
        attempts.append(1)
        raise PermissionError(13, "in use", None, 32)

    monkeypatch.setattr(os, "rename", locked)
    monkeypatch.setattr(os, "link", locked)
    (tmp_path / "src").write_text("x")
    with pytest.raises(fs.Refused) as caught:
        fs.move_no_overwrite(str(tmp_path / "src"), str(tmp_path / "dst"), sleep=delays.append)
    assert caught.value.kind == "locked" and len(attempts) == 1 + len(fs.RETRY_DELAYS) and delays == list(fs.RETRY_DELAYS)


def test_an_error_that_is_not_a_lock_is_not_retried(tmp_path, monkeypatch):
    attempts = []

    def broken(src, dst):
        attempts.append(1)
        raise OSError(errno.EIO, "disk on fire")

    monkeypatch.setattr(os, "rename", broken)
    monkeypatch.setattr(os, "link", broken)
    (tmp_path / "src").write_text("x")
    with pytest.raises(fs.Refused) as caught:
        fs.move_no_overwrite(str(tmp_path / "src"), str(tmp_path / "dst"), sleep=lambda s: pytest.fail("slept"))
    assert caught.value.kind == "other" and len(attempts) == 1


@pytest.mark.parametrize(("exc", "kind"), [
    (PermissionError(13, "x", None, 32), "locked"), (PermissionError(13, "x", None, 33), "locked"), (PermissionError(13, "x", None, 5), "permission"),
    (FileExistsError(17, "x"), "exists"), (OSError(80, "x", None, 80), "exists"), (FileNotFoundError(2, "x"), "missing"), (OSError(errno.EIO, "x"), "other"),
])
def test_an_oserror_becomes_one_of_five_answers(exc, kind):
    assert fs.classify(exc) == kind


def test_a_case_only_rename_goes_through_a_temporary_name_that_is_announced_first(tmp_path):
    (tmp_path / "Foo.pdf").write_text("x")
    announced = []
    steps = fs.safe_rename(str(tmp_path / "Foo.pdf"), str(tmp_path / "foo.pdf"), token="7", before_step=announced.append)
    assert os.listdir(tmp_path) == ["foo.pdf"] and steps == announced == [str(tmp_path / ".kv-moving-7")]


def test_an_ordinary_rename_uses_no_temporary_name(tmp_path):
    (tmp_path / "a.pdf").write_text("x")
    assert fs.safe_rename(str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf"), token="1", before_step=lambda p: pytest.fail("announced")) == []
    assert os.listdir(tmp_path) == ["b.pdf"]


def test_a_rename_that_would_land_on_an_existing_file_leaves_both_alone(tmp_path):
    (tmp_path / "a.pdf").write_text("a")
    (tmp_path / "b.pdf").write_text("b")
    with pytest.raises(fs.Refused):
        fs.safe_rename(str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf"), token="1")
    assert (tmp_path / "a.pdf").read_text() == "a" and (tmp_path / "b.pdf").read_text() == "b"


def test_names_that_differ_only_in_case_or_unicode_form_are_recognised():
    assert fs.same_name_ignoring_case_and_form("Foo.pdf", "foo.PDF")
    assert fs.same_name_ignoring_case_and_form("Cafe\u0301.pdf", "Caf\u00e9.pdf")
    assert not fs.same_name_ignoring_case_and_form("a.pdf", "b.pdf")


def test_directories_are_made_only_where_missing_and_removed_only_when_empty(tmp_path):
    made = fs.make_directories(str(tmp_path / "x" / "y" / "z"), str(tmp_path))
    assert made == [str(tmp_path / "x"), str(tmp_path / "x" / "y"), str(tmp_path / "x" / "y" / "z")]
    assert fs.make_directories(str(tmp_path / "x" / "y"), str(tmp_path)) == []  # already there: nothing to record, so nothing to undo
    (tmp_path / "x" / "y" / "z" / "keep.txt").write_text("k")
    assert fs.remove_if_empty(str(tmp_path / "x" / "y" / "z")) is False and fs.remove_if_empty(str(tmp_path / "nowhere")) is False
    (tmp_path / "x" / "y" / "z" / "keep.txt").unlink()
    assert fs.remove_if_empty(str(tmp_path / "x" / "y" / "z")) is True


def test_containment_refuses_dot_dot_and_accepts_ordinary_paths(tmp_path):
    root = tmp_path / "lib"
    (root / "sub").mkdir(parents=True)
    assert fs.contained(str(root), "sub/a.pdf") and fs.contained(str(root), "a.pdf")
    assert not fs.contained(str(root), "../escape.pdf") and not fs.contained(str(root), "sub/../../escape.pdf")
    assert not fs.contained(str(root), "")  # the root itself is not a file inside it


def test_a_symlink_is_a_reparse_point_wherever_it_sits_on_the_path_and_a_ordinary_folder_is_not(tmp_path):
    root = tmp_path / "lib"
    (root / "real").mkdir(parents=True)
    (root / "real" / "a.pdf").write_text("x")
    assert fs.reparse_problem(str(root), "real/a.pdf") is None
    try:
        os.symlink(root / "real", root / "linked", target_is_directory=True)
    except OSError:
        pytest.skip("this account may not create symlinks")
    assert fs.reparse_problem(str(root), "linked/a.pdf") == str(root / "linked")
    assert fs.is_reparse(str(root / "linked")) and not fs.is_reparse(str(root / "real"))


@pytest.mark.skipif(not WINDOWS, reason="junctions are a Windows mechanism")
def test_a_junction_that_leaves_the_root_is_both_a_reparse_point_and_not_contained(tmp_path):
    import _winapi

    root, outside = tmp_path / "lib", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "a.pdf").write_text("x")
    _winapi.CreateJunction(str(outside), str(root / "jump"))
    assert fs.is_reparse(str(root / "jump")) and fs.reparse_problem(str(root), "jump/a.pdf") == str(root / "jump")
    assert fs.contained(str(root), "jump/a.pdf") is False  # resolves to somewhere else


@pytest.mark.skipif(not WINDOWS, reason="UNC and extended-length paths are Windows forms")
def test_unc_and_long_paths_are_composed_and_compared_in_one_form():
    assert fs.absolute("\\\\server\\share\\lib", "a/b.pdf") == "\\\\server\\share\\lib\\a\\b.pdf"
    assert fs.os_path("\\\\server\\share\\lib", "a/b.pdf") == "\\\\server\\share\\lib\\a\\b.pdf"  # short: used as is
    long_relative = "/".join(["x" * 60] * 5) + "/a.pdf"
    assert fs.os_path("\\\\server\\share\\lib", long_relative).startswith("\\\\?\\UNC\\server\\share\\lib\\")
    assert fs.os_path("C:\\lib", long_relative).startswith("\\\\?\\C:\\lib\\")


def test_signature_and_hash_report_the_file_as_it_is(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"abc")
    size, mtime = fs.signature(str(tmp_path / "a.bin"))
    assert size == 3 and mtime > 0
    outcome = fs.hash_path(str(tmp_path / "a.bin"))
    assert outcome.kind == "ok" and outcome.sha256 == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert fs.hash_path(str(tmp_path / "missing")).kind == "unreadable"
