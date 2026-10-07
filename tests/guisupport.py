"""What every window test needs: Qt without a display, an application object, and a loud failure (not a skip) where the GUI is required.

`QT_QPA_PLATFORM=offscreen` is set before Qt is imported, so no window ever appears on the machine running the tests, and no test
needs a desktop session. In CI, `KV_REQUIRE_GUI=1` turns "PySide6 is not installed" from a skip into a failure: a suite that skips the
whole window because the extra was missing would be green for the wrong reason.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def require_qt() -> None:
    if os.environ.get("KV_REQUIRE_GUI"):
        import PySide6  # noqa: F401 - missing means the CI install is wrong, and the run must say so
    else:
        pytest.importorskip("PySide6")


require_qt()

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def application() -> QApplication:
    app = QApplication.instance()
    return app if app is not None else QApplication(["kv-tests"])


def pump(ms: int = 0) -> None:
    """Let queued signals and timers run for `ms` milliseconds (and at least once)."""
    import time

    end = time.monotonic() + ms / 1000
    while True:
        QCoreApplication.processEvents()
        if time.monotonic() >= end:
            return
        time.sleep(0.002)


def wait_for(predicate, timeout_ms: int = 10000, what: str = "the condition"):
    """Pump events until `predicate()` is truthy; fail with `what` if it never is. Returns the predicate's value."""
    import time

    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        QCoreApplication.processEvents()
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout_ms} ms waiting for {what}")
        time.sleep(0.005)
