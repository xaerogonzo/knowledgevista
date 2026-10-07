"""Handing a file to the operating system: open it, or show it in its folder.

The window never launches anything itself; it asks its shell. A scripted run and the tests swap in `RecordingShell`, so a driven
check never starts a PDF viewer on the machine it runs on.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from knowledgevista.services import opener


class SystemShell:
    def open_file(self, path: str) -> None:
        opener.launch(path)

    def reveal(self, path: str) -> None:
        """Show the file in its folder (selected, where the platform can)."""
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", f"/select,{Path(path)}"])  # noqa: S603,S607 - the point of the action
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])  # noqa: S603,S607
        else:
            opener.launch(str(Path(path).parent))


class RecordingShell:
    """Records what would have been opened or revealed, and opens nothing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def open_file(self, path: str) -> None:
        self.calls.append(("open", path))

    def reveal(self, path: str) -> None:
        self.calls.append(("reveal", path))
