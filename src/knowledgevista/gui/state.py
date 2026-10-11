"""What the window remembers between runs: its size and layout, and what it was showing, for each library separately.

A convenience and nothing more. The library's state is the catalog; this file only saves a person from re-arranging the window and
re-finding their place. So it is tolerant in every direction: a missing, damaged, newer or hand-edited file gives the defaults (a
damaged one says so in the log), a value of the wrong type is replaced by its default, and deleting the file loses nothing but the
window's position.

Each library has its own entry (keyed by the library id the catalog carries, so it follows the catalog even if the file is moved):
size, layout, scope, selected document, filters, search and tab. A library opened for the first time starts from the shape the window
last had, so a new library appears where you were, not at the factory size. `last_folder` (where the Add folder chooser starts) is
shared. Entries are capped (the oldest-used fall off), and a format-1 file (one library's state, no entries) is still read.

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

STATE_FORMAT = 2
OLDEST_READABLE_FORMAT = 1
#: How many libraries' entries are kept; the one used longest ago falls off.
MAX_LIBRARIES = 100
TABS = ("documents", "search", "review", "health")
#: Everything, sorted so what needs a person comes first. "Inbox" is a VIEW (only those documents); the default is to see the whole library.
DEFAULT_SCOPE = "all"
#: What a library with no entry of its own takes from the window's last shape.
SHARED = ("geometry", "layout", "last_folder")


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


_TYPES: dict[str, Any] = {"library_id": (str, type(None)), "geometry": (str, type(None)), "layout": (str, type(None)), "scope": (str,),
                          "document_id": (str, type(None)), "filter_text": (str,), "search_text": (str,), "tab": (str,),
                          "sort_column": (int,), "sort_descending": (bool,), "last_folder": (str, type(None))}


@dataclass
class StateFile:
    """The whole file: the window's last shape, and one entry per library."""

    defaults: GuiState
    libraries: dict[str, GuiState]


def entry_from_dict(data: Any) -> GuiState:
    """Keep every value that is the right type, default the rest; ignore keys this version does not know."""
    state = GuiState()
    if not isinstance(data, dict):
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


def from_dict(data: Any) -> StateFile:
    """Read either format. Anything that is not ours, or newer than this program, gives the defaults."""
    empty = StateFile(GuiState(), {})
    if not isinstance(data, dict) or not isinstance(data.get("format"), int) or isinstance(data.get("format"), bool):
        return empty
    version = data["format"]
    if version == 1:  # one library's state, flat: its shape becomes the default and it becomes that library's entry
        old = entry_from_dict(data)
        result = StateFile(GuiState(geometry=old.geometry, layout=old.layout, last_folder=old.last_folder), {})
        if old.library_id:
            result.libraries[old.library_id] = old
        return result
    if version != STATE_FORMAT:
        return empty
    shared = entry_from_dict(data.get("defaults"))
    result = StateFile(GuiState(geometry=shared.geometry, layout=shared.layout, last_folder=shared.last_folder), {})
    entries = data.get("libraries")
    for library_id, entry in (entries.items() if isinstance(entries, dict) else []):
        if isinstance(library_id, str) and library_id:
            state = entry_from_dict(entry)
            state.library_id = library_id  # the key is the truth; an entry cannot claim to be another library
            result.libraries[library_id] = state
    return result


def read(path: Path | None = None) -> StateFile:
    target = path or paths.gui_state_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return StateFile(GuiState(), {})
    except OSError as exc:
        log.warning("could not read the window state %s (%s); starting from the defaults", target, exc)
        return StateFile(GuiState(), {})
    try:
        return from_dict(json.loads(text))
    except ValueError as exc:
        log.warning("the window state %s is damaged (%s); starting from the defaults", target, exc)
        return StateFile(GuiState(), {})


def load(path: Path | None = None, library_id: str | None = None) -> GuiState:
    """What to restore for `library_id`: its own entry, else the window's last shape with nothing selected."""
    stored = read(path)
    entry = stored.libraries.get(library_id) if library_id else None
    if entry is not None:
        entry.last_folder = stored.defaults.last_folder  # shared, so the chooser starts where the last one ended, whichever library
        return entry
    return GuiState(library_id=library_id, geometry=stored.defaults.geometry, layout=stored.defaults.layout, last_folder=stored.defaults.last_folder)


def save(state: GuiState, path: Path | None = None) -> bool:
    """Record `state` as its library's entry and as the window's last shape, keeping every other library's entry. Written so a crash
    never leaves half a file. Returns False (and logs) if it could not; the window carries on."""
    target = path or paths.gui_state_path()
    stored = read(target)
    stored.defaults = GuiState(geometry=state.geometry, layout=state.layout, last_folder=state.last_folder)
    if state.library_id:
        stored.libraries.pop(state.library_id, None)  # re-inserted last: insertion order is recency
        stored.libraries[state.library_id] = state
        while len(stored.libraries) > MAX_LIBRARIES:
            del stored.libraries[next(iter(stored.libraries))]
    payload = {"format": STATE_FORMAT, "defaults": {name: getattr(stored.defaults, name) for name in SHARED},
               "libraries": {library_id: {k: v for k, v in asdict(entry).items() if k != "library_id"} for library_id, entry in stored.libraries.items()}}
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
