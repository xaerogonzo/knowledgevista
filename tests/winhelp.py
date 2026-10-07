"""Helpers for driving the real window in tests through its real widgets: select a row, press a button, fill in a dialog, wait."""

from __future__ import annotations

from guisupport import pump, wait_for
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QPushButton

from knowledgevista.gui.app import make_window
from knowledgevista.gui.models import ITEM_ROLE
from knowledgevista.gui.shell import RecordingShell


def open_window(catalog, **options):
    options.setdefault("shell", RecordingShell())
    options.setdefault("remember", False)
    options.setdefault("poll_ms", 150)
    window = make_window(catalog, **options)
    wait_for(lambda: window.listing is not None, what="the first snapshot")
    settle(window)
    return window


def settle(window, timeout_ms: int = 20000) -> None:
    """Until every job has finished and its result has been put on screen."""
    assert window.jobs.wait_idle(timeout_ms), "jobs were still running"
    pump(20)
    assert window.jobs.wait_idle(timeout_ms)


def titles(window) -> list[str]:
    model = window.doc_proxy
    return [model.index(r, 1).data(Qt.ItemDataRole.DisplayRole) for r in range(model.rowCount())]


def cell(table, row: int, column: int) -> str:
    return table.model().index(row, column).data(Qt.ItemDataRole.DisplayRole)


def row_of(window, name: str) -> int:
    """The proxy row of the document called `name` (its title, else its path)."""
    for r in range(window.doc_proxy.rowCount()):
        if window.doc_proxy.index(r, 1).data(ITEM_ROLE).display_title == name:
            return r
    raise AssertionError(f"no row called {name!r} in {titles(window)}")


def select_document(window, name: str) -> None:
    window.doc_table.selectRow(row_of(window, name))
    settle(window)
    assert window.detail.document_id is not None


def press(button: QPushButton) -> None:
    assert button.isEnabled(), f"{button.objectName() or button.text()} is disabled"
    button.click()


def dialogs(window, name: str) -> list[QDialog]:
    return [d for d in window._dialogs if d.objectName() == name]


def the_dialog(window, name: str) -> QDialog:
    found = dialogs(window, name)
    assert len(found) == 1, f"expected one {name}, found {len(found)} among {[d.objectName() for d in window._dialogs]}"
    return found[0]


def close_dialogs(window) -> None:
    for dialog in list(window._dialogs):
        dialog.close()
    for box in list(window._boxes):
        box.close()
    pump(20)


def error_texts(window) -> list[str]:
    return [m for kind, m in window.messages if kind == "error"]


def review_row(window, document_name: str, field_label: str) -> int:
    """The proxy row of a review item, by what the Paper and Field columns say."""
    proxy = window.review_proxy
    for r in range(proxy.rowCount()):
        item = proxy.index(r, 0).data(ITEM_ROLE)
        if (item["title"] or item["name"]) == document_name and item["label"] == field_label:
            return r
    raise AssertionError(f"no review item {document_name!r} / {field_label!r}")


def select_review(window, document_name: str, field_label: str) -> None:
    window.tabs.setCurrentIndex(2)
    window.review_table.selectRow(review_row(window, document_name, field_label))
    settle(window)


def find_item(tree, text: str):
    """The first tree item whose text is exactly `text` (anywhere in the tree)."""
    from PySide6.QtCore import Qt as _Qt

    found = tree.findItems(text, _Qt.MatchFlag.MatchExactly | _Qt.MatchFlag.MatchRecursive)
    assert found, f"no sidebar item {text!r}"
    return found[0]
