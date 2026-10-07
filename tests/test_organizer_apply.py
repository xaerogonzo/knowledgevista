"""Apply and undo: the filesystem matrix. Every test works on a scratch library under tmp_path; nothing here touches anything else.

The order of the file is the order of the plan's acceptance for milestone 6: the happy path and its identity guarantees, the byte-identical
undo, then each way it can go wrong (stale or edited plan, source changed, destination appeared, locked file, case-only and Unicode-only
renames, long and reserved names, chains and swaps, links, read-only and hidden files, antivirus races, repeated apply).
"""

from __future__ import annotations

import os
import stat
import sys
import unicodedata

import pytest
from organizer_support import TITLE, describe, document, library, make_and_apply, paths_now, plan_for, registered, tree

from knowledgevista.domain import plan as planmod
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import metadata, organizer, organizer_fs, organize, relations
from knowledgevista.domain.pathkeys import fs_path

NAME = f"Examplar (2021) - {TITLE}.pdf"
WINDOWS = sys.platform.startswith("win")


def refused(code, fn, *args, **kw):
    with pytest.raises(KvError) as caught:
        fn(*args, **kw)
    assert caught.value.code == code, caught.value.message
    return caught.value


def states(report):
    return {i.old_path: i.state for i in report.items}


# ------------------------------------------------------------------------------------------------ the happy path


def test_apply_renames_the_file_and_the_catalog_follows_with_history(tmp_path):
    env = library(tmp_path, {"papers/cm4c01978.pdf": "the paper's bytes"})
    doc = describe(env, "papers/cm4c01978.pdf")
    artifact = env.one("SELECT artifact_id FROM document_artifact WHERE document_id = ?", doc)
    old_location = env.one("SELECT location_id FROM location WHERE relative_path = 'papers/cm4c01978.pdf'")
    before_revision = env.revision()
    plan_path, report = make_and_apply(env, tmp_path)
    assert states(report) == {"papers/cm4c01978.pdf": "succeeded"} and report.problems == 0 and report.operation_id
    assert not (env.lib / "papers" / "cm4c01978.pdf").exists() and (env.lib / "papers" / NAME).read_bytes() == b"the paper's bytes"
    assert paths_now(env) == [f"papers/{NAME}"]
    ended = env.rows("SELECT * FROM location WHERE location_id = ?", old_location)[0]
    assert (ended["end_reason"], ended["state"]) == ("moved", "active") and ended["ended_at"]
    new = env.rows("SELECT * FROM location WHERE location_id = ?", ended["successor_location_id"])[0]
    assert (new["relative_path"], new["artifact_id"], new["state"], new["ended_at"], new["size"]) == (f"papers/{NAME}", artifact, "active", None, 17)
    assert new["mtime_ns"] == (env.lib / "papers" / NAME).stat().st_mtime_ns  # so the next scan sees an unchanged file and does not re-read it
    events = {e["event"]: e for e in env.rows("SELECT * FROM location_event WHERE actor = 'organizer'")}
    assert set(events) == {"organizer_moved_to", "organizer_moved_from"} and events["organizer_moved_to"]["run_id"] is None
    assert env.revision() > before_revision


def test_the_journal_records_the_operation_its_items_and_who_did_it(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    _, report = make_and_apply(env, tmp_path, actor="user")
    op = env.rows("SELECT * FROM operation")[0]
    assert (op["kind"], op["status"], op["actor"], op["operation_schema"]) == ("apply", "completed", "user", organizer.OPERATION_SCHEMA) and op["finished_at"] and op["app_version"]
    item = env.rows("SELECT * FROM operation_item")[0]
    assert (item["state"], item["old_path"], item["new_path"], item["expected_sha256"] == item["artifact_id"]) == ("succeeded", "a.pdf", NAME, True)
    assert item["location_id_after"] and item["started_at"] and item["finished_at"] and item["error_code"] is None


def test_a_rename_never_changes_identity_collections_tags_notes_or_relations(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "b.pdf": "two"})
    doc, other = describe(env, "a.pdf"), document(env, "b.pdf")
    organize.new_collection(env.conn, "Shelf", [doc])
    organize.add_tags(env.conn, [doc], ["toxicology"])
    relations.add_relation_by_user(env.conn, "document", "related_to", doc, other)
    snapshot = lambda: (env.rows("SELECT * FROM document"), env.rows("SELECT * FROM collection_member"), env.rows("SELECT * FROM document_tag"),
                        env.rows("SELECT * FROM document_relation"), env.rows("SELECT * FROM metadata_value"), env.rows("SELECT * FROM document_artifact"), env.rows("SELECT * FROM artifact"))
    before = [[tuple(r) for r in table] for table in snapshot()]
    make_and_apply(env, tmp_path)
    assert [[tuple(r) for r in table] for table in snapshot()] == before


def test_apply_then_undo_gives_a_byte_identical_tree_and_the_original_paths(tmp_path):
    env = library(tmp_path, {"papers/a.pdf": "one", "papers/sub/b.pdf": "two", "notes.txt": "three", "papers/other.pdf": "four"})
    describe(env, "papers/a.pdf")
    describe(env, "papers/sub/b.pdf", title="A Second Paper About Other Esters")
    original_tree, original_paths = tree(env.lib), paths_now(env)
    _, report = make_and_apply(env, tmp_path)
    assert report.counts() == {"succeeded": 2} and tree(env.lib) != original_tree
    undone = organizer.undo_operation(env.conn)
    assert undone.counts() == {"undone": 2} and undone.problems == 0
    assert tree(env.lib) == original_tree  # every byte, every file name, every folder
    assert paths_now(env) == original_paths
    assert env.rows("SELECT state FROM operation_item WHERE operation_id = ?", report.operation_id) and {r["state"] for r in env.rows("SELECT state FROM operation_item WHERE operation_id = ?", report.operation_id)} == {"undone"}


def test_a_move_into_new_year_folders_is_undone_down_to_the_folders_it_created(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "b.pdf": "two"})
    describe(env, "a.pdf")
    describe(env, "b.pdf", title="The Other One About Esters", year="1999")
    original = tree(env.lib)
    path = registered(env, tmp_path, plan_for(env, layout="by_year"))
    report = organizer.apply_plan(env.conn, str(path))
    assert report.counts() == {"succeeded": 2} and (env.lib / "2021").is_dir() and (env.lib / "1999").is_dir()
    organizer.undo_operation(env.conn)
    assert tree(env.lib) == original and not (env.lib / "2021").exists() and not (env.lib / "1999").exists()


def test_undo_leaves_a_folder_it_did_not_create_even_if_it_is_empty(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    (env.lib / "2021").mkdir()  # the person's own, existing, empty folder
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env, layout="by_year"))
    organizer.apply_plan(env.conn, str(path))
    organizer.undo_operation(env.conn)
    assert (env.lib / "2021").is_dir() and (env.lib / "a.pdf").is_file()


def test_applying_the_same_plan_again_reports_already_applied_and_changes_nothing(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path, first = make_and_apply(env, tmp_path)
    after_first, operations, revision = tree(env.lib), env.one("SELECT COUNT(*) FROM operation"), env.revision()
    second = organizer.apply_plan(env.conn, str(path))
    assert second.already_done is True and second.items[0].state == "already_applied" and second.operation_id is None
    assert tree(env.lib) == after_first and env.one("SELECT COUNT(*) FROM operation") == operations and env.revision() == revision


def test_after_an_undo_the_same_plan_can_be_applied_again(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path, _ = make_and_apply(env, tmp_path)
    organizer.undo_operation(env.conn)
    again = organizer.apply_plan(env.conn, str(path))
    assert again.counts() == {"succeeded": 1} and (env.lib / NAME).is_file()
    assert env.one("SELECT COUNT(*) FROM operation WHERE kind = 'apply'") == 2


def test_a_plan_with_nothing_to_do_does_nothing_and_says_so(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    path = registered(env, tmp_path, plan_for(env))
    report = organizer.apply_plan(env.conn, str(path))
    assert report.items == [] and report.operation_id is None and report.skipped_by_plan == {"unchanged": 1}


def test_dry_run_runs_every_check_and_moves_nothing(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "b.pdf": "two"})
    describe(env, "a.pdf")
    describe(env, "b.pdf", title="Another Fine Title For B")
    path = registered(env, tmp_path, plan_for(env))
    (env.lib / "b.pdf").write_text("edited since the plan")
    before, revision = tree(env.lib), env.revision()
    report = organizer.apply_plan(env.conn, str(path), dry_run=True)
    assert states(report) == {"a.pdf": "would_move", "b.pdf": "failed"} and report.operation_id is None
    assert [i.code for i in report.items if i.state == "failed"] == [ErrorCode.FILE_CHANGED]
    assert tree(env.lib) == before and env.revision() == revision and env.one("SELECT COUNT(*) FROM operation") == 0


# ------------------------------------------------------------------------------------------------ the plan must still describe the world


def test_a_plan_is_stale_when_the_metadata_it_was_built_from_changed_and_nothing_is_moved(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    doc = describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))
    metadata.set_value(env.conn, doc, "year", "1999")
    before = tree(env.lib)
    error = refused(ErrorCode.PLAN_STALE, organizer.apply_plan, env.conn, str(path))
    assert error.details["stale"] == 1 and "year changed" in error.details["items"][0]["reason"] and tree(env.lib) == before
    assert env.one("SELECT COUNT(*) FROM operation") == 0


def test_a_plan_is_stale_when_a_file_was_moved_by_hand_or_the_document_merged_but_not_for_an_unrelated_tag(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "b.pdf": "two"})
    doc = describe(env, "a.pdf")
    describe(env, "b.pdf", title="The Other Paper On Esters")
    path = registered(env, tmp_path, plan_for(env))
    organize.add_tags(env.conn, [doc], ["unrelated"])  # a tag moves the catalog revision and must not matter
    assert organizer.apply_plan(env.conn, str(path), dry_run=True).problems == 0
    os.rename(env.lib / "a.pdf", env.lib / "moved-by-hand.pdf")
    env.scan()
    error = refused(ErrorCode.PLAN_STALE, organizer.apply_plan, env.conn, str(path))
    assert "no longer at the path" in error.details["items"][0]["reason"]


def test_a_plan_is_stale_after_a_merge_and_nothing_partial_happens(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "b.pdf": "two", "c.pdf": "three"})
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        describe(env, name, title=f"Distinct Title For File {name} Here")
    path = registered(env, tmp_path, plan_for(env))
    relations.merge(env.conn, document(env, "a.pdf"), document(env, "b.pdf"))
    before = tree(env.lib)
    error = refused(ErrorCode.PLAN_STALE, organizer.apply_plan, env.conn, str(path))
    assert error.details["stale"] == 1 and "merged" in error.details["items"][0]["reason"] and tree(env.lib) == before  # a stale plan is refused whole


def test_an_edited_plan_a_stranger_s_plan_a_missing_file_and_a_newer_format_are_refused(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))
    text = path.read_text(encoding="utf-8")
    edited = tmp_path / "edited.json"
    edited.write_text(text.replace(NAME, "Evil.pdf"), encoding="utf-8")
    assert "hash does not match" in refused(ErrorCode.PLAN_INVALID, organizer.apply_plan, env.conn, str(edited)).message
    other = library(tmp_path / "other", {"x.pdf": "elsewhere"})
    describe(other, "x.pdf")
    stranger = registered(other, tmp_path, plan_for(other), "stranger.json")
    assert "did not make that plan" in refused(ErrorCode.PLAN_INVALID, organizer.apply_plan, env.conn, str(stranger)).message
    newer = tmp_path / "newer.json"
    newer.write_text(text.replace('"plan_format": 1', '"plan_format": 2'), encoding="utf-8")
    assert "format 2" in refused(ErrorCode.PLAN_INVALID, organizer.apply_plan, env.conn, str(newer)).message
    junk = tmp_path / "junk.json"
    junk.write_text("not json", encoding="utf-8")
    refused(ErrorCode.PLAN_INVALID, organizer.apply_plan, env.conn, str(junk))
    path.unlink()
    refused(ErrorCode.FILE_MISSING, organizer.apply_plan, env.conn, planmod.loads(text).plan_id)  # known by id, but the file is gone
    refused(ErrorCode.NOT_FOUND, organizer.apply_plan, env.conn, "feedfacefeed")


def test_a_plan_can_be_named_by_its_id_or_a_prefix_of_it(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    plan = plan_for(env)
    registered(env, tmp_path, plan)
    assert organizer.apply_plan(env.conn, plan.plan_id[:10], dry_run=True).counts() == {"would_move": 1}


def test_a_plan_from_another_naming_policy_is_stale(tmp_path, monkeypatch):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))
    monkeypatch.setattr("knowledgevista.domain.naming.NAMING_POLICY_VERSION", "naming-2")
    assert "naming-1" in refused(ErrorCode.PLAN_STALE, organizer.apply_plan, env.conn, str(path)).message


def test_the_root_must_still_allow_organizing_be_online_and_be_the_folder_the_plan_was_made_for(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))
    env.conn.execute("UPDATE root SET allow_organize = 0")
    refused(ErrorCode.ROOT_NOT_ORGANIZABLE, organizer.apply_plan, env.conn, str(path))
    env.conn.execute("UPDATE root SET allow_organize = 1, status = 'unavailable'")
    refused(ErrorCode.ROOT_UNAVAILABLE, organizer.apply_plan, env.conn, str(path))
    env.conn.execute("UPDATE root SET status = 'online', root_key = 'x:/somewhere/else'")
    assert "not the folder the plan was made for" in refused(ErrorCode.PLAN_STALE, organizer.apply_plan, env.conn, str(path)).message
    assert (env.lib / "a.pdf").is_file()


# ------------------------------------------------------------------------------------------------ hard preconditions


def test_a_file_edited_after_the_plan_is_left_alone_and_the_others_still_move(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "b.pdf": "two"})
    describe(env, "a.pdf")
    describe(env, "b.pdf", title="The Second Document Title")
    path = registered(env, tmp_path, plan_for(env))
    (env.lib / "a.pdf").write_text("one, then the person edited it")
    report = organizer.apply_plan(env.conn, str(path))
    assert states(report) == {"a.pdf": "failed", "b.pdf": "succeeded"} and report.problems == 1
    failed = next(i for i in report.items if i.state == "failed")
    assert failed.code == ErrorCode.FILE_CHANGED and (env.lib / "a.pdf").read_text() == "one, then the person edited it"
    assert env.one("SELECT status FROM operation") == "completed_with_problems"


def test_a_destination_that_appeared_after_the_plan_is_never_overwritten(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))
    (env.lib / NAME).write_text("a file that arrived after the plan, which is the person's work")
    report = organizer.apply_plan(env.conn, str(path))
    assert states(report) == {"a.pdf": "failed"} and report.items[0].code == ErrorCode.DESTINATION_EXISTS
    assert (env.lib / NAME).read_text() == "a file that arrived after the plan, which is the person's work" and (env.lib / "a.pdf").read_text() == "one"


def test_a_source_that_vanished_is_a_failed_item_not_a_crash(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "b.pdf": "two"})
    describe(env, "a.pdf")
    describe(env, "b.pdf", title="The Second Document Title")
    path = registered(env, tmp_path, plan_for(env))
    (env.lib / "a.pdf").unlink()
    report = organizer.apply_plan(env.conn, str(path))
    assert states(report) == {"a.pdf": "failed", "b.pdf": "succeeded"} and next(i for i in report.items if i.state == "failed").code == ErrorCode.FILE_MISSING


@pytest.mark.skipif(not WINDOWS, reason="a held-open file blocks a rename on Windows; on POSIX the rename succeeds")
def test_a_locked_file_is_skipped_and_reported_the_batch_continues_and_a_later_apply_finishes_it(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "b.pdf": "two"})
    describe(env, "a.pdf")
    describe(env, "b.pdf", title="The Second Document Title")
    path = registered(env, tmp_path, plan_for(env))
    with open(env.lib / "a.pdf", "rb"):  # a viewer has it open
        report = organizer.apply_plan(env.conn, str(path), hooks=organizer.Hooks(sleep=lambda s: None))
    assert states(report) == {"a.pdf": "skipped", "b.pdf": "succeeded"} and report.problems == 0
    skipped = next(i for i in report.items if i.state == "skipped")
    assert skipped.code == ErrorCode.PERMISSION_DENIED and "in use" in skipped.message and (env.lib / "a.pdf").is_file()
    assert env.one("SELECT status FROM operation") == "completed"  # skipped and reported: never a batch failure
    again = organizer.apply_plan(env.conn, str(path))
    assert states(again) == {"a.pdf": "succeeded", "b.pdf": "already_applied"} and (env.lib / NAME).is_file()


def test_an_antivirus_scan_that_holds_a_file_for_a_moment_is_waited_out(tmp_path, monkeypatch):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))
    real, calls, delays = os.rename, [], []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError(13, "sharing violation", None, 32)  # winerror 32
        return real(src, dst)

    monkeypatch.setattr(os, "rename", flaky)
    if not WINDOWS:
        monkeypatch.setattr(os, "link", flaky)
    report = organizer.apply_plan(env.conn, str(path), hooks=organizer.Hooks(sleep=delays.append))
    assert report.counts() == {"succeeded": 1} and delays == list(organizer_fs.RETRY_DELAYS[:2])


def test_a_file_that_stays_locked_is_skipped_after_a_few_short_waits(tmp_path, monkeypatch):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))
    delays = []

    def locked(src, dst):
        raise PermissionError(13, "sharing violation", None, 32)

    monkeypatch.setattr(os, "rename", locked)
    monkeypatch.setattr(os, "link", locked)
    report = organizer.apply_plan(env.conn, str(path), hooks=organizer.Hooks(sleep=delays.append))
    assert states(report) == {"a.pdf": "skipped"} and delays == list(organizer_fs.RETRY_DELAYS) and (env.lib / "a.pdf").is_file()


def test_another_unfinished_operation_blocks_a_new_one_until_it_is_recovered(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path, _ = make_and_apply(env, tmp_path)  # something to undo, so that undo is refused for the right reason
    describe(env, NAME, title="A Later Different Title Entirely")  # and a second plan that would apply
    second = registered(env, tmp_path, plan_for(env), "second.json")
    env.conn.execute("INSERT INTO operation (operation_id, kind, started_at, status, actor, app_version, operation_schema) VALUES ('f' || hex(randomblob(15)), 'apply', 'then', 'running', 'user', '0', 1)")
    error = refused(ErrorCode.OPERATION_UNFINISHED, organizer.apply_plan, env.conn, str(second))
    assert "kv recover" in error.message and (env.lib / NAME).is_file()
    refused(ErrorCode.OPERATION_UNFINISHED, organizer.undo_operation, env.conn)
    assert organizer.apply_plan(env.conn, str(second), dry_run=True).problems == 0  # looking is always allowed


# ------------------------------------------------------------------------------------------------ names the filesystem finds tricky


def test_a_case_only_rename_goes_through_a_temporary_name_and_lands_with_exactly_the_new_spelling(tmp_path):
    lower = f"examplar (2021) - {TITLE}.pdf"
    env = library(tmp_path, {lower: "one"})
    describe(env, lower)
    _, report = make_and_apply(env, tmp_path)
    assert report.counts() == {"succeeded": 1}
    assert os.listdir(env.lib) == [NAME]  # not the old spelling, and no temporary left behind
    assert paths_now(env) == [NAME]
    row = env.rows("SELECT * FROM operation_item")[0]
    assert row["temp_path"] and row["state"] == "succeeded"  # journaled before it was used
    organizer.undo_operation(env.conn)
    assert os.listdir(env.lib) == [lower] and paths_now(env) == [lower]


@pytest.mark.skipif(not WINDOWS, reason="NTFS keeps NFC and NFD spellings as different names; most POSIX filesystems do too, but the case matters only here")
def test_a_unicode_form_only_rename_is_an_ordinary_rename_on_a_filesystem_that_keeps_the_forms_apart(tmp_path):
    title = "Caf\u00e9 society and the esters"
    decomposed = unicodedata.normalize("NFD", f"Examplar (2021) - {title}.pdf")
    env = library(tmp_path, {decomposed: "one"})
    describe(env, decomposed, title=title)
    _, report = make_and_apply(env, tmp_path)
    assert report.counts() == {"succeeded": 1}
    assert os.listdir(env.lib) == [unicodedata.normalize("NFC", f"Examplar (2021) - {title}.pdf")]


def test_hostile_titles_become_usable_names_on_disk(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "b.pdf": "two", "c.pdf": "three"})
    describe(env, "a.pdf", title="CON")
    describe(env, "b.pdf", title="What: is this? A <test> | of \"names\"")
    describe(env, "c.pdf", title="trailing dots and spaces ... ")
    _, report = make_and_apply(env, tmp_path)
    assert report.counts() == {"succeeded": 3}
    names = sorted(os.listdir(env.lib))
    assert all(not n.endswith((" ", ".")) or n.endswith(".pdf") for n in names) and not any(c in n for n in names for c in '<>:"|?*')
    assert any(n.startswith("Examplar (2021) - CON") for n in names)


@pytest.mark.skipif(not WINDOWS, reason="the extended-length form is a Windows mechanism")
def test_a_path_longer_than_the_classic_limit_is_moved_and_hashed_through_the_extended_form(tmp_path):
    deep = "/".join(["deepfolder" * 5] * 4)  # 200 characters of folders
    env = library(tmp_path, {})
    os.makedirs(fs_path(str(env.lib / deep.replace("/", os.sep))), exist_ok=True)
    with open(fs_path(str(env.lib / deep.replace("/", os.sep) / "cm4c01978.pdf")), "wb") as handle:
        handle.write(b"long path bytes")
    env.conn.execute("UPDATE root SET allow_organize = 1")
    env.scan()
    describe(env, f"{deep}/cm4c01978.pdf", title="A Rather Long Title " * 4)
    path = registered(env, tmp_path, plan_for(env))
    report = organizer.apply_plan(env.conn, str(path))
    assert report.counts() == {"succeeded": 1}
    now = paths_now(env)[0]
    assert len(str(env.lib)) + len(now) > 260 and os.path.isfile(fs_path(str(env.lib / now.replace("/", os.sep))))
    organizer.undo_operation(env.conn)
    assert paths_now(env) == [f"{deep}/cm4c01978.pdf"]


@pytest.mark.skipif(not WINDOWS, reason="read-only and hidden are NTFS attributes")
def test_read_only_and_hidden_files_are_renamed_and_keep_their_attributes(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    path = registered(env, tmp_path, plan_for(env))
    os.chmod(env.lib / "a.pdf", stat.S_IREAD)
    import ctypes
    ctypes.windll.kernel32.SetFileAttributesW(str(env.lib / "a.pdf"), 0x1 | 0x2)  # read-only + hidden
    organizer.apply_plan(env.conn, str(path))
    attributes = ctypes.windll.kernel32.GetFileAttributesW(str(env.lib / NAME))
    assert attributes & 0x1 and attributes & 0x2
    ctypes.windll.kernel32.SetFileAttributesW(str(env.lib / NAME), 0x80)  # let pytest clean up


# ------------------------------------------------------------------------------------------------ chains and swaps


def test_a_chain_is_ordered_so_the_vacating_move_goes_first(tmp_path):
    env = library(tmp_path, {NAME: "first", "second.pdf": "second"})
    describe(env, NAME, title="A Different Title For The First")  # the first leaves the name the second takes
    describe(env, "second.pdf")
    path = registered(env, tmp_path, plan_for(env))
    report = organizer.apply_plan(env.conn, str(path))
    assert report.counts() == {"succeeded": 2} and (env.lib / NAME).read_text() == "second"
    assert (env.lib / "Examplar (2021) - A Different Title For The First.pdf").read_text() == "first"
    organizer.undo_operation(env.conn)
    assert (env.lib / NAME).read_text() == "first" and (env.lib / "second.pdf").read_text() == "second"


def test_a_swap_is_staged_through_a_journaled_temporary_name_and_undone_the_same_way(tmp_path):
    name_a, name_b = f"Examplar (2021) - Title Alpha About Esters.pdf", f"Examplar (2021) - Title Beta About Esters.pdf"
    env = library(tmp_path, {name_a: "was alpha", name_b: "was beta"})
    describe(env, name_a, title="Title Beta About Esters")  # each file carries the OTHER's name
    describe(env, name_b, title="Title Alpha About Esters")
    path = registered(env, tmp_path, plan_for(env))
    original = tree(env.lib)
    report = organizer.apply_plan(env.conn, str(path))
    assert report.counts() == {"succeeded": 2} and report.problems == 0
    assert (env.lib / name_a).read_text() == "was beta" and (env.lib / name_b).read_text() == "was alpha"
    assert sorted(os.listdir(env.lib)) == sorted([name_a, name_b])  # no temporary left behind
    assert any(r["temp_path"] for r in env.rows("SELECT temp_path FROM operation_item"))
    undone = organizer.undo_operation(env.conn)
    assert undone.counts() == {"undone": 2} and tree(env.lib) == original


def test_a_ring_of_three_is_staged_too(tmp_path):
    names = [f"Examplar (2021) - Title {x} About Esters.pdf" for x in ("Alpha", "Beta", "Gamma")]
    env = library(tmp_path, {names[0]: "0", names[1]: "1", names[2]: "2"})
    for held, wanted in zip(names, ("Beta", "Gamma", "Alpha")):  # alpha's file wants beta's name, beta's wants gamma's, gamma's wants alpha's
        describe(env, held, title=f"Title {wanted} About Esters")
    path = registered(env, tmp_path, plan_for(env))
    original = tree(env.lib)
    assert organizer.apply_plan(env.conn, str(path)).counts() == {"succeeded": 3}
    assert [(env.lib / n).read_text() for n in names] == ["2", "0", "1"]
    organizer.undo_operation(env.conn)
    assert tree(env.lib) == original


# ------------------------------------------------------------------------------------------------ links


@pytest.mark.skipif(not WINDOWS, reason="junctions are a Windows mechanism")
def test_a_folder_that_became_a_junction_is_never_moved_through(tmp_path):
    import _winapi

    env = library(tmp_path, {"papers/a.pdf": "one"})
    describe(env, "papers/a.pdf")
    path = registered(env, tmp_path, plan_for(env))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.pdf").write_text("one")  # what the junction points at: same name, same bytes, somewhere else
    (env.lib / "papers" / "a.pdf").unlink()
    (env.lib / "papers").rmdir()
    _winapi.CreateJunction(str(outside), str(env.lib / "papers"))
    report = organizer.apply_plan(env.conn, str(path))
    assert states(report) == {"papers/a.pdf": "failed"} and report.items[0].code == ErrorCode.PATH_UNSAFE and "link" in report.items[0].message
    assert (outside / "a.pdf").read_text() == "one" and os.listdir(outside) == ["a.pdf"]  # nothing happened out there


def test_a_symlink_is_never_moved_through_or_followed(tmp_path):
    env = library(tmp_path, {"papers/a.pdf": "one"})
    describe(env, "papers/a.pdf")
    path = registered(env, tmp_path, plan_for(env))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.pdf").write_text("one")
    (env.lib / "papers" / "a.pdf").unlink()
    try:
        os.symlink(outside / "a.pdf", env.lib / "papers" / "a.pdf")
    except OSError:
        pytest.skip("this account may not create symlinks")
    report = organizer.apply_plan(env.conn, str(path))
    assert states(report) == {"papers/a.pdf": "failed"} and report.items[0].code == ErrorCode.PATH_UNSAFE
    assert (outside / "a.pdf").read_text() == "one" and os.listdir(outside) == ["a.pdf"]


# ------------------------------------------------------------------------------------------------ undo refuses to damage user work


def test_undo_after_the_moved_file_was_edited_refuses_and_does_not_touch_it(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    _, applied = make_and_apply(env, tmp_path)
    (env.lib / NAME).write_text("one, plus an annotation the person added afterwards")
    report = organizer.undo_operation(env.conn)
    assert report.items[0].state == "failed"
    assert report.items[0].code == ErrorCode.FILE_CHANGED and "edited or replaced" in report.items[0].message
    assert (env.lib / NAME).read_text() == "one, plus an annotation the person added afterwards" and not (env.lib / "a.pdf").exists()
    assert env.one("SELECT state FROM operation_item WHERE operation_id = ?", applied.operation_id) == "succeeded"  # still in place, still undoable if the file is restored
    assert env.one("SELECT status FROM operation WHERE kind = 'undo'") == "completed_with_problems"


def test_undo_when_the_old_path_is_now_occupied_refuses_and_overwrites_nothing(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    make_and_apply(env, tmp_path)
    (env.lib / "a.pdf").write_text("the person's new file at the old name")
    report = organizer.undo_operation(env.conn)
    assert report.items[0].state == "failed" and report.items[0].code == ErrorCode.DESTINATION_EXISTS
    assert (env.lib / "a.pdf").read_text() == "the person's new file at the old name" and (env.lib / NAME).read_text() == "one"


def test_undo_twice_says_already_undone_and_undoing_an_undo_is_refused(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    make_and_apply(env, tmp_path)
    organizer.undo_operation(env.conn)
    refused(ErrorCode.NOT_FOUND, organizer.undo_operation, env.conn)  # nothing left to undo
    undo_id = env.one("SELECT operation_id FROM operation WHERE kind = 'undo'")
    assert "not an apply" in refused(ErrorCode.INVALID_ARGUMENTS, organizer.undo_operation, env.conn, undo_id).message
    apply_id = env.one("SELECT operation_id FROM operation WHERE kind = 'apply'")
    assert organizer.undo_operation(env.conn, apply_id).already_done is True


def test_undo_with_nothing_ever_applied_is_not_found_and_an_unknown_operation_too(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    refused(ErrorCode.NOT_FOUND, organizer.undo_operation, env.conn)
    refused(ErrorCode.NOT_FOUND, organizer.undo_operation, env.conn, "deadbeef")


def test_undo_is_a_dry_run_on_request_and_changes_nothing(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    make_and_apply(env, tmp_path)
    before, revision = tree(env.lib), env.revision()
    assert organizer.undo_operation(env.conn, dry_run=True).counts() == {"would_move": 1}
    assert tree(env.lib) == before and env.revision() == revision
    (env.lib / NAME).write_text("edited")
    assert organizer.undo_operation(env.conn, dry_run=True).items[0].code == ErrorCode.FILE_CHANGED


def test_undo_refuses_when_the_root_no_longer_allows_organizing(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    make_and_apply(env, tmp_path)
    env.conn.execute("UPDATE root SET allow_organize = 0")
    refused(ErrorCode.ROOT_NOT_ORGANIZABLE, organizer.undo_operation, env.conn)
    assert (env.lib / NAME).is_file()


def test_a_second_apply_then_undo_undoes_only_the_latest(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "b.pdf": "two"})
    describe(env, "a.pdf")
    make_and_apply(env, tmp_path)
    describe(env, "b.pdf", title="The Second Document Title")
    registered_path = registered(env, tmp_path, plan_for(env), "second.json")
    organizer.apply_plan(env.conn, str(registered_path))
    organizer.undo_operation(env.conn)
    assert (env.lib / NAME).is_file() and (env.lib / "b.pdf").is_file()  # the first apply stands; only the second was undone
    organizer.undo_operation(env.conn)
    assert (env.lib / "a.pdf").is_file()


def test_a_scan_after_an_apply_sees_nothing_new(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "b.pdf": "two"})
    describe(env, "a.pdf")
    make_and_apply(env, tmp_path)
    report = env.scan()
    assert (report.new_artifacts, report.moved, report.went_missing, report.hashed) == (0, 0, 0, 0)  # the stat the organizer recorded is the file's
    assert paths_now(env) == sorted([NAME, "b.pdf"])
