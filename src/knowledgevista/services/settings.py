"""App-wide settings: the few switches a person owns.

Online lookups are OFF by default, and every way this file can go wrong lands on "off": a missing file, an unreadable
one, a value of the wrong type. A corrupt settings file must never be the thing that turns the network on.

The contact address (`mailto`) is the user's own and is never hard-coded; it is sent only with provider requests, and
only if they set one (it selects Crossref's "polite" pool, which is a courtesy to Crossref, not a requirement).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any

from knowledgevista import paths
from knowledgevista.errors import ErrorCode, KvError

DEFAULTS: dict[str, Any] = {"online_lookup": False, "mailto": None, "request_budget": 1000}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TRUE, _FALSE = {"true", "yes", "on", "1"}, {"false", "no", "off", "0"}
HELP = {
    "online_lookup": "true/false: allow `kv resolve --online` to send DOIs and titles to metadata providers (default false)",
    "mailto": "your contact address, sent to providers with requests so they can reach you instead of blocking you (default: none)",
    "request_budget": "the most requests one `kv resolve --online` run may make, retries included (default 1000)",
}


@dataclass
class Settings:
    values: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS))
    problem: str | None = None  # why the file was ignored, if it was

    def __getitem__(self, key: str) -> Any:
        return self.values[key]


def coerce(key: str, text: str) -> Any:
    """The typed value for `text`, or a KvError naming what was expected."""
    if key not in DEFAULTS:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"Unknown setting {key!r}. Settings: {', '.join(DEFAULTS)}")
    raw = text.strip()
    if key == "online_lookup":
        if raw.lower() in _TRUE:
            return True
        if raw.lower() in _FALSE:
            return False
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"{key} must be true or false, not {text!r}")
    if key == "mailto":
        if raw.lower() in ("", "none", "null"):
            return None
        if not _EMAIL.match(raw):
            raise KvError(ErrorCode.INVALID_ARGUMENTS, f"{text!r} is not an email address")
        return raw
    try:
        number = int(raw)
    except ValueError:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"{key} must be a whole number, not {text!r}") from None
    if not 1 <= number <= 100_000:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"{key} must be between 1 and 100000")
    return number


def load() -> Settings:
    """The settings, with defaults for anything missing. A damaged file yields the defaults and says why."""
    path = paths.settings_path()
    if not path.exists():
        return Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("the file is not a JSON object")
    except (OSError, ValueError) as exc:
        return Settings(problem=f"{path} could not be read ({exc}); using the defaults, which are offline")
    result = Settings()
    for key, value in data.items():
        if key not in DEFAULTS:
            continue
        try:
            result.values[key] = coerce(key, json.dumps(value) if not isinstance(value, str) else value) if key != "online_lookup" else (value is True)
        except KvError:
            result.problem = f"ignored an invalid value for {key!r} in {path}"
    return result


def set_value(key: str, text: str) -> Any:
    """Validate and persist one setting (atomically: write beside, then replace)."""
    value = coerce(key, text)
    current = load().values
    current[key] = value
    path = paths.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(current, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return value
