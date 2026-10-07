"""What the window remembers between runs: its size and layout, and what it was showing.

A convenience and nothing more. The library's state is the catalog; this file only saves a person from re-arranging the window and
re-finding their place. So it is tolerant in every direction: a missing, damaged, newer or hand-edited file gives the defaults (a
damaged one says so in the log), a value of the wrong type is replaced by its default, and deleting the file loses nothing but the
window's position. It also records WHICH library it was written for, so a selection from one catalog is never applied to another.

No Qt in this module: geometry and layout are the opaque base64 text Qt hands back, stored and returned unread.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from knowledgevista import paths

log = logging.getLogger("knowledgevista.gui.state")

STATE_FORMAT = 1
TABS = ("documents", "search", "review", "health")
#: Everything, sorted so what needs a person comes first. "Inbox" is a VIEW (only those documents); the default is to see the whole library.
DEFAULT_SCOPE = "all"


@dataclass
class GuiState:
    library_id: str | None = None
    geometry: str | None = None
    layout: str | None = None
    scope: str = DEFAULT_SCOPE
    document_id: str | None = None
    filter_text: str = ""
    search_text: str = ""
    tab: str = "documents"
    sort_column: int = -1  # -1: the default order (inbox first)
    sort_descending: bool = False
    last_folder: str | None = None

    def for_library(self, library_id: str | None) -> GuiState:
        """The part of this state that still means something for `library_id`: the window layout always does; what was selected,
        searched and filtered only does for the library it was saved from."""
        if library_id is not None and library_id == self.library_id:
            return self
        return GuiState(library_id=library_id, geometry=self.geometry, layout=self.layout, last_folder=self.last_folder)


_TYPES: dict[str, Any] = {"library_id": (str, type(None)), "geometry": (str, type(None)), "layout": (str, type(None)), "scope": (str,),
                          "document_id": (str, type(None)), "filter_text": (str,), "search_text": (str,), "tab": (str,),
                          "sort_column": (int,), "sort_descending": (bool,), "last_folder": (str, type(None))}


def from_dict(data: Any) -> GuiState:
    """Keep every value that is the right type, default the rest; ignore keys this version does not know."""
    state = GuiState()
    if not isinstance(data, dict) or data.get("format") != STATE_FORMAT:
        return state
    for f in fields(GuiState):
        value = data.get(f.name)
        allowed = _TYPES[f.name]
        if value is None and type(None) not in allowed:
            continue
        # bool is an int in Python: a stored `true` must not become a column number.
        if isinstance(value, allowed) and not (isinstance(value, bool) and bool not in allowed):
            setattr(state, f.name, value)
    if state.tab not in TABS:
        state.tab = "documents"
    return state


def load(path: Path | None = None) -> GuiState:
    target = path or paths.gui_state_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return GuiState()
    except OSError as exc:
        log.warning("could not read the window state %s (%s); starting from the defaults", target, exc)
        return GuiState()
    try:
        return from_dict(json.loads(text))
    except ValueError as exc:
        log.warning("the window state %s is damaged (%s); starting from the defaults", target, exc)
        return GuiState()


def save(state: GuiState, path: Path | None = None) -> bool:
    """Write the state so a crash never leaves half a file. Returns False (and logs) if it could not; the window carries on."""
    target = path or paths.gui_state_path()
    payload = {"format": STATE_FORMAT, **asdict(state)}
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        log.warning("could not save the window state to %s (%s)", target, exc)
        try:
            temporary.unlink()
        except OSError:
            pass
        return False
    return True
