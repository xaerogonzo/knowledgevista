"""Starting the application: `kv gui`.

`prepare` is the only place the interface thread touches the catalog, once, before the window exists: it makes (or migrates) the
catalog and reads the library id, so every later read can be strict (never migrating) and the window knows whose state it is
restoring. After that the window reads only through jobs.
"""

from __future__ import annotations

import sys
from pathlib import Path

from knowledgevista import paths
from knowledgevista.db.catalog import open_catalog
from knowledgevista.errors import ErrorCode, KvError


def prepare(catalog: Path | str | None = None) -> tuple[Path, str]:
    """(the catalog path, the library id). Creates an empty library the first time, and upgrades an older one (with its backup)."""
    target = Path(catalog) if catalog else paths.catalog_path()
    conn = open_catalog(target, create=True)
    try:
        return target, conn.execute("SELECT library_id FROM library").fetchone()[0]
    finally:
        conn.close()


def make_window(catalog: Path | str | None = None, **options):  # noqa: ANN201 - the window class is imported lazily with Qt
    from knowledgevista.gui.window import MainWindow

    target, library_id = prepare(catalog)
    return MainWindow(target, library_id, **options)


def run(catalog: Path | str | None = None, argv: list[str] | None = None) -> int:
    from PySide6.QtWidgets import QApplication

    from knowledgevista.gui import drive

    target = Path(catalog) if catalog else paths.catalog_path()
    why = drive.refuses_real_library(target) if drive.script_path() else None
    if why:  # before anything is created: a refused run must leave no trace
        raise KvError(ErrorCode.INVALID_ARGUMENTS, why)
    drive.begin()  # first: a warning raised while the window is being built must already be on the record
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv[:1])
    app.setApplicationName("Knowledge Vista")
    app.setOrganizationName("Knowledge Vista")
    window = make_window(catalog)
    driver = drive.start_if_requested(app, window)  # a scripted run (KNOWLEDGEVISTA_DRIVE); None for a person
    if not drive.hidden():
        window.show()
    code = app.exec()
    del driver
    return code
