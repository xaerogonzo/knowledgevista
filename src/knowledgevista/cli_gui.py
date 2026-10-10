"""`kv gui`: open the library window. A person's command; it is not part of the integration surface (docs/GUI.md)."""

from __future__ import annotations

import argparse

from knowledgevista.cli_support import Outcome
from knowledgevista.errors import ErrorCode, KvError


def cmd_gui(args: argparse.Namespace) -> Outcome:
    from knowledgevista import gui

    if not gui.qt_available():
        raise KvError(ErrorCode.DEPENDENCY_MISSING, "The window needs PySide6, which is not installed. Install it with: pip install \"knowledgevista[gui]\"",
                      {"extra": "gui", "package": "PySide6"})
    from knowledgevista.gui import app

    app.run(getattr(args, "catalog", None))  # None: open the library used last, else the default (an explicit --catalog always wins)
    return Outcome(silent=True)  # the window said everything; there is no envelope to print


def add_parsers(sub, shared: argparse.ArgumentParser, make_parser) -> None:  # noqa: ANN001 - argparse's subparsers action
    sub.add_parser("gui", parents=[shared], help="open the library window (needs the gui extra)").set_defaults(handler=cmd_gui)
