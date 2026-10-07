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
