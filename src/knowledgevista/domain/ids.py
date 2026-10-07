"""Identifiers. Opaque on purpose: an ID never encodes a title, author or path, because metadata changes and an
ID that changed with it would stop being an identity (docs/ARCHITECTURE.md, "Conventions")."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import NewType

DocumentId = NewType("DocumentId", str)  # a stable UUID (hex): one logical library item
ArtifactId = NewType("ArtifactId", str)  # the lowercase SHA-256 of the bytes
LocationId = NewType("LocationId", str)  # a UUID (hex): one observed path
RootId = NewType("RootId", str)  # a UUID (hex): one configured library root

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def new_id() -> str:
    return uuid.uuid4().hex


def is_uuid_hex(text: str) -> bool:
    return bool(_HEX32.match(text or ""))


def normalise_sha256(text: str) -> str | None:
    """The canonical lowercase SHA-256, or None if `text` is not one. The only place a hash is validated."""
    candidate = (text or "").strip().lower()
    return candidate if _HEX64.match(candidate) else None


def utc_now() -> str:
    """UTC, ISO 8601 with a trailing Z. Stored times are always UTC; display converts."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
