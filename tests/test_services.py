"""Walker, hashing, roots, resolve, explain, stats, doctor, verify."""

from __future__ import annotations

import os
import sys

import pytest
from support import make_env, write

from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import doctor, explain, resolve, stats, verify
from knowledgevista.services.hashing import hash_file
from knowledgevista.services.roots import add_root, find_roots, list_roots
from knowledgevista.services.walker import walk_tree

# --- walker -----------------------------------------------------------------------------------------------------


class FakeEntry:
    """Just enough of os.DirEntry to model a directory the real filesystem cannot give us (case-colliding names)."""

    def __init__(self, name, is_dir=False, size=1):
        import stat as st
        self.name, self._dir, self._size = name, is_dir, size
        self._mode = st.S_IFDIR if is_dir else st.S_IFREG

    def stat(self, follow_symlinks=True):
        return os.stat_result((self._mode, 0, 0, 1, 0, 0, self._size, 0, 5, 0))


class FakeDir(list):
    def __enter__(self):
        return iter(self)

    def __exit__(self, *exc):
        return False


def test_walker_lists_regular_files_with_posix_relative_paths_sorted(tmp_path):
    write(tmp_path / "b" / "z.txt", "1")
    write(tmp_path / "a.txt", "2")
    result = walk_tree(str(tmp_path))
    assert [e.relative_path for e in result.entries] == ["a.txt", "b/z.txt"]
    assert result.unreadable_dirs == []


def test_walker_reports_a_listing_failure_as_unreadable_not_empty(tmp_path):
    write(tmp_path / "ok" / "a.txt", "1")
    write(tmp_path / "bad" / "b.txt", "2")

    def scandir(path):
        if str(path).endswith("bad"):
            raise PermissionError
        return os.scandir(path)

    result = walk_tree(str(tmp_path), scandir=scandir)
    assert result.unreadable_dirs == ["bad"]
    assert [e.relative_path for e in result.entries] == ["ok/a.txt"]


def test_walker_skips_a_second_file_whose_key_collides_and_says_so():
    tree = {"": FakeDir([FakeEntry("A.txt"), FakeEntry("a.txt"), FakeEntry("b.txt")])}
    result = walk_tree("root", scandir=lambda path: tree[""], case_sensitive=False)
    assert [e.relative_path for e in result.entries] == ["A.txt", "b.txt"]
    assert result.key_collisions == ["a.txt"]


def test_walker_keeps_names_apart_on_a_case_sensitive_filesystem():
    tree = FakeDir([FakeEntry("A.txt"), FakeEntry("a.txt")])
    result = walk_tree("root", scandir=lambda path: tree, case_sensitive=True)
    assert len(result.entries) == 2 and result.key_collisions == []


# --- hashing ----------------------------------------------------------------------------------------------------


def test_hash_file_matches_hashlib_and_reports_stat_and_head(tmp_path):
    import hashlib
    path = write(tmp_path / "f.bin", b"hello world" * 1000)
    out = hash_file(str(path))
    assert out.kind == "ok" and out.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert out.size == path.stat().st_size and out.mtime_ns == path.stat().st_mtime_ns
    assert out.head.startswith(b"hello world")


def test_hash_file_handles_empty_and_multi_block_files(tmp_path):
    import hashlib
    assert hash_file(str(write(tmp_path / "e", b""))).sha256 == hashlib.sha256(b"").hexdigest()
    big = write(tmp_path / "big", os.urandom(3 * (1 << 20) + 17))
    assert hash_file(str(big)).sha256 == hashlib.sha256(big.read_bytes()).hexdigest()


def test_hash_file_reports_missing_as_unreadable_not_an_exception(tmp_path):
    out = hash_file(str(tmp_path / "nope"))
    assert out.kind == "unreadable" and "FileNotFoundError" in out.error


def test_hash_file_detects_a_change_during_the_read(tmp_path):
    path = write(tmp_path / "f.bin", b"x" * 100)
    out = hash_file(str(path), mid_read=lambda: open(path, "ab").write(b"appended"))
    assert out.kind == "changing" and out.sha256 is None


def test_hash_file_detects_a_same_size_rewrite_during_the_read_only_if_the_mtime_moves(tmp_path):
    path = write(tmp_path / "f.bin", b"A" * 100)
    stamp = path.stat().st_mtime_ns

    def rewrite_same_size():
        path.write_bytes(b"B" * 100)
        os.utime(path, ns=(stamp + 5_000_000_000, stamp + 5_000_000_000))

    assert hash_file(str(path), mid_read=rewrite_same_size).kind == "changing"


# --- roots ------------------------------------------------------------------------------------------------------


def test_add_root_records_label_status_and_defaults(tmp_path):
    env = make_env(tmp_path, {})
    root = list_roots(env.conn)[0]
    assert root.label == "lib" and root.status == "online" and root.enabled and not root.allow_organize
    assert root.volume_id


def test_add_root_refuses_a_missing_folder(tmp_path):
    env = make_env(tmp_path, {})
    with pytest.raises(KvError) as caught:
        add_root(env.conn, str(tmp_path / "does-not-exist"))
    assert caught.value.code == ErrorCode.ROOT_UNAVAILABLE


def test_add_root_refuses_overlap_in_both_directions_and_duplicates(tmp_path):
    env = make_env(tmp_path, {"sub/x.txt": "1"})
    (tmp_path / "other").mkdir()
    for path in (env.lib, env.lib / "sub", tmp_path):
        with pytest.raises(KvError) as caught:
            add_root(env.conn, str(path))
        assert caught.value.code == ErrorCode.ROOT_OVERLAP, path
    assert add_root(env.conn, str(tmp_path / "other"))  # a sibling is fine
    assert len(list_roots(env.conn)) == 2


def test_sibling_with_a_shared_name_prefix_does_not_overlap(tmp_path):
    env = make_env(tmp_path, {})
    (tmp_path / "lib2").mkdir()
    add_root(env.conn, str(tmp_path / "lib2"))


def test_find_roots_by_id_label_path_and_ambiguity(tmp_path):
    env = make_env(tmp_path, {})
    other = tmp_path / "Other"
    other.mkdir()
    second = add_root(env.conn, str(other), label="lib")  # same label as the first, on purpose
    assert [r.root_id for r in find_roots(env.conn, second.root_id)] == [second.root_id]
    assert [r.root_id for r in find_roots(env.conn, str(other))] == [second.root_id]
    with pytest.raises(KvError) as caught:
        find_roots(env.conn, "lib")
    assert caught.value.code == ErrorCode.AMBIGUOUS and len(caught.value.details["candidates"]) == 2
    with pytest.raises(KvError) as caught:
        find_roots(env.conn, "nonsense")
    assert caught.value.code == ErrorCode.NOT_FOUND
    assert len(find_roots(env.conn, None)) == 2


# --- resolve and explain ----------------------------------------------------------------------------------------


def test_resolve_by_every_kind_of_reference(tmp_path):
    env = make_env(tmp_path, {"papers/kaya2022.pdf": b"%PDF-1.4 kaya", "other.txt": "other"})
    env.scan()
    loc = env.location("papers/kaya2022.pdf")
    document = env.one("SELECT document_id FROM document_artifact WHERE artifact_id = ?", loc["artifact_id"])
    for reference in (document, loc["artifact_id"], loc["artifact_id"][:10].upper(), "kaya2022.pdf", "papers/kaya2022.pdf",
                      "PAPERS\\Kaya2022.PDF", str(env.lib / "papers" / "kaya2022.pdf")):
        match = resolve.resolve_one(env.conn, reference)
        assert match.document_id == document, reference


def test_resolve_never_guesses_between_two_documents(tmp_path):
    env = make_env(tmp_path, {"a/report.pdf": b"%PDF-1.4 one", "b/report.pdf": b"%PDF-1.4 two"})
    env.scan()
    with pytest.raises(KvError) as caught:
        resolve.resolve_one(env.conn, "report.pdf")
    assert caught.value.code == ErrorCode.AMBIGUOUS
    assert len(caught.value.details["candidates"]) == 2
    assert resolve.resolve_one(env.conn, "a/report.pdf").path == "a/report.pdf"


def test_two_locations_of_one_document_are_not_ambiguous(tmp_path):
    env = make_env(tmp_path, {"a/same.txt": "dup", "b/same.txt": "dup"})
    env.scan()
    assert resolve.resolve_one(env.conn, "same.txt").document_id


def test_a_name_matching_only_a_past_location_is_answered_from_history(tmp_path):
    env = make_env(tmp_path, {"old name.pdf": b"%PDF-1.4 moved"})
    env.scan()
    os.rename(env.lib / "old name.pdf", env.lib / "new name.pdf")
    env.scan()
    match = resolve.resolve_one(env.conn, "old name.pdf")
    assert match.historical is True
    assert resolve.resolve_one(env.conn, "new name.pdf").historical is False
    assert resolve.resolve_one(env.conn, "old name.pdf").document_id == resolve.resolve_one(env.conn, "new name.pdf").document_id


def test_a_current_match_beats_history_so_a_stale_name_cannot_make_a_current_file_ambiguous(tmp_path):
    env = make_env(tmp_path, {"report.pdf": b"%PDF-1.4 first"})
    env.scan()
    write(env.lib / "report.pdf", b"%PDF-1.4 second, different")  # replaced: the old location is now history
    env.scan()
    match = resolve.resolve_one(env.conn, "report.pdf")
    assert match.historical is False


def test_resolve_not_found_and_empty_and_short_hex(tmp_path):
    env = make_env(tmp_path, {"a.txt": "x"})
    env.scan()
    for text, code in (("nothing.pdf", ErrorCode.NOT_FOUND), ("   ", ErrorCode.INVALID_ARGUMENTS), ("abcd", ErrorCode.NOT_FOUND)):
        with pytest.raises(KvError) as caught:
            resolve.resolve_one(env.conn, text)
        assert caught.value.code == code, text


def test_path_matching_treats_percent_and_underscore_literally(tmp_path):
    env = make_env(tmp_path, {"50%_off.txt": "a", "50xxoff.txt": "b"})
    env.scan()
    assert resolve.resolve_one(env.conn, "50%_off.txt").path == "50%_off.txt"


def test_explain_shows_locations_history_and_derived_availability(tmp_path):
    env = make_env(tmp_path, {"a.txt": "content"})
    env.scan()
    document = resolve.resolve_one(env.conn, "a.txt").document_id
    doc = explain.explain_document(env.conn, document)
    art = doc["artifacts"][0]
    assert art["available"] is True and art["canonical"] is True
    assert art["locations"][0]["history"][0]["event"] == "first_seen"
    assert doc["metadata"]["status"] == "not_yet_available", "an unbuilt section must say so, not be left out"
    os.remove(env.lib / "a.txt")
    env.scan()
    doc = explain.explain_document(env.conn, document)
    assert doc["artifacts"][0]["available"] is False
    assert any(w["code"] == "KVD_NOT_AVAILABLE" for w in doc["warnings"])


def test_explain_flags_an_extension_that_contradicts_the_bytes(tmp_path):
    env = make_env(tmp_path, {"report.pdf": "this is plainly not a pdf"})
    env.scan()
    doc = explain.explain_document(env.conn, resolve.resolve_one(env.conn, "report.pdf").document_id)
    assert any(w["code"] == "KVD_KIND_MISMATCH" for w in doc["warnings"])


def test_explain_unknown_document_is_not_found(tmp_path):
    env = make_env(tmp_path, {})
    with pytest.raises(KvError) as caught:
        explain.explain_document(env.conn, "0" * 32)
    assert caught.value.code == ErrorCode.NOT_FOUND


# --- stats ------------------------------------------------------------------------------------------------------


def test_stats_separates_inventory_from_health(tmp_path):
    env = make_env(tmp_path, {"a.pdf": b"%PDF-1.4 a", "b.txt": "dup", "c.txt": "dup", "fake.pdf": "not a pdf", "gone.txt": "bye"})
    env.scan()
    os.remove(env.lib / "gone.txt")
    env.scan()
    s = stats.library_stats(env.conn)
    inv, health = s["inventory"], s["health"]
    assert inv["documents"] == 4 and inv["artifacts"] == 4 and inv["locations_current"] == 5
    assert inv["locations_by_state"] == {"active": 4, "missing": 1}
    assert inv["by_extension_kind"]["pdf"] == 2
    assert health["locations_missing"] == 1
    assert (health["exact_duplicate_groups"], health["exact_duplicate_files"]) == (1, 2)
    assert health["exact_duplicate_bytes_reclaimable"] == 3
    assert health["extension_content_mismatches"] == 1
    assert "locations_missing" not in inv and "documents" not in health


# --- doctor -----------------------------------------------------------------------------------------------------


def test_doctor_is_clean_on_a_healthy_catalog_and_names_what_it_checked(tmp_path):
    env = make_env(tmp_path, {"a.txt": "x"})
    env.scan()
    assert doctor.run_doctor(env.conn) == []
    assert doctor.CHECKED_CATEGORIES == ["filesystem", "catalog", "extraction", "search"]


def test_doctor_reports_each_injected_defect_by_its_own_code(tmp_path):
    env = make_env(tmp_path, {"a.txt": "x", "b.txt": "y"})
    env.scan()
    env.conn.execute("PRAGMA foreign_keys = OFF")
    env.conn.execute("DELETE FROM document_artifact WHERE artifact_id = ?", (env.location("a.txt")["artifact_id"],))
    env.conn.execute("DELETE FROM artifact WHERE artifact_id = ?", (env.location("b.txt")["artifact_id"],))
    env.conn.execute("INSERT INTO scan_run (run_id, root_id, started_at, status, app_version) VALUES ('r', ?, 'x', 'running', '0')", (env.root.root_id,))
    env.conn.execute("INSERT INTO document (document_id, created_at) VALUES ('empty-doc', 'x')")
    codes = {f.code for f in doctor.run_doctor(env.conn)}
    assert {"KVD_ARTIFACT_WITHOUT_DOCUMENT", "KVD_ORPHAN_ROW", "KVD_STALE_RUN", "KVD_NO_CANONICAL"} <= codes


def test_doctor_reports_unavailable_roots_and_missing_files_but_changes_nothing(tmp_path):
    env = make_env(tmp_path, {"a.txt": "x"})
    env.scan()
    os.rename(env.lib, tmp_path / "away")
    revision, snapshot = env.revision(), env.snapshot()
    codes = {f.code for f in doctor.run_doctor(env.conn)}
    assert "KVD_ROOT_NOT_ONLINE" in codes
    assert env.revision() == revision and env.snapshot() == snapshot


def test_doctor_notes_a_root_that_is_back_but_not_yet_reconciled(tmp_path):
    env = make_env(tmp_path, {"a.txt": "x"})
    env.scan()
    os.rename(env.lib, tmp_path / "away")
    env.scan()
    os.rename(tmp_path / "away", env.lib)
    assert "KVD_ROOT_STATUS_STALE" in {f.code for f in doctor.run_doctor(env.conn)}


def test_doctor_does_not_read_file_contents(tmp_path, monkeypatch):
    env = make_env(tmp_path, {"a.txt": "x"})
    env.scan()
    monkeypatch.setattr("builtins.open", lambda *a, **k: pytest.fail("doctor opened a file"))
    doctor.run_doctor(env.conn)


# --- verify -----------------------------------------------------------------------------------------------------


def test_verify_counts_matches_and_reports_missing_without_changing_the_catalog(tmp_path):
    env = make_env(tmp_path, {"a.txt": "one", "b.txt": "two"})
    env.scan()
    assert verify.verify_hashes(env.conn).ok == 2
    os.remove(env.lib / "b.txt")
    revision, snapshot = env.revision(), env.snapshot()
    report = verify.verify_hashes(env.conn)
    assert (report.ok, report.missing, report.mismatched) == (1, ["b.txt"], [])
    assert env.revision() == revision and env.snapshot() == snapshot, "verify reports; it never edits the catalog"


def test_verify_skips_an_unavailable_root_and_says_so(tmp_path):
    env = make_env(tmp_path, {"a.txt": "one"})
    env.scan()
    os.rename(env.lib, tmp_path / "away")
    env.scan()
    report = verify.verify_hashes(env.conn)
    assert report.checked == 0 and report.skipped_roots[0]["status"] == "unavailable"


@pytest.mark.skipif(sys.platform != "win32", reason="uses Windows file locking semantics")
def test_a_file_locked_by_another_process_is_inaccessible_not_a_crash(tmp_path):
    env = make_env(tmp_path, {"locked.txt": "x"})
    import msvcrt
    handle = open(env.lib / "locked.txt", "r+b")
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        report = env.scan()
        assert (report.unreadable, report.hashed) == (1, 0), "measured: a byte-range lock makes the read fail with PermissionError"
        assert env.location("locked.txt")["state"] == "inaccessible"
        assert env.conn.in_transaction is False
    finally:
        handle.close()
    report = env.scan()  # the other process let go
    assert env.location("locked.txt")["state"] == "active" and report.new_artifacts == 1


def test_doctor_detects_a_row_that_violates_a_check_constraint(tmp_path):
    env = make_env(tmp_path, {"a.txt": "x"})
    env.scan()
    assert not any(f.code == "KVD_INTEGRITY" for f in doctor.run_doctor(env.conn)), "clean first: the oracle is alive"
    env.conn.execute("PRAGMA ignore_check_constraints = ON")
    env.conn.execute(
        "INSERT INTO location (location_id, root_id, relative_path, path_key, state, first_seen, last_seen) "
        "VALUES ('bad', ?, 'bad.txt', 'bad.txt', 'active', 'x', 'x')", (env.root.root_id,))
    env.conn.execute("PRAGMA ignore_check_constraints = OFF")
    findings = doctor.run_doctor(env.conn)
    assert any(f.code == "KVD_INTEGRITY" and f.severity == "error" for f in findings)
    assert doctor.has_errors(findings)
