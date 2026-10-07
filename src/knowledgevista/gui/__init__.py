"""The library window (PySide6), the optional `gui` extra: `pip install knowledgevista[gui]`, then `kv gui`.

A thin adapter, like the command line and the MCP server: it carries no SQL and no rules of its own. What it shows is decided in
`services/library_view.py` (tested without a display); what it changes goes through the same services `kv` calls. Importing this
package imports no Qt, so the core installs and `kv --version` run without it.

    jobs      the one place work leaves the interface thread (a JobManager over two Qt thread pools)
    state     what the window remembers between runs
    models    the table models: documents, search hits, the review queue
    detail    the detail pane and the "why this value?" window
    dialogs   add a folder, propose a rename, name a collection
    window    the main window
    app       `run()`: start the application
    drive     the in-app driver (a scripted run that ends in a verdict) and its ledger
"""

from __future__ import annotations


def qt_available() -> bool:
    """Whether PySide6 can be imported here. The command line asks this before it tries to open a window."""
    try:
        import PySide6.QtWidgets  # noqa: F401
    except ImportError:
        return False
    return True
