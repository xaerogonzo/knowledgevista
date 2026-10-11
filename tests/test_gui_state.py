"""What the window remembers: tolerant in every direction, kept for each library separately, and never applied to the wrong one."""

from __future__ import annotations

import json
import logging

import pytest

from knowledgevista import paths
from knowledgevista.gui import state as S


def sample(library_id="lib1", **changes) -> S.GuiState:
    values = dict(library_id=library_id, geometry="AAEC", layout="BBB=", scope="collection:abc", document_id="d" * 32, filter_text="ester",
                  search_text="year:2020 aqueous", tab="review", sort_column=3, sort_descending=True, last_folder="D:/x")
    values.update(changes)
    return S.GuiState(**values)


def test_a_first_run_has_no_file_and_gets_the_defaults(tmp_path):
    assert S.load(tmp_path / "none.json", "lib1") == S.GuiState(library_id="lib1")


def test_save_then_load_returns_the_same_state(tmp_path):
    original = sample()
    target = tmp_path / "s.json"
    assert S.save(original, target) is True
    assert S.load(target, "lib1") == original


def test_the_default_location_is_in_the_config_folder(tmp_path):
    assert S.save(S.GuiState(library_id="lib1", tab="search")) is True
    assert paths.gui_state_path().exists() and paths.gui_state_path().is_relative_to(paths.config_dir())
    assert S.load(library_id="lib1").tab == "search"


def test_saving_is_atomic_and_leaves_no_temporary_file(tmp_path):
    target = tmp_path / "sub" / "s.json"
    S.save(S.GuiState(library_id="lib1"), target)
    S.save(S.GuiState(library_id="lib1", tab="review"), target)
    assert [p.name for p in target.parent.iterdir()] == ["s.json"]


def test_a_damaged_file_gives_the_defaults_and_says_so(tmp_path, caplog):
    target = tmp_path / "s.json"
    target.write_text("{ this is not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="knowledgevista.gui.state"):
        assert S.load(target, "lib1") == S.GuiState(library_id="lib1")
    assert any("damaged" in r.getMessage() for r in caplog.records), "a silent reset would hide that the file was lost"


@pytest.mark.parametrize("content", ['[]', '"text"', "null", "{}", json.dumps({"format": 99, "scope": "view:missing"}), json.dumps({"scope": "view:missing"}),
                                      json.dumps({"format": True, "libraries": {"lib1": {"tab": "review"}}})])
def test_a_file_that_is_not_ours_gives_the_defaults(tmp_path, content):
    target = tmp_path / "s.json"
    target.write_text(content, encoding="utf-8")
    assert S.load(target, "lib1") == S.GuiState(library_id="lib1"), "no format marker, or a newer one: not understood, so not trusted"


def entry_file(target, entry, library_id="lib1", defaults=None):
    target.write_text(json.dumps({"format": S.STATE_FORMAT, "defaults": defaults or {}, "libraries": {library_id: entry}}), encoding="utf-8")


def test_a_value_of_the_wrong_type_is_replaced_not_believed(tmp_path):
    target = tmp_path / "s.json"
    entry_file(target, {"scope": 7, "sort_column": "3", "sort_descending": 1, "filter_text": ["x"], "tab": "nonsense", "document_id": 5, "geometry": "ok=="})
    loaded = S.load(target, "lib1")
    assert (loaded.scope, loaded.sort_column, loaded.sort_descending, loaded.filter_text, loaded.tab, loaded.document_id) == (
        S.DEFAULT_SCOPE, -1, False, "", "documents", None)
    assert loaded.geometry == "ok==", "the good values beside the bad ones are kept"


def test_a_stored_true_is_not_a_column_number(tmp_path):
    target = tmp_path / "s.json"
    entry_file(target, {"sort_column": True})
    assert S.load(target, "lib1").sort_column == -1


def test_unknown_keys_are_ignored(tmp_path):
    target = tmp_path / "s.json"
    entry_file(target, {"tab": "review", "colour": "red"})
    assert S.load(target, "lib1").tab == "review"


def test_an_entry_cannot_claim_to_be_another_library(tmp_path):
    target = tmp_path / "s.json"
    entry_file(target, {"tab": "review", "library_id": "someone-else"})
    assert S.load(target, "lib1").library_id == "lib1"


# --------------------------------------------------------------------------------------------------------- one entry per library


def test_each_library_keeps_its_own_place(tmp_path):
    target = tmp_path / "s.json"
    S.save(sample("A", scope="collection:x", document_id="docA", tab="review", filter_text="alpha"), target)
    S.save(sample("B", scope="all", document_id="docB", tab="health", filter_text="beta", sort_column=1), target)
    a, b = S.load(target, "A"), S.load(target, "B")
    assert (a.scope, a.document_id, a.tab, a.filter_text) == ("collection:x", "docA", "review", "alpha"), "saving B did not overwrite A"
    assert (b.document_id, b.tab, b.filter_text, b.sort_column) == ("docB", "health", "beta", 1)


def test_each_library_keeps_its_own_window_shape(tmp_path):
    target = tmp_path / "s.json"
    S.save(sample("A", geometry="geomA", layout="layoutA"), target)
    S.save(sample("B", geometry="geomB", layout="layoutB"), target)
    assert (S.load(target, "A").geometry, S.load(target, "A").layout) == ("geomA", "layoutA")
    assert (S.load(target, "B").geometry, S.load(target, "B").layout) == ("geomB", "layoutB")


def test_a_library_never_seen_before_starts_from_the_window_last_shape_with_nothing_selected(tmp_path):
    target = tmp_path / "s.json"
    S.save(sample("A", geometry="lastgeom", layout="lastlayout", last_folder="D:/p"), target)
    fresh = S.load(target, "NEW")
    assert (fresh.geometry, fresh.layout, fresh.last_folder) == ("lastgeom", "lastlayout", "D:/p")
    assert (fresh.scope, fresh.document_id, fresh.filter_text, fresh.search_text, fresh.tab, fresh.library_id) == (S.DEFAULT_SCOPE, None, "", "", "documents", "NEW")


def test_the_last_folder_is_shared_by_every_library(tmp_path):
    target = tmp_path / "s.json"
    S.save(sample("A", last_folder="D:/old"), target)
    S.save(sample("B", last_folder="D:/newest"), target)
    assert S.load(target, "A").last_folder == "D:/newest"


def test_the_entries_are_capped_and_the_least_recently_used_falls_off(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "MAX_LIBRARIES", 3)
    target = tmp_path / "s.json"
    for name in ("A", "B", "C"):
        S.save(sample(name, tab="review"), target)
    S.save(sample("A", tab="health"), target)  # A is used again, so B is now the oldest
    S.save(sample("D", tab="review"), target)
    assert S.load(target, "B").tab == "documents", "the oldest entry was dropped"
    assert [S.load(target, n).tab for n in ("A", "C", "D")] == ["health", "review", "review"]


def test_a_format_1_file_is_still_read_as_that_librarys_entry_and_the_default_shape(tmp_path):
    target = tmp_path / "s.json"
    old = {"format": 1, "library_id": "old-lib", "geometry": "g1", "layout": "l1", "scope": "collection:z", "document_id": "doc", "filter_text": "f",
           "search_text": "s", "tab": "review", "sort_column": 2, "sort_descending": True, "last_folder": "D:/q"}
    target.write_text(json.dumps(old), encoding="utf-8")
    kept = S.load(target, "old-lib")
    assert (kept.scope, kept.document_id, kept.tab, kept.sort_column, kept.geometry) == ("collection:z", "doc", "review", 2, "g1")
    other = S.load(target, "other-lib")
    assert (other.geometry, other.layout, other.last_folder, other.document_id) == ("g1", "l1", "D:/q", None)
    S.save(sample("old-lib", tab="health"), target)
    assert json.loads(target.read_text(encoding="utf-8"))["format"] == S.STATE_FORMAT, "saving upgrades the file"


def test_saving_without_a_library_records_only_the_window_shape(tmp_path):
    target = tmp_path / "s.json"
    S.save(S.GuiState(geometry="g", layout="l", tab="review"), target)
    assert json.loads(target.read_text(encoding="utf-8"))["libraries"] == {}
    assert S.load(target, None) == S.GuiState(geometry="g", layout="l")


def test_an_unwritable_location_does_not_stop_the_window(tmp_path, caplog):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    with caplog.at_level(logging.WARNING, logger="knowledgevista.gui.state"):
        assert S.save(S.GuiState(library_id="lib1"), blocker / "s.json") is False
    assert any("could not save" in r.getMessage() for r in caplog.records)
