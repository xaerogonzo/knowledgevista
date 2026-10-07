"""The table models: documents, search hits and the review queue.

A model holds plain data (the dataclass rows and dicts `services/library_view.py` returns) and says how each cell reads. It never
queries anything: a refresh is a new list handed to `reset()`. Every tooltip is built from outside text and goes through
`text.tooltip`; display text needs no help, because item views draw it as plain text.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QFont

from knowledgevista.domain import evidence_view as ev
from knowledgevista.gui.text import tooltip
from knowledgevista.services.library_view import Row

SORT_ROLE = Qt.ItemDataRole.UserRole + 1
ITEM_ROLE = Qt.ItemDataRole.UserRole + 2  # the row's data: a library_view.Row, a hit or a review item

_Index = QModelIndex | QPersistentModelIndex

#: How a document's inbox reason reads in the "Why" column.
REASON_LABELS = {"unresolved": "Unresolved", "ambiguous": "Ambiguous", "missing": "Missing", "new": "New"}


def format_size(size: int | None) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


class _Table(QAbstractTableModel):
    COLUMNS: tuple[tuple[str, str], ...] = ()
    #: Columns drawn right-aligned (numbers).
    NUMERIC: frozenset[str] = frozenset()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[Any] = []

    def reset(self, items: list[Any]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()

    def item_at(self, row: int) -> Any:
        return self._items[row]

    def rowCount(self, parent: _Index = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent: _Index = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole and 0 <= section < len(self.COLUMNS):
            return self.COLUMNS[section][1]
        return None

    def column_key(self, column: int) -> str:
        return self.COLUMNS[column][0]

    def text(self, item: Any, key: str) -> str:
        raise NotImplementedError

    def sort_value(self, item: Any, key: str):
        return self.text(item, key).casefold()

    def tip(self, item: Any, key: str) -> str | None:
        return None

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item, key = self._items[index.row()], self.column_key(index.column())
        if role == Qt.ItemDataRole.DisplayRole:
            return self.text(item, key)
        if role == SORT_ROLE:
            return self.sort_value(item, key)
        if role == ITEM_ROLE:
            return item
        if role == Qt.ItemDataRole.ToolTipRole:
            tip = self.tip(item, key)
            return tooltip(tip) if tip else None
        if role == Qt.ItemDataRole.TextAlignmentRole and key in self.NUMERIC:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return self.extra(item, key, role)

    def extra(self, item: Any, key: str, role: int):
        return None


class DocumentModel(_Table):
    COLUMNS = (("why", "Why"), ("title", "Title"), ("src", "Src"), ("authors", "Authors"), ("year", "Year"), ("doi", "DOI"),
               ("kind", "Type"), ("waiting", "Proposals"), ("file", "File"))
    NUMERIC = frozenset({"year", "waiting"})

    def text(self, row: Row, key: str) -> str:
        if key == "why":
            return REASON_LABELS.get(row.reason or "", (row.reason or "").capitalize())
        if key == "title":
            return row.display_title
        if key == "src":
            if row.title_origin is None:
                return ""
            return ev.ORIGIN_BADGES.get(row.title_origin, "?") + (" locked" if row.title_locked else "")
        if key == "authors":
            return row.authors or ""
        if key == "year":
            return row.year or ""
        if key == "doi":
            return row.doi or ""
        if key == "kind":
            return row.kind or ""
        if key == "waiting":
            return str(row.waiting) if row.waiting else ""
        return row.name

    def sort_value(self, row: Row, key: str):
        if key == "why":
            return row.rank  # the Why column sorts into the default order
        if key == "year":
            return int(row.year) if row.year and row.year.isdigit() else 0
        if key == "waiting":
            return row.waiting
        if key == "title":
            return row.display_title.casefold()
        return self.text(row, key).casefold()

    def tip(self, row: Row, key: str) -> str | None:
        if key == "why" and row.reason:
            return row.reason_detail or row.reason
        if key == "src" and row.title_origin:
            return f"The title was {ev.origin_label(row.title_origin)}." + (" It is locked: no resolver may change it." if row.title_locked else "")
        if key == "title":
            lines = [row.display_title, f"{row.root or '(no root)'}/{row.name}"]
            if not row.available:
                lines.append("No reachable copy right now (the file is missing, or its folder is offline).")
            return "\n".join(lines)
        if key == "waiting" and row.waiting:
            return f"{row.waiting} proposal(s) are waiting for a person (see the Review tab)."
        return None

    def extra(self, row: Row, key: str, role: int):
        if role == Qt.ItemDataRole.FontRole and key == "title" and row.title is None:
            font = QFont()
            font.setItalic(True)  # an untitled document is called by its path, and says so
            return font
        if role == Qt.ItemDataRole.ForegroundRole and not row.available:
            from PySide6.QtGui import QGuiApplication, QPalette

            return QGuiApplication.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        return None

    def index_of(self, document_id: str) -> int:
        return next((n for n, row in enumerate(self._items) if row.document_id == document_id), -1)


class HitModel(_Table):
    COLUMNS = (("document", "Document"), ("page", "Page"), ("snippet", "Text near the match"))
    NUMERIC = frozenset({"page"})

    def text(self, hit: dict, key: str) -> str:
        if key == "document":
            return hit["title"] or hit["name"]
        if key == "page":
            label = f" ({hit['printed_label']})" if hit["printed_label"] else ""
            return f"{hit['pdf_page']}{label}"
        return hit["snippet"]

    def sort_value(self, hit: dict, key: str):
        return hit["pdf_page"] if key == "page" else self.text(hit, key).casefold()

    def tip(self, hit: dict, key: str) -> str | None:
        if key == "snippet":
            return "A navigation aid, not a quotation: open the page to read it in the document."
        if key == "document":
            return f"{hit['name']}\npage {hit['pdf_page']}" + ("" if hit["available"] else "\nNo reachable copy right now.") + (
                "\nText imported from the OpenChem index (provisional)." if hit["provisional"] else "")
        return None

    def extra(self, hit: dict, key: str, role: int):
        if role == Qt.ItemDataRole.ForegroundRole and not hit["available"]:
            from PySide6.QtGui import QGuiApplication, QPalette

            return QGuiApplication.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        return None


class ReviewModel(_Table):
    COLUMNS = (("paper", "Paper"), ("field", "Field"), ("proposed", "Proposed"), ("accepted", "Accepted now"), ("from", "From"),
               ("confidence", "Confidence"), ("review", "Review"))

    def text(self, item: dict, key: str) -> str:
        if key == "paper":
            return item["title"] or item["name"]
        if key == "field":
            return item["label"]
        if key == "proposed":
            return item["display"]
        if key == "accepted":
            return item["differs_from_accepted"] or ""
        if key == "from":
            return item["sources_text"]
        if key == "confidence":
            return item["confidence"].capitalize()
        return "Safe for the batch rule" if item["review"] == "safe" else "Needs a person"

    def sort_value(self, item: dict, key: str):
        if key == "confidence":
            return ("exact", "high", "medium", "low", "ambiguous").index(item["confidence"])
        return self.text(item, key).casefold()

    def tip(self, item: dict, key: str) -> str | None:
        if key == "paper":
            return item["name"]
        if key == "review":
            return ("A rule may accept this unattended, because every source that proposed it earned that." if item["review"] == "safe"
                    else "Nothing accepts this unattended; a person decides.")
        if key == "proposed":
            return "\n".join(f"{name}: {value}" for name, value in item["evidence_lines"][:8]) or None
        return None


class DocumentProxy(QSortFilterProxyModel):
    """Sorts by the model's SORT_ROLE and filters by a text typed in the list's own filter box (title, authors, DOI, path)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._needle = ""

    def set_filter(self, text: str) -> None:
        self._needle = " ".join(text.casefold().split())
        self.setFilterFixedString(text)  # the way to ask for a re-filter that Qt 6.9+ does not deprecate; our own filterAcceptsRow does the matching

    @property
    def needle(self) -> str:
        return self._needle

    def filterAcceptsRow(self, source_row: int, source_parent: _Index) -> bool:
        if not self._needle:
            return True
        row: Row = self.sourceModel().item_at(source_row)
        haystack = " ".join(filter(None, (row.display_title, row.name, row.authors, row.doi, row.year, row.kind))).casefold()
        return all(word in haystack for word in self._needle.split())
