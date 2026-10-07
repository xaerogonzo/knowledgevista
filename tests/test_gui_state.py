"""What the window remembers: tolerant in every direction, and never applied to the wrong library."""

from __future__ import annotations

import json
import logging

import pytest

from knowledgevista import paths
from knowledgevista.gui import state as S


def test_a_first_run_has_no_file_and_gets_the_defaults(tmp_path):
    assert S.load(tmp_path / "none.json") == S.GuiState()


def test_save_then_load_returns_the_same_state(tmp_path):
    original = S.GuiState(library_id="lib1", geometry="AAEC", layout="BBB=", scope="collection:abc", document_id="d" * 32, filter_text="ester",
                          search_text="year:2020 aqueous", tab="review", sort_column=3, sort_descending=True, last_folder="D:/x")
    target = tmp_path / "s.json"
    assert S.save(original, target) is True
    assert S.load(target) == original


def test_the_default_location_is_in_the_config_folder(tmp_path):
    assert S.save(S.GuiState(tab="search")) is True
    assert paths.gui_state_path().exists() and paths.gui_state_path().is_relative_to(paths.config_dir())
    assert S.load().tab == "search"


def test_saving_is_atomic_and_leaves_no_temporary_file(tmp_path):
    target = tmp_path / "sub" / "s.json"
    S.save(S.GuiState(), target)
    S.save(S.GuiState(tab="review"), target)
    assert [p.name for p in target.parent.iterdir()] == ["s.json"]


def test_a_damaged_file_gives_the_defaults_and_says_so(tmp_path, caplog):
    target = tmp_path / "s.json"
    target.write_text("{ this is not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="knowledgevista.gui.state"):
        assert S.load(target) == S.GuiState()
    assert any("damaged" in r.getMessage() for r in caplog.records), "a silent reset would hide that the file was lost"


@pytest.mark.parametrize("content", ['[]', '"text"', "null", "{}", json.dumps({"format": 99, "scope": "view:missing"}), json.dumps({"scope": "view:missing"})])
def test_a_file_that_is_not_ours_gives_the_defaults(tmp_path, content):
    target = tmp_path / "s.json"
    target.write_text(content, encoding="utf-8")
    assert S.load(target) == S.GuiState(), "no format marker, or a newer one: not understood, so not trusted"


def test_a_value_of_the_wrong_type_is_replaced_not_believed(tmp_path):
    target = tmp_path / "s.json"
    target.write_text(json.dumps({"format": S.STATE_FORMAT, "scope": 7, "sort_column": "3", "sort_descending": 1, "filter_text": ["x"],
                                  "tab": "nonsense", "document_id": 5, "geometry": "ok=="}), encoding="utf-8")
    loaded = S.load(target)
    assert (loaded.scope, loaded.sort_column, loaded.sort_descending, loaded.filter_text, loaded.tab, loaded.document_id) == (
        S.DEFAULT_SCOPE, -1, False, "", "documents", None)
    assert loaded.geometry == "ok==", "the good values beside the bad ones are kept"


def test_a_stored_true_is_not_a_column_number(tmp_path):
    target = tmp_path / "s.json"
    target.write_text(json.dumps({"format": S.STATE_FORMAT, "sort_column": True}), encoding="utf-8")
    assert S.load(target).sort_column == -1


def test_unknown_keys_are_ignored(tmp_path):
    target = tmp_path / "s.json"
    target.write_text(json.dumps({"format": S.STATE_FORMAT, "tab": "review", "colour": "red"}), encoding="utf-8")
    assert S.load(target).tab == "review"


def test_a_selection_is_only_applied_to_the_library_it_came_from():
    saved = S.GuiState(library_id="A", geometry="g", layout="l", scope="collection:x", document_id="doc", filter_text="f", search_text="s",
                       tab="review", last_folder="D:/p")
    same = saved.for_library("A")
    assert same.document_id == "doc" and same.scope == "collection:x"
    other = saved.for_library("B")
    assert (other.geometry, other.layout, other.last_folder) == ("g", "l", "D:/p"), "the window's shape follows the person"
    assert (other.scope, other.document_id, other.filter_text, other.search_text, other.tab) == (S.DEFAULT_SCOPE, None, "", "", "documents")
    assert other.library_id == "B"


def test_an_unwritable_location_does_not_stop_the_window(tmp_path, caplog):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    with caplog.at_level(logging.WARNING, logger="knowledgevista.gui.state"):
        assert S.save(S.GuiState(), blocker / "s.json") is False
    assert any("could not save" in r.getMessage() for r in caplog.records)
