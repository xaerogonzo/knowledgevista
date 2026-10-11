"""The libraries the window knows about: which catalogs it has opened, under what name, and which one it opened last.

A library is a catalog file; this module only remembers where they are. It never opens, creates, moves or deletes one, so deleting
`gui-libraries.json` loses the list and nothing else. Like `gui/state.py` it is tolerant in every direction (a missing, damaged or newer
file gives an empty list; an entry of the wrong shape is dropped, the rest kept) and it writes atomically. No Qt here.

Names are labels, not file names: every catalog is called `catalog.sqlite`, so the name a person chose (or the folder it lives in) is
what tells two of them apart. A catalog whose file has gone stays in the list and is reported as missing; it is never removed behind a
person's back.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from knowledgevista import paths

log = logging.getLogger("knowledgevista.gui.libraries")

LIBRARIES_FORMAT = 1
#: How many libraries the list keeps. The oldest falls off; its catalog file is untouched.
MAX_KNOWN = 10
DEFAULT_NAME = "Default library"


def key_of(catalog: Path | str) -> str:
    """What makes two spellings of one path the same library (case and separators on Windows). Used to compare, never to store."""
    return os.path.normcase(os.path.abspath(catalog))


@dataclass(frozen=True)
class Known:
    path: str
    name: str

    @property
    def exists(self) -> bool:
        return Path(self.path).is_file()


@dataclass
class Registry:
    last: str | None = None
    libraries: list[Known] = field(default_factory=list)
    #: Whether launching offers a choice of library (only ever when two or more exist). A person can switch it off in that dialog.
    ask_at_startup: bool = True

    def find(self, catalog: Path | str) -> Known | None:
        wanted = key_of(catalog)
        return next((k for k in self.libraries if key_of(k.path) == wanted), None)


def is_default(catalog: Path | str) -> bool:
    """Whether this is the app's own catalog. It is always available, even before the first run has created it."""
    return key_of(catalog) == key_of(paths.catalog_path())


def label_for(catalog: Path | str) -> str:
    """The name to show for a catalog nobody has named: 'Default library' for the app's own, else the folder it sits in."""
    if is_default(catalog):
        return DEFAULT_NAME
    target = Path(os.path.abspath(catalog))
    return target.parent.name or target.stem


def from_dict(data: Any) -> Registry:
    """Keep every entry that is well-formed, drop the rest; a file from another format is ignored whole."""
    registry = Registry()
    if not isinstance(data, dict) or data.get("format") != LIBRARIES_FORMAT:
        return registry
    seen: set[str] = set()
    for entry in data.get("libraries") if isinstance(data.get("libraries"), list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"].strip():
            continue
        if key_of(entry["path"]) in seen:
            continue
        seen.add(key_of(entry["path"]))
        name = entry.get("name") if isinstance(entry.get("name"), str) and entry["name"].strip() else label_for(entry["path"])
        registry.libraries.append(Known(entry["path"], name.strip()))
    last = data.get("last")
    if isinstance(last, str) and last.strip():
        registry.last = last
    ask = data.get("ask_at_startup")
    registry.ask_at_startup = ask if isinstance(ask, bool) else True
    del registry.libraries[MAX_KNOWN:]
    return registry


def load(path: Path | None = None) -> Registry:
    target = path or paths.gui_libraries_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Registry()
    except OSError as exc:
        log.warning("could not read the library list %s (%s); starting with none", target, exc)
        return Registry()
    try:
        return from_dict(json.loads(text))
    except ValueError as exc:
        log.warning("the library list %s is damaged (%s); starting with none", target, exc)
        return Registry()


def save(registry: Registry, path: Path | None = None) -> bool:
    """Write the list so a crash never leaves half a file. Returns False (and logs) if it could not; the window carries on."""
    target = path or paths.gui_libraries_path()
    payload = {"format": LIBRARIES_FORMAT, "last": registry.last, "ask_at_startup": registry.ask_at_startup,
               "libraries": [{"path": k.path, "name": k.name} for k in registry.libraries]}
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        log.warning("could not save the library list to %s (%s)", target, exc)
        try:
            temporary.unlink()
        except OSError:
            pass
        return False
    return True


def remembered(registry: Registry, catalog: Path | str, name: str | None = None) -> Registry:
    """`registry` with `catalog` first and marked as the one opened last. An existing name is kept unless a new one is given."""
    existing = registry.find(catalog)
    chosen = (name or "").strip() or (existing.name if existing else label_for(catalog))
    stored = os.path.abspath(catalog)
    others = [k for k in registry.libraries if key_of(k.path) != key_of(catalog)]
    return replace(registry, last=stored, libraries=[Known(stored, chosen), *others][:MAX_KNOWN])


def forgotten(registry: Registry, catalog: Path | str) -> Registry:
    """`registry` without `catalog` (the list only; the file is not touched)."""
    last = registry.last if registry.last and key_of(registry.last) != key_of(catalog) else None
    return replace(registry, last=last, libraries=[k for k in registry.libraries if key_of(k.path) != key_of(catalog)])


def renamed(registry: Registry, catalog: Path | str, name: str) -> Registry:
    """`registry` with `catalog` called `name` (listed, at the end, if it was not: the app's own library is offered before it is ever listed).
    A blank name changes nothing."""
    chosen = " ".join(name.split())
    if not chosen:
        return registry
    if registry.find(catalog) is None:
        return replace(registry, libraries=[*registry.libraries, Known(os.path.abspath(catalog), chosen)][:MAX_KNOWN])
    return replace(registry, libraries=[Known(k.path, chosen) if key_of(k.path) == key_of(catalog) else k for k in registry.libraries])


def moved(registry: Registry, old: Path | str, new: Path | str) -> Registry:
    """`registry` after a library's catalog was copied from `old` to `new`: the entry now points at `new`, keeping its name and its place
    in the list (and as the last-opened one). The old file is not forgotten about on disk, only dropped from the list."""
    existing = registry.find(old)
    name = existing.name if existing else label_for(new)
    stored = os.path.abspath(new)
    entries: list[Known] = []
    for known in registry.libraries:
        if key_of(known.path) == key_of(old):
            entries.append(Known(stored, name))
        elif key_of(known.path) != key_of(new):  # a second entry for the destination would be a duplicate
            entries.append(known)
    if existing is None:
        entries.insert(0, Known(stored, name))
    last = stored if registry.last and key_of(registry.last) == key_of(old) else registry.last
    return replace(registry, last=last, libraries=entries[:MAX_KNOWN])


def with_ask_at_startup(registry: Registry, ask: bool) -> Registry:
    return replace(registry, ask_at_startup=ask)


def choosable(registry: Registry) -> list[Known]:
    """The libraries a person can be offered at launch: those whose file exists (the app's own counts once it has been created)."""
    return [k for k in known_for_menu(registry) if k.exists]


def should_ask(registry: Registry) -> bool:
    """Whether launching shows the 'which library?' dialog: only when it is switched on AND there is a real choice (two or more)."""
    return registry.ask_at_startup and len(choosable(registry)) >= 2


def last_existing(registry: Registry) -> Path | None:
    """The library opened last, if its file is still there. A vanished one is not silently replaced by a new empty catalog."""
    if registry.last and Path(registry.last).is_file():
        return Path(registry.last)
    return None


def note_opened(catalog: Path | str, name: str | None = None, path: Path | None = None) -> Registry:
    """Load, record `catalog` as the library now open, save, and return the result."""
    updated = remembered(load(path), catalog, name)
    save(updated, path)
    return updated


def known_for_menu(registry: Registry) -> list[Known]:
    """What the Recent libraries menu lists: the remembered ones, with the app's own default always available."""
    default = paths.catalog_path()
    if registry.find(default) is not None:
        return list(registry.libraries)
    return [*registry.libraries, Known(str(default), DEFAULT_NAME)]
