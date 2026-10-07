"""Stable error codes and exit codes: part of the machine contract (docs/CLI_CONTRACT.md).

Callers (OpenChem, Claude Code, scripts) branch on `code`, never on the message text, so renaming a code is an API
break and the human message can improve freely. Add codes here and in docs/CLI_CONTRACT.md together; a test fails
if they drift apart.
"""

from __future__ import annotations

from typing import Any

EXIT_OK = 0
EXIT_FAILURE = 1  # an expected operational failure: the command ran and the answer is "no"
EXIT_INVALID = 2  # the arguments or query were not valid


class ErrorCode:
    INVALID_ARGUMENTS = "KV_INVALID_ARGUMENTS"
    NOT_FOUND = "KV_NOT_FOUND"
    AMBIGUOUS = "KV_AMBIGUOUS"
    FILE_CHANGED = "KV_FILE_CHANGED"
    FILE_MISSING = "KV_FILE_MISSING"
    DESTINATION_EXISTS = "KV_DESTINATION_EXISTS"
    PERMISSION_DENIED = "KV_PERMISSION_DENIED"
    METADATA_UNRESOLVED = "KV_METADATA_UNRESOLVED"
    METADATA_LOCKED = "KV_METADATA_LOCKED"
    NETWORK_DISABLED = "KV_NETWORK_DISABLED"
    QUERY_INVALID = "KV_QUERY_INVALID"
    CURSOR_STALE = "KV_CURSOR_STALE"
    ROOT_UNAVAILABLE = "KV_ROOT_UNAVAILABLE"
    ROOT_OVERLAP = "KV_ROOT_OVERLAP"
    CATALOG_TOO_NEW = "KV_CATALOG_TOO_NEW"
    CATALOG_OUTDATED = "KV_CATALOG_OUTDATED"
    CATALOG_MISSING = "KV_CATALOG_MISSING"
    DOCTOR_FOUND_PROBLEMS = "KV_DOCTOR_FOUND_PROBLEMS"
    DEPENDENCY_MISSING = "KV_DEPENDENCY_MISSING"
    NOT_EXTRACTED = "KV_NOT_EXTRACTED"
    EXTRACTION_FAILED = "KV_EXTRACTION_FAILED"
    NOTHING_SEARCHABLE = "KV_NOTHING_SEARCHABLE"
    INTERNAL = "KV_INTERNAL"


def all_codes() -> frozenset[str]:
    return frozenset(value for name, value in vars(ErrorCode).items() if not name.startswith("_"))


class KvError(Exception):
    """A failure with a stable code and machine-readable details."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}

    @property
    def exit_code(self) -> int:
        return EXIT_INVALID if self.code in (ErrorCode.INVALID_ARGUMENTS, ErrorCode.QUERY_INVALID) else EXIT_FAILURE
