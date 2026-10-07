"""What a file looks like: from its extension (a claim) and from its first bytes (an observation).

They are kept apart because they can disagree, and the disagreement is information: a `.pdf` that is not a PDF, a
`.txt` that is a ZIP. `extension_kind` is derived from a path whenever needed; `content_kind` is a property of the
bytes and is stored on the artifact. Neither is a user's interpretation (that is `assigned_kind`, a later milestone),
and "scan" is NOT a kind: it is a text-layer state of a PDF.
"""

from __future__ import annotations

import posixpath

#: Extension -> kind. Data-driven so a new format is a table row, not another branch.
EXTENSION_KINDS: dict[str, str] = {
    "pdf": "pdf",
    "epub": "epub",
    "docx": "word_document", "doc": "word_document", "rtf": "word_document", "odt": "word_document",
    "txt": "text", "md": "text", "csv": "text", "tsv": "text", "json": "text", "xml": "text", "html": "text",
    "htm": "text", "log": "text", "ini": "text", "toml": "text", "yaml": "text", "yml": "text",
    "xls": "spreadsheet", "xlsx": "spreadsheet", "ods": "spreadsheet",
    "xyz": "chemistry_data", "mol": "chemistry_data", "sdf": "chemistry_data", "cif": "chemistry_data",
    "pdb": "chemistry_data", "mol2": "chemistry_data", "smi": "chemistry_data",
    "zip": "archive", "rar": "archive", "7z": "archive", "tar": "archive", "gz": "archive",
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image", "bmp": "image", "tif": "image", "tiff": "image",
    "py": "code", "ipynb": "code", "js": "code", "mdb": "database", "accdb": "database", "sqlite": "database",
}

_MAGIC: list[tuple[bytes, str]] = [
    (b"%PDF-", "pdf"),
    (b"PK\x03\x04", "zip_container"),  # also the shape of docx, xlsx, epub: a container, not yet which
    (b"\x89PNG\r\n\x1a\n", "image"),
    (b"\xff\xd8\xff", "image"),
    (b"GIF8", "image"),
    (b"Rar!", "archive"),
    (b"7z\xbc\xaf\x27\x1c", "archive"),
    (b"SQLite format 3\x00", "database"),
    (b"{\\rtf", "word_document"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole_container"),  # legacy Office: doc, xls, mdb
]

#: A PDF may carry junk before its header (the spec allows up to 1024 bytes), so look a little way in.
PDF_HEADER_WINDOW = 1024


def extension_of(relative_path: str) -> str:
    """Lower-case extension without the dot; '' if none. Uses the last path component only."""
    name = posixpath.basename(relative_path.replace("\\", "/"))
    if "." not in name.lstrip("."):
        return ""
    return name.rsplit(".", 1)[1].lower()


def extension_kind(relative_path: str) -> str:
    return EXTENSION_KINDS.get(extension_of(relative_path), "other")


def _looks_like_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start < len(head) - 4:  # a multi-byte character cut by the window is fine; a bad byte earlier is not
            return False
    controls = sum(1 for byte in head if byte < 32 and byte not in (9, 10, 13))
    return controls <= max(1, len(head) // 100)


def content_kind(head: bytes, size: int) -> str:
    """The kind the first bytes show: `pdf`, `zip_container`, `image`, `text`, `empty`, `binary`, ..."""
    if size == 0:
        return "empty"
    if b"%PDF-" in head[:PDF_HEADER_WINDOW]:
        return "pdf"
    if head[4:19] in (b"Standard Jet DB", b"Standard ACE DB"):  # Microsoft Access: the signature is at offset 4
        return "database"
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    return "text" if _looks_like_text(head) else "binary"


def kinds_disagree(extension: str, content: str) -> bool:
    """Whether the extension's claim and the bytes' observation contradict each other, ignoring pairs that are
    normal (a `.docx` IS a zip container; a `.mol` is text)."""
    claimed = EXTENSION_KINDS.get(extension, "other")
    if claimed == "other" or content in ("empty",):
        return False
    compatible = {
        "pdf": {"pdf"},
        "epub": {"zip_container"},
        "word_document": {"zip_container", "ole_container", "word_document", "text"},
        "spreadsheet": {"zip_container", "ole_container", "text"},
        "text": {"text"},
        "chemistry_data": {"text"},
        "archive": {"archive", "zip_container"},
        "image": {"image"},
        "code": {"text"},
        "database": {"database", "ole_container"},
    }.get(claimed)
    return compatible is not None and content not in compatible
