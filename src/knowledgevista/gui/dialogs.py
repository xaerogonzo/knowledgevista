"""The window's small dialogs: add a folder, pick a name, and the rename proposal.

None of them runs its own event loop. The window shows them with `open()` and acts on `accepted`, so a scripted run (and a test) can
find a dialog, fill it in through its real widgets and press its real buttons, which is the path a person takes.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
                               QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from knowledgevista.domain import plan as planmod
from knowledgevista.gui.text import plain_label, set_plain, tooltip


class AddRootDialog(QDialog):
    """A folder to look in. Allowing the organizer to rename files there is a separate, unticked box: adding a folder to read is not
    permission to change it."""

    def __init__(self, parent: QWidget | None = None, *, start_folder: str = ""):
        super().__init__(parent)
        self.setObjectName("addRootDialog")
        self.setWindowTitle("Add a folder")
        self.resize(520, 0)
        self._start = start_folder
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label("Knowledge Vista will look in this folder and everything under it. Nothing is moved, renamed or changed there.",
                                     name="addRootIntro"))
        form = QFormLayout()
        row = QHBoxLayout()
        self.path_edit = QLineEdit(start_folder)
        self.path_edit.setObjectName("rootPathEdit")
        self.path_edit.setPlaceholderText("D:\\Documents\\Papers")
        self.browse_button = QPushButton("Browse…")
        self.browse_button.setObjectName("browseButton")
        self.browse_button.clicked.connect(self.browse)
        row.addWidget(self.path_edit, 1)
        row.addWidget(self.browse_button)
        form.addRow("Folder", row)
        self.label_edit = QLineEdit()
        self.label_edit.setObjectName("rootLabelEdit")
        self.label_edit.setPlaceholderText("a short name (optional)")
        form.addRow("Name", self.label_edit)
        layout.addLayout(form)
        self.organize_check = QCheckBox("Allow the organizer to rename and move files in this folder")
        self.organize_check.setObjectName("allowOrganizeCheck")
        self.organize_check.setChecked(False)
        self.organize_check.setToolTip("Off unless you say so. Even when on, nothing is renamed without a reviewed plan, and every rename can be undone.")
        layout.addWidget(self.organize_check)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add and scan")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.path_edit.textChanged.connect(self._sync)
        self._sync()

    def _sync(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self.path_edit.text().strip()))

    def browse(self) -> None:
        """The system folder chooser. Modal by nature, and only ever reached by a click on Browse."""
        chosen = QFileDialog.getExistingDirectory(self, "Choose a folder", self.path_edit.text().strip() or self._start)
        if chosen:
            self.path_edit.setText(chosen)

    def values(self) -> dict[str, Any]:
        return {"path": self.path_edit.text().strip(), "label": self.label_edit.text().strip() or None, "allow_organize": self.organize_check.isChecked()}


class ChoiceDialog(QDialog):
    """Pick an existing name or type a new one (a collection, a tag)."""

    def __init__(self, title: str, prompt: str, choices: list[str], accept_text: str, parent: QWidget | None = None, *, name: str = "choiceDialog"):
        super().__init__(parent)
        self.setObjectName(name)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(prompt, name=f"{name}Prompt"))
        self.combo = QComboBox()
        self.combo.setObjectName("choiceCombo")
        self.combo.setEditable(True)
        self.combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo.addItems(choices)
        self.combo.setCurrentIndex(-1)
        layout.addWidget(self.combo)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(accept_text)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.combo.editTextChanged.connect(self._sync)
        self._sync()

    def _sync(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self.value()))

    def value(self) -> str:
        return " ".join(self.combo.currentText().split())


class ConfirmDialog(QDialog):
    """A question with a Yes and a No, for one thing that changes many (the batch rule). Plain text; shown with `open()`."""

    def __init__(self, title: str, text: str, yes_text: str, parent: QWidget | None = None, *, name: str = "confirmDialog"):
        super().__init__(parent)
        self.setObjectName(name)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(text, name=f"{name}Text"))
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Yes).setText(yes_text)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)


PLAN_COLUMNS = ("Now", "Would become", "What", "Why", "Risk", "")


class RenameDialog(QDialog):
    """A rename proposal, shown and not applied. The same plan the command line makes (`kv plan create`): old name, new name, why, risk,
    and for a blocked item the reason. Saving writes the plan file (outside the library) and registers it; applying is `kv apply`, after
    a dry run, and is deliberately not a button here."""

    saveRequested = Signal()

    def __init__(self, plan: planmod.Plan, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("renameDialog")
        self.setWindowTitle("Propose a rename")
        self.resize(900, 360)
        self.plan = plan
        layout = QVBoxLayout(self)
        summary = plan.summary()
        planned = summary["status"].get("planned", 0)
        text = (f"{planned} file(s) would be renamed or moved." if planned else "Nothing would change.") + \
               f" ({summary['items']} considered; naming policy {plan.naming_policy}.) Nothing has been renamed."
        self.summary = plain_label(text, name="renameSummary")
        layout.addWidget(self.summary)
        self.table = QTableWidget(len(plan.items), len(PLAN_COLUMNS))
        self.table.setObjectName("renameTable")
        self.table.setHorizontalHeaderLabels(list(PLAN_COLUMNS))
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(True)
        header = self.table.horizontalHeader()
        for column in (0, 1, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        for column in (2, 4, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for row, item in enumerate(plan.items):
            status = item.blocked_reason or ("" if item.status == "planned" else item.status)
            for column, value in enumerate((item.old_path, item.new_path if item.status != "unchanged" else "(unchanged)", item.operation, item.reason, item.risk, status)):
                cell = QTableWidgetItem(value)
                cell.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                cell.setToolTip(tooltip(value))
                self.table.setItem(row, column, cell)
        self.table.resizeRowsToContents()
        layout.addWidget(self.table, 1)
        self.footer = plain_label("A saved plan is applied with:  kv apply <plan file>   (add --dry-run first). Every rename can be undone with:  kv undo",
                                  name="renameFooter")
        layout.addWidget(self.footer)
        buttons = QDialogButtonBox()
        self.save_button = buttons.addButton("Save plan file", QDialogButtonBox.ButtonRole.ActionRole)
        self.save_button.setObjectName("savePlanButton")
        self.save_button.setEnabled(planned > 0)
        self.save_button.clicked.connect(self.saveRequested)
        close = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        close.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def saved(self, path: str) -> None:
        set_plain(self.footer, f"Plan saved to {path}. Nothing has been renamed. Read it, then:  kv apply \"{path}\" --dry-run")
        self.save_button.setEnabled(False)

    def rows(self) -> list[list[str]]:
        return [[self.table.item(r, c).text() for c in range(self.table.columnCount())] for r in range(self.table.rowCount())]
