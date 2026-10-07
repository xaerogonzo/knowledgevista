"""The extraction profile: exactly which process produced a piece of extracted text.

"Reindex because the extractor changed" and "the PDF changed" are different causes, so they are different things. The
PDF is identified by its hash (the artifact); the PROCESS is identified by this profile. An extraction whose profile
is not the current one is stale and is redone; one whose artifact changed is simply a different artifact.

The profile deliberately reads PyMuPDF's version from package METADATA, never by importing it: the main process must
be able to search and show pages without the optional `extract` group installed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib import metadata

from knowledgevista.errors import ErrorCode, KvError

EXTRACTOR = "pymupdf-text"
#: The shape of what we store (page rows, states, label handling). Independent of PyMuPDF's own version: bump it
#: when this module's representation changes even though the upstream parser did not.
FORMAT_VERSION = 1
#: Everything that changes the produced text. Hashed into the profile so a change here is visible as staleness.
OPTIONS = {"text": "page.get_text() default", "native_min_chars": 50, "label_source": "page.get_label()"}

IMPORTED_SOURCE = "imported_openchem_index"
IMPORTED_PROFILE = "imported:openchem_index_v1"
NATIVE_SOURCE = "native"


@dataclass(frozen=True)
class Profile:
    extractor: str
    extractor_version: str
    format_version: int
    options_hash: str
    profile_id: str

    def as_dict(self) -> dict:
        return dict(vars(self))


def installed_pymupdf_version() -> str | None:
    """PyMuPDF's version from package metadata, or None if the `extract` group is not installed."""
    try:
        return metadata.version("pymupdf")
    except metadata.PackageNotFoundError:
        return None


def make_profile(extractor_version: str) -> Profile:
    options_hash = hashlib.sha256(json.dumps(OPTIONS, sort_keys=True).encode()).hexdigest()[:12]
    identity = json.dumps([EXTRACTOR, extractor_version, FORMAT_VERSION, options_hash])
    return Profile(EXTRACTOR, extractor_version, FORMAT_VERSION, options_hash, hashlib.sha256(identity.encode()).hexdigest()[:16])


def current_profile() -> Profile:
    version = installed_pymupdf_version()
    if version is None:
        raise KvError(
            ErrorCode.DEPENDENCY_MISSING,
            "Extraction needs PyMuPDF, which is in the optional 'extract' group. Install it: uv sync --extra extract "
            "(or: pip install 'knowledgevista[extract]').",
            {"group": "extract", "package": "pymupdf"},
        )
    return make_profile(version)
