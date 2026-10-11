"""The library window.

A thin adapter. It shows what `services/library_view.py` returns, sends what a person does to the same services the command line
calls, and puts every piece of work through the job manager (gui/jobs.py): the interface thread never reads the catalog, hashes a
file or waits on the disk. It follows the catalog's revision, so a change made by `kv` in a terminal while the window is open shows up
without a restart. Text from outside (titles, paths, tags) is only ever displayed as text (gui/text.py).

A person's path through it: Add folder (adds and scans) -> Extract text -> Resolve (proposes titles and DOIs) -> the Review tab
(accept or reject each proposal, or the batch rule for the safe ones) -> search inside the documents -> open one. "Why?" beside any
value says where it came from. "Propose rename…" shows what the organizer would do and changes nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QModelIndex, QSize, QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDockWidget, QFileDialog, QHBoxLayout, QHeaderView, QLineEdit, QMainWindow, QMessageBox,
                               QPlainTextEdit, QPushButton, QTableView, QTableWidget, QTableWidgetItem, QTabWidget, QToolBar, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from knowledgevista import __version__
from knowledgevista.domain import health_view
from knowledgevista.errors import ErrorCode
from knowledgevista.gui import jobs as J
from knowledgevista.gui import libraries
from knowledgevista.gui import state as statemod
from knowledgevista.gui import work
from knowledgevista.gui.detail import DetailPane, EvidenceDialog
from knowledgevista.gui.dialogs import (AddRootDialog, ChoiceDialog, ConfirmDialog, ManageLibrariesDialog, MoveLibraryDialog, NewLibraryDialog,
                                        RenameDialog)
from knowledgevista.gui.models import ITEM_ROLE, SORT_ROLE, DocumentModel, DocumentProxy, HitModel, ReviewModel
from knowledgevista.gui.shell import SystemShell
from knowledgevista.gui.text import message_box, plain_label, set_plain, tooltip

log = logging.getLogger("knowledgevista.gui.window")

TAB_KEYS = statemod.TABS
REVIEW_FILTERS = (("All proposals", None), ("Safe for the batch rule", "safe"), ("Needs a person", "required"))
STATUS_MS = 12000
#: Jobs that are the window reading things for itself. They are not 'work' a person asked for, so the Jobs panel lists them only if one fails.
QUIET_KINDS = frozenset({"revision", "detail", "snapshot", "health", "evidence", "search", "open", "reveal"})


def describe_scan(result: dict[str, Any]) -> str:
    text = f"Scanned {result['files']:,} file(s): {result['new_documents']:,} new document(s), {result['moved']:,} moved, {result['went_missing']:,} missing."
    if result["skipped"]:
        text += f" Not reachable, so nothing was changed there: {', '.join(result['skipped'])}."
    return text


def describe_extract(result: dict[str, Any]) -> str:
    text = (f"Extracted text from {result['extracted']:,} PDF(s) ({result['complete']:,} complete, {result['partial']:,} partial, {result['failed']:,} failed); "
            f"{result['current']:,} were already up to date.")
    if result["stopped"]:
        text += " Stopped early; run it again to finish."
    return text


def describe_resolve(result: dict[str, Any]) -> str:
    if result["documents"] and result["not_extracted"] == result["documents"]:
        return "Nothing could be read: none of the documents has extracted text yet. Use “Extract text” first, then Resolve."
    text = f"Read DOIs and titles from {result['documents'] - result['not_extracted']:,} of {result['documents']:,} document(s); proposals are waiting in the Review tab."
    if result["not_extracted"]:
        text += f" {result['not_extracted']:,} have no extracted text yet."
    if result["stopped"]:
        text += " Stopped early; run it again to finish."
    return text


class MainWindow(QMainWindow):
    refreshed = Signal(int)  # the catalog revision the lists now show
    notified = Signal(str, str)  # kind, text
    switchRequested = Signal(str, str, bool)  # catalog path, name ('' = keep), create it: the application shows that library instead (app.Session)

    def __init__(self, catalog: Path | str, library_id: str | None = None, *, state_path: Path | None = None, shell: Any = None,
                 poll_ms: int = 1500, remember: bool = True, library_name: str | None = None, libraries_path: Path | None = None):
        super().__init__()
        self.setObjectName("mainWindow")
        self.catalog = Path(catalog)
        self.library_id = library_id
        self.shell = shell or SystemShell()
        self._state_path = state_path
        self._libraries_path = libraries_path
        self._remember = remember
        known = libraries.load(libraries_path).find(self.catalog) if remember else None
        self.library_name = library_name or (known.name if known else libraries.label_for(self.catalog))
        self.setWindowTitle(f"Knowledge Vista — {self.library_name}")
        if remember:
            libraries.note_opened(self.catalog, library_name, libraries_path)
        self.state = statemod.load(state_path, library_id) if remember else statemod.GuiState(library_id=library_id)
        self.scope = self.state.scope
        self.current_document: str | None = self.state.document_id
        self.revision: int | None = None
        self.listing = None
        self.sidebar_data: dict[str, Any] = {"views": [], "collections": [], "tags": [], "roots": [], "documents": 0}
        self.review_all: list[dict[str, Any]] = []
        self.search_state: dict[str, Any] = {"text": "", "hits": [], "next": None}
        self.messages: list[tuple[str, str]] = []
        self._boxes: list[Any] = []
        self._dialogs: list[Any] = []
        self._manage: ManageLibrariesDialog | None = None
        #: Said by the NEXT window if this one hands over to another library (a moved catalog: where the old file was left).
        self.handover_message = ""
        self._restoring = False
        self._closing = False

        self.jobs = J.JobManager(self.catalog, self)
        self.jobs.jobChanged.connect(self._on_job_changed)
        self.jobs.jobAdded.connect(self._on_job_changed)
        self.doc_model, self.hit_model, self.review_model = DocumentModel(self), HitModel(self), ReviewModel(self)
        self.doc_proxy = DocumentProxy(self)
        self.doc_proxy.setSourceModel(self.doc_model)
        self.review_proxy = QSortFilterProxyModel(self)
        self.review_proxy.setSortRole(SORT_ROLE)
        self.review_proxy.setSourceModel(self.review_model)

        self._build_actions()
        self._build_toolbar()
        self._build_tabs()
        self._build_docks()
        self._build_statusbar()
        self.resize(1500, 860)
        if self.state.geometry:
            self.restoreGeometry(QByteArray.fromBase64(self.state.geometry.encode("ascii")))
        if self.state.layout:
            self.restoreState(QByteArray.fromBase64(self.state.layout.encode("ascii")))
        self.filter_box.setText(self.state.filter_text)
        self.search_box.setText(self.state.search_text)
        self.tabs.setCurrentIndex(TAB_KEYS.index(self.state.tab))
        if self.state.sort_column >= 0:
            self.doc_table.sortByColumn(self.state.sort_column, Qt.SortOrder.DescendingOrder if self.state.sort_descending else Qt.SortOrder.AscendingOrder)

        self._watch = QTimer(self)
        self._watch.setInterval(poll_ms)
        self._watch.timeout.connect(self._poll)
        self.refresh()
        if self.state.search_text.strip():
            self.run_search(self.state.search_text, switch=False)
        self._watch.start()

    # -- building -------------------------------------------------------------------------------------------------------

    def _action(self, name: str, text: str, slot: Callable[[], None], shortcut: str | None = None, tip: str | None = None) -> QAction:
        action = QAction(text, self)
        action.setObjectName(name)
        action.triggered.connect(lambda _checked=False: slot())
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        if tip:
            action.setToolTip(tip)
            action.setStatusTip(tip)
        return action

    def _build_actions(self) -> None:
        self.act_add = self._action("actionAddRoot", "Add folder…", self.open_add_root, "Ctrl+Shift+A", "Point Knowledge Vista at a folder of documents. Nothing in it is changed.")
        self.act_scan = self._action("actionScan", "Scan", self.start_scan, "F5", "Look for new, moved and missing files in every folder.")
        self.act_extract = self._action("actionExtract", "Extract text", self.start_extract, None, "Read the text of each PDF so it can be searched. Takes a while for a big library; it can be stopped.")
        self.act_resolve = self._action("actionResolve", "Resolve", self.start_resolve, None, "Find DOIs and titles in the extracted text and propose them. Offline: nothing leaves this computer.")
        self.act_refresh = self._action("actionRefresh", "Refresh", self.refresh, "Ctrl+R", "Read the library again.")
        self.act_reset = self._action("actionResetOrder", "Reset order", self.reset_order, None, "Back to the default order: what needs a person first, then alphabetical.")
        self.act_open = self._action("actionOpen", "Open file", self.open_current, "Ctrl+O", "Open the selected document in your PDF viewer.")
        self.act_open_library = self._action("actionOpenLibrary", "Open library…", self.open_library, "Ctrl+Shift+O", "Show another library (a catalog file) in this window. Nothing in either library is changed.")
        self.act_new_library = self._action("actionNewLibrary", "New library…", self.open_new_library, "Ctrl+Shift+N", "Start a separate, empty library with its own folders, documents and tags.")
        self.act_manage_libraries = self._action("actionManageLibraries", "Manage libraries…", self.open_manage_libraries, None, "Rename a library, show where its catalog is, copy the catalog to another folder, or take a library off the list. Documents are never touched.")
        self.act_ask_startup = self._action("actionAskStartup", "Ask which library at startup", self.toggle_ask_at_startup, None, "At launch, offer a choice of library when there is more than one. Off: open the library used last.")
        self.act_ask_startup.setCheckable(True)
        self.act_ask_startup.setChecked(self._registry().ask_at_startup)
        self.act_quit = self._action("actionQuit", "Quit", self.close, "Ctrl+Q")
        self.act_about = self._action("actionAbout", "About Knowledge Vista", self.show_about)
        menu = self.menuBar()
        file_menu = menu.addMenu("&File")
        file_menu.addActions([self.act_add, self.act_open])
        file_menu.addSeparator()
        file_menu.addActions([self.act_open_library, self.act_new_library])
        self.recent_menu = file_menu.addMenu("Recent libraries")
        self.recent_menu.setObjectName("recentLibrariesMenu")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        file_menu.addActions([self.act_manage_libraries, self.act_ask_startup])
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)
        library_menu = menu.addMenu("&Library")
        library_menu.addActions([self.act_scan, self.act_extract, self.act_resolve, self.act_refresh])
        self.view_menu = menu.addMenu("&View")
        self.view_menu.addAction(self.act_reset)
        menu.addMenu("&Help").addAction(self.act_about)

    def _build_toolbar(self) -> None:
        bar = QToolBar("Main", self)
        bar.setObjectName("mainToolbar")
        bar.setMovable(False)
        bar.addActions([self.act_add, self.act_scan, self.act_extract, self.act_resolve])
        bar.addSeparator()
        self.search_box = QLineEdit()
        self.search_box.setObjectName("searchBox")
        self.search_box.setPlaceholderText("Search inside documents…   e.g. aqueous solubility   year:2020   author:smith")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setMinimumWidth(420)
        self.search_box.returnPressed.connect(lambda: self.run_search(self.search_box.text()))
        bar.addWidget(self.search_box)
        bar.addSeparator()
        bar.addActions([self.act_refresh])
        self.addToolBar(bar)

    def _build_tabs(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.setObjectName("tabs")
        self.tabs.currentChanged.connect(self._on_tab)
        self.setCentralWidget(self.tabs)

        # Documents
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        self.filter_box = QLineEdit()
        self.filter_box.setObjectName("filterBox")
        self.filter_box.setPlaceholderText("Filter this list by title, author, DOI or path")
        self.filter_box.setClearButtonEnabled(True)
        self.filter_box.textChanged.connect(self._on_filter)
        row.addWidget(self.filter_box, 1)
        reset = QPushButton("Reset order")
        reset.setObjectName("resetOrderButton")
        reset.clicked.connect(self.reset_order)
        row.addWidget(reset)
        layout.addLayout(row)
        self.doc_table = self._table("docTable", self.doc_proxy)
        self.doc_table.setSortingEnabled(True)
        self.doc_table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.doc_table.selectionModel().currentRowChanged.connect(self._on_doc_current)
        self.doc_table.doubleClicked.connect(lambda _index: self.open_current())
        widths = {0: 80, 1: 330, 2: 64, 3: 140, 4: 46, 5: 150, 6: 50, 7: 70}
        for column, width in widths.items():
            self.doc_table.setColumnWidth(column, width)
        layout.addWidget(self.doc_table, 1)
        self.list_note = plain_label(name="listNote", selectable=False)
        layout.addWidget(self.list_note)
        self.tabs.addTab(page, "Documents")

        # Search results
        page = QWidget()
        layout = QVBoxLayout(page)
        self.search_summary = plain_label("Type in the search box above and press Enter.", name="searchSummary")
        layout.addWidget(self.search_summary)
        self.hit_table = self._table("hitTable", self.hit_model)
        self.hit_table.setSortingEnabled(False)
        self.hit_table.horizontalHeader().setStretchLastSection(True)
        self.hit_table.setColumnWidth(0, 340)
        self.hit_table.setColumnWidth(1, 90)
        self.hit_table.selectionModel().currentRowChanged.connect(self._on_hit_current)
        self.hit_table.doubleClicked.connect(self._on_hit_activated)
        layout.addWidget(self.hit_table, 1)
        self.more_hits = QPushButton("More results")
        self.more_hits.setObjectName("moreHitsButton")
        self.more_hits.clicked.connect(self.more_search)
        self.more_hits.hide()
        layout.addWidget(self.more_hits)
        self.tabs.addTab(page, "Search results")

        # Review
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        self.review_filter = QComboBox()
        self.review_filter.setObjectName("reviewFilter")
        for text, _value in REVIEW_FILTERS:
            self.review_filter.addItem(text)
        self.review_filter.currentIndexChanged.connect(self._apply_review_filter)
        row.addWidget(self.review_filter)
        self.accept_button = QPushButton("Accept")
        self.accept_button.setObjectName("acceptButton")
        self.reject_button = QPushButton("Reject")
        self.reject_button.setObjectName("rejectButton")
        self.review_why_button = QPushButton("Why?")
        self.review_why_button.setObjectName("reviewWhyButton")
        self.accept_safe_button = QPushButton("Accept all safe…")
        self.accept_safe_button.setObjectName("acceptSafeButton")
        self.accept_safe_button.setToolTip("Accept every proposal the batch rule marks safe. It never replaces a value a person set, and leaves disagreements alone.")
        self.accept_button.clicked.connect(lambda: self.decide_selected(True))
        self.reject_button.clicked.connect(lambda: self.decide_selected(False))
        self.review_why_button.clicked.connect(self.why_selected_review)
        self.accept_safe_button.clicked.connect(self.confirm_accept_safe)
        for button in (self.accept_button, self.reject_button, self.review_why_button):
            row.addWidget(button)
        row.addStretch(1)
        row.addWidget(self.accept_safe_button)
        layout.addLayout(row)
        self.review_table = self._table("reviewTable", self.review_proxy)
        self.review_table.setSortingEnabled(True)
        self.review_table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.review_table.horizontalHeader().setStretchLastSection(True)
        self.review_table.setColumnWidth(0, 280)
        self.review_table.setColumnWidth(1, 90)
        self.review_table.setColumnWidth(2, 260)
        self.review_table.setColumnWidth(3, 180)
        self.review_table.selectionModel().currentRowChanged.connect(self._on_review_current)
        self.review_table.doubleClicked.connect(lambda _index: self.why_selected_review())
        layout.addWidget(self.review_table, 1)
        self.review_note = plain_label(name="reviewNote", selectable=False)
        layout.addWidget(self.review_note)
        self.tabs.addTab(page, "Review")

        # Health
        page = QWidget()
        layout = QVBoxLayout(page)
        self.inventory_text = QPlainTextEdit()
        self.inventory_text.setObjectName("inventoryText")
        self.health_text = QPlainTextEdit()
        self.health_text.setObjectName("healthText")
        for title, edit in (("What the library holds", self.inventory_text), ("What needs attention", self.health_text)):
            edit.setReadOnly(True)
            layout.addWidget(plain_label(title, name=f"{edit.objectName()}Title", selectable=False))
            layout.addWidget(edit, 1)
        self.tabs.addTab(page, "Health")

    def _table(self, name: str, model: Any) -> QTableView:
        table = QTableView()
        table.setObjectName(name)
        table.setModel(model)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(24)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        table.horizontalHeader().setHighlightSections(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def _build_docks(self) -> None:
        self.sidebar = QTreeWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setHeaderHidden(True)
        self.sidebar.setIndentation(14)
        self.sidebar.currentItemChanged.connect(self._on_sidebar_current)
        self.detail = DetailPane()
        self.detail.whyRequested.connect(self.request_why)
        self.detail.openRequested.connect(self.open_current)
        self.detail.revealRequested.connect(self.reveal_current)
        self.detail.renameRequested.connect(self.request_rename)
        self.detail.collectionRequested.connect(self.request_collection)
        self.detail.tagRequested.connect(self.request_tag)
        self.jobs_table = QTableWidget(0, 4)
        self.jobs_table.setObjectName("jobsTable")
        self.jobs_table.setHorizontalHeaderLabels(["Job", "State", "Progress", ""])
        self.jobs_table.verticalHeader().hide()
        self.jobs_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.jobs_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.jobs_table.horizontalHeader().setStretchLastSection(False)
        self.jobs_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._job_rows: dict[int, int] = {}
        for area, name, title, widget, size in (
            (Qt.DockWidgetArea.LeftDockWidgetArea, "libraryDock", "Library", self.sidebar, QSize(230, 400)),
            (Qt.DockWidgetArea.RightDockWidgetArea, "detailDock", "Details", self.detail, QSize(420, 400)),
            (Qt.DockWidgetArea.BottomDockWidgetArea, "jobsDock", "Jobs", self.jobs_table, QSize(800, 110)),
        ):
            dock = QDockWidget(title, self)
            dock.setObjectName(name)
            dock.setWidget(widget)
            widget.setMinimumSize(size.width() // 2, 60)
            self.addDockWidget(area, dock)
            self.view_menu.addAction(dock.toggleViewAction())
        self.resizeDocks([self.findChild(QDockWidget, "libraryDock"), self.findChild(QDockWidget, "detailDock")], [230, 420], Qt.Orientation.Horizontal)
        self.resizeDocks([self.findChild(QDockWidget, "jobsDock")], [110], Qt.Orientation.Vertical)

    def _build_statusbar(self) -> None:
        self.count_label = plain_label(name="countLabel", wrap=False, selectable=False)
        self.revision_label = plain_label(name="revisionLabel", wrap=False, selectable=False)
        self.revision_label.setToolTip("The catalog revision this window shows. It moves whenever anything changes the library, including `kv` in a terminal.")
        self.statusBar().addPermanentWidget(self.count_label)
        self.statusBar().addPermanentWidget(self.revision_label)

    # -- telling the person ---------------------------------------------------------------------------------------------

    def notify(self, text: str, kind: str = "info") -> None:
        self.messages.append((kind, text))
        self.statusBar().showMessage(text, STATUS_MS)
        self.notified.emit(kind, text)

    def error(self, title: str, text: str) -> None:
        """Something a person asked for did not happen. In the status bar AND a message that stays until it is dismissed."""
        self.notify(text, "error")
        box = message_box(self, title, text, icon=QMessageBox.Icon.Warning, name="errorBox")
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.destroyed.connect(lambda _o=None, b=box: self._boxes.remove(b) if b in self._boxes else None)
        self._boxes.append(box)
        box.open()

    def _open_dialog(self, dialog: Any) -> None:
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._dialogs.append(dialog)
        dialog.destroyed.connect(lambda _o=None, d=dialog: self._dialogs.remove(d) if d in self._dialogs else None)
        dialog.open()

    def show_about(self) -> None:
        box = message_box(self, "About Knowledge Vista", f"Knowledge Vista {__version__}\nA local-first library for messy document folders.\nAGPL-3.0-or-later.", name="aboutBox")
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._boxes.append(box)
        box.open()

    # -- reading the library --------------------------------------------------------------------------------------------

    def refresh(self) -> None:
        """Read everything the lists show, as one snapshot, on a worker; the newest request wins."""
        scope = self.scope
        self.jobs.submit("snapshot", "Read the library", lambda ctx: work.snapshot(ctx, scope), channel="snapshot", on_done=lambda job: self._on_snapshot(job, scope))

    def _on_snapshot(self, job: J.Job, scope: str) -> None:
        if job.state == J.FAILED:
            if job.error_code == ErrorCode.NOT_FOUND and scope != statemod.DEFAULT_SCOPE:
                self.notify("That collection no longer exists; showing all documents.", "info")
                self.scope = statemod.DEFAULT_SCOPE
                self.refresh()
                return
            self.notify(job.error or "Could not read the library.", "error")
            return
        if job.state != J.SUCCEEDED:
            return
        snap = job.result
        self.listing, self.revision = snap["listing"], snap["revision"]
        self.library_id = snap["library_id"]
        self.sidebar_data = snap["sidebar"]
        self._restoring = True
        try:
            self.doc_model.reset(self.listing.rows)
            self._fill_sidebar()
            self.review_all = snap["review"] or []
            self._apply_review_filter()
        finally:
            self._restoring = False
        self._reselect()
        self._update_notes()
        self.refreshed.emit(self.revision)
        if self.current_document:
            self.load_detail(self.current_document)
        if self.current_tab == "health":
            self.load_health()

    def _reselect(self) -> None:
        if not self.current_document:
            return
        source_row = self.doc_model.index_of(self.current_document)
        if source_row < 0:
            return
        index = self.doc_proxy.mapFromSource(self.doc_model.index(source_row, 0))
        if not index.isValid():
            return
        self._restoring = True
        try:
            self.doc_table.setCurrentIndex(index)
            self.doc_table.selectRow(index.row())
            self.doc_table.scrollTo(index)
        finally:
            self._restoring = False

    def _update_notes(self) -> None:
        shown, total = self.doc_proxy.rowCount(), self.sidebar_data["documents"]
        label = self._scope_label()
        if total == 0:
            note = "No folders yet. Use “Add folder…” to point Knowledge Vista at your documents."
        elif shown == 0 and self.doc_proxy.needle:
            note = f"Nothing in {label} matches “{self.filter_box.text().strip()}”."
        elif shown == 0:
            note = f"{label} is empty. That is good news." if self.scope.startswith("view:") else f"{label} has no documents."
        else:
            note = f"{shown:,} of {total:,} document(s) — {label}"
        set_plain(self.list_note, note)
        set_plain(self.count_label, f"{total:,} documents   ")
        set_plain(self.revision_label, f"revision {self.revision}   ")

    def _scope_label(self) -> str:
        kind, _, name = self.scope.partition(":")
        for group in ("views", "collections", "tags"):
            for entry in self.sidebar_data[group]:
                if entry["scope"] == self.scope:
                    return entry["label"]
        return "All documents" if kind == "all" else name

    # -- the sidebar ----------------------------------------------------------------------------------------------------

    def _fill_sidebar(self) -> None:
        tree = self.sidebar
        tree.blockSignals(True)
        tree.clear()
        wanted = None
        sections = (("Views", self.sidebar_data["views"]), ("Collections", self.sidebar_data["collections"]), ("Tags", self.sidebar_data["tags"]))
        everything = QTreeWidgetItem(["All documents"])
        everything.setData(0, Qt.ItemDataRole.UserRole, "all")
        tree.addTopLevelItem(everything)
        if self.scope == "all":
            wanted = everything
        for title, entries in sections:
            top = QTreeWidgetItem([title])
            top.setFlags(Qt.ItemFlag.ItemIsEnabled)
            tree.addTopLevelItem(top)
            for entry in entries:
                item = QTreeWidgetItem([f"{entry['label']}  ({entry['count']:,})"])
                item.setData(0, Qt.ItemDataRole.UserRole, entry["scope"])
                top.addChild(item)
                if entry["scope"] == self.scope:
                    wanted = item
            top.setExpanded(True)
        top = QTreeWidgetItem(["Folders"])
        top.setFlags(Qt.ItemFlag.ItemIsEnabled)
        tree.addTopLevelItem(top)
        for root in self.sidebar_data["roots"]:
            state = root["status"] if root["enabled"] else "disabled"
            item = QTreeWidgetItem([f"{root['label']}  ({state})"])
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            item.setToolTip(0, tooltip(f"{root['configured_path']}\n" + ("The organizer may rename files here." if root["allow_organize"] else "Read only: the organizer may not change files here.")
                                       + ("\nLast scanned " + root["last_scan_at"] if root["last_scan_at"] else "\nNever scanned.")))
            top.addChild(item)
        top.setExpanded(True)
        if wanted is not None:
            tree.setCurrentItem(wanted)
        tree.blockSignals(False)

    def _on_sidebar_current(self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        scope = current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None
        if not scope or scope == self.scope:
            return
        self.set_scope(scope)

    def set_scope(self, scope: str) -> None:
        self.scope = scope
        self.tabs.setCurrentIndex(TAB_KEYS.index("documents"))
        self.refresh()

    # -- the document table and its detail -------------------------------------------------------------------------------

    def _on_filter(self, text: str) -> None:
        self.doc_proxy.set_filter(text)
        if self.listing is not None:
            self._update_notes()

    def reset_order(self) -> None:
        self.doc_table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.doc_proxy.sort(-1)

    def _on_doc_current(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if self._restoring or not current.isValid():
            return
        row = current.data(ITEM_ROLE)
        self.current_document = row.document_id
        self.load_detail(row.document_id)

    def _on_hit_current(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if not current.isValid():
            return
        hit = current.data(ITEM_ROLE)
        if hit["document_id"]:
            self.current_document = hit["document_id"]
            self.load_detail(hit["document_id"])

    def _on_review_current(self, current: QModelIndex, _previous: QModelIndex) -> None:
        valid = current.isValid()
        for button in (self.accept_button, self.reject_button, self.review_why_button):
            button.setEnabled(valid)
        if valid:
            item = current.data(ITEM_ROLE)
            self.current_document = item["document_id"]
            self.load_detail(item["document_id"])

    def load_detail(self, document_id: str) -> None:
        self.jobs.submit("detail", "Load details", lambda ctx: work.detail(ctx, document_id), channel="detail", on_done=self._on_detail)

    def _on_detail(self, job: J.Job) -> None:
        if job.state == J.SUCCEEDED:
            if job.result["document_id"] == self.current_document:
                self.detail.show_detail(job.result)
        elif job.state == J.FAILED:
            self.detail.clear("This document is no longer in the library." if job.error_code == ErrorCode.NOT_FOUND else (job.error or "Could not read this document."))

    @property
    def current_tab(self) -> str:
        return TAB_KEYS[self.tabs.currentIndex()]

    def _on_tab(self, _index: int) -> None:
        if self.current_tab == "health":
            self.load_health()

    def load_health(self) -> None:
        self.jobs.submit("health", "Read library health", work.health, channel="health", on_done=self._on_health)

    def _on_health(self, job: J.Job) -> None:
        if job.state == J.SUCCEEDED:
            self.inventory_text.setPlainText(health_view.inventory_text(job.result))
            self.health_text.setPlainText(health_view.health_text(job.result))
        elif job.state == J.FAILED:
            self.notify(job.error or "Could not read the library's health.", "error")

    # -- why this value --------------------------------------------------------------------------------------------------

    def request_why(self, field: str, document_id: str | None = None) -> None:
        document_id = document_id or self.detail.document_id
        if document_id is None:
            return
        self.jobs.submit("evidence", "Look up the evidence", lambda ctx: work.evidence(ctx, document_id, field), on_done=self._on_evidence)

    def _on_evidence(self, job: J.Job) -> None:
        if job.state == J.SUCCEEDED:
            self.evidence_dialog = EvidenceDialog(job.result, self)
            self._open_dialog(self.evidence_dialog)
        elif job.state == J.FAILED:
            self.error("Why this value?", job.error or "Could not look that up.")

    # -- opening ---------------------------------------------------------------------------------------------------------

    def open_current(self, pdf_page: int | None = None, document_id: str | None = None) -> None:
        document_id = document_id or self.current_document
        if document_id:
            self.jobs.submit("open", "Find the file", lambda ctx: work.locate_for_open(ctx, document_id, pdf_page), on_done=lambda job: self._opened(job, "open"))

    def reveal_current(self) -> None:
        if self.current_document:
            document_id = self.current_document
            self.jobs.submit("reveal", "Find the file", lambda ctx: work.locate_for_open(ctx, document_id), on_done=lambda job: self._opened(job, "reveal"))

    def _opened(self, job: J.Job, how: str) -> None:
        if job.state == J.FAILED:
            self.error("Open", job.error or "Could not find the file.")
        elif job.state == J.SUCCEEDED:
            path = job.result["path"]
            (self.shell.open_file if how == "open" else self.shell.reveal)(path)
            page = job.result["requested_page"]
            extra = f" Go to page {page['pdf_page'] or page['printed_label']} yourself: a PDF viewer cannot be told which page." if page else ""
            self.notify(("Opened " if how == "open" else "Showing ") + path + "." + (extra if how == "open" else ""), "info")

    # -- searching -------------------------------------------------------------------------------------------------------

    def run_search(self, text: str, *, switch: bool = True) -> None:
        text = text.strip()
        self.search_state = {"text": text, "hits": [], "next": None}
        if not text:
            return
        self.jobs.submit("search", f"Search for {text}", lambda ctx: work.search(ctx, text), channel="search", on_done=lambda job: self._on_search(job, text, switch, False))

    def more_search(self) -> None:
        token, text = self.search_state["next"], self.search_state["text"]
        if token:
            self.jobs.submit("search", f"More results for {text}", lambda ctx: work.search(ctx, text, token), channel="search",
                             on_done=lambda job: self._on_search(job, text, False, True))

    def _on_search(self, job: J.Job, text: str, switch: bool, append: bool) -> None:
        if job.state == J.FAILED:
            self.hit_model.reset([])
            self.more_hits.hide()
            set_plain(self.search_summary, job.error or "The search failed.")
            self.notify(job.error or "The search failed.", "error")
            if switch:
                self.tabs.setCurrentIndex(TAB_KEYS.index("search"))
            return
        if job.state != J.SUCCEEDED:
            return
        found = job.result
        hits = (self.search_state["hits"] if append else []) + found["hits"]
        self.search_state = {"text": text, "hits": hits, "next": found["next_cursor"]}
        self.hit_model.reset(hits)
        documents = len({h["document_id"] for h in hits})
        if found["searchable"] == 0:
            summary = "No document has extracted text, so this search could not have found anything. Use Library > Extract text."
        else:
            summary = f"{len(hits):,} page(s) in {documents:,} document(s) for “{text}”" + ("  — more are available" if found["next_cursor"] else "")
            if found["scope"] is not None:
                summary += f"\nFilters selected {found['scope']['documents_matching']:,} document(s); only those were searched."
            if found["note"]:
                summary += "\n" + found["note"]
        set_plain(self.search_summary, summary)
        self.more_hits.setVisible(bool(found["next_cursor"]))
        if switch:
            self.tabs.setCurrentIndex(TAB_KEYS.index("search"))

    def _on_hit_activated(self, index: QModelIndex) -> None:
        hit = index.data(ITEM_ROLE)
        if hit and hit["document_id"]:
            self.open_current(hit["pdf_page"], hit["document_id"])

    # -- the review queue ------------------------------------------------------------------------------------------------

    def _apply_review_filter(self) -> None:
        wanted = REVIEW_FILTERS[self.review_filter.currentIndex()][1]
        shown = [i for i in self.review_all if wanted is None or i["review"] == wanted]
        self.review_model.reset(shown)
        safe = sum(1 for i in self.review_all if i["review"] == "safe")
        self.tabs.setTabText(TAB_KEYS.index("review"), f"Review ({len(self.review_all)})" if self.review_all else "Review")
        set_plain(self.review_note, f"{len(shown):,} shown of {len(self.review_all):,} waiting; {safe:,} are safe for the batch rule. "
                                    "Nothing here is accepted until you accept it.")
        self.accept_safe_button.setEnabled(safe > 0)
        for button in (self.accept_button, self.reject_button, self.review_why_button):
            button.setEnabled(False)

    def _selected_review(self) -> dict[str, Any] | None:
        index = self.review_table.currentIndex()
        return index.data(ITEM_ROLE) if index.isValid() else None

    def decide_selected(self, accept: bool) -> None:
        item = self._selected_review()
        if item is None:
            return
        self.jobs.submit("decide", "Accept" if accept else "Reject", lambda ctx: work.decide(ctx, item, accept), lane=J.WRITE, on_done=lambda job: self._decided(job, item))

    def _decided(self, job: J.Job, item: dict[str, Any]) -> None:
        if job.state == J.FAILED:
            self.error("Review", job.error or "Could not record that.")
        elif job.state == J.SUCCEEDED:
            self.notify(f"{job.result['decision'].capitalize()} {item['label']}: {item['display']}", "info")
        self.refresh()

    def why_selected_review(self) -> None:
        item = self._selected_review()
        if item is not None:
            self.request_why(item["field"], item["document_id"])

    def confirm_accept_safe(self) -> None:
        safe = sum(1 for i in self.review_all if i["review"] == "safe")
        dialog = ConfirmDialog("Accept all safe proposals",
                               f"Accept the {safe:,} proposal(s) the batch rule marks safe?\n\nThe rule never replaces a value a person set or locked, and it leaves any "
                               "field where safe proposals disagree for you. Each acceptance is recorded as made by the rule, and can be changed afterwards.",
                               "Accept them", self, name="confirmAcceptSafe")
        dialog.accepted.connect(self.accept_all_safe)
        self._open_dialog(dialog)

    def accept_all_safe(self) -> None:
        self.jobs.submit("accept_safe", "Accept all safe proposals", work.accept_safe, lane=J.WRITE, once=True, on_done=self._accepted_safe)

    def _accepted_safe(self, job: J.Job) -> None:
        if job.state == J.FAILED:
            self.error("Review", job.error or "Could not accept the safe proposals.")
        elif job.state == J.SUCCEEDED:
            r = job.result
            self.notify(f"Accepted {r['accepted']:,} value(s) by the batch rule; {r['left_for_a_person']:,} left for a person.", "info")
        self.refresh()

    # -- choosing the library --------------------------------------------------------------------------------------------

    def _fill_recent(self) -> None:
        """The Recent libraries menu, read when it opens: every library the window has shown, this one ticked, a vanished file marked."""
        for old in self.recent_menu.actions():
            old.deleteLater()
        self.recent_menu.clear()
        here = libraries.key_of(self.catalog)
        registry = libraries.load(self._libraries_path) if self._remember else libraries.Registry()
        for number, known in enumerate(libraries.known_for_menu(registry)):
            current = libraries.key_of(known.path) == here
            missing = not current and not known.exists and not libraries.is_default(known.path)
            label = known.name.replace("&", "&&") + (" — file missing" if missing else "")  # '&' would be read as a shortcut
            action = self._action(f"recentLibrary{number}", label, lambda k=known: self.pick_library(k.path, k.name), tip=known.path)
            action.setCheckable(True)
            action.setChecked(current)
            self.recent_menu.addAction(action)

    def pick_library(self, path: str, name: str = "") -> None:
        """A library chosen from the list. One whose file has gone is not opened (that would invent an empty one); the person is asked
        whether to take it off the list."""
        if libraries.key_of(path) == libraries.key_of(self.catalog):
            self.notify(f"“{self.library_name}” is already open.", "info")
        elif libraries.is_default(path):
            self.request_switch(path, name, create=not Path(path).is_file())  # the app's own library is made on first use, as `kv gui` makes it
        elif not Path(path).is_file():
            dialog = ConfirmDialog("Library not found", f"There is no library file at {path}.\n\nRemove it from the list? Nothing on disk is touched.",
                                   "Remove from list", self, name="confirmForgetLibrary")
            dialog.accepted.connect(lambda: self.forget_library(path))
            self._open_dialog(dialog)
        else:
            self.request_switch(path, name)

    def forget_library(self, path: str) -> None:
        if self._remember:
            libraries.save(libraries.forgotten(libraries.load(self._libraries_path), path), self._libraries_path)
        self.notify("Removed it from the list of libraries.", "info")
        self._refill_manage()

    def open_library(self) -> None:
        dialog = QFileDialog(self, "Open a library", str(self.catalog.parent), "Library files (*.sqlite);;All files (*)")
        dialog.setObjectName("openLibraryDialog")
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        dialog.fileSelected.connect(lambda chosen: self.request_switch(chosen))
        self._open_dialog(dialog)

    def open_new_library(self) -> None:
        dialog = NewLibraryDialog(self)
        dialog.accepted.connect(lambda: self.create_library(**dialog.values()))
        self._open_dialog(dialog)

    def create_library(self, name: str, folder: str) -> None:
        directory = Path(folder).expanduser()
        if not directory.is_absolute():
            self.error("New library", "Choose a full folder path (for example D:\\Libraries\\Chemistry).")
        elif (directory / "catalog.sqlite").exists():
            self.error("New library", f"{directory} already holds a library (catalog.sqlite). Use File > Open library… to show it, or choose another folder.")
        else:
            self.request_switch(str(directory / "catalog.sqlite"), name, create=True)

    def request_switch(self, path: str, name: str = "", create: bool = False) -> None:
        """Ask the application to show another library here. Unfinished work is stopped only after a person says so."""
        if self._closing:
            return
        if self.jobs.busy:
            dialog = ConfirmDialog("Switch library", "A job is still running (see the Jobs panel).\n\nSwitching stops it at its next safe point; what it has "
                                   "finished is kept, and you can run it again from the library later.", "Stop and switch", self, name="confirmSwitchLibrary")
            dialog.accepted.connect(lambda: self.switchRequested.emit(path, name, create))
            self._open_dialog(dialog)
        else:
            self.switchRequested.emit(path, name, create)

    # -- managing the list of libraries ----------------------------------------------------------------------------------
    # Nothing here changes a document or deletes a file. Rename and remove edit the LIST; Show in folder asks the shell; Move catalog
    # copies the catalog (services/catalog_copy.py) and leaves the original where it is.

    def _registry(self) -> libraries.Registry:
        return libraries.load(self._libraries_path) if self._remember else libraries.Registry()

    def _save_registry(self, registry: libraries.Registry) -> None:
        if self._remember:
            libraries.save(registry, self._libraries_path)

    def inform(self, title: str, text: str) -> None:
        """A message that stays until it is dismissed (a status-bar line would be gone before it was read)."""
        box = message_box(self, title, text, icon=QMessageBox.Icon.Information, name="infoBox")
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.destroyed.connect(lambda _o=None, b=box: self._boxes.remove(b) if b in self._boxes else None)
        self._boxes.append(box)
        box.open()

    def toggle_ask_at_startup(self) -> None:
        ask = self.act_ask_startup.isChecked()
        self._save_registry(libraries.with_ask_at_startup(self._registry(), ask))
        self.notify("Next time, you will be asked which library to open." if ask else "Next time, the library you used last will open without asking.", "info")

    def open_manage_libraries(self) -> None:
        dialog = ManageLibrariesDialog(self)
        dialog.renameRequested.connect(self.request_rename_library)
        dialog.moveRequested.connect(self.request_move_library)
        dialog.revealRequested.connect(self.reveal_library)
        dialog.removeRequested.connect(self.remove_library)
        dialog.destroyed.connect(lambda _o=None: setattr(self, "_manage", None))
        self._manage = dialog
        self._refill_manage()
        self._open_dialog(dialog)

    def _refill_manage(self) -> None:
        if self._manage is not None:
            try:
                self._manage.fill(libraries.known_for_menu(self._registry()), str(self.catalog))
            except RuntimeError:  # the dialog was closed and deleted a moment ago
                self._manage = None

    def request_rename_library(self, path: str) -> None:
        known = self._registry().find(path)
        dialog = ChoiceDialog("Rename library", "A new name for this library. Only the name changes; the catalog file keeps its name.", [], "Rename", self,
                              name="renameLibraryDialog")
        dialog.combo.setEditText(known.name if known else libraries.label_for(path))
        dialog.accepted.connect(lambda: self.rename_library(path, dialog.value()))
        self._open_dialog(dialog)

    def rename_library(self, path: str, name: str) -> None:
        name = " ".join(name.split())
        if not name:
            return
        self._save_registry(libraries.renamed(self._registry(), path, name))
        if libraries.key_of(path) == libraries.key_of(self.catalog):
            self.library_name = name
            self.setWindowTitle(f"Knowledge Vista — {name}")
        self.notify(f"Renamed to “{name}”.", "info")
        self._refill_manage()

    def reveal_library(self, path: str) -> None:
        self.shell.reveal(path)

    def remove_library(self, path: str) -> None:
        """Take a library off the list. The catalog file, and every document, stay exactly where they are."""
        if libraries.key_of(path) == libraries.key_of(self.catalog):
            self.notify("The library that is open cannot be removed from the list.", "info")
        elif libraries.is_default(path):
            self.notify("The default library is always on the list.", "info")
        else:
            known = self._registry().find(path)
            self._save_registry(libraries.forgotten(self._registry(), path))
            self.notify(f"Removed “{known.name if known else path}” from the list. Its catalog file was not touched.", "info")
        self._refill_manage()

    def request_move_library(self, path: str) -> None:
        known = self._registry().find(path)
        dialog = MoveLibraryDialog(self, name=known.name if known else libraries.label_for(path))
        dialog.accepted.connect(lambda: self.move_library(path, **dialog.values()))
        self._open_dialog(dialog)

    def move_library(self, path: str, folder: str) -> None:
        """Copy the catalog to `folder`, then use the copy. The original file is left where it is."""
        if libraries.is_default(path):
            self.error("Move catalog", "The default library stays where `kv` looks for it.")
            return
        self.jobs.submit("copy_catalog", "Move a library", lambda ctx: work.copy_catalog(ctx, path, folder), lane=J.WRITE, once=True,
                         on_done=lambda job: self._library_copied(job, path))

    def _library_copied(self, job: J.Job, old: str) -> None:
        if job.state == J.FAILED:
            self.error("Move catalog", job.error or "Could not copy the catalog. Nothing was changed.")
            return
        if job.state != J.SUCCEEDED:
            return
        result = job.result
        known = self._registry().find(old)
        self._save_registry(libraries.moved(self._registry(), old, result["destination"]))
        text = (f"The catalog was copied to {result['destination']}, and that copy is what Knowledge Vista uses from now on.\n\n"
                f"The original at {old} was left where it is. Your documents were not touched. Delete the old catalog yourself once you are sure you do not "
                "need it; anything that still points at it (a `kv --catalog` shortcut, an MCP setting) keeps seeing the old copy.")
        if result["notes"]:
            text += "\n\n" + "\n".join(result["notes"])
        if libraries.key_of(old) == libraries.key_of(self.catalog):
            self.handover_message = text  # this window is about to be replaced; the new one says it
            self.request_switch(result["destination"], known.name if known else "")
        else:
            self.inform("Catalog copied", text)
            self._refill_manage()

    # -- changing the library --------------------------------------------------------------------------------------------

    def open_add_root(self) -> None:
        dialog = AddRootDialog(self, start_folder=self.state.last_folder or "")
        dialog.accepted.connect(lambda: self.add_root(**dialog.values()))
        self._open_dialog(dialog)

    def add_root(self, path: str, label: str | None = None, allow_organize: bool = False) -> None:
        self.jobs.submit("add_root", "Add a folder", lambda ctx: work.add_root(ctx, path, label, allow_organize), lane=J.WRITE, on_done=self._root_added)

    def _root_added(self, job: J.Job) -> None:
        if job.state == J.FAILED:
            self.error("Add a folder", job.error or "Could not add that folder.")
            return
        if job.state != J.SUCCEEDED:
            return
        self.state.last_folder = job.result["path"]
        self.notify(f"Added {job.result['path']}. Scanning it now.", "info")
        self.refresh()
        self.start_scan()

    def start_scan(self) -> None:
        self.jobs.submit("scan", "Scan", work.scan, lane=J.WRITE, once=True, on_done=lambda job: self._finished(job, "Scan", describe_scan))

    def start_extract(self) -> None:
        self.jobs.submit("extract", "Extract text", work.extract, lane=J.WRITE, once=True, on_done=lambda job: self._finished(job, "Extract text", describe_extract))

    def start_resolve(self) -> None:
        self.jobs.submit("resolve", "Resolve", work.resolve, lane=J.WRITE, once=True, on_done=lambda job: self._finished(job, "Resolve", describe_resolve))

    def _finished(self, job: J.Job, title: str, describe: Callable[[dict[str, Any]], str]) -> None:
        if job.state == J.FAILED:
            self.error(title, job.error or f"{title} failed.")
        elif job.state == J.CANCELLED:
            self.notify(f"{title} was stopped. What it had done is kept; run it again to continue.", "info")
        elif job.state == J.SUCCEEDED:
            self.notify(describe(job.result), "info")
        self.refresh()

    def request_collection(self) -> None:
        document_id = self.detail.document_id
        if document_id is None:
            return
        names = [c["label"] for c in self.sidebar_data["collections"]]
        dialog = ChoiceDialog("Add to a collection", "Pick a collection, or type a name to start a new one.", names, "Add", self, name="collectionDialog")
        dialog.accepted.connect(lambda: self._add_to_collection(dialog.value(), document_id))  # the value is read HERE, on this thread, not inside the job
        self._open_dialog(dialog)

    def _add_to_collection(self, name: str, document_id: str) -> None:
        self.jobs.submit("collection", "Add to a collection", lambda ctx: work.add_to_collection(ctx, name, document_id), lane=J.WRITE, on_done=self._collected)

    def _collected(self, job: J.Job) -> None:
        if job.state == J.FAILED:
            self.error("Add to a collection", job.error or "Could not add it.")
        elif job.state == J.SUCCEEDED:
            r = job.result
            self.notify((f"Started the collection “{r['name']}”." if r["created"] else f"Added to “{r['name']}”.") if (r["added"] or r["created"]) else f"Already in “{r['name']}”.", "info")
        self.refresh()

    def request_tag(self) -> None:
        document_id = self.detail.document_id
        if document_id is None:
            return
        names = [t["label"] for t in self.sidebar_data["tags"]]
        dialog = ChoiceDialog("Tag", "Pick a tag, or type a new one.", names, "Tag", self, name="tagDialog")
        dialog.accepted.connect(lambda: self._add_tag(dialog.value(), document_id))
        self._open_dialog(dialog)

    def _add_tag(self, tag: str, document_id: str) -> None:
        self.jobs.submit("tag", "Tag", lambda ctx: work.add_tag(ctx, tag, document_id), lane=J.WRITE, on_done=self._tagged)

    def _tagged(self, job: J.Job) -> None:
        if job.state == J.FAILED:
            self.error("Tag", job.error or "Could not tag it.")
        elif job.state == J.SUCCEEDED:
            self.notify("Tagged." if job.result["added"] else "It already had that tag.", "info")
        self.refresh()

    # -- proposing a rename ----------------------------------------------------------------------------------------------

    def request_rename(self) -> None:
        document_id = self.detail.document_id
        if document_id is not None:
            self.jobs.submit("rename_plan", "Propose a rename", lambda ctx: work.rename_plan(ctx, document_id), on_done=self._plan_ready)

    def _plan_ready(self, job: J.Job) -> None:
        if job.state == J.FAILED:
            self.error("Propose a rename", job.error or "Could not make a proposal.")
        elif job.state == J.SUCCEEDED:
            plan = job.result
            self.rename_dialog = RenameDialog(plan, self)
            self.rename_dialog.saveRequested.connect(lambda: self.jobs.submit("save_plan", "Save the plan", lambda ctx: work.save_plan(ctx, plan), lane=J.WRITE,
                                                                               on_done=self._plan_saved))
            self._open_dialog(self.rename_dialog)

    def _plan_saved(self, job: J.Job) -> None:
        if job.state == J.FAILED:
            self.error("Save the plan", job.error or "Could not save the plan.")
        elif job.state == J.SUCCEEDED:
            self.rename_dialog.saved(job.result)
            self.notify(f"Plan saved to {job.result}. Nothing has been renamed.", "info")
        self.refresh()

    # -- following the catalog -------------------------------------------------------------------------------------------

    def _poll(self) -> None:
        if self.revision is None or self.jobs.busy or self.jobs.find_active("revision") or self._closing:
            return
        self.jobs.submit("revision", "Check for changes", work.current_revision, channel="revision", on_done=self._on_revision)

    def _on_revision(self, job: J.Job) -> None:
        if job.state != J.SUCCEEDED or self.revision is None:
            return
        if job.result != self.revision:
            self.notify("The library changed outside this window; updated.", "info")
            self.refresh()

    # -- the jobs panel --------------------------------------------------------------------------------------------------

    def _on_job_changed(self, job: J.Job) -> None:
        if job.kind in QUIET_KINDS and (job.state != J.FAILED or job.error_code):
            return  # quick background reads would make the panel flicker; they are listed only if they FAIL unexpectedly
        table = self.jobs_table
        row = self._job_rows.get(job.job_id)
        if row is None:
            row = table.rowCount()
            table.insertRow(row)
            self._job_rows[job.job_id] = row
            for column in range(3):
                table.setItem(row, column, QTableWidgetItem())
            button = QPushButton("Stop")
            button.setObjectName(f"cancel_{job.job_id}")
            button.clicked.connect(lambda _c=False, job_id=job.job_id: self.jobs.cancel(job_id))
            table.setCellWidget(row, 3, button)
        table.item(row, 0).setText(job.title)
        table.item(row, 1).setText(job.state if not job.cancel_requested or job.state != J.RUNNING else "stopping")
        table.item(row, 2).setText(job.error if job.state == J.FAILED and job.error else job.progress)
        button = table.cellWidget(row, 3)
        if button is not None:
            button.setEnabled(job.unfinished)
        table.resizeColumnToContents(0)

    # -- state and closing -----------------------------------------------------------------------------------------------

    def collect_state(self) -> statemod.GuiState:
        header = self.doc_table.horizontalHeader()
        return statemod.GuiState(
            library_id=self.library_id, geometry=bytes(self.saveGeometry().toBase64().data()).decode("ascii"),
            layout=bytes(self.saveState().toBase64().data()).decode("ascii"), scope=self.scope, document_id=self.current_document,
            filter_text=self.filter_box.text(), search_text=self.search_state["text"] or self.search_box.text(), tab=self.current_tab,
            sort_column=header.sortIndicatorSection(), sort_descending=header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder, last_folder=self.state.last_folder)

    def save_state(self) -> bool:
        return statemod.save(self.collect_state(), self._state_path) if self._remember else False

    def shutdown(self) -> bool:
        """Save the layout, stop the timer and ask every job to stop, then wait for the workers. Returns whether they finished."""
        if self._closing:
            return True
        self._closing = True
        self._watch.stop()
        self.save_state()
        return self.jobs.shutdown(8000)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        if not self.shutdown():
            log.warning("a job was still running when the window closed")
        event.accept()
