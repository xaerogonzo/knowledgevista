"""Starting the application: `kv gui`.

`prepare` is the only place the interface thread touches the catalog, once, before the window exists: it makes (or migrates) the
catalog and reads the library id, so every later read can be strict (never migrating) and the window knows whose state it is
restoring. After that the window reads only through jobs.

A window shows ONE library. Picking another one (File > Open library, New library, Recent libraries) does not rebind the window: the
`Session` below prepares the other catalog first (so a bad file fails while the old window is still there), then shuts the old window
down and shows a fresh one over the new catalog. Nothing of the old library's jobs, models or selection can leak into the new one.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from knowledgevista import paths
from knowledgevista.db.catalog import open_catalog
from knowledgevista.db.connection import connect
from knowledgevista.db.migrations import SchemaTooNew, current_version
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.gui import libraries


def prepare(catalog: Path | str | None = None, *, create: bool = True) -> tuple[Path, str]:
    """(the catalog path, the library id). Creates an empty library the first time, and upgrades an older one (with its backup).

    `create=False` is for opening a library a person pointed at: a path that is not a Knowledge Vista catalog is refused and left
    exactly as it was (migrating would write this program's tables into, say, another application's database)."""
    target = Path(catalog) if catalog else paths.catalog_path()
    if not create:
        require_catalog(target)
    conn = open_catalog(target, create=create)
    try:
        return target, conn.execute("SELECT library_id FROM library").fetchone()[0]
    finally:
        conn.close()


def require_catalog(target: Path) -> None:
    """Raise a KvError unless `target` is an existing Knowledge Vista catalog. Reads only; it never writes to the file."""
    if not target.is_file():
        raise KvError(ErrorCode.CATALOG_MISSING, f"There is no library file at {target}.", {"catalog": str(target)})
    try:
        conn = connect(target, read_only=True)
        try:
            version = current_version(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"{target} is not a Knowledge Vista library ({exc}).", {"catalog": str(target)}) from exc
    if version < 1:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"{target} is a database, but not a Knowledge Vista library; it was left untouched.", {"catalog": str(target)})


def resolve_start_catalog(explicit: Path | str | None = None, *, use_list: bool = True, list_path: Path | None = None) -> Path:
    """Which catalog `kv gui` opens: the one named with --catalog; else the one opened last, if its file is still there; else the app's own."""
    if explicit:
        return Path(explicit)
    if use_list:
        last = libraries.last_existing(libraries.load(list_path))
        if last is not None:
            return last
    return paths.catalog_path()


def make_window(catalog: Path | str | None = None, **options):  # noqa: ANN201 - the window class is imported lazily with Qt
    from knowledgevista.gui.window import MainWindow

    target, library_id = prepare(catalog)
    return MainWindow(target, library_id, **options)


class Session:
    """Owns whichever window is current and swaps it when a person picks another library."""

    def __init__(self, window: Any, *, visible: bool = True, **options: Any):
        self.window = window
        self._visible = visible
        self._options = options
        window.switchRequested.connect(self.switch)

    def switch(self, catalog: str, name: str = "", create: bool = False) -> bool:
        """Show `catalog` instead. Returns False, with the reason shown in the current window, if it could not be opened (nothing changes).
        `name` is what the library is called ('' keeps the name already remembered, or the folder's); `create` makes a new empty one."""
        old = self.window
        try:
            target, library_id = prepare(catalog, create=create)
        except (KvError, SchemaTooNew, sqlite3.Error, OSError) as exc:
            old.error("Open a library", str(exc))
            return False
        from knowledgevista.gui.window import MainWindow

        message = getattr(old, "handover_message", "")
        old.shutdown()  # saves the layout and stops this library's jobs first; the new window reads that state
        new = MainWindow(target, library_id, library_name=name or None, **self._options)
        new.switchRequested.connect(self.switch)
        self.window = new
        if self._visible:
            new.show()
        if message:
            new.inform("Catalog copied", message)
        old.close()
        old.deleteLater()
        return True


def choose_at_startup(registry: libraries.Registry, list_path: Path | None = None, execute: Callable[[Any], int] | None = None) -> Path | None:
    """Ask which library to open (the launch dialog). Returns the catalog chosen, or None if the person quit. Ticking "don't ask again" is
    saved here, so the next launch opens the library used last. `execute` runs the dialog (`exec` by default); a test supplies its own."""
    from PySide6.QtWidgets import QDialog

    from knowledgevista.gui.dialogs import LibraryChooserDialog

    dialog = LibraryChooserDialog(libraries.known_for_menu(registry), registry.last)
    accepted = (execute or (lambda d: d.exec()))(dialog) == QDialog.DialogCode.Accepted
    chosen = dialog.chosen() if accepted else None
    if chosen is not None and dialog.dont_ask():
        libraries.save(libraries.with_ask_at_startup(registry, False), list_path)
    return Path(chosen) if chosen is not None else None


def run(catalog: Path | str | None = None, argv: list[str] | None = None) -> int:
    from PySide6.QtWidgets import QApplication

    from knowledgevista.gui import drive

    scripted = bool(drive.script_path())
    target = resolve_start_catalog(catalog, use_list=not scripted)  # a scripted run never picks up "the last library" from a config file
    why = drive.refuses_real_library(target) if scripted else None
    if why:  # before anything is created: a refused run must leave no trace
        raise KvError(ErrorCode.INVALID_ARGUMENTS, why)
    drive.begin()  # first: a warning raised while the window is being built must already be on the record
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv[:1])
    app.setApplicationName("Knowledge Vista")
    app.setOrganizationName("Knowledge Vista")
    if catalog is None and not scripted:  # an explicit --catalog, or a scripted run, is never second-guessed by a dialog
        registry = libraries.load()
        if libraries.should_ask(registry):
            picked = choose_at_startup(registry)
            if picked is None:
                return 0  # the person chose Quit
            target = picked
    window = make_window(target)
    session = Session(window, visible=not drive.hidden())
    driver = drive.start_if_requested(app, window)  # a scripted run (KNOWLEDGEVISTA_DRIVE); None for a person
    if not drive.hidden():
        window.show()
    code = app.exec()
    del driver, session
    return code
