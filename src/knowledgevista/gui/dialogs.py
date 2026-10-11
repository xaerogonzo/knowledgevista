"""The window's small dialogs: add a folder, pick a name, and the rename proposal.

None of them runs its own event loop. The window shows them with `open()` and acts on `accepted`, so a scripted run (and a test) can
find a dialog, fill it in through its real widgets and press its real buttons, which is the path a person takes.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
                               QLineEdit, QListWidget, QListWidgetItem, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from knowledgevista.domain import plan as planmod
from knowledgevista.gui import libraries as libs
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


class NewLibraryDialog(QDialog):
    """A separate, empty library: a name, and the folder its catalog file (catalog.sqlite) will live in. The catalog is never a document
    folder: keep it outside any folder you add to read."""

    def __init__(self, parent: QWidget | None = None, *, start_folder: str = ""):
        super().__init__(parent)
        self.setObjectName("newLibraryDialog")
        self.setWindowTitle("New library")
        self.resize(520, 0)
        self._start = start_folder
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label("A library keeps its own folders, documents, proposals, collections and tags, apart from every other library. "
                                     "Choose where its catalog file lives; that folder should not be one you add as a folder of documents.", name="newLibraryIntro"))
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("libraryNameEdit")
        self.name_edit.setPlaceholderText("a short name, e.g. Chemistry")
        form.addRow("Name", self.name_edit)
        row = QHBoxLayout()
        self.folder_edit = QLineEdit(start_folder)
        self.folder_edit.setObjectName("libraryFolderEdit")
        self.folder_edit.setPlaceholderText("D:\\Libraries\\Chemistry")
        self.browse_button = QPushButton("Browse…")
        self.browse_button.setObjectName("browseButton")
        self.browse_button.clicked.connect(self.browse)
        row.addWidget(self.folder_edit, 1)
        row.addWidget(self.browse_button)
        form.addRow("Folder", row)
        layout.addLayout(form)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.name_edit.textChanged.connect(self._sync)
        self.folder_edit.textChanged.connect(self._sync)
        self._sync()

    def _sync(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self.name_edit.text().strip() and self.folder_edit.text().strip()))

    def browse(self) -> None:
        """The system folder chooser. Modal by nature, and only ever reached by a click on Browse."""
        chosen = QFileDialog.getExistingDirectory(self, "Choose a folder for the library", self.folder_edit.text().strip() or self._start)
        if chosen:
            self.folder_edit.setText(chosen)

    def values(self) -> dict[str, Any]:
        return {"name": " ".join(self.name_edit.text().split()), "folder": self.folder_edit.text().strip()}


class LibraryChooserDialog(QDialog):
    """Shown at launch when there is more than one library: which one to open. Unlike the window's dialogs it is run with `exec()` by
    `app.run`, before any window exists. A library whose file has gone is listed but cannot be chosen (choosing it would invent an empty one)."""

    def __init__(self, entries: list[libs.Known], last: str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("libraryChooserDialog")
        self.setWindowTitle("Knowledge Vista — choose a library")
        self.resize(560, 320)
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label("Which library do you want to open?", name="libraryChooserIntro"))
        self.list = QListWidget()
        self.list.setObjectName("libraryList")
        wanted = libs.key_of(last) if last else None
        first_usable = preferred = None
        for known in entries:
            missing = not known.exists and not libs.is_default(known.path)
            item = QListWidgetItem(f"{known.name}  —  {known.path}" + ("  (file missing)" if missing else ""))
            item.setData(Qt.ItemDataRole.UserRole, known.path)
            if missing:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(item)
            if not missing:
                first_usable = first_usable or item
                if wanted is not None and libs.key_of(known.path) == wanted:
                    preferred = item
        layout.addWidget(self.list, 1)
        self.dont_ask_check = QCheckBox("Don't ask again: open the library I used last. (File > Ask which library at startup turns this back on.)")
        self.dont_ask_check.setObjectName("dontAskCheck")
        layout.addWidget(self.dont_ask_check)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Open")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Quit")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept() if self.chosen() else None)
        self.list.currentItemChanged.connect(lambda *_: self._sync())
        pick = preferred or first_usable
        if pick is not None:
            self.list.setCurrentItem(pick)
        self._sync()

    def _sync(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.chosen() is not None)

    def chosen(self) -> str | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None and item.flags() & Qt.ItemFlag.ItemIsEnabled else None

    def dont_ask(self) -> bool:
        return self.dont_ask_check.isChecked()


class ManageLibrariesDialog(QDialog):
    """The libraries the window knows: rename one, show its catalog in its folder, copy it to another folder, take it off the list. None of
    this changes a document, and nothing here deletes a file: removing is from the LIST only. The window acts on the signals."""

    renameRequested = Signal(str)
    moveRequested = Signal(str)
    revealRequested = Signal(str)
    removeRequested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("manageLibrariesDialog")
        self.setWindowTitle("Manage libraries")
        self.resize(820, 340)
        self._current: str | None = None
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label("A library is a catalog file with its own folders, documents and tags. Nothing here changes a document, and nothing "
                                     "deletes a file: removing only takes a library off this list.", name="manageIntro"))
        self.table = QTableWidget(0, 3)
        self.table.setObjectName("librariesTable")
        self.table.setHorizontalHeaderLabels(["Name", "Catalog file", "State"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.rename_button = self._button("Rename…", "renameLibraryButton", self.renameRequested)
        self.move_button = self._button("Move catalog…", "moveLibraryButton", self.moveRequested)
        self.reveal_button = self._button("Show in folder", "revealLibraryButton", self.revealRequested)
        self.remove_button = self._button("Remove from list", "removeLibraryButton", self.removeRequested)
        for button in (self.rename_button, self.move_button, self.reveal_button, self.remove_button):
            row.addWidget(button)
        row.addStretch(1)
        close = QPushButton("Close")
        close.setObjectName("closeManageButton")
        close.clicked.connect(self.close)
        row.addWidget(close)
        layout.addLayout(row)
        self.table.itemSelectionChanged.connect(self._sync)
        self._sync()

    def _button(self, text: str, name: str, signal: Any) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(name)
        button.clicked.connect(lambda _c=False: signal.emit(self.selected()) if self.selected() else None)
        return button

    def fill(self, entries: list[libs.Known], current: str | None) -> None:
        """Show `entries`; `current` is the library open in the window (it cannot be removed from the list)."""
        selected = self.selected()
        self._current = libs.key_of(current) if current else None
        self.table.setRowCount(0)
        for known in entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            is_current, is_default = libs.key_of(known.path) == self._current, libs.is_default(known.path)
            state = ", ".join(s for s in ("open now" if is_current else "", "default" if is_default else "",
                                         "file missing" if not known.exists and not is_default else "") if s)
            for column, text in enumerate((known.name, known.path, state)):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, known.path)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.table.setItem(row, column, item)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(2)
        if selected is not None:
            for row in range(self.table.rowCount()):
                if libs.key_of(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)) == libs.key_of(selected):
                    self.table.selectRow(row)
        self._sync()

    def selected(self) -> str | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole) if rows else None

    def _sync(self) -> None:
        path = self.selected()
        usable = path is not None and libs.Known(path, "").exists
        plain = path is not None and not libs.is_default(path)
        self.rename_button.setEnabled(path is not None)
        self.reveal_button.setEnabled(usable)
        self.move_button.setEnabled(usable and plain)  # the app's own library stays where `kv` looks for it
        self.remove_button.setEnabled(plain and libs.key_of(path) != self._current)


class MoveLibraryDialog(QDialog):
    """Where to copy a library's catalog. The wording is the promise: documents untouched, original file left where it is."""

    def __init__(self, parent: QWidget | None = None, *, name: str = "", start_folder: str = ""):
        super().__init__(parent)
        self.setObjectName("moveLibraryDialog")
        self.setWindowTitle("Move catalog")
        self.resize(560, 0)
        self._start = start_folder
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(f"The catalog of “{name}” will be copied to the folder you choose, and Knowledge Vista will use the copy from now on.\n\n"
                                     "Your documents are not touched. The original catalog file is left where it is: delete it yourself when you are sure you "
                                     "do not need it. Anything else that points at the old file (a `kv --catalog` shortcut, an MCP setting) keeps seeing the old "
                                     "copy until you change it.", name="moveIntro"))
        row = QHBoxLayout()
        self.folder_edit = QLineEdit(start_folder)
        self.folder_edit.setObjectName("moveFolderEdit")
        self.folder_edit.setPlaceholderText("D:\\Libraries\\Chemistry")
        self.browse_button = QPushButton("Browse…")
        self.browse_button.setObjectName("browseButton")
        self.browse_button.clicked.connect(self.browse)
        row.addWidget(self.folder_edit, 1)
        row.addWidget(self.browse_button)
        form = QFormLayout()
        form.addRow("New folder", row)
        layout.addLayout(form)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Copy and switch")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.folder_edit.textChanged.connect(self._sync)
        self._sync()

    def _sync(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self.folder_edit.text().strip()))

    def browse(self) -> None:
        """The system folder chooser. Modal by nature, and only ever reached by a click on Browse."""
        chosen = QFileDialog.getExistingDirectory(self, "Choose a folder for the catalog", self.folder_edit.text().strip() or self._start)
        if chosen:
            self.folder_edit.setText(chosen)

    def values(self) -> dict[str, Any]:
        return {"folder": self.folder_edit.text().strip()}


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
