"""Choosing a library in the window: the window is replaced by one over the other catalog, a bad file is refused and left alone, and the
list of libraries is kept as the person uses them.

Driven through the real window, menu and dialogs (offscreen), against real catalogs on disk.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

import pytest
from guisupport import pump, wait_for
from lab import Lab
from winhelp import close_dialogs, dialogs, error_texts, open_window, press, settle, the_dialog, titles

from knowledgevista.gui import jobs as J
from knowledgevista.gui import libraries as L
from knowledgevista.gui.app import Session, prepare
from knowledgevista.gui.shell import RecordingShell

OPTIONS = {"shell": RecordingShell(), "remember": False, "poll_ms": 150}


@pytest.fixture
def lab(qapp, tmp_path):
    made = Lab(tmp_path)
    yield made
    made.close()


def hold(window, options=None):
    """A session over `window`, as `kv gui` makes one; the test shuts down whichever window is current at the end."""
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


# ------------------------------------------------------------------------------------------------------------------ switching


def test_switching_shows_the_other_library_and_nothing_of_the_first(lab, tmp_path):
    second, _ = prepare(tmp_path / "chem" / "catalog.sqlite")
    first = open_window(lab.catalog)
    session = hold(first)
    try:
        assert len(titles(first)) == 7
        assert session.switch(str(second), "Chemistry") is True
        now = settled(session)
        assert now is not first and first._closing, "the old window was shut down, not reused"
        assert now.catalog == second and now.jobs is not first.jobs and now.jobs.catalog == second
        assert now.windowTitle() == "Knowledge Vista — Chemistry" and now.library_name == "Chemistry"
        assert titles(now) == [] and "No folders yet" in now.list_note.text()
        assert now.library_id != first.library_id and now.sidebar_data["documents"] == 0

        assert session.switch(str(lab.catalog)) is True  # and back: every document is still there
        back = settled(session)
        assert len(titles(back)) == 7 and back.library_id == first.library_id and back is not now
    finally:
        finish(session, first, now)


def test_the_old_window_saves_its_state_before_the_new_one_reads_it(lab, tmp_path):
    """The new window picks up the layout the old one saved (a selection is not carried across libraries, the layout and last folder are)."""
    second, _ = prepare(tmp_path / "chem" / "catalog.sqlite")
    state_file = tmp_path / "state.json"
    options = {**OPTIONS, "remember": True, "state_path": state_file, "libraries_path": tmp_path / "libs.json"}
    first = open_window(lab.catalog, remember=True, state_path=state_file, libraries_path=tmp_path / "libs.json")
    session = hold(first, options)
    try:
        first.state.last_folder = "D:/Papers/Somewhere"
        assert not state_file.exists()
        assert session.switch(str(second)) is True
        now = settled(session)
        assert state_file.exists() and now.state.last_folder == "D:/Papers/Somewhere"
    finally:
        finish(session, first)


def test_a_file_that_is_not_a_library_is_refused_and_left_exactly_as_it_was(lab, tmp_path):
    foreign = tmp_path / "other.sqlite"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO notes (body) VALUES ('not ours')")
    conn.commit()
    conn.close()
    junk = tmp_path / "junk.sqlite"
    junk.write_text("this is not a database at all", encoding="utf-8")
    before = {foreign: digest(foreign), junk: digest(junk)}
    window = open_window(lab.catalog)
    session = hold(window)
    try:
        for refused in (foreign, junk):
            assert session.switch(str(refused)) is False
            assert session.window is window and not window._closing, "the window the person was using is still theirs"
        assert len(error_texts(window)) == 2 and "not a Knowledge Vista library" in error_texts(window)[0]
        assert {p: digest(p) for p in before} == before, "refusing must not write a byte into the file"
        tables = {r[0] for r in sqlite3.connect(foreign).execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert tables == {"notes"}, "this program's tables were not written into someone else's database"
    finally:
        finish(session)


def test_a_path_that_does_not_exist_is_refused_and_no_file_is_created(lab, tmp_path):
    window = open_window(lab.catalog)
    session = hold(window)
    try:
        missing = tmp_path / "nowhere" / "catalog.sqlite"
        assert session.switch(str(missing)) is False
        assert not missing.exists() and not missing.parent.exists() and session.window is window
        assert "no library file" in error_texts(window)[0]
    finally:
        finish(session)


def test_opening_the_library_already_shown_just_says_so(lab):
    window = open_window(lab.catalog)
    asked: list[tuple] = []
    window.switchRequested.connect(lambda *args: asked.append(args))
    try:
        window.pick_library(str(lab.catalog))
        assert asked == [] and "already open" in window.messages[-1][1]
    finally:
        finish(hold(window))


# --------------------------------------------------------------------------------------------------------------- a new library


def test_a_new_library_is_created_named_remembered_and_opened_empty(lab, tmp_path):
    listing = tmp_path / "libs.json"
    options = {**OPTIONS, "remember": True, "libraries_path": listing}
    window = open_window(lab.catalog, remember=True, libraries_path=listing)
    session = hold(window, options)
    try:
        folder = tmp_path / "Libraries" / "Physics"
        window.create_library("Physics", str(folder))
        now = settled(session)
        assert (folder / "catalog.sqlite").is_file() and now.catalog == folder / "catalog.sqlite"
        assert now.library_name == "Physics" and now.windowTitle() == "Knowledge Vista — Physics"
        assert titles(now) == [] and "No folders yet" in now.list_note.text()
        registry = L.load(listing)
        assert registry.last == str(folder / "catalog.sqlite") and [k.name for k in registry.libraries] == ["Physics", lab.catalog.parent.name]
    finally:
        finish(session, window)


def test_a_folder_that_already_holds_a_library_is_not_overwritten(lab, tmp_path):
    existing, _ = prepare(tmp_path / "taken" / "catalog.sqlite")
    before = digest(existing)
    window = open_window(lab.catalog)
    asked: list[tuple] = []
    window.switchRequested.connect(lambda *args: asked.append(args))
    try:
        window.create_library("Again", str(existing.parent))
        assert asked == [] and "already holds a library" in error_texts(window)[0] and digest(existing) == before
        window.create_library("Relative", "some/relative/folder")
        assert asked == [] and "full folder path" in error_texts(window)[1]
    finally:
        finish(hold(window))


def test_the_new_library_dialog_needs_a_name_and_a_folder(lab, tmp_path):
    window = open_window(lab.catalog)
    try:
        window.act_new_library.trigger()
        dialog = the_dialog(window, "newLibraryDialog")
        ok = dialog.buttons.button(dialog.buttons.StandardButton.Ok)
        assert not ok.isEnabled()
        dialog.name_edit.setText("  Physics   papers ")
        assert not ok.isEnabled()
        dialog.folder_edit.setText(str(tmp_path / "p"))
        assert ok.isEnabled() and dialog.values() == {"name": "Physics papers", "folder": str(tmp_path / "p")}
        asked: list[tuple] = []
        window.switchRequested.connect(lambda *args: asked.append(args))
        press(ok)
        assert asked == [("{}".format(tmp_path / "p" / "catalog.sqlite"), "Physics papers", True)], "pressing Create asks to make and open it"
    finally:
        finish(hold(window))


# ----------------------------------------------------------------------------------------------------------------- running jobs


def test_switching_while_a_job_runs_asks_first_and_stops_it_only_on_yes(lab, tmp_path):
    second, _ = prepare(tmp_path / "other" / "catalog.sqlite")
    window = open_window(lab.catalog)
    session = hold(window)
    started = []

    def wait_for_stop(ctx):
        started.append(True)
        while not ctx.should_stop():
            time.sleep(0.01)
        return "stopped"

    try:
        window.jobs.submit("slow", "A long job", wait_for_stop, lane=J.WRITE)
        wait_for(lambda: started, what="the long job to start")
        window.request_switch(str(second))
        pump(50)
        assert session.window is window and not window._closing, "nothing changed before the person answered"
        dialog = the_dialog(window, "confirmSwitchLibrary")
        assert "still running" in dialog.findChild(type(dialog.layout().itemAt(0).widget()), "confirmSwitchLibraryText").text()
        dialog.reject()
        pump(50)
        assert session.window is window and window.jobs.busy, "No leaves the job running and the library open"

        window.request_switch(str(second))
        the_dialog(window, "confirmSwitchLibrary").accept()
        now = settled(session)
        assert now is not window and now.catalog == second and window._closing and not window.jobs.busy
    finally:
        finish(session, window)


# --------------------------------------------------------------------------------------------------------------- recent libraries


def test_the_recent_menu_ticks_this_library_marks_a_missing_one_and_always_offers_the_default(lab, tmp_path):
    listing = tmp_path / "libs.json"
    kept, _ = prepare(tmp_path / "kept" / "catalog.sqlite")
    gone = tmp_path / "gone" / "catalog.sqlite"
    registry = L.remembered(L.remembered(L.Registry(), gone, "Old & Gone"), kept, "R&D")
    L.save(registry, listing)
    window = open_window(kept, remember=True, libraries_path=listing)
    try:
        window._fill_recent()
        actions = window.recent_menu.actions()
        texts = [a.text() for a in actions]
        assert texts == ["R&&D", "Old && Gone — file missing", L.DEFAULT_NAME], "'&' is doubled so it is shown, not taken as a shortcut"
        assert [a.isChecked() for a in actions] == [True, False, False]
        assert actions[1].statusTip() == str(gone)
    finally:
        finish(hold(window))


def test_choosing_a_missing_library_offers_to_forget_it_and_never_opens_or_creates_it(lab, tmp_path):
    listing = tmp_path / "libs.json"
    gone = tmp_path / "gone" / "catalog.sqlite"
    L.save(L.remembered(L.Registry(), gone, "Gone"), listing)
    window = open_window(lab.catalog, remember=True, libraries_path=listing)
    asked: list[tuple] = []
    window.switchRequested.connect(lambda *args: asked.append(args))
    try:
        window.pick_library(str(gone), "Gone")
        assert asked == [] and not gone.parent.exists()
        dialog = the_dialog(window, "confirmForgetLibrary")
        assert str(gone) in dialog.findChild(type(dialog.layout().itemAt(0).widget()), "confirmForgetLibraryText").text()
        dialog.accept()
        pump(50)
        assert gone.as_posix() not in [k.path.replace("\\", "/") for k in L.load(listing).libraries] and not gone.parent.exists()
        assert [k.name for k in L.load(listing).libraries] == [lab.catalog.parent.name]
    finally:
        finish(hold(window))


def test_opening_a_library_records_it_only_for_a_window_that_remembers(lab, tmp_path):
    listing = tmp_path / "libs.json"
    quiet = open_window(lab.catalog, libraries_path=listing)  # the test helper's default is remember=False
    try:
        assert not listing.exists() and not list(tmp_path.glob("libs.json*")), "a window that does not remember leaves no list behind"
    finally:
        finish(hold(quiet))
    loud = open_window(lab.catalog, remember=True, libraries_path=listing)
    try:
        assert L.load(listing).last == str(lab.catalog)
    finally:
        finish(hold(loud))


def test_the_default_library_can_always_be_chosen_even_before_it_exists(lab):
    from knowledgevista import paths

    assert not paths.catalog_path().exists()
    window = open_window(lab.catalog)
    asked: list[tuple] = []
    window.switchRequested.connect(lambda *args: asked.append(args))
    try:
        window.pick_library(str(paths.catalog_path()), L.DEFAULT_NAME)
        assert asked == [(str(paths.catalog_path()), L.DEFAULT_NAME, True)] and dialogs(window, "confirmForgetLibrary") == []
    finally:
        finish(hold(window))
