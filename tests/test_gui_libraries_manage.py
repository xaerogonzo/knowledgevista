"""Managing libraries from the window: each library keeps its own place, the manage dialog (rename, show, remove, move a catalog), and the
choice offered at launch.

The rule behind most of these: the documents are the part that cannot be rebuilt. So every move, refusal and failure is checked from the
documents' side too: after it, every file in the library's folder is byte-identical and the original catalog is still there.
"""

from __future__ import annotations

import hashlib

import pytest
from guisupport import pump, wait_for
from lab import Lab
from PySide6.QtCore import Qt
from support import tree_hashes
from winhelp import close_dialogs, error_texts, open_window, press, select_document, settle, the_dialog, titles

from knowledgevista import paths
from knowledgevista.gui import libraries as L
from knowledgevista.gui import text as T
from knowledgevista.gui.app import Session, choose_at_startup, prepare
from knowledgevista.gui.dialogs import LibraryChooserDialog
from knowledgevista.gui.shell import RecordingShell

OPTIONS = {"shell": RecordingShell(), "remember": False, "poll_ms": 150}


@pytest.fixture
def lab(qapp, tmp_path):
    made = Lab(tmp_path)
    yield made
    made.close()


def hold(window, options=None):
    return Session(window, visible=False, **(options if options is not None else OPTIONS))


def finish(session, *extra):
    for window in (session.window, *extra):
        try:
            close_dialogs(window)
            window.shutdown()
            window.close()
            window.deleteLater()
        except RuntimeError:
            pass  # a window the session replaced has already been deleted


def settled(session):
    wait_for(lambda: session.window.listing is not None, what="the first snapshot of the new window")
    settle(session.window)
    return session.window


def digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------------------------------- each library keeps its own place


def test_going_back_to_a_library_returns_to_where_you_were_in_it(lab, tmp_path):
    second, _ = prepare(tmp_path / "chem" / "catalog.sqlite")
    state_file, listing = tmp_path / "state.json", tmp_path / "libs.json"
    options = {**OPTIONS, "remember": True, "state_path": state_file, "libraries_path": listing}
    first = open_window(lab.catalog, remember=True, state_path=state_file, libraries_path=listing)
    session = hold(first, options)
    try:
        select_document(first, "papers/aqueous.pdf")
        first.filter_box.setText("papers")
        first.tabs.setCurrentIndex(2)
        wanted_document = first.current_document
        assert session.switch(str(second)) is True
        there = settled(session)
        assert (there.filter_box.text(), there.current_tab, there.current_document) == ("", "documents", None), "the other library does not inherit this one's place"
        there.filter_box.setText("beta")
        there.tabs.setCurrentIndex(3)
        assert session.switch(str(lab.catalog)) is True
        back = settled(session)
        assert (back.filter_box.text(), back.current_tab, back.current_document) == ("papers", "review", wanted_document)
        assert session.switch(str(second)) is True
        again = settled(session)
        assert (again.filter_box.text(), again.current_tab) == ("beta", "health"), "and the other one remembered its own"
    finally:
        finish(session, first)


# ------------------------------------------------------------------------------------------------------- managing the libraries


def row_for(dialog, path) -> int:
    for row in range(dialog.table.rowCount()):
        if dialog.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == str(path):
            return row
    raise AssertionError(f"no row for {path} among {[dialog.table.item(r, 1).text() for r in range(dialog.table.rowCount())]}")


def managed(lab, tmp_path):
    """A window on the lab library with a list holding it, a second library that exists, and one whose file has gone; the dialog is open."""
    listing = tmp_path / "libs.json"
    other, _ = prepare(tmp_path / "other" / "catalog.sqlite")
    gone = tmp_path / "gone" / "catalog.sqlite"
    registry = L.Registry()
    for catalog, name in ((gone, "Gone"), (other, "Other"), (lab.catalog, "Lab")):
        registry = L.remembered(registry, catalog, name)
    L.save(registry, listing)
    window = open_window(lab.catalog, remember=True, libraries_path=listing)
    window.act_manage_libraries.trigger()
    return window, the_dialog(window, "manageLibrariesDialog"), listing, other, gone


def test_the_manage_dialog_lists_every_library_with_its_state_and_offers_only_what_is_safe(lab, tmp_path):
    prepare(paths.catalog_path())  # the app's own library exists, so "cannot move it" is the rule at work and not just a missing file
    window, dialog, listing, other, gone = managed(lab, tmp_path)
    try:
        states = {dialog.table.item(r, 1).text(): dialog.table.item(r, 2).text() for r in range(dialog.table.rowCount())}
        assert states == {str(lab.catalog): "open now", str(other): "", str(gone): "file missing", str(paths.catalog_path()): "default"}

        def offered(path):
            dialog.table.selectRow(row_for(dialog, path))
            return {"rename": dialog.rename_button.isEnabled(), "move": dialog.move_button.isEnabled(), "reveal": dialog.reveal_button.isEnabled(),
                    "remove": dialog.remove_button.isEnabled()}

        assert offered(other) == {"rename": True, "move": True, "reveal": True, "remove": True}
        assert offered(lab.catalog) == {"rename": True, "move": True, "reveal": True, "remove": False}, "the open library cannot be taken off the list"
        assert offered(gone) == {"rename": True, "move": False, "reveal": False, "remove": True}, "a missing file can be renamed or removed, not moved or shown"
        assert offered(paths.catalog_path()) == {"rename": True, "move": False, "reveal": True, "remove": False}, "the app's own library stays where kv looks for it"
        assert T.audit(window) == [], "the dialog shows names and paths as text only"
    finally:
        finish(hold(window))


def test_renaming_a_library_changes_its_name_in_the_list_and_the_title_of_the_open_one(lab, tmp_path):
    window, dialog, listing, other, gone = managed(lab, tmp_path)
    try:
        dialog.table.selectRow(row_for(dialog, other))
        press(dialog.rename_button)
        rename = the_dialog(window, "renameLibraryDialog")
        assert rename.combo.currentText() == "Other", "the dialog starts from the current name"
        rename.combo.setEditText("  Organic   chemistry ")
        rename.accept()
        pump(50)
        assert {k.path: k.name for k in L.load(listing).libraries}[str(other)] == "Organic chemistry"
        assert dialog.table.item(row_for(dialog, other), 0).text() == "Organic chemistry"
        assert other.is_file() and window.windowTitle() == "Knowledge Vista — Lab", "another library's rename does not retitle this window"

        window.rename_library(str(lab.catalog), "Papers")
        assert window.windowTitle() == "Knowledge Vista — Papers" and window.library_name == "Papers"
        assert {k.path: k.name for k in L.load(listing).libraries}[str(lab.catalog)] == "Papers"
    finally:
        finish(hold(window))


def test_removing_a_library_takes_it_off_the_list_and_never_touches_its_files(lab, tmp_path):
    window, dialog, listing, other, gone = managed(lab, tmp_path)
    try:
        before = digest(other)
        dialog.table.selectRow(row_for(dialog, other))
        press(dialog.remove_button)
        assert str(other) not in [k.path for k in L.load(listing).libraries]
        assert other.is_file() and digest(other) == before, "the catalog file is exactly where and what it was"
        assert not any(dialog.table.item(r, 1).text() == str(other) for r in range(dialog.table.rowCount()))

        window.remove_library(str(lab.catalog))  # the open one: refused, still listed
        assert str(lab.catalog) in [k.path for k in L.load(listing).libraries] and "cannot be removed" in window.messages[-1][1]
    finally:
        finish(hold(window))


def test_show_in_folder_asks_the_shell_and_nothing_else(lab, tmp_path):
    window, dialog, listing, other, gone = managed(lab, tmp_path)
    try:
        dialog.table.selectRow(row_for(dialog, other))
        press(dialog.reveal_button)
        assert window.shell.calls == [("reveal", str(other))]
    finally:
        finish(hold(window))


# ------------------------------------------------------------------------------------------------------------ moving a catalog


def test_moving_another_library_copies_its_catalog_and_leaves_the_original_and_every_document(lab, tmp_path):
    other_lab = Lab(tmp_path / "second")  # a library with real documents, which is NOT the one open
    empty, _ = prepare(tmp_path / "empty" / "catalog.sqlite")
    listing = tmp_path / "libs.json"
    L.save(L.remembered(L.remembered(L.Registry(), other_lab.catalog, "Papers"), empty, "Empty"), listing)
    window = open_window(empty, remember=True, libraries_path=listing)
    try:
        destination = tmp_path / "Moved"
        before_catalog, before_tree = digest(other_lab.catalog), tree_hashes(other_lab.env.lib)
        window.move_library(str(other_lab.catalog), str(destination))
        wait_for(lambda: any(b.objectName() == "infoBox" for b in window._boxes), what="the copy to finish")
        settle(window)
        assert (destination / "catalog.sqlite").is_file() and not error_texts(window)
        assert digest(other_lab.catalog) == before_catalog and tree_hashes(other_lab.env.lib) == before_tree, "original catalog and every document untouched"
        registry = L.load(listing)
        assert registry.find(destination / "catalog.sqlite").name == "Papers" and registry.find(other_lab.catalog) is None, "the list points at the copy now"
        info = next(b for b in window._boxes if b.objectName() == "infoBox").text()
        assert "was left where it is" in info and str(other_lab.catalog) in info and "Your documents were not touched" in info
        assert window.catalog == empty and not window._closing, "moving another library does not move this window"
    finally:
        finish(hold(window))
        other_lab.close()


def test_moving_the_open_library_switches_to_the_copy_and_says_where_the_old_file_is(lab, tmp_path):
    listing = tmp_path / "libs.json"
    options = {**OPTIONS, "remember": True, "libraries_path": listing, "state_path": tmp_path / "state.json"}
    window = open_window(lab.catalog, remember=True, libraries_path=listing, state_path=tmp_path / "state.json")
    session = hold(window, options)
    try:
        destination = tmp_path / "Moved"
        before_catalog, before_tree = digest(lab.catalog), tree_hashes(lab.env.lib)
        window.move_library(str(lab.catalog), str(destination))
        wait_for(lambda: session.window is not window, timeout_ms=30000, what="the window to switch to the copy")
        now = settled(session)
        assert now.catalog == destination / "catalog.sqlite" and len(titles(now)) == 7 and now.library_id == window.library_id, "the same library, from its new place"
        assert digest(lab.catalog) == before_catalog and tree_hashes(lab.env.lib) == before_tree
        info = next(b for b in now._boxes if b.objectName() == "infoBox").text()
        assert str(lab.catalog) in info and "was left where it is" in info
        assert L.load(listing).last == str(destination / "catalog.sqlite") and L.load(listing).find(lab.catalog) is None
    finally:
        finish(session, window)


def test_a_move_the_service_refuses_changes_nothing_and_says_why(lab, tmp_path):
    window = open_window(lab.catalog)
    try:
        before = (digest(lab.catalog), tree_hashes(lab.env.lib))
        window.move_library(str(lab.catalog), str(lab.env.lib / "inside"))
        wait_for(lambda: error_texts(window), what="the refusal")
        assert "catalogue the catalog" in error_texts(window)[0] and not (lab.env.lib / "inside").exists()
        assert (digest(lab.catalog), tree_hashes(lab.env.lib)) == before and window.catalog == lab.catalog
    finally:
        finish(hold(window))


def test_the_default_library_is_never_moved(lab):
    window = open_window(lab.catalog)
    try:
        window.move_library(str(paths.catalog_path()), str(lab.env.lib.parent / "elsewhere"))
        assert "stays where `kv` looks" in error_texts(window)[0] and window.jobs.find_active("copy_catalog") is None
    finally:
        finish(hold(window))


# --------------------------------------------------------------------------------------------------------------- the startup choice


def test_the_launch_dialog_offers_the_libraries_preselects_the_last_and_cannot_pick_a_missing_one(lab, tmp_path):
    other, _ = prepare(tmp_path / "other" / "catalog.sqlite")
    gone = tmp_path / "gone" / "catalog.sqlite"
    registry = L.remembered(L.remembered(L.remembered(L.Registry(), gone, "Gone"), other, "Other"), lab.catalog, "Lab")
    dialog = LibraryChooserDialog(L.known_for_menu(registry), registry.last)
    try:
        assert dialog.chosen() == str(lab.catalog), "the library used last is selected"
        missing = next(i for i in range(dialog.list.count()) if "file missing" in dialog.list.item(i).text())
        dialog.list.setCurrentRow(missing)
        assert dialog.chosen() is None and not dialog.buttons.button(dialog.buttons.StandardButton.Ok).isEnabled()
        assert not dialog.dont_ask()
    finally:
        dialog.deleteLater()


def test_choosing_at_launch_returns_the_pick_and_remembers_dont_ask_again(lab, tmp_path):
    other, _ = prepare(tmp_path / "other" / "catalog.sqlite")
    listing = tmp_path / "libs.json"
    registry = L.remembered(L.remembered(L.Registry(), other, "Other"), lab.catalog, "Lab")

    def pick_other_and_stop_asking(dialog):
        dialog.list.setCurrentRow(next(i for i in range(dialog.list.count()) if "Other" in dialog.list.item(i).text()))
        dialog.dont_ask_check.setChecked(True)
        dialog.accept()
        return dialog.result()

    chosen = choose_at_startup(registry, listing, execute=pick_other_and_stop_asking)
    assert chosen == other and L.load(listing).ask_at_startup is False


def test_quitting_the_launch_dialog_opens_nothing_and_changes_nothing(lab, tmp_path):
    other, _ = prepare(tmp_path / "other" / "catalog.sqlite")
    listing = tmp_path / "libs.json"
    registry = L.remembered(L.remembered(L.Registry(), other, "Other"), lab.catalog, "Lab")

    def quit_even_though_ticked(dialog):
        dialog.dont_ask_check.setChecked(True)
        dialog.reject()
        return dialog.result()

    assert choose_at_startup(registry, listing, execute=quit_even_though_ticked) is None
    assert not listing.exists(), "a person who quit did not also switch the question off"


def test_the_file_menu_toggle_turns_the_question_on_and_off(lab, tmp_path):
    listing = tmp_path / "libs.json"
    L.save(L.with_ask_at_startup(L.Registry(), False), listing)
    window = open_window(lab.catalog, remember=True, libraries_path=listing)
    try:
        assert window.act_ask_startup.isCheckable() and not window.act_ask_startup.isChecked(), "it shows the saved setting"
        window.act_ask_startup.trigger()
        assert window.act_ask_startup.isChecked() and L.load(listing).ask_at_startup is True
        window.act_ask_startup.trigger()
        assert L.load(listing).ask_at_startup is False
    finally:
        finish(hold(window))
