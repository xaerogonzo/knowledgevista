"""Text from outside is text. This is where the window makes that true.

A title read from a PDF, a file name, a provider's record, a tag someone typed: none of it is the window's, and none may be able to
change how the window looks or reach the network. Qt guesses: a QLabel, a tooltip or a message box shows its text as HTML if it looks
like HTML, and an HTML `<img src="http://...">` is a request to a stranger's server the moment a title is displayed. So:

  * every QLabel the window makes is made here, with its format fixed to plain text;
  * a tooltip built from outside text goes through `tooltip()`, which escapes it and says "show exactly this";
  * a message goes through `message_box()`, which fixes its format;
  * text that a widget parses for mnemonics (a button, a menu entry) goes through `mnemonic_safe()`;
  * `audit()` walks a live window and reports anything that breaks these rules, so a test (and the driven run) can say no.

Item views (tables, trees) draw their text as plain text and need nothing; their TOOLTIPS are the exception and use `tooltip()`.
"""

from __future__ import annotations

import html

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import Qt as GuiQt  # `mightBeRichText` is declared on this one, not on QtCore's
from PySide6.QtWidgets import QLabel, QMessageBox, QTextBrowser, QTextEdit, QWidget

#: A tooltip that has been through `tooltip()` starts with this; `audit()` treats a tooltip that looks like HTML and does not as a fault.
TOOLTIP_PREFIX = '<span style="white-space:pre-wrap">'
TOOLTIP_SUFFIX = "</span>"


def tooltip(text: str) -> str:
    """`text` as a tooltip that shows exactly these characters: markup in it is displayed, not interpreted."""
    return f"{TOOLTIP_PREFIX}{html.escape(text, quote=True)}{TOOLTIP_SUFFIX}"


def mnemonic_safe(text: str) -> str:
    """`text` for a button, menu entry or tab: an ampersand there marks a keyboard shortcut, so a literal one is doubled."""
    return text.replace("&", "&&")


def plain_label(text: str = "", parent: QWidget | None = None, *, wrap: bool = True, selectable: bool = True, name: str | None = None) -> QLabel:
    """A label that can only show text. Selectable by mouse so a path or an id can be copied."""
    label = QLabel(parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setText(text)
    label.setWordWrap(wrap)
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if name:
        label.setObjectName(name)
    return label


def set_plain(label: QLabel, text: str) -> None:
    """Set a label's text, making sure it is still plain first (the format is part of what the text means)."""
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setText(text)


def message_box(parent: QWidget | None, title: str, text: str, *, icon: QMessageBox.Icon = QMessageBox.Icon.Information,
                buttons: QMessageBox.StandardButton = QMessageBox.StandardButton.Ok, detail: str | None = None, name: str = "messageBox") -> QMessageBox:
    """A message that does not block: the caller connects to `finished` (or just shows it) and calls `.open()`. Plain text throughout."""
    box = QMessageBox(parent)
    box.setObjectName(name)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    if detail:
        box.setDetailedText(detail)
    box.setStandardButtons(buttons)
    box.setWindowModality(Qt.WindowModality.WindowModal)
    return box


def audit(root: QWidget) -> list[str]:
    """Everything under `root` that could show outside text as markup. Empty means the window obeys the rules above.

    It checks the live widgets rather than the source, because what matters is what a widget is doing now: a label somebody later
    creates without `plain_label` fails here."""
    problems: list[str] = []
    for child in [root, *root.findChildren(QObject)]:
        name = child.objectName() or type(child).__name__
        if isinstance(child, QLabel) and child.textFormat() != Qt.TextFormat.PlainText:
            problems.append(f"label {name!r} is {child.textFormat().name}; it must be PlainText")
        if isinstance(child, QMessageBox) and child.textFormat() != Qt.TextFormat.PlainText:
            problems.append(f"message box {name!r} is {child.textFormat().name}; it must be PlainText")
        if isinstance(child, (QTextEdit, QTextBrowser)):
            problems.append(f"{name!r} is a rich text widget ({type(child).__name__}); use QPlainTextEdit")
        if isinstance(child, QWidget):
            tip = child.toolTip()
            if tip and GuiQt.mightBeRichText(tip) and not (tip.startswith(TOOLTIP_PREFIX) and tip.endswith(TOOLTIP_SUFFIX)):
                problems.append(f"{name!r} has a tooltip Qt would render as HTML: {tip[:60]!r}")
    return problems
