"""Where Knowledge Vista keeps its own files. The ONLY module that decides this; nothing else builds a path
under the user's home (docs/ARCHITECTURE.md, "Conventions").

Three storage classes, kept apart on purpose:

  data    the catalog (valuable user state: back it up).
  cache   rebuildable derivatives (extractions, thumbnails) plus ephemeral work dirs. Safe to delete.
  config  settings.

The user's source library is a fourth thing and is never under any of these.
`KNOWLEDGEVISTA_HOME` moves all three under one folder, which is what the tests and a portable install use.
"""

from __future__ import annotations

import os
from pathlib import Path

HOME_ENV = "KNOWLEDGEVISTA_HOME"
_APP = "KnowledgeVista"


def _under_home(home: str) -> tuple[Path, Path, Path]:
    base = Path(home)
    return base / "data", base / "cache", base / "config"


def _defaults() -> tuple[Path, Path, Path]:
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return local / _APP / "data", local / _APP / "cache", roaming / _APP
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return data / _APP, cache / _APP, config / _APP


def _resolve() -> tuple[Path, Path, Path]:
    # Read the environment on every call, never at import: a value cached at import time cannot be moved
    # by a test or a portable launcher that sets it afterwards.
    home = os.environ.get(HOME_ENV)
    return _under_home(home) if home else _defaults()


def data_dir() -> Path:
    return _resolve()[0]


def cache_dir() -> Path:
    return _resolve()[1]


def config_dir() -> Path:
    return _resolve()[2]


def catalog_path() -> Path:
    return data_dir() / "catalog.sqlite"


def index_path(catalog: Path | str | None = None) -> Path:
    """The extraction store (cache). Beside an explicit catalog as `<name>.cache/extractions.sqlite`, else in the
    cache directory. Deriving it from the catalog keeps a test catalog, a portable catalog and its cache together."""
    if catalog is None:
        return cache_dir() / "extractions.sqlite"
    target = Path(catalog)
    if target == catalog_path():
        return cache_dir() / "extractions.sqlite"
    return target.with_name(target.stem + ".cache") / "extractions.sqlite"


def metacache_path(catalog: Path | str | None = None) -> Path:
    """The metadata cache (cache): provider responses (positive AND negative) and the front matter read from each file.
    Lives beside the extraction store and follows the catalog the same way."""
    return index_path(catalog).with_name("metadata.sqlite")


def settings_path() -> Path:
    """App-wide settings (config). Per-library overrides are a later milestone."""
    return config_dir() / "settings.json"
