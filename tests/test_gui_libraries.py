"""The list of libraries the window knows: tolerant in every direction, never inventing or removing a catalog, and choosing the start-up one."""

from __future__ import annotations

import json
import logging

from knowledgevista import paths
from knowledgevista.gui import libraries as L
from knowledgevista.gui.app import resolve_start_catalog


def make_catalog(folder, name="catalog.sqlite"):
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    target.write_bytes(b"")
    return target


def test_a_first_run_has_no_file_and_gets_an_empty_list(tmp_path):
    assert L.load(tmp_path / "none.json") == L.Registry()


def test_remembering_puts_the_library_first_and_marks_it_last(tmp_path):
    one, two = make_catalog(tmp_path / "one"), make_catalog(tmp_path / "two")
    registry = L.remembered(L.remembered(L.Registry(), one, "Chemistry"), two, "Physics")
    assert [k.name for k in registry.libraries] == ["Physics", "Chemistry"] and registry.last == str(two)
    again = L.remembered(registry, one)  # re-opening one moves it up; no new name was given, so the chosen name is kept
    assert [k.name for k in again.libraries] == ["Chemistry", "Physics"] and again.last == str(one)


def test_the_same_path_spelled_two_ways_is_one_library(tmp_path):
    target = make_catalog(tmp_path / "lib")
    registry = L.remembered(L.Registry(), target, "A")
    registry = L.remembered(registry, tmp_path / "lib" / ".." / "lib" / "catalog.sqlite", "A again")
    assert len(registry.libraries) == 1 and registry.libraries[0].name == "A again"


def test_the_list_is_capped_and_the_oldest_falls_off_without_touching_its_file(tmp_path):
    made = [make_catalog(tmp_path / f"lib{i}") for i in range(L.MAX_KNOWN + 3)]
    registry = L.Registry()
    for target in made:
        registry = L.remembered(registry, target)
    assert len(registry.libraries) == L.MAX_KNOWN and registry.libraries[0].path == str(made[-1])
    assert str(made[0]) not in [k.path for k in registry.libraries] and made[0].exists()


def test_a_name_defaults_to_the_folder_and_the_app_own_catalog_is_the_default_library(tmp_path):
    assert L.label_for(tmp_path / "Chemistry" / "catalog.sqlite") == "Chemistry"
    assert L.label_for(paths.catalog_path()) == L.DEFAULT_NAME


def test_save_then_load_returns_the_same_list_and_leaves_no_temporary_file(tmp_path):
    target = tmp_path / "sub" / "libs.json"
    original = L.remembered(L.remembered(L.Registry(), tmp_path / "a" / "catalog.sqlite", "A"), tmp_path / "b" / "catalog.sqlite", "B")
    assert L.save(original, target) is True and L.save(original, target) is True
    assert L.load(target) == original
    assert [p.name for p in target.parent.iterdir()] == ["libs.json"]


def test_the_default_location_is_in_the_config_folder():
    L.note_opened(paths.catalog_path())
    assert paths.gui_libraries_path().is_relative_to(paths.config_dir()) and L.load().last == str(paths.catalog_path())


def test_a_damaged_or_newer_file_gives_an_empty_list_and_says_so(tmp_path, caplog):
    target = tmp_path / "libs.json"
    target.write_text("{ not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="knowledgevista.gui.libraries"):
        assert L.load(target) == L.Registry()
    assert "damaged" in caplog.text
    target.write_text(json.dumps({"format": L.LIBRARIES_FORMAT + 1, "last": "x", "libraries": [{"path": "x", "name": "x"}]}), encoding="utf-8")
    assert L.load(target) == L.Registry()


def test_a_malformed_entry_is_dropped_and_the_rest_kept(tmp_path):
    target = tmp_path / "libs.json"
    good = str(tmp_path / "good" / "catalog.sqlite")
    target.write_text(json.dumps({"format": L.LIBRARIES_FORMAT, "last": 7, "libraries": [
        {"path": good, "name": "Good"}, {"path": 3, "name": "x"}, "nonsense", {"name": "no path"}, {"path": "   "},
        {"path": good, "name": "duplicate"}, {"path": str(tmp_path / "n" / "catalog.sqlite"), "name": 5}]}), encoding="utf-8")
    registry = L.load(target)
    assert [k.name for k in registry.libraries] == ["Good", "n"], "a non-string name falls back to the folder name; duplicates and junk are dropped"
    assert registry.last is None, "a last that is not text is ignored"


def test_forgetting_removes_it_from_the_list_and_never_from_the_disk(tmp_path):
    one, two = make_catalog(tmp_path / "one"), make_catalog(tmp_path / "two")
    registry = L.remembered(L.remembered(L.Registry(), one), two)
    after = L.forgotten(registry, two)
    assert [k.path for k in after.libraries] == [str(one)] and after.last is None and two.exists()


def test_the_last_library_is_offered_only_while_its_file_exists(tmp_path):
    target = make_catalog(tmp_path / "lib")
    registry = L.remembered(L.Registry(), target)
    assert L.last_existing(registry) == target
    target.unlink()
    assert L.last_existing(registry) is None
    assert L.last_existing(L.Registry()) is None


def test_the_menu_always_offers_the_default_library(tmp_path):
    names = [k.name for k in L.known_for_menu(L.Registry())]
    assert names == [L.DEFAULT_NAME]
    registry = L.remembered(L.Registry(), tmp_path / "x" / "catalog.sqlite", "X")
    assert [k.name for k in L.known_for_menu(registry)] == ["X", L.DEFAULT_NAME]
    registry = L.remembered(registry, paths.catalog_path())
    assert [k.name for k in L.known_for_menu(registry)] == [L.DEFAULT_NAME, "X"], "already listed, so not offered twice"


# ------------------------------------------------------------------------------------------------------- renaming, moving, asking


def test_renaming_changes_the_name_and_nothing_else(tmp_path):
    one, two = make_catalog(tmp_path / "one"), make_catalog(tmp_path / "two")
    registry = L.remembered(L.remembered(L.Registry(), one, "One"), two, "Two")
    after = L.renamed(registry, one, "  A   better  name ")
    assert [(k.path, k.name) for k in after.libraries] == [(str(two), "Two"), (str(one), "A better name")] and after.last == registry.last
    assert L.renamed(registry, one, "   ") == registry, "a blank name changes nothing"


def test_renaming_the_default_library_lists_it_first(tmp_path):
    registry = L.renamed(L.Registry(), paths.catalog_path(), "Everything")
    assert [(k.path, k.name) for k in registry.libraries] == [(str(paths.catalog_path()), "Everything")]
    assert [k.name for k in L.known_for_menu(registry)] == ["Everything"], "and it is not offered a second time as 'Default library'"


def test_moving_repoints_the_entry_keeping_its_name_place_and_last(tmp_path):
    old, other, new = (tmp_path / "old" / "catalog.sqlite"), (tmp_path / "other" / "catalog.sqlite"), (tmp_path / "new" / "catalog.sqlite")
    registry = L.remembered(L.remembered(L.Registry(), other, "Other"), old, "Chemistry")  # old is first and last
    after = L.moved(registry, old, new)
    assert [(k.path, k.name) for k in after.libraries] == [(str(new), "Chemistry"), (str(other), "Other")] and after.last == str(new)
    third = L.moved(L.remembered(L.Registry(), other, "Other"), old, new)  # moving a library that was not listed: it is listed now
    assert [k.name for k in third.libraries] == [L.label_for(new), "Other"] and third.last == str(other)


def test_moving_onto_a_path_already_listed_does_not_duplicate_it(tmp_path):
    old, new = tmp_path / "old" / "catalog.sqlite", tmp_path / "new" / "catalog.sqlite"
    registry = L.remembered(L.remembered(L.Registry(), new, "Stale entry"), old, "Chemistry")
    after = L.moved(registry, old, new)
    assert [(k.path, k.name) for k in after.libraries] == [(str(new), "Chemistry")]


def test_the_startup_question_is_a_setting_that_survives_every_edit(tmp_path):
    assert L.Registry().ask_at_startup is True
    listing = tmp_path / "libs.json"
    a, b, c = (tmp_path / n / "catalog.sqlite" for n in "abc")
    off = L.with_ask_at_startup(L.remembered(L.Registry(), a, "A"), False)
    L.save(off, listing)
    assert L.load(listing).ask_at_startup is False
    edited = L.moved(L.renamed(L.forgotten(L.remembered(off, b), b), a, "AA"), a, c)
    assert edited.ask_at_startup is False, "no edit of the list silently turns the question back on"
    listing.write_text(json.dumps({"format": L.LIBRARIES_FORMAT, "ask_at_startup": "no", "libraries": []}), encoding="utf-8")
    assert L.load(listing).ask_at_startup is True, "a value that is not a boolean is not believed"


def test_the_startup_question_is_asked_only_when_there_is_a_real_choice(tmp_path):
    one, two = make_catalog(tmp_path / "one"), make_catalog(tmp_path / "two")
    gone = tmp_path / "gone" / "catalog.sqlite"
    assert not L.should_ask(L.Registry()), "nothing known"
    assert not L.should_ask(L.remembered(L.Registry(), one)), "one library is not a choice"
    assert not L.should_ask(L.remembered(L.remembered(L.Registry(), one), gone)), "a library whose file has gone is not a choice"
    both = L.remembered(L.remembered(L.Registry(), one), two)
    assert L.should_ask(both)
    assert not L.should_ask(L.with_ask_at_startup(both, False)), "switched off"
    make_catalog(paths.catalog_path().parent, paths.catalog_path().name)
    assert L.should_ask(L.remembered(L.Registry(), one)), "the app's own library counts once it exists"


# ------------------------------------------------------------------------------------------------------- which library `kv gui` opens


def test_an_explicit_catalog_beats_the_last_one_which_beats_the_default(tmp_path):
    last, explicit = make_catalog(tmp_path / "last"), make_catalog(tmp_path / "explicit")
    listing = tmp_path / "libs.json"
    L.save(L.remembered(L.Registry(), last), listing)
    assert resolve_start_catalog(explicit, list_path=listing) == explicit
    assert resolve_start_catalog(None, list_path=listing) == last
    assert resolve_start_catalog(None, use_list=False, list_path=listing) == paths.catalog_path(), "a scripted run ignores the list"


def test_a_vanished_last_library_falls_back_to_the_default_not_to_an_invented_one(tmp_path):
    last = make_catalog(tmp_path / "last")
    listing = tmp_path / "libs.json"
    L.save(L.remembered(L.Registry(), last), listing)
    last.unlink()
    assert resolve_start_catalog(None, list_path=listing) == paths.catalog_path()
    assert not last.exists(), "choosing the start-up library never creates a file"
