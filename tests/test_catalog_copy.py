"""Copying a catalog to another folder ("Move catalog…"): faithful, read-only on the original, and unable to touch a single document.

The documents are the part of a library that cannot be rebuilt, so most of these tests are the same assertion made from different
sides: after a copy, a refusal or a failure, every file in the library's folder is byte-identical and the original catalog is still there.
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest
from lab import Lab
from support import tree_hashes

from knowledgevista import paths
from knowledgevista.db.catalog import open_catalog
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import catalog_copy
from knowledgevista.services.roots import list_roots


@pytest.fixture
def lab(tmp_path):
    made = Lab(tmp_path)
    yield made
    made.close()


def digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path) -> list[str]:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def everything_under(folder) -> list[str]:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*")) if folder.exists() else []


def test_the_copy_is_the_same_library_and_the_original_is_untouched(lab, tmp_path):
    destination = tmp_path / "Libraries" / "Moved"
    before_catalog, before_tree = digest(lab.catalog), tree_hashes(lab.env.lib)
    result = catalog_copy.copy_catalog(lab.catalog, destination)
    assert result.destination == destination / "catalog.sqlite" and result.destination.is_file()
    assert dump(result.destination) == dump(lab.catalog), "every table and row is the same"
    assert digest(lab.catalog) == before_catalog and lab.catalog.is_file(), "the original was neither changed nor removed"
    assert tree_hashes(lab.env.lib) == before_tree, "not one document was touched"
    moved = open_catalog(result.destination, create=False)
    try:
        assert moved.execute("SELECT library_id FROM library").fetchone()[0] == result.library_id
        assert [r.configured_path for r in list_roots(moved)] == [r.configured_path for r in list_roots(lab.conn)], "the folders are the same folders"
    finally:
        moved.close()


def test_the_extracted_text_and_metadata_caches_come_along(lab, tmp_path):
    destination = tmp_path / "Moved"
    old_index, old_meta = paths.index_path(lab.catalog), paths.metacache_path(lab.catalog)
    assert old_index.is_file(), "the lab really has extracted text, so this test can fail"
    result = catalog_copy.copy_catalog(lab.catalog, destination)
    new_index, new_meta = paths.index_path(result.destination), paths.metacache_path(result.destination)
    assert result.cache_copied and dump(new_index) == dump(old_index)
    assert new_meta.is_file() == old_meta.is_file() and (not old_meta.is_file() or dump(new_meta) == dump(old_meta))
    assert old_index.is_file(), "the original cache stays where it was"


def test_a_cache_that_cannot_be_copied_is_a_note_not_a_failure(lab, tmp_path):
    destination = tmp_path / "Moved"
    blocker = paths.index_path(destination / "catalog.sqlite")
    blocker.parent.mkdir(parents=True)
    blocker.write_text("already here", encoding="utf-8")
    result = catalog_copy.copy_catalog(lab.catalog, destination)
    assert result.destination.is_file() and any("extracted text was not copied" in n for n in result.notes)
    assert blocker.read_text(encoding="utf-8") == "already here", "an existing file is never overwritten, cache or not"


def test_a_destination_inside_the_librarys_own_folder_is_refused_and_nothing_is_created(lab, tmp_path):
    before = tree_hashes(lab.env.lib)
    for inside in (lab.env.lib, lab.env.lib / "papers", lab.env.lib / "new" / "deeper"):
        with pytest.raises(KvError) as caught:
            catalog_copy.copy_catalog(lab.catalog, inside)
        assert caught.value.code == ErrorCode.INVALID_ARGUMENTS and "catalogue the catalog" in str(caught.value)
    assert tree_hashes(lab.env.lib) == before and not (lab.env.lib / "new").exists() and not (lab.env.lib / "catalog.sqlite").exists()


def test_a_folder_beside_the_library_folder_is_fine(lab, tmp_path):
    sibling = lab.env.lib.parent / "lib-catalog"  # shares a prefix with the root, but is not inside it
    assert catalog_copy.copy_catalog(lab.catalog, sibling).destination.is_file()


def test_an_existing_catalog_in_the_destination_is_never_overwritten(lab, tmp_path):
    destination = tmp_path / "Taken"
    destination.mkdir()
    (destination / "catalog.sqlite").write_bytes(b"somebody else's data")
    before = digest(destination / "catalog.sqlite")
    with pytest.raises(KvError, match="already holds a file named catalog.sqlite"):
        catalog_copy.copy_catalog(lab.catalog, destination)
    assert digest(destination / "catalog.sqlite") == before and everything_under(destination) == ["catalog.sqlite"]


def test_copying_a_library_onto_itself_is_refused(lab, tmp_path):
    first = catalog_copy.copy_catalog(lab.catalog, tmp_path / "A").destination
    with pytest.raises(KvError, match="already in that folder"):
        catalog_copy.copy_catalog(first, tmp_path / "A")


def test_a_file_that_is_not_a_library_is_refused_and_nothing_is_created(lab, tmp_path):
    foreign = tmp_path / "other.sqlite"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE notes (body TEXT)")
    conn.commit()
    conn.close()
    junk = tmp_path / "junk.sqlite"
    junk.write_text("not a database", encoding="utf-8")
    before = {foreign: digest(foreign), junk: digest(junk)}
    for refused in (foreign, junk):
        with pytest.raises(KvError):
            catalog_copy.copy_catalog(refused, tmp_path / "Out")
    assert not (tmp_path / "Out").exists() and {p: digest(p) for p in before} == before


def test_a_missing_original_is_refused_without_creating_anything(lab, tmp_path):
    with pytest.raises(KvError) as caught:
        catalog_copy.copy_catalog(tmp_path / "nowhere" / "catalog.sqlite", tmp_path / "Out")
    assert caught.value.code == ErrorCode.CATALOG_MISSING and not (tmp_path / "Out").exists()


def test_a_relative_folder_is_refused(lab):
    with pytest.raises(KvError, match="full folder path"):
        catalog_copy.copy_catalog(lab.catalog, "some/relative")


def test_a_failure_part_way_removes_only_its_own_temporary_file_and_changes_nothing_else(lab, tmp_path, monkeypatch):
    destination = tmp_path / "Out"
    before = (digest(lab.catalog), tree_hashes(lab.env.lib))

    def explode(*_args):
        raise OSError("disk went away")

    monkeypatch.setattr(catalog_copy.os, "rename", explode)
    with pytest.raises(KvError, match="The original was not changed"):
        catalog_copy.copy_catalog(lab.catalog, destination)
    assert not destination.exists(), "a folder the call made, left empty, is removed again"
    assert (digest(lab.catalog), tree_hashes(lab.env.lib)) == before


def test_a_failure_in_a_folder_that_already_existed_leaves_that_folder_and_its_files(lab, tmp_path, monkeypatch):
    destination = tmp_path / "Existing"
    destination.mkdir()
    (destination / "mine.txt").write_text("keep me", encoding="utf-8")
    monkeypatch.setattr(catalog_copy.os, "rename", lambda *_a: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(KvError):
        catalog_copy.copy_catalog(lab.catalog, destination)
    assert everything_under(destination) == ["mine.txt"] and (destination / "mine.txt").read_text(encoding="utf-8") == "keep me"
