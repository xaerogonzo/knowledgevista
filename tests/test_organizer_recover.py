"""Interruption: the journal is written before the filesystem is touched, and `recover` reconciles it with the disk without ever guessing.

A crash is simulated by a BaseException raised from the test seams at the three instants that matter (the intent is journaled but nothing has
moved; the file has moved but the catalog does not know; mid-way through a staged move). BaseException, because that is what a killed process
looks like to the code: nothing after the raise runs, and no `except Exception` gets to tidy up.
"""

from __future__ import annotations

import os
import sqlite3

import pytest
from organizer_support import TITLE, describe, library, paths_now, plan_for, registered, tree

from knowledgevista.domain import plan as planmod
from knowledgevista.errors import ErrorCode
from knowledgevista.services import doctor, organizer, organizer_fs

NAME = f"Examplar (2021) - {TITLE}.pdf"


class Crash(BaseException):
    """The process was killed here."""


def crash_at(point: str, item_id: str):
    def hook(row):
        if row["item_id"] == item_id:
            raise Crash(point)
    return organizer.Hooks(**{point: hook})


def three(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "b.pdf": "two", "c.pdf": "three"})
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        describe(env, name, title=f"Distinct Title For Document {name.split('.')[0].upper()} Here")
    return env, registered(env, tmp_path, plan_for(env))


def new_connection(env):
    from knowledgevista.db.catalog import open_catalog

    return open_catalog(env.catalog, create=False, read_only=True)


def test_the_intent_is_journaled_before_the_file_moves_and_the_outcome_only_after(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))
    seen = {}

    def before(row):
        other = new_connection(env)  # what ANOTHER process would see: only committed data
        seen["before"] = (other.execute("SELECT state FROM operation_item").fetchone()[0], (env.lib / "a.pdf").is_file(), (env.lib / NAME).exists())
        other.close()

    def after(row):
        other = new_connection(env)
        seen["after"] = (other.execute("SELECT state FROM operation_item").fetchone()[0], (env.lib / "a.pdf").exists(), (env.lib / NAME).is_file(),
                         other.execute("SELECT relative_path FROM location WHERE ended_at IS NULL").fetchone()[0])
        other.close()

    organizer.apply_plan(env.conn, str(path), hooks=organizer.Hooks(before_move=before, after_move=after))
    assert seen["before"] == ("executing", True, False)  # journaled, not yet moved
    assert seen["after"] == ("executing", False, True, "a.pdf")  # moved, and neither the journal nor the catalog knows yet
    assert env.one("SELECT state FROM operation_item") == "succeeded" and paths_now(env) == [NAME]


def test_a_crash_before_a_move_leaves_it_journaled_not_done_and_recover_says_so_then_apply_finishes_the_rest(tmp_path):
    env, path = three(tmp_path)
    with pytest.raises(Crash):
        organizer.apply_plan(env.conn, str(path), hooks=crash_at("before_move", "i00002"))
    states = {r["old_path"]: r["state"] for r in env.rows("SELECT * FROM operation_item")}
    assert states == {"a.pdf": "succeeded", "b.pdf": "executing", "c.pdf": "planned"}
    assert env.one("SELECT status FROM operation") == "running" and (env.lib / "b.pdf").is_file() and (env.lib / "c.pdf").is_file()
    found = doctor.run_doctor(env.conn)
    assert any(f.code == "KVD_OPERATION_UNFINISHED" for f in found)
    report = organizer.recover(env.conn)
    assert {i.old_path: i.state for i in report.items} == {"b.pdf": "failed", "c.pdf": "failed"} and {i.code for i in report.items} == {ErrorCode.INTERRUPTED}
    assert env.one("SELECT status FROM operation WHERE kind = 'apply'") == "completed_with_problems" and env.one("SELECT COUNT(*) FROM operation WHERE kind = 'recover'") == 1
    assert not any(f.code == "KVD_OPERATION_UNFINISHED" for f in doctor.run_doctor(env.conn))
    again = organizer.apply_plan(env.conn, str(path))  # the plan is still valid: it carries on from where the world is
    assert {i.old_path: i.state for i in again.items} == {"a.pdf": "already_applied", "b.pdf": "succeeded", "c.pdf": "succeeded"}
    assert sorted(os.listdir(env.lib)) == sorted(i.new_path for i in planmod.loads(path.read_text(encoding="utf-8")).items)


def test_a_crash_after_the_move_but_before_the_bookkeeping_is_completed_by_recover_from_what_the_disk_shows(tmp_path):
    env, path = three(tmp_path)
    with pytest.raises(Crash):
        organizer.apply_plan(env.conn, str(path), hooks=crash_at("after_move", "i00002"))
    assert not (env.lib / "b.pdf").exists() and "b.pdf" in paths_now(env)  # the file moved; the catalog still names the old path
    report = organizer.recover(env.conn)
    assert {i.old_path: i.state for i in report.items} == {"b.pdf": "succeeded", "c.pdf": "failed"}
    new_b = next(p for p in paths_now(env) if "Document B" in p)
    assert (env.lib / new_b).is_file() and "b.pdf" not in paths_now(env)
    ended = env.rows("SELECT * FROM location WHERE relative_path = 'b.pdf'")[0]
    assert ended["end_reason"] == "moved" and ended["successor_location_id"]
    assert env.rows("SELECT state, message FROM operation_item WHERE old_path = 'b.pdf'")[0]["state"] == "succeeded"
    organizer.apply_plan(env.conn, str(path))  # the rest still goes through
    assert not any(os.path.exists(env.lib / n) for n in ("a.pdf", "b.pdf", "c.pdf"))
    undone = organizer.undo_operation(env.conn)  # and what recover completed is undoable like anything else
    assert undone.problems == 0


def test_a_crash_between_the_two_steps_of_a_case_only_rename_puts_the_file_back_where_it_started(tmp_path, monkeypatch):
    lower = f"examplar (2021) - {TITLE}.pdf"
    env = library(tmp_path, {lower: "one"})
    describe(env, lower)
    path = registered(env, tmp_path, plan_for(env))
    calls = []
    real = organizer_fs.move_no_overwrite

    def die_on_second(src, dst, **kw):
        calls.append((src, dst))
        if len(calls) == 2:
            raise Crash("between the two steps")
        return real(src, dst, **kw)

    monkeypatch.setattr(organizer_fs, "move_no_overwrite", die_on_second)
    with pytest.raises(Crash):
        organizer.apply_plan(env.conn, str(path))
    monkeypatch.setattr(organizer_fs, "move_no_overwrite", real)
    temp = env.rows("SELECT temp_path FROM operation_item")[0]["temp_path"]
    assert temp and os.listdir(env.lib) == [os.path.basename(temp)]  # the file is at the journaled temporary name
    report = organizer.recover(env.conn)
    assert report.items[0].state == "failed" and "put back where it started" in report.items[0].message
    assert os.listdir(env.lib) == [lower] and paths_now(env) == [lower]
    assert (env.lib / lower).read_text() == "one"


def swap(tmp_path):
    names = [f"Examplar (2021) - Title {x} About Esters.pdf" for x in ("Alpha", "Beta")]
    env = library(tmp_path, {names[0]: "was alpha", names[1]: "was beta"})
    describe(env, names[0], title="Title Beta About Esters")
    describe(env, names[1], title="Title Alpha About Esters")
    return env, names, registered(env, tmp_path, plan_for(env))


def test_a_swap_interrupted_with_a_file_at_its_temporary_name_is_put_back_and_the_tree_is_what_it_was(tmp_path):
    env, names, path = swap(tmp_path)
    original, original_paths = tree(env.lib), paths_now(env)
    with pytest.raises(Crash):
        organizer.apply_plan(env.conn, str(path), hooks=crash_at("before_move", "i00002"))  # i00001 staged out; i00002 about to land
    leftovers = [n for n in os.listdir(env.lib) if n.startswith(".kv-moving-")]
    assert len(leftovers) == 1 and (env.lib / leftovers[0]).read_text() == "was alpha"
    assert env.rows("SELECT ended_at FROM location WHERE relative_path = ?", names[0])[0]["ended_at"]  # the catalog does not claim a file at a path it left
    report = organizer.recover(env.conn)
    by_item = {i.item_id: i for i in report.items}
    assert {k: v.state for k, v in by_item.items()} == {"i00001": "failed", "i00002": "failed"}
    assert "put back where it started" in by_item["i00001"].message and "never happened" in by_item["i00002"].message
    assert tree(env.lib) == original and paths_now(env) == original_paths
    assert env.one("SELECT COUNT(*) FROM location WHERE relative_path = ? AND ended_at IS NULL", names[0]) == 1
    assert env.rows("SELECT event FROM location_event WHERE event = 'organizer_restored'")


def test_a_swap_stopped_after_one_member_landed_leaves_the_stranded_file_untouched_and_says_where_it_is(tmp_path):
    env, names, path = swap(tmp_path)
    seen = []

    def crash_on_the_second_visit_to_the_staged_item(row):
        if row["item_id"] == "i00001":
            seen.append(1)
            if len(seen) == 2:  # the first visit staged it out; the second is its landing
                raise Crash("before stage_in")

    with pytest.raises(Crash):
        organizer.apply_plan(env.conn, str(path), hooks=organizer.Hooks(before_move=crash_on_the_second_visit_to_the_staged_item))
    assert env.one("SELECT state FROM operation_item WHERE item_id = 'i00002'") == "succeeded" and (env.lib / names[0]).read_text() == "was beta"
    stranded = next(n for n in os.listdir(env.lib) if n.startswith(".kv-moving-"))
    report = organizer.recover(env.conn)
    stuck = next(i for i in report.items if i.item_id == "i00001")
    assert stuck.state == "uncertain" and stuck.code == ErrorCode.UNCERTAIN and stranded in stuck.message  # it names the temporary file
    assert (env.lib / stranded).read_text() == "was alpha" and (env.lib / names[0]).read_text() == "was beta" and not (env.lib / names[1]).exists()  # nothing was moved or overwritten
    assert any(f.code == "KVD_ITEM_UNCERTAIN" for f in doctor.run_doctor(env.conn))


def test_a_move_whose_file_vanished_mid_way_is_uncertain_it_is_reported_with_what_was_seen_and_never_retried(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))

    def vanish(row):
        os.unlink(env.lib / NAME)  # the file is gone from BOTH names, as if something deleted it between the move and the bookkeeping
        raise Crash("deleted")

    with pytest.raises(Crash):
        organizer.apply_plan(env.conn, str(path), hooks=organizer.Hooks(after_move=vanish))
    report = organizer.recover(env.conn)
    assert report.items[0].state == "uncertain" and report.items[0].code == ErrorCode.UNCERTAIN
    assert "source absent, destination absent" in report.items[0].message and "Nothing was changed" in report.items[0].message
    again = organizer.recover(env.conn)  # an uncertain item is never retried and never re-decided: it stays reported
    assert again.items[0].state == "uncertain" and os.listdir(env.lib) == []
    assert env.rows("SELECT state FROM operation_item")[0]["state"] == "uncertain"


def test_recover_with_nothing_to_recover_says_so_and_a_dry_run_changes_nothing(tmp_path):
    env, path = three(tmp_path)
    assert organizer.recover(env.conn).already_done is True
    with pytest.raises(Crash):
        organizer.apply_plan(env.conn, str(path), hooks=crash_at("after_move", "i00001"))
    before_rows, before_tree = [tuple(r) for r in env.rows("SELECT * FROM operation_item")], tree(env.lib)
    report = organizer.recover(env.conn, dry_run=True)
    assert report.items[0].state == "would_move" and [tuple(r) for r in env.rows("SELECT * FROM operation_item")] == before_rows and tree(env.lib) == before_tree
    assert env.one("SELECT COUNT(*) FROM operation WHERE kind = 'recover'") == 0


def test_recover_never_moves_a_file_forward(tmp_path):
    env, path = three(tmp_path)
    with pytest.raises(Crash):
        organizer.apply_plan(env.conn, str(path), hooks=crash_at("before_move", "i00001"))
    before = tree(env.lib)
    organizer.recover(env.conn)
    assert tree(env.lib) == before  # the interrupted move was not completed on its behalf


def test_an_integrity_failure_after_the_move_is_uncertain_with_a_way_out(tmp_path, monkeypatch):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))

    def broken(*a, **k):
        raise sqlite3.IntegrityError("simulated catalog conflict")

    monkeypatch.setattr(organizer, "_relocate", broken)
    report = organizer.apply_plan(env.conn, str(path))
    assert report.items[0].state == "uncertain" and "kv recover" in report.items[0].message and (env.lib / NAME).is_file()
    monkeypatch.undo()
    fixed = organizer.recover(env.conn)  # the file is at the destination with the expected hash: the bookkeeping can be finished
    assert fixed.items[0].state == "succeeded" and paths_now(env) == [NAME]
