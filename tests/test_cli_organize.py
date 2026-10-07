"""The organizer through `kv` as a person or a script would drive it: allow, plan, read, apply, undo, history, and the exit codes between."""

from __future__ import annotations

import json
import os
import sys

import pytest
from organizer_support import tree
from support import write
from test_cli import run, run_json

TITLE = "Aqueous Solubility of Invented Esters"
NAME = f"Examplar (2021) - {TITLE}.pdf"


@pytest.fixture
def world(capsys, tmp_path):
    lib = tmp_path / "papers"
    write(lib / "cm4c01978.pdf", "the first paper")
    write(lib / "sub" / "kaya2022.pdf", "the second paper")
    write(lib / "notes.txt", "no title for this one")
    assert run_json(capsys, tmp_path, "root", "add", str(lib), "--label", "papers")[0] == 0
    run_json(capsys, tmp_path, "scan")
    for name, title, year in (("cm4c01978.pdf", TITLE, "2021"), ("kaya2022.pdf", "A Second Study Of Invented Compounds", "2022")):
        run_json(capsys, tmp_path, "metadata", "set", name, "title", title)
        run_json(capsys, tmp_path, "metadata", "set", name, "year", year)
        run_json(capsys, tmp_path, "metadata", "set", name, "authors", "Examplar, A.")
    return tmp_path, lib


def allow(capsys, tmp):
    return run_json(capsys, tmp, "root", "allow-organize", "papers")


def make_plan(capsys, tmp, *extra):
    allow(capsys, tmp)
    code, env, _ = run_json(capsys, tmp, "plan", "create", "--out", str(tmp / "plans" / "plan.json"), *extra)
    assert code == 0, env
    return tmp / "plans" / "plan.json", env


def of(env, kind):
    return [r for r in env["records"] if r["type"] == kind]


def test_a_root_does_not_allow_organizing_until_a_person_says_so(capsys, world):
    tmp, lib = world
    code, env, _ = run_json(capsys, tmp, "plan", "create", "--out", str(tmp / "p.json"))
    assert code == 1 and env["errors"][0]["code"] == "KV_ROOT_NOT_ORGANIZABLE" and "kv root allow-organize" in env["errors"][0]["message"] and not (tmp / "p.json").exists()
    code, env, _ = allow(capsys, tmp)
    assert code == 0 and env["records"][0]["allow_organize"] is True and env["records"][0]["changed"] is True
    assert allow(capsys, tmp)[1]["records"][0]["changed"] is False
    code, env, _ = run_json(capsys, tmp, "root", "allow-organize", "papers", "--off")
    assert env["records"][0]["allow_organize"] is False and run_json(capsys, tmp, "root", "list")[1]["records"][0]["allow_organize"] is False
    assert run_json(capsys, tmp, "root", "allow-organize", "nonesuch")[0] == 1


def test_plan_create_writes_a_file_outside_the_library_moves_nothing_and_says_what_it_would_do(capsys, world):
    tmp, lib = world
    before = tree(lib)
    path, env = make_plan(capsys, tmp)
    assert path.is_file() and tree(lib) == before
    summary = of(env, "summary")[0]
    assert summary["plan_id"] and summary["path"] == str(path) and summary["status"] == {"planned": 2, "unchanged": 1} and summary["naming_policy"] == "naming-1"
    items = of(env, "item")
    assert sorted(i["old_path"] for i in items) == ["cm4c01978.pdf", "sub/kaya2022.pdf"] and all(i["status"] == "planned" for i in items)  # the unchanged one is in the file, not the list
    assert next(i for i in items if i["old_path"] == "cm4c01978.pdf")["new_path"] == NAME
    assert env["complete"] is True and env["next_cursor"] is None


def test_the_plan_refuses_to_be_written_inside_the_library_or_over_another_plan(capsys, world):
    tmp, lib = world
    allow(capsys, tmp)
    code, env, _ = run_json(capsys, tmp, "plan", "create", "--out", str(lib / "plan.json"))
    assert code == 2 and "outside the library" in env["errors"][0]["message"] and not (lib / "plan.json").exists()
    run_json(capsys, tmp, "plan", "create", "--out", str(tmp / "p.json"))
    code, env, _ = run_json(capsys, tmp, "plan", "create", "--out", str(tmp / "p.json"))
    assert code == 1 and env["errors"][0]["code"] == "KV_DESTINATION_EXISTS"


def test_plan_show_reads_the_file_or_the_id_and_pages_with_a_cursor(capsys, world):
    tmp, lib = world
    path, created = make_plan(capsys, tmp)
    plan_id = of(created, "summary")[0]["plan_id"]
    by_file = run_json(capsys, tmp, "plan", "show", str(path), "--status", "all")[1]
    by_id = run_json(capsys, tmp, "plan", "show", plan_id[:10], "--status", "all")[1]
    assert by_file["records"] == by_id["records"] and len(of(by_file, "item")) == 3
    first = run_json(capsys, tmp, "plan", "show", str(path), "--status", "all", "--limit", "2")[1]
    assert len(of(first, "item")) == 2 and first["complete"] is False and first["next_cursor"]
    second = run_json(capsys, tmp, "plan", "show", str(path), "--status", "all", "--limit", "2", "--cursor", first["next_cursor"])[1]
    assert [i["item_id"] for i in of(first, "item") + of(second, "item")] == [i["item_id"] for i in of(by_file, "item")] and second["next_cursor"] is None


def test_apply_moves_the_files_reports_each_move_and_history_and_locate_agree(capsys, world):
    tmp, lib = world
    path, created = make_plan(capsys, tmp)
    sha = run_json(capsys, tmp, "explain", "cm4c01978.pdf")[1]["records"][0]["artifacts"][0]["artifact_id"]
    code, env, _ = run_json(capsys, tmp, "apply", str(path))
    assert code == 0 and env["ok"] is True and env["errors"] == []
    summary = of(env, "summary")[0]
    assert summary["counts"] == {"succeeded": 2} and summary["problems"] == 0 and summary["operation_id"] and summary["left_by_the_plan"] == {"unchanged": 1}
    assert (lib / NAME).read_text() == "the first paper" and (lib / "sub" / "Examplar (2022) - A Second Study Of Invented Compounds.pdf").is_file() and (lib / "notes.txt").is_file()
    located = run_json(capsys, tmp, "locate", sha)[1]["records"][0]
    assert located["locations"][0]["relative_path"] == NAME and located["status"] == "available"  # the library follows the move
    operations = of(run_json(capsys, tmp, "history")[1], "operation")
    assert [(o["kind"], o["status"]) for o in operations] == [("apply", "completed")] and operations[0]["items"] == {"succeeded": 2}
    detail = run_json(capsys, tmp, "history", summary["operation_id"][:8])[1]
    assert of(detail, "operation")[0]["actor"] == "user" and [i["state"] for i in of(detail, "item")] == ["succeeded", "succeeded"]
    assert run_json(capsys, tmp, "doctor")[1]["ok"] is True


def test_apply_then_undo_through_the_cli_gives_back_a_byte_identical_tree(capsys, world):
    tmp, lib = world
    before = tree(lib)
    path, _ = make_plan(capsys, tmp)
    run_json(capsys, tmp, "apply", str(path))
    assert tree(lib) != before
    code, env, _ = run_json(capsys, tmp, "undo")
    assert code == 0 and of(env, "summary")[0]["counts"] == {"undone": 2} and tree(lib) == before
    assert run_json(capsys, tmp, "undo")[0] == 1  # nothing left to undo
    assert run_json(capsys, tmp, "doctor")[1]["ok"] is True


def test_a_dry_run_reports_the_moves_and_changes_nothing_and_a_repeat_apply_reports_already_applied(capsys, world):
    tmp, lib = world
    path, _ = make_plan(capsys, tmp)
    before = tree(lib)
    code, env, _ = run_json(capsys, tmp, "apply", str(path), "--dry-run")
    assert code == 0 and of(env, "summary")[0]["dry_run"] is True and of(env, "summary")[0]["counts"] == {"would_move": 2} and tree(lib) == before
    assert of(env, "summary")[0]["operation_id"] is None
    run_json(capsys, tmp, "apply", str(path))
    code, env, _ = run_json(capsys, tmp, "apply", str(path))
    assert code == 0 and of(env, "summary")[0]["already_done"] is True and of(env, "summary")[0]["counts"] == {"already_applied": 2}


def test_a_stale_plan_exits_1_with_the_items_and_a_broken_one_exits_2(capsys, world):
    tmp, lib = world
    path, _ = make_plan(capsys, tmp)
    run_json(capsys, tmp, "metadata", "set", "cm4c01978.pdf", "year", "1999")
    code, env, _ = run_json(capsys, tmp, "apply", str(path))
    assert code == 1 and env["errors"][0]["code"] == "KV_PLAN_STALE" and env["errors"][0]["details"]["stale"] == 1 and (lib / "cm4c01978.pdf").is_file()
    path.write_text(path.read_text(encoding="utf-8").replace(NAME, "Evil.pdf"), encoding="utf-8")
    code, env, _ = run_json(capsys, tmp, "apply", str(path))
    assert code == 2 and env["errors"][0]["code"] == "KV_PLAN_INVALID" and "hash does not match" in env["errors"][0]["message"]
    assert run_json(capsys, tmp, "apply", str(tmp / "nowhere.json"))[0] == 1
    assert run_json(capsys, tmp, "apply")[0] == 2  # a plan is required


def test_a_file_that_changed_is_an_error_entry_with_exit_1_while_the_others_still_moved(capsys, world):
    tmp, lib = world
    path, _ = make_plan(capsys, tmp)
    (lib / "cm4c01978.pdf").write_text("edited after the plan")
    code, env, _ = run_json(capsys, tmp, "apply", str(path))
    assert code == 1 and env["ok"] is False
    assert [e["code"] for e in env["errors"]] == ["KV_FILE_CHANGED"] and env["errors"][0]["details"]["old_path"] == "cm4c01978.pdf"
    assert of(env, "summary")[0]["counts"] == {"failed": 1, "succeeded": 1} and (lib / "cm4c01978.pdf").read_text() == "edited after the plan"


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="a held-open file blocks a rename on Windows")
def test_a_locked_file_is_a_warning_not_a_failure(capsys, world, monkeypatch):
    from knowledgevista.services import organizer_fs

    tmp, lib = world
    path, _ = make_plan(capsys, tmp)
    monkeypatch.setattr(organizer_fs, "RETRY_DELAYS", (0.0,))
    with open(lib / "cm4c01978.pdf", "rb"):
        code, env, _ = run_json(capsys, tmp, "apply", str(path))
    assert code == 0 and env["ok"] is True and [w["code"] for w in env["warnings"]] == ["KV_ITEM_SKIPPED"] and env["warnings"][0]["details"]["reason_code"] == "KV_PERMISSION_DENIED"


def test_recover_and_history_on_a_clean_library(capsys, world):
    tmp, lib = world
    allow(capsys, tmp)
    code, env, _ = run_json(capsys, tmp, "recover")
    assert code == 0 and of(env, "summary")[0]["already_done"] is True
    assert run_json(capsys, tmp, "history")[1]["records"] == []
    assert run_json(capsys, tmp, "history", "deadbeef")[0] == 1


def test_the_layout_option_moves_into_year_folders_and_undo_removes_the_folders_it_made(capsys, world):
    tmp, lib = world
    before = tree(lib)
    path, env = make_plan(capsys, tmp, "--layout", "by_year")
    assert {i["operation"] for i in of(env, "item")} == {"rename+move"} and of(env, "summary")[0]["layout"] == "by_year"
    run_json(capsys, tmp, "apply", str(path))
    assert (lib / "2021" / NAME).is_file() and (lib / "2022").is_dir()
    run_json(capsys, tmp, "undo")
    assert tree(lib) == before


def test_a_documents_filter_narrows_the_plan_and_the_command_flags_are_in_capabilities(capsys, world):
    tmp, lib = world
    allow(capsys, tmp)
    code, env, _ = run_json(capsys, tmp, "plan", "create", "--document", "kaya2022.pdf", "--out", str(tmp / "one.json"))
    assert [i["old_path"] for i in of(env, "item")] == ["sub/kaya2022.pdf"]
    caps = run_json(capsys, tmp, "capabilities")[1]["records"][0]["commands"]
    table = {c["name"]: c for c in caps}
    assert [(table[n]["kind"], table[n]["read_only"]) for n in ("apply", "undo", "recover")] == [("filesystem", False)] * 3
    assert table["plan create"]["kind"] == "catalog" and table["plan show"]["read_only"] is True and table["history"]["read_only"] is True


def test_read_commands_print_the_same_bytes_twice(capsys, world):
    tmp, lib = world
    path, _ = make_plan(capsys, tmp)
    for command in (("plan", "show", str(path), "--status", "all"), ("history",)):
        args = ["--json", "--catalog", str(tmp / "c.sqlite"), *command]
        _, first, _ = run(capsys, *args)
        _, second, _ = run(capsys, *args)
        assert first == second and json.loads(first)["ok"] is True and first.count("\n") == 1
    assert os.path.exists(path)


def test_blocked_items_are_warned_about_when_the_plan_is_made(capsys, world):
    tmp, lib = world
    (lib / NAME).write_text("somebody else's file already at the name")
    run_json(capsys, tmp, "scan")
    allow(capsys, tmp)
    code, env, _ = run_json(capsys, tmp, "plan", "create", "--out", str(tmp / "p.json"))
    assert code == 0 and [w["code"] for w in env["warnings"]] == ["KV_ITEMS_BLOCKED"] and env["warnings"][0]["details"] == {"blocked": 1}
    assert of(env, "summary")[0]["status"]["blocked"] == 1
