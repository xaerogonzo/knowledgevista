"""The organizer's edges that a mutation sweep found nothing watching: each test here exists because a planted fault survived the suite.

It also pins one REAL defect the sweep led to: undo applied the destination-naming rules to the name a file is being put BACK under, so a
file that another program had called ` lead.pdf` could be renamed by apply and then never restored.
"""

from __future__ import annotations

import json
import os
import sys

import pytest
from organizer_support import TITLE, describe, document, library, make_and_apply, paths_now, plan_for, registered, tree

from knowledgevista.domain import plan as planmod
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import organizer, organizer_fs, organizer_plan
from knowledgevista.services.hashing import HashOutcome

NAME = f"Examplar (2021) - {TITLE}.pdf"
WINDOWS = sys.platform.startswith("win")


def refused(code, fn, *args, **kw):
    with pytest.raises(KvError) as caught:
        fn(*args, **kw)
    assert caught.value.code == code, caught.value.message
    return caught.value


def spec_for(env, **kw):
    base = dict(item_id="i1", operation="rename", root_id=env.root.root_id, artifact_id="a" * 64, document_id="d" * 32, src="a.pdf", dst=NAME,
                expected_sha256="a" * 64, expected_size=3, location_before=None)
    base.update(kw)
    return organizer._Spec(**base)


def root_row(env):
    return env.rows("SELECT * FROM root")[0]


# ------------------------------------------------------------------------------------------------ the plan must be the registered one


def test_a_plan_whose_own_hash_is_valid_but_which_is_not_what_this_catalog_registered_is_refused(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"][0]["reason"] = "someone rewrote the reason and re-sealed the file"
    data["hash"] = planmod.hash_of(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    assert planmod.loads(path.read_text(encoding="utf-8"))  # internally consistent: the plan file's own hash is fine
    assert "not what this catalog registered" in refused(ErrorCode.PLAN_INVALID, organizer.apply_plan, env.conn, str(path)).message


# ------------------------------------------------------------------------------------------------ staleness, from the other side


def test_a_different_file_at_the_planned_path_makes_the_plan_stale(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))
    (env.lib / "a.pdf").write_text("replaced by different bytes")
    env.scan()  # the catalog now knows the path holds another artifact
    error = refused(ErrorCode.PLAN_STALE, organizer.apply_plan, env.conn, str(path))
    assert "no longer at the path the plan names, or is no longer that file" in error.details["items"][0]["reason"]


def test_a_move_that_was_applied_and_then_moved_again_by_hand_is_stale_not_already_applied(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path, _ = make_and_apply(env, tmp_path)
    os.rename(env.lib / NAME, env.lib / "moved-since.pdf")
    env.scan()
    error = refused(ErrorCode.PLAN_STALE, organizer.apply_plan, env.conn, str(path))
    assert "has since moved again" in error.details["items"][0]["reason"]


def test_undo_refuses_when_the_root_has_gone_offline(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    make_and_apply(env, tmp_path)
    env.conn.execute("UPDATE root SET status = 'unavailable'")
    refused(ErrorCode.ROOT_UNAVAILABLE, organizer.undo_operation, env.conn)
    assert (env.lib / NAME).is_file()


# ------------------------------------------------------------------------------------------------ the catalog as a second witness


def test_a_destination_the_catalog_records_is_refused_even_when_the_disk_does_not_have_it(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "ghost.pdf": "two"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))
    ghost_artifact = env.one("SELECT artifact_id FROM location WHERE relative_path = 'ghost.pdf'")
    env.conn.execute("UPDATE location SET relative_path = ?, path_key = ?, state = 'missing' WHERE relative_path = 'ghost.pdf'", (NAME, NAME.lower()))  # the catalog's record of a file that is not on disk
    assert not (env.lib / NAME).exists() and ghost_artifact
    report = organizer.apply_plan(env.conn, str(path))
    assert report.items[0].state == "failed" and report.items[0].code == ErrorCode.DESTINATION_EXISTS and "The catalog records another file" in report.items[0].message
    assert (env.lib / "a.pdf").is_file()


def test_planning_blocks_a_destination_the_catalog_knows_even_if_the_disk_does_not_show_it(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "ghost.pdf": "two"})
    describe(env, "a.pdf")
    env.conn.execute("UPDATE location SET relative_path = ?, path_key = ?, state = 'missing' WHERE relative_path = 'ghost.pdf'", (NAME, NAME.lower()))
    item = next(i for i in plan_for(env).items if i.old_path == "a.pdf")
    assert item.status == "blocked" and "the catalog knows is already at that path" in item.blocked_reason


def test_planning_leaves_out_a_file_that_is_missing_and_a_merged_away_document(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "b.pdf": "two"})
    describe(env, "a.pdf")
    describe(env, "b.pdf", title="The Second Document Title")
    (env.lib / "a.pdf").unlink()
    env.scan()  # a.pdf is now `missing` in the catalog
    assert {i.old_path for i in plan_for(env).items} == {"b.pdf"}


# ------------------------------------------------------------------------------------------------ preconditions, one at a time


def test_the_destination_name_rules_are_enforced_on_an_apply_spec_and_a_path_over_the_limit_is_refused(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    root = root_row(env)
    bad = organizer._precheck(env.conn, spec_for(env, dst="con.pdf", enforce_name=True), root)
    assert (bad.ok, bad.code) == (False, ErrorCode.PATH_UNSAFE) and "not usable" in bad.message
    too_long = organizer._precheck(env.conn, spec_for(env, dst="/".join(["x" * 100] * 12) + "/a.pdf", enforce_name=True), root)
    assert (too_long.ok, too_long.code) == (False, ErrorCode.PATH_UNSAFE) and "over 1024 characters" in too_long.message


def test_a_file_that_vanished_between_the_plan_and_the_apply_says_there_is_no_file(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))
    (env.lib / "a.pdf").unlink()
    report = organizer.apply_plan(env.conn, str(path))
    assert report.items[0].code == ErrorCode.FILE_MISSING and report.items[0].message.startswith("There is no file at a.pdf")


def test_a_file_still_being_written_to_is_skipped_not_moved(tmp_path, monkeypatch):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))
    monkeypatch.setattr(organizer_fs, "hash_path", lambda p: HashOutcome("changing", size=3, mtime_ns=1))
    report = organizer.apply_plan(env.conn, str(path))
    assert report.items[0].state == "skipped" and report.items[0].code == ErrorCode.FILE_CHANGED and "being written to" in report.items[0].message
    assert (env.lib / "a.pdf").is_file() and report.problems == 0


def test_an_unreadable_file_is_skipped_when_it_is_locked_and_failed_when_it_is_not(tmp_path, monkeypatch):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))
    monkeypatch.setattr(organizer_fs, "hash_path", lambda p: HashOutcome("unreadable", error="PermissionError: [WinError 32] in use"))
    locked = organizer.apply_plan(env.conn, str(path), dry_run=True).items[0]
    assert (locked.state, locked.code) == ("skipped", ErrorCode.PERMISSION_DENIED)
    monkeypatch.setattr(organizer_fs, "hash_path", lambda p: HashOutcome("unreadable", error="OSError: the device is not ready"))
    other = organizer.apply_plan(env.conn, str(path), dry_run=True).items[0]
    assert (other.state, other.code) == ("failed", ErrorCode.FILE_MISSING)


def test_a_destination_folder_that_is_a_link_is_never_used_or_created_through(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env, layout="by_year"))
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        if WINDOWS:
            import _winapi

            _winapi.CreateJunction(str(outside), str(env.lib / "2021"))
        else:
            os.symlink(outside, env.lib / "2021", target_is_directory=True)
    except OSError:
        pytest.skip("this account may not create links")
    report = organizer.apply_plan(env.conn, str(path))
    assert report.items[0].state == "failed" and report.items[0].code == ErrorCode.PATH_UNSAFE and "folders through one" in report.items[0].message
    assert os.listdir(outside) == [] and (env.lib / "a.pdf").is_file()


# ------------------------------------------------------------------------------------------------ what happens around the move


def test_a_move_whose_catalog_record_cannot_be_made_is_uncertain_with_the_file_left_where_it_landed(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))

    def end_the_location_behind_its_back(row):
        env.conn.execute("UPDATE location SET ended_at = 'now', end_reason = 'replaced' WHERE relative_path = 'a.pdf' AND ended_at IS NULL")

    report = organizer.apply_plan(env.conn, str(path), hooks=organizer.Hooks(after_move=end_the_location_behind_its_back))
    assert report.items[0].state == "uncertain" and "could not record it" in report.items[0].message and (env.lib / NAME).is_file()


def test_a_refused_move_that_changed_nothing_is_a_failure_with_the_reason_not_an_uncertainty(tmp_path, monkeypatch):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))

    def refuse(src, dst, **kw):
        raise organizer_fs.Refused("exists", "something appeared in the instant between the check and the move")

    monkeypatch.setattr(organizer_fs, "move_no_overwrite", refuse)
    report = organizer.apply_plan(env.conn, str(path))
    assert report.items[0].state == "failed" and report.items[0].code == ErrorCode.DESTINATION_EXISTS and "nothing changed" in report.items[0].message
    assert (env.lib / "a.pdf").is_file() and env.one("SELECT state FROM operation_item") == "failed"


def test_a_file_that_is_not_there_or_not_the_same_size_after_the_move_is_uncertain_and_is_not_moved_again(tmp_path):
    for hook_name, damage in (("vanishes", lambda p: os.unlink(p)), ("shrinks", lambda p: open(p, "wb").write(b"x"))):
        sub = tmp_path / hook_name
        sub.mkdir()
        env = library(sub, {"a.pdf": "one"})
        describe(env, "a.pdf")
        path = registered(env, sub, plan_for(env))
        report = organizer.apply_plan(env.conn, str(path), hooks=organizer.Hooks(after_move=lambda row, d=damage, e=env: d(str(e.lib / NAME))))
        assert report.items[0].state == "uncertain" and "did not verify" in report.items[0].message, hook_name


def test_verify_hashes_reads_the_file_again_and_the_default_does_not(tmp_path):
    for verify, expected in ((False, "succeeded"), (True, "uncertain")):
        sub = tmp_path / str(verify)
        sub.mkdir()
        env = library(sub, {"a.pdf": "one"})
        describe(env, "a.pdf")
        path = registered(env, sub, plan_for(env))
        same_size_other_bytes = lambda row, e=env: (e.lib / NAME).write_bytes(b"two")  # noqa: E731 - three bytes either way
        report = organizer.apply_plan(env.conn, str(path), verify_hashes=verify, hooks=organizer.Hooks(after_move=same_size_other_bytes))
        assert report.items[0].state == expected, verify


def test_when_the_staged_half_of_a_ring_could_not_start_its_landing_is_skipped_quietly(tmp_path):
    names = [f"Examplar (2021) - Title {x} About Esters.pdf" for x in ("Alpha", "Beta")]
    env = library(tmp_path, {names[0]: "was alpha", names[1]: "was beta"})
    describe(env, names[0], title="Title Beta About Esters")
    describe(env, names[1], title="Title Alpha About Esters")
    path = registered(env, tmp_path, plan_for(env))
    (env.lib / names[0]).write_text("edited: so the staged item's source no longer matches")
    before = tree(env.lib)
    report = organizer.apply_plan(env.conn, str(path))
    assert {i.item_id: i.state for i in report.items} == {"i00001": "failed", "i00002": "failed"}
    assert [i.code for i in report.items] == [ErrorCode.FILE_CHANGED, ErrorCode.DESTINATION_EXISTS] and tree(env.lib) == before


# ------------------------------------------------------------------------------------------------ undo


def test_undo_tries_only_the_items_that_succeeded(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "b.pdf": "two"})
    describe(env, "a.pdf")
    describe(env, "b.pdf", title="The Second Document Title")
    path = registered(env, tmp_path, plan_for(env))
    (env.lib / "b.pdf").write_text("edited after the plan")  # this item fails; the other succeeds
    organizer.apply_plan(env.conn, str(path))
    report = organizer.undo_operation(env.conn)
    assert [i.state for i in report.items] == ["undone"] and report.operation_id
    assert env.one("SELECT COUNT(*) FROM operation_item WHERE operation_id = ? AND state = 'undone'", env.one("SELECT operation_id FROM operation WHERE kind = 'apply'")) == 1


def test_a_file_whose_original_name_the_naming_rules_would_refuse_is_still_restored_by_undo(tmp_path):
    odd = " lead.pdf"  # a name another program made: a leading space is legal on disk and not something the policy would choose
    env = library(tmp_path, {odd: "one"})
    describe(env, odd)
    original = tree(env.lib)
    path, applied = make_and_apply(env, tmp_path)
    assert applied.counts() == {"succeeded": 1} and not (env.lib / odd).exists()
    undone = organizer.undo_operation(env.conn)
    assert undone.counts() == {"undone": 1} and tree(env.lib) == original and paths_now(env) == [odd]


# ------------------------------------------------------------------------------------------------ recovery messages and risk


def test_recover_tells_apart_a_move_that_never_started_from_one_that_started_and_did_not_happen(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "b.pdf": "two", "c.pdf": "three"})
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        describe(env, name, title=f"Distinct Title For Document {name.split('.')[0].upper()} Here")
    path = registered(env, tmp_path, plan_for(env))

    class Crash(BaseException):
        pass

    def die(row):
        if row["item_id"] == "i00002":
            raise Crash

    with pytest.raises(Crash):
        organizer.apply_plan(env.conn, str(path), hooks=organizer.Hooks(before_move=die))
    messages = {i.old_path: i.message for i in organizer.recover(env.conn).items}
    assert "never happened" in messages["b.pdf"] and "interrupted before this move started" in messages["c.pdf"]


def test_a_pure_move_is_medium_risk_for_a_stated_title_and_high_for_a_rule_accepted_one(tmp_path):
    env = library(tmp_path, {f"misc/{NAME}": "stated", "misc/Examplar (2022) - A Rule Accepted Paper.pdf": "ruled"})
    describe(env, f"misc/{NAME}")
    describe(env, "misc/Examplar (2022) - A Rule Accepted Paper.pdf", title="A Rule Accepted Paper", year="2022")
    env.conn.execute("UPDATE metadata_value SET accepted_by = 'rule:safe_batch_v1', source = 'layout_title' WHERE field = 'title' AND document_id = ?",
                     (document(env, "misc/Examplar (2022) - A Rule Accepted Paper.pdf"),))
    items = {i.old_path: i for i in plan_for(env, layout="by_year").items}
    assert (items[f"misc/{NAME}"].operation, items[f"misc/{NAME}"].risk) == ("move", "medium")
    assert (items["misc/Examplar (2022) - A Rule Accepted Paper.pdf"].operation, items["misc/Examplar (2022) - A Rule Accepted Paper.pdf"].risk) == ("move", "high")
