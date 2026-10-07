"""The detail pane (one document: what it is, where it is, each value with its origin badge) and the "why this value?" window.

Everything shown here is built from `library_view.document_detail` / `field_evidence`. A value never appears without its origin: the
badge letter says whether it was read from the file (O), returned by a provider (R), deduced (I) or stated by a person (A), and the
"Why?" button beside it opens the whole answer. All text is plain text (gui/text.py).
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QHeaderView, QPlainTextEdit, QPushButton,
                               QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from knowledgevista.domain import evidence_view as ev
from knowledgevista.gui.models import format_size
from knowledgevista.gui.text import plain_label, set_plain, tooltip

AGREEMENT_SHORT = {"agreement": "agrees", "discrepancy": "DIFFERS", "unresolved": "waiting", "uncorroborated": "one source", "unknown": ""}
FIELD_COLUMNS = ("Field", "Value", "Src", "Check", "")
LEGEND = "Src: O read from the file · R returned by a provider · I deduced · A stated by a person"


class DetailPane(QWidget):
    whyRequested = Signal(str)  # a field name
    openRequested = Signal()
    revealRequested = Signal()
    renameRequested = Signal()
    collectionRequested = Signal()
    tagRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("detailPane")
        self.document_id: str | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)

        self.title = plain_label(name="detailTitle")
        font = self.title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.2)
        self.title.setFont(font)
        self.sub = plain_label(name="detailSub")
        self.banner = plain_label(name="detailBanner")
        self.banner.hide()
        self.fields = QTableWidget(0, len(FIELD_COLUMNS))
        self.fields.setObjectName("detailFields")
        self.fields.setHorizontalHeaderLabels(list(FIELD_COLUMNS))
        self.fields.verticalHeader().hide()
        self.fields.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.fields.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.fields.setWordWrap(True)
        self.fields.setTextElideMode(Qt.TextElideMode.ElideNone)
        header = self.fields.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.legend = plain_label(LEGEND, name="detailLegend", selectable=False)
        self.legend.setEnabled(False)
        self.where = plain_label(name="detailFiles")
        self.organization = plain_label(name="detailOrganization")
        self.relations = plain_label(name="detailRelations")
        self.empty = plain_label("Select a document to see what is known about it.", name="detailEmpty")

        buttons = QVBoxLayout()
        first, second = QHBoxLayout(), QHBoxLayout()
        self.open_button = QPushButton("Open")
        self.open_button.setObjectName("openButton")
        self.reveal_button = QPushButton("Show in folder")
        self.reveal_button.setObjectName("revealButton")
        self.rename_button = QPushButton("Propose rename…")
        self.rename_button.setObjectName("renameButton")
        self.collection_button = QPushButton("Add to collection…")
        self.collection_button.setObjectName("collectionButton")
        self.tag_button = QPushButton("Tag…")
        self.tag_button.setObjectName("tagButton")
        for row, group in ((first, ((self.open_button, self.openRequested), (self.reveal_button, self.revealRequested), (self.rename_button, self.renameRequested))),
                           (second, ((self.collection_button, self.collectionRequested), (self.tag_button, self.tagRequested)))):
            for button, signal in group:
                button.clicked.connect(signal)
                row.addWidget(button)
            row.addStretch(1)
            buttons.addLayout(row)
        self._buttons = [self.open_button, self.reveal_button, self.rename_button, self.collection_button, self.tag_button]

        for widget in (self.empty, self.title, self.sub, self.banner, self.fields, self.legend, self.where, self.organization, self.relations):
            layout.addWidget(widget)
        layout.addLayout(buttons)
        layout.addStretch(1)
        self._content = [self.title, self.sub, self.fields, self.legend, self.where, self.organization, self.relations, *self._buttons]
        self.clear()

    # -- showing --------------------------------------------------------------------------------------------------------

    def clear(self, message: str = "Select a document to see what is known about it.") -> None:
        self.document_id = None
        set_plain(self.empty, message)
        self.empty.show()
        self.banner.hide()
        for widget in self._content:
            widget.hide()

    def show_detail(self, detail: dict[str, Any]) -> None:
        self.document_id = detail["document_id"]
        self.empty.hide()
        for widget in self._content:
            widget.show()
        set_plain(self.title, detail["title"] or detail["name"])
        facts = [detail["kind"] or "unknown type", format_size(detail["size"]) or "unknown size",
                 "a reachable copy exists" if detail["available"] else "NO reachable copy right now"]
        set_plain(self.sub, " · ".join(facts))
        notes = [w["message"] for w in detail["warnings"]]
        if detail["retired"]:
            notes.insert(0, f"This document was merged into {detail['merged_into']}; use that one." if detail["merged_into"] else "This document was retired.")
        if detail["waiting"]:
            notes.append(f"{detail['waiting']} proposed value(s) are waiting for a person (Review tab).")
        set_plain(self.banner, "\n".join(notes))
        self.banner.setVisible(bool(notes))
        self._fill_fields(detail["fields"])
        set_plain(self.where, "Where it is\n" + ("\n".join(f"  {f['root']}/{f['path']}  ({f['state']}{'' if f['root_status'] == 'online' else ', folder ' + f['root_status']})"
                                                           for f in detail["files"]) or "  nowhere right now"))
        set_plain(self.organization, "Collections: " + (", ".join(detail["collections"]) or "none") + "\nTags: " + (", ".join(detail["tags"]) or "none"))
        lines = [f"  {r['kind']} ({r['direction']}) {r['other_document'][:12]}" for r in detail["relations"]]
        lines += [f"  proposal: {p['kind']} ({p['confidence']})" for p in detail["proposals"]]
        set_plain(self.relations, "Relations\n" + ("\n".join(lines) if lines else "  none"))
        for button in self._buttons[:2]:
            button.setEnabled(detail["available"])
        self.rename_button.setEnabled(not detail["retired"])
        self.collection_button.setEnabled(not detail["retired"])
        self.tag_button.setEnabled(not detail["retired"])

    def _fill_fields(self, fields: list[dict[str, Any]]) -> None:
        table = self.fields
        table.setRowCount(len(fields))
        for row, f in enumerate(fields):
            if f["set"]:
                value = f["display"]
                source = f["badge"] + (" locked" if f["locked"] else "")
            else:
                value = "—" + (f"   ({f['waiting']} proposed)" if f["waiting"] else "")
                source = ""
            cells = [f["label"], value, source, AGREEMENT_SHORT.get(f["agreement"], "")]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                if column == 2 and f["set"]:
                    item.setToolTip(tooltip(f"{f['origin_label'].capitalize()}.\nFrom {f['source_label']}.\nAccepted by {f['accepted_by']}."))
                if column == 3 and f["agreement"] != "unknown":
                    item.setToolTip(tooltip(ev.AGREEMENT_TEXT[f["agreement"]]))
                table.setItem(row, column, item)
            button = QPushButton("Why?")
            button.setObjectName(f"why_{f['field']}")
            button.setEnabled(bool(f["set"] or f["waiting"]))
            button.setToolTip("Where this value came from, and what else was proposed" if button.isEnabled() else "Nothing is known for this field yet")
            button.clicked.connect(lambda _checked=False, name=f["field"]: self.whyRequested.emit(name))
            table.setCellWidget(row, 4, button)
        self._fit_rows()
        QTimer.singleShot(0, self, self._fit_rows)  # once more after the layout has given the columns their width

    def _fit_rows(self) -> None:
        """Row heights follow the column widths, which are only known once the pane has a width: so this runs again on every resize."""
        table = self.fields
        table.resizeRowsToContents()
        table.setFixedHeight(table.horizontalHeader().height() + sum(table.rowHeight(r) for r in range(table.rowCount())) + 4)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        super().resizeEvent(event)
        if self.fields.rowCount():
            QTimer.singleShot(0, self, self._fit_rows)

    # -- reading back (for the driver and tests) --------------------------------------------------------------------------

    def field_rows(self) -> list[dict[str, str]]:
        return [{"field": self.fields.item(r, 0).text(), "value": self.fields.item(r, 1).text(), "source": self.fields.item(r, 2).text(),
                 "check": self.fields.item(r, 3).text()} for r in range(self.fields.rowCount())]


class EvidenceDialog(QDialog):
    """"Why this value?": the answer as text. Opened with `open()` (never `exec()`), so a scripted run is not stuck behind it."""

    def __init__(self, why: dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("evidenceDialog")
        self.setWindowTitle(f"Why this value? — {why['label']}")
        self.resize(640, 520)
        layout = QVBoxLayout(self)
        self.text_edit = QPlainTextEdit(self)
        self.text_edit.setObjectName("evidenceText")
        self.text_edit.setReadOnly(True)
        self.text_edit.setPlainText(ev.render_why(why))
        layout.addWidget(self.text_edit)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self.why = why

    def text(self) -> str:
        return self.text_edit.toPlainText()
