"""Milestone 1 acceptance: identity follows the bytes, and absence is never destruction.

Every case here is a way a library really changes: a file is copied, renamed, moved, overwritten, deleted, restored,
a drive is unplugged, a scan is killed. Each asserts the property the plan promises, not merely that a function ran.
"""

from __future__ import annotations

import functools
import os
import sqlite3
import sys

import pytest
from support import make_env, tree_hashes, write

from knowledgevista.db.catalog import open_catalog
from knowledgevista.services.hashing import hash_file

WINDOWS = sys.platform.startswith("win")


# --- the first scan ---------------------------------------------------------------------------------------------


def test_first_scan_creates_one_artifact_and_document_per_distinct_content(tmp_path):
    env = make_env(tmp_path, {"a.txt": "alpha", "sub/b.txt": "beta", "sub/deep/c.pdf": b"%PDF-1.4 x"})
    report = env.scan()
    assert report.files_seen == report.hashed == report.new_locations == 3
    assert (report.new_artifacts, report.new_documents) == (3, 3)
    assert env.one("SELECT COUNT(*) FROM location WHERE state = 'active'") == 3
    kinds = {r["relative_path"]: r["content_kind"] for r in env.rows(
        "SELECT l.relative_path, a.content_kind FROM location l JOIN artifact a USING (artifact_id)")}
    assert kinds == {"a.txt": "text", "sub/b.txt": "text", "sub/deep/c.pdf": "pdf"}


def test_every_new_artifact_has_exactly_one_document_with_one_canonical_artifact(tmp_path):
    env = make_env(tmp_path, {"a": "1", "b": "2"})
    env.scan()
    assert env.one("SELECT COUNT(*) FROM document_artifact WHERE canonical = 1") == env.one("SELECT COUNT(*) FROM document")


def test_rescan_with_nothing_changed_reads_nothing_and_does_not_bump_the_revision(tmp_path):
    env = make_env(tmp_path, {"a.txt": "alpha", "b.txt": "beta"})
    env.scan()
    before = env.revision()
    report = env.scan()
    assert (report.hashed, report.unchanged, report.changes) == (0, 2, 0)
    assert env.revision() == before, "a scan that changed nothing must not look like a catalog change"


def test_revision_bumps_when_something_meaningful_changes(tmp_path):
    env = make_env(tmp_path, {"a.txt": "alpha"})
    first = env.revision()
    env.scan()
    second = env.revision()
    write(env.lib / "new.txt", "new")
    env.scan()
    assert first < second < env.revision()


# --- copies: same bytes, several paths --------------------------------------------------------------------------


def test_same_bytes_at_two_paths_is_one_artifact_one_document_two_locations(tmp_path):
    env = make_env(tmp_path, {"a.txt": "same", "elsewhere/copy.txt": "same"})
    report = env.scan()
    assert (report.new_artifacts, report.new_documents, report.new_locations) == (1, 1, 2)
    assert env.one("SELECT COUNT(*) FROM location WHERE ended_at IS NULL") == 2


# --- external rename and move: identity follows the bytes -------------------------------------------------------


def test_external_move_keeps_artifact_and_document_and_ends_the_old_location(tmp_path):
    env = make_env(tmp_path, {"papers/kaya2022.pdf": b"%PDF-1.4 unique content"})
    env.scan()
    old = env.location("papers/kaya2022.pdf")
    artifact, document = old["artifact_id"], env.one("SELECT document_id FROM document_artifact")
    docs_before = env.one("SELECT COUNT(*) FROM document")

    (env.lib / "sorted").mkdir()
    os.rename(env.lib / "papers" / "kaya2022.pdf", env.lib / "sorted" / "Kaya 2022 - A Title.pdf")
    report = env.scan()

    assert (report.moved, report.new_artifacts, report.new_documents, report.went_missing) == (1, 0, 0, 1)
    assert env.one("SELECT COUNT(*) FROM document") == docs_before
    assert env.one("SELECT document_id FROM document_artifact") == document
    new = env.location("sorted/Kaya 2022 - A Title.pdf")
    assert new["artifact_id"] == artifact and new["state"] == "active"
    ended = env.conn.execute("SELECT * FROM location WHERE location_id = ?", (old["location_id"],)).fetchone()
    assert ended["end_reason"] == "moved" and ended["successor_location_id"] == new["location_id"]
    events = [r["event"] for r in env.rows("SELECT event FROM location_event WHERE location_id = ? ORDER BY event_id", old["location_id"])]
    assert events[-1] == "moved_to"
    assert env.location("papers/kaya2022.pdf") is None, "the old path is history now, not a current location"


def test_rename_in_place_is_a_move_too(tmp_path):
    env = make_env(tmp_path, {"cm4c01978.pdf": b"%PDF-1.4 renamed later"})
    env.scan()
    os.rename(env.lib / "cm4c01978.pdf", env.lib / "A Real Title.pdf")
    report = env.scan()
    assert (report.moved, report.new_documents) == (1, 0)


def test_which_duplicate_moved_does_not_depend_on_random_ids(tmp_path):
    # Two copies of one file are missing and one new path appears: the choice must be reproducible. Prefer the
    # copy with the same file name, then the same folder.
    results = set()
    for attempt in range(6):
        env = make_env(tmp_path / f"t{attempt}", {"x/report.pdf": "dup", "y/other.pdf": "dup"})
        env.scan()
        os.remove(env.lib / "x" / "report.pdf")
        os.remove(env.lib / "y" / "other.pdf")
        write(env.lib / "z" / "report.pdf", "dup")
        env.scan()
        moved_from = env.one("SELECT l.relative_path FROM location l WHERE end_reason = 'moved'")
        results.add(moved_from)
    assert results == {"x/report.pdf"}, f"the same-named copy should be the one that moved, got {results}"


def test_moving_one_of_two_present_copies_is_not_a_move(tmp_path):
    # Both originals still exist, so a new path with the same bytes is a third copy, not a relocation.
    env = make_env(tmp_path, {"a.txt": "dup", "b.txt": "dup"})
    env.scan()
    write(env.lib / "c.txt", "dup")
    report = env.scan()
    assert (report.moved, report.new_locations) == (0, 1)
    assert env.one("SELECT COUNT(*) FROM location WHERE state = 'active'") == 3


# --- external replacement: new bytes at the same path -----------------------------------------------------------


def test_replaced_bytes_make_a_new_artifact_and_document_and_keep_the_old_history(tmp_path):
    env = make_env(tmp_path, {"paper.pdf": b"%PDF-1.4 version one"})
    env.scan()
    old_loc = env.location("paper.pdf")
    old_artifact = old_loc["artifact_id"]
    old_document = env.one("SELECT document_id FROM document_artifact WHERE artifact_id = ?", old_artifact)

    write(env.lib / "paper.pdf", b"%PDF-1.4 a completely different version two")
    report = env.scan()

    assert (report.replaced, report.new_artifacts, report.new_documents) == (1, 1, 1)
    new_loc = env.location("paper.pdf")
    assert new_loc["artifact_id"] != old_artifact
    new_document = env.one("SELECT document_id FROM document_artifact WHERE artifact_id = ?", new_loc["artifact_id"])
    assert new_document != old_document, "metadata that belonged to the old bytes must not be inherited by the new ones"

    ended = env.conn.execute("SELECT * FROM location WHERE location_id = ?", (old_loc["location_id"],)).fetchone()
    assert ended["end_reason"] == "replaced" and ended["successor_location_id"] == new_loc["location_id"]
    assert env.one("SELECT COUNT(*) FROM artifact WHERE artifact_id = ?", old_artifact) == 1, "the old artifact stays"
    assert env.one("SELECT COUNT(*) FROM document WHERE document_id = ?", old_document) == 1, "and so does its document"


def test_replacement_with_bytes_that_are_already_known_joins_the_existing_document(tmp_path):
    env = make_env(tmp_path, {"a.txt": "known content", "b.txt": "other content"})
    env.scan()
    known_doc = env.one("SELECT document_id FROM document_artifact WHERE artifact_id = ?", env.location("a.txt")["artifact_id"])
    write(env.lib / "b.txt", "known content")
    report = env.scan()
    assert (report.replaced, report.new_artifacts, report.new_documents) == (1, 0, 0)
    assert env.one("SELECT document_id FROM document_artifact WHERE artifact_id = ?", env.location("b.txt")["artifact_id"]) == known_doc


# --- the freshness hint is not proof ----------------------------------------------------------------------------


def test_same_size_and_mtime_with_different_bytes_slips_past_scan_and_is_caught_by_verify_and_full_scan(tmp_path):
    from knowledgevista.services.verify import verify_hashes

    env = make_env(tmp_path, {"p.bin": b"AAAAAAAA"})
    env.scan()
    stamp = os.stat(env.lib / "p.bin")
    (env.lib / "p.bin").write_bytes(b"BBBBBBBB")  # same size
    os.utime(env.lib / "p.bin", ns=(stamp.st_atime_ns, stamp.st_mtime_ns))  # same mtime
    assert os.stat(env.lib / "p.bin").st_mtime_ns == stamp.st_mtime_ns, "the oracle must be alive: mtime really was restored"

    report = env.scan()
    assert (report.hashed, report.unchanged) == (0, 1), "size+mtime is a hint, so an ordinary scan trusts it"
    old_artifact = env.location("p.bin")["artifact_id"]

    verified = verify_hashes(env.conn)
    assert [m["path"] for m in verified.mismatched] == ["p.bin"], "verify reads the bytes and must notice"

    full = env.scan(full=True)
    assert (full.hashed, full.replaced) == (1, 1)
    assert env.location("p.bin")["artifact_id"] != old_artifact


def test_touching_a_file_without_changing_its_bytes_is_not_a_content_change(tmp_path):
    env = make_env(tmp_path, {"a.txt": "stable bytes"})
    env.scan()
    artifact = env.location("a.txt")["artifact_id"]
    os.utime(env.lib / "a.txt", ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    report = env.scan()
    assert (report.hashed, report.content_touched, report.replaced, report.new_artifacts) == (1, 1, 0, 0)
    assert env.location("a.txt")["artifact_id"] == artifact


# --- absence is never destruction -------------------------------------------------------------------------------


def test_a_deleted_file_is_missing_not_deleted_and_comes_back_when_restored(tmp_path):
    env = make_env(tmp_path, {"a.txt": "come back"})
    env.scan()
    artifact = env.location("a.txt")["artifact_id"]
    os.remove(env.lib / "a.txt")
    report = env.scan()
    assert report.went_missing == 1
    assert env.location("a.txt")["state"] == "missing"
    assert env.one("SELECT COUNT(*) FROM artifact") == 1 and env.one("SELECT COUNT(*) FROM document") == 1

    write(env.lib / "a.txt", "come back")  # restored from the Recycle Bin / a backup: new mtime, same bytes
    report = env.scan()
    assert report.reappeared == 1
    restored = env.location("a.txt")
    assert restored["state"] == "active" and restored["artifact_id"] == artifact
    assert env.one("SELECT COUNT(*) FROM location") == 1, "the same location revived; no duplicate row"


def test_unavailable_root_changes_only_its_status_and_recovers_cleanly(tmp_path):
    env = make_env(tmp_path, {"a.txt": "one", "sub/b.txt": "two"})
    env.scan()
    before = env.snapshot()
    gone = tmp_path / "lib-unplugged"
    os.rename(env.lib, gone)

    report = env.scan()
    assert report.status == "skipped_root_unavailable" and report.root_status == "unavailable"
    assert env.snapshot() == before, "an unavailable root must delete or change nothing"
    assert env.one("SELECT COUNT(*) FROM location_event WHERE event = 'missing'") == 0
    assert env.one("SELECT status FROM root") == "unavailable"
    assert env.one("SELECT status FROM scan_run ORDER BY started_at DESC LIMIT 1") == "skipped_root_unavailable"

    os.rename(gone, env.lib)
    report = env.scan()
    assert (report.status, report.went_missing, report.hashed, report.unchanged) == ("completed", 0, 0, 2)
    assert env.one("SELECT status FROM root") == "online"
    assert env.snapshot() == before


def test_a_root_that_exists_but_is_empty_marks_files_missing_and_warns_without_deleting(tmp_path):
    files = {f"f{i}.txt": f"content {i}" for i in range(12)}
    env = make_env(tmp_path, files)
    env.scan()
    for name in files:
        os.remove(env.lib / name)  # a half-mounted drive looks exactly like this
    report = env.scan()
    assert report.went_missing == 12
    assert any(w["code"] == "KV_MASS_MISSING" for w in report.warnings)
    assert env.one("SELECT COUNT(*) FROM artifact") == 12 and env.one("SELECT COUNT(*) FROM document") == 12
    assert env.one("SELECT COUNT(*) FROM location WHERE ended_at IS NOT NULL") == 0

    for name, content in files.items():
        write(env.lib / name, content)
    report = env.scan()
    assert (report.reappeared, report.new_artifacts, report.new_documents) == (12, 0, 0)


def test_files_under_a_directory_that_cannot_be_listed_become_inaccessible_not_missing(tmp_path):
    env = make_env(tmp_path, {"open/a.txt": "visible", "locked/b.txt": "hidden one", "locked/deep/c.txt": "hidden two"})
    env.scan()

    def flaky_scandir(path):
        if str(path).replace("\\", "/").endswith("/locked"):
            raise PermissionError(13, "Access is denied", path)
        return os.scandir(path)

    report = env.scan(scandir=flaky_scandir)
    assert report.went_missing == 0 and report.inaccessible == 2
    assert env.location("open/a.txt")["state"] == "active"
    assert {env.location("locked/b.txt")["state"], env.location("locked/deep/c.txt")["state"]} == {"inaccessible"}
    assert any(w["code"] == "KV_UNREADABLE_DIRECTORY" for w in report.warnings)

    report = env.scan()  # the directory is readable again
    assert report.reappeared == 2
    assert env.one("SELECT COUNT(*) FROM location WHERE state = 'active'") == 3


# --- reading a file while it changes ----------------------------------------------------------------------------


def test_a_file_that_changes_while_being_read_is_not_committed_and_is_picked_up_next_scan(tmp_path):
    env = make_env(tmp_path, {"stable.txt": "fine", "growing.bin": b"x" * 5000})

    def hasher(path):
        if path.endswith("growing.bin"):
            return hash_file(path, mid_read=lambda: open(path, "ab").write(b"more bytes arrive"))
        return hash_file(path)

    report = env.scan(hasher=hasher)
    assert report.changing == 1
    assert env.location("growing.bin") is None, "no identity may be committed for bytes that never existed together"
    assert env.location("stable.txt") is not None

    report = env.scan()  # the writer has finished
    assert report.changing == 0 and env.location("growing.bin")["state"] == "active"


def test_an_unreadable_new_file_is_recorded_inaccessible_and_later_gets_an_artifact(tmp_path):
    env = make_env(tmp_path, {"locked.bin": b"secret bytes"})
    from knowledgevista.services.hashing import HashOutcome

    report = env.scan(hasher=lambda p: HashOutcome("unreadable", error="PermissionError: sharing violation"))
    assert report.unreadable == 1
    loc = env.location("locked.bin")
    assert loc["state"] == "inaccessible" and loc["artifact_id"] is None
    assert env.one("SELECT COUNT(*) FROM artifact") == 0

    report = env.scan()
    assert env.location("locked.bin")["state"] == "active" and env.location("locked.bin")["artifact_id"]
    assert env.one("SELECT COUNT(*) FROM location") == 1


# --- a killed scan ----------------------------------------------------------------------------------------------


class Boom(Exception):
    pass


def test_a_scan_killed_between_files_leaves_a_consistent_catalog_and_the_next_scan_finishes_it(tmp_path):
    from knowledgevista.services.doctor import has_errors, run_doctor

    files = {f"f{i}.txt": f"content number {i}" for i in range(6)}
    env = make_env(tmp_path, files)
    count = {"n": 0}

    def die_after_third(_path):
        count["n"] += 1
        if count["n"] == 3:
            raise Boom

    with pytest.raises(Boom):
        env.scan(after_file=die_after_third)

    assert env.one("SELECT COUNT(*) FROM location") == 3, "files already committed stay committed"
    assert not has_errors(run_doctor(env.conn)), "a killed scan must leave nothing structurally wrong"
    assert env.one("SELECT status FROM scan_run") == "interrupted"
    assert not env.conn.in_transaction

    env.scan()
    clean = make_env(tmp_path, files, name="clean")
    clean.scan()
    assert env.snapshot() == clean.snapshot(), "finishing a killed scan must equal a scan that was never interrupted"


def test_a_scan_that_never_got_to_clean_up_is_closed_by_the_next_one(tmp_path):
    env = make_env(tmp_path, {"a.txt": "x"})
    env.conn.execute(
        "INSERT INTO scan_run (run_id, root_id, started_at, status, app_version) VALUES ('ghost', ?, '2026-01-01T00:00:00Z', 'running', '0')",
        (env.root.root_id,),
    )
    env.scan()
    assert env.one("SELECT status FROM scan_run WHERE run_id = 'ghost'") == "interrupted"


# --- read-only means the source tree is untouched ---------------------------------------------------------------


def test_scan_and_every_read_command_leave_every_source_byte_unchanged(tmp_path):
    from knowledgevista.services import doctor, explain, resolve, stats, verify

    env = make_env(tmp_path, {"a.pdf": b"%PDF-1.4 one", "sub/b.txt": "two", "sub/c.txt": "two"})
    before = tree_hashes(env.lib)
    mtimes = {p.name: p.stat().st_mtime_ns for p in env.lib.rglob("*") if p.is_file()}
    env.scan()
    env.scan(full=True)
    stats.library_stats(env.conn)
    doctor.run_doctor(env.conn)
    verify.verify_hashes(env.conn)
    explain.explain_document(env.conn, resolve.resolve_one(env.conn, "a.pdf").document_id)
    assert tree_hashes(env.lib) == before
    assert {p.name: p.stat().st_mtime_ns for p in env.lib.rglob("*") if p.is_file()} == mtimes
    assert sorted(p.name for p in env.lib.rglob("*")) == sorted(["a.pdf", "sub", "b.txt", "c.txt"])


def test_a_read_only_connection_cannot_change_the_catalog_even_by_mistake(tmp_path):
    env = make_env(tmp_path, {"a.txt": "x"})
    env.scan()
    ro = open_catalog(env.catalog, create=False, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("DELETE FROM location")
    finally:
        ro.close()


# --- Windows filesystem edge cases ------------------------------------------------------------------------------


@pytest.mark.skipif(not WINDOWS, reason="a case-insensitive filesystem is needed")
def test_case_only_rename_is_the_same_file_with_a_new_display_name(tmp_path):
    env = make_env(tmp_path, {"Foo.pdf": b"%PDF-1.4 case"})
    env.scan()
    document = env.one("SELECT document_id FROM document")
    os.rename(env.lib / "Foo.pdf", env.lib / "foo.pdf")
    report = env.scan()
    assert (report.renamed, report.moved, report.new_documents, report.went_missing) == (1, 0, 0, 0)
    loc = env.location("foo.pdf")
    assert loc is not None and loc["state"] == "active"
    assert env.one("SELECT COUNT(*) FROM location") == 1 and env.one("SELECT document_id FROM document") == document


@pytest.mark.skipif(not WINDOWS, reason="extended-length paths are a Windows concern")
def test_a_path_longer_than_max_path_is_scanned(tmp_path):
    env = make_env(tmp_path, {})
    deep = env.lib
    for index in range(8):
        deep = deep / (f"directory-segment-number-{index}-" + "x" * 30)
    target = deep / "deep file.txt"
    os.makedirs("\\\\?\\" + str(deep))
    with open("\\\\?\\" + str(target), "wb") as handle:
        handle.write(b"deep content")
    assert len(str(target)) > 260, "the oracle must be alive: this path really exceeds MAX_PATH"

    report = env.scan()
    assert (report.files_seen, report.new_artifacts, report.unreadable) == (1, 1, 0)
    assert env.one("SELECT state FROM location") == "active"


@pytest.mark.skipif(not WINDOWS, reason="uses an NTFS junction")
def test_a_junction_is_not_followed_out_of_the_root(tmp_path):
    import subprocess

    outside = tmp_path / "outside"
    write(outside / "secret.txt", "must not be catalogued")
    env = make_env(tmp_path, {"inside.txt": "ok"})
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(env.lib / "link"), str(outside)], capture_output=True)
    if done.returncode != 0:
        pytest.skip("could not create a junction here")
    report = env.scan()
    assert report.files_seen == 1
    assert env.location("link/secret.txt") is None
    assert any(w["code"] == "KV_LINKS_SKIPPED" for w in report.warnings)


# --- the schema enforces what the code assumes ------------------------------------------------------------------


def test_database_rejects_two_current_locations_for_one_path(tmp_path):
    env = make_env(tmp_path, {"a.txt": "x"})
    env.scan()
    row = env.location("a.txt")
    with pytest.raises(sqlite3.IntegrityError):
        env.conn.execute(
            "INSERT INTO location (location_id, root_id, relative_path, path_key, artifact_id, state, first_seen, last_seen) "
            "VALUES ('dup', ?, ?, ?, ?, 'active', 'x', 'x')",
            (row["root_id"], row["relative_path"], row["path_key"], row["artifact_id"]),
        )


def test_database_rejects_an_active_location_without_an_artifact_and_an_artifact_in_two_documents(tmp_path):
    env = make_env(tmp_path, {"a.txt": "x"})
    env.scan()
    with pytest.raises(sqlite3.IntegrityError):
        env.conn.execute(
            "INSERT INTO location (location_id, root_id, relative_path, path_key, state, first_seen, last_seen) "
            "VALUES ('no-artifact', ?, 'ghost.txt', 'ghost.txt', 'active', 'x', 'x')", (env.root.root_id,))
    env.conn.execute("INSERT INTO document (document_id, created_at) VALUES ('other', 'x')")
    with pytest.raises(sqlite3.IntegrityError):
        env.conn.execute(
            "INSERT INTO document_artifact (document_id, artifact_id, role) VALUES ('other', ?, 'alternate_copy')",
            (env.location("a.txt")["artifact_id"],))


def test_the_catalog_survives_being_reopened_with_the_same_state(tmp_path):
    env = make_env(tmp_path, {"a.txt": "persist", "b/c.txt": "me"})
    env.scan()
    shape = env.snapshot()
    env.conn.close()
    reopened = open_catalog(env.catalog, create=False)
    env.conn = reopened
    assert env.snapshot() == shape
    assert env.scan().hashed == 0


def test_a_location_already_ended_as_moved_is_never_paired_with_a_later_copy(tmp_path):
    # A moved location keeps state 'missing' once ended. A new copy of the same bytes appearing later must be a
    # plain new location: re-pairing it would rewrite the old move's successor and count a move that never happened.
    env = make_env(tmp_path, {"a.txt": "bytes"})
    env.scan()
    old = env.location("a.txt")["location_id"]
    os.rename(env.lib / "a.txt", env.lib / "b.txt")
    env.scan()
    successor = env.one("SELECT successor_location_id FROM location WHERE location_id = ?", old)
    assert successor == env.location("b.txt")["location_id"]

    write(env.lib / "c.txt", "bytes")
    report = env.scan()
    assert (report.moved, report.new_locations, report.new_artifacts) == (0, 1, 0)
    assert env.one("SELECT successor_location_id FROM location WHERE location_id = ?", old) == successor
