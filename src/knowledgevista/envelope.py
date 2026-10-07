"""The JSON envelope every `--json` command prints, and nothing else on stdout.

    {"schema_version": 1, "command": "scan", "ok": true, "records": [...], "warnings": [...],
     "errors": [{"code", "message", "details"}], "next_cursor": null}

`records` is ALWAYS a list, and a list that could be partial says so through `warnings` and `complete`: a bare
array on the wire would lose the difference between "none exist" and "could not look" (docs/gotchas/
empty-is-not-unknown.md, section 3). JSON is emitted with ASCII escapes so it survives any console encoding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

JSON_SCHEMA_VERSION = 1


@dataclass
class Envelope:
    command: str
    ok: bool = True
    records: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = True
    next_cursor: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": JSON_SCHEMA_VERSION,
            "command": self.command,
            "ok": self.ok,
            "complete": self.complete,
            "records": self.records,
            "warnings": self.warnings,
            "errors": self.errors,
            "next_cursor": self.next_cursor,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=True, sort_keys=False, default=str)
