"""Pure-function tests: ids, path keys, kind detection. No filesystem, no database."""

from __future__ import annotations

import os

import pytest

from knowledgevista.domain import ids, kinds, pathkeys

# --- ids --------------------------------------------------------------------------------------------------------


def test_new_ids_are_32_hex_and_unique():
    made = {ids.new_id() for _ in range(200)}
    assert len(made) == 200 and all(ids.is_uuid_hex(i) for i in made)


@pytest.mark.parametrize("text", ["", "xyz", "g" * 64, "a" * 63, "a" * 65])
def test_normalise_sha256_rejects_non_hashes(text):
    assert ids.normalise_sha256(text) is None


def test_normalise_sha256_lowercases_and_strips():
    assert ids.normalise_sha256("  " + "AB" * 32 + "\n") == "ab" * 32


def test_utc_now_is_iso_utc_with_z():
    stamp = ids.utc_now()
    assert stamp.endswith("Z") and "T" in stamp and len(stamp) == 27


# --- path keys --------------------------------------------------------------------------------------------------


def test_case_insensitive_key_lowercases_and_sensitive_key_does_not():
    assert pathkeys.path_key("Dir/Foo.PDF", case_sensitive=False) == "dir/foo.pdf"
    assert pathkeys.path_key("Dir/Foo.PDF", case_sensitive=True) == "Dir/Foo.PDF"


def test_key_does_not_fold_sharp_s_or_normalise_unicode():
    # NTFS keeps these as DIFFERENT files, so the key must too (casefold would merge the first pair).
    assert pathkeys.path_key("stra\u00dfe.pdf", case_sensitive=False) != pathkeys.path_key("strasse.pdf", case_sensitive=False)
    assert pathkeys.path_key("\u00e9.pdf", case_sensitive=False) != pathkeys.path_key("e\u0301.pdf", case_sensitive=False)


def test_key_is_idempotent():
    for text in ["A/B/C.PDF", "\u00c9t\u00e9.txt", "plain"]:
        once = pathkeys.path_key(text, case_sensitive=False)
        assert pathkeys.path_key(once, case_sensitive=False) == once


def test_strip_extended_prefix():
    assert pathkeys.strip_extended_prefix("\\\\?\\C:\\a\\b") == "C:\\a\\b"
    assert pathkeys.strip_extended_prefix("\\\\?\\UNC\\srv\\share\\x") == "\\\\srv\\share\\x"
    assert pathkeys.strip_extended_prefix("C:\\plain") == "C:\\plain"


def test_is_within_respects_path_boundaries():
    base = os.path.abspath("lib")
    assert pathkeys.is_within(base, base)
    assert pathkeys.is_within(base, os.path.join(base, "sub", "x"))
    assert not pathkeys.is_within(base, base + "rary"), "'lib' must not contain 'library'"
    assert pathkeys.overlaps(base, os.path.join(base, "sub")) and pathkeys.overlaps(os.path.join(base, "sub"), base)
    assert not pathkeys.overlaps(base, base + "2")


def test_normalise_root_strips_trailing_separator_and_extended_prefix():
    display, key = pathkeys.normalise_root(os.path.abspath("somewhere") + os.sep)
    assert not display.endswith(os.sep) and key
    if os.name == "nt":
        assert pathkeys.normalise_root("\\\\?\\C:\\Temp")[0] == "C:\\Temp"


@pytest.mark.skipif(os.name != "nt", reason="extended-length prefix is Windows only")
def test_fs_path_prefixes_only_long_paths():
    assert pathkeys.fs_path("C:\\short\\path") == "C:\\short\\path"
    long_path = "C:\\" + "\\".join(["a" * 40] * 7)
    assert pathkeys.fs_path(long_path).startswith("\\\\?\\")
    assert pathkeys.fs_path(pathkeys.fs_path(long_path)) == pathkeys.fs_path(long_path)
    assert pathkeys.fs_path("\\\\srv\\share\\" + "\\".join(["a" * 40] * 7)).startswith("\\\\?\\UNC\\")


def test_join_relative_uses_the_os_separator():
    assert pathkeys.join_relative("root", "a/b/c.txt") == os.path.join("root", "a", "b", "c.txt")


# --- kinds ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "kind"),
    [("a.PDF", "pdf"), ("x/y/mol.sdf", "chemistry_data"), ("noext", "other"), (".hidden", "other"),
     ("archive.tar.gz", "archive"), ("dir.v2/file.txt", "text"), ("w\\n.docx", "word_document")],
)
def test_extension_kind(name, kind):
    assert kinds.extension_kind(name) == kind


@pytest.mark.parametrize(
    ("head", "size", "kind"),
    [(b"%PDF-1.7\n...", 100, "pdf"), (b"junk" * 50 + b"%PDF-1.4", 400, "pdf"), (b"PK\x03\x04rest", 50, "zip_container"),
     (b"\x89PNG\r\n\x1a\n", 20, "image"), (b"", 0, "empty"), (b"plain ascii text\nsecond line\n", 30, "text"),
     (b"\x00\x01\x02\x03binary", 20, "binary"),
     (b"\x00\x01\x00\x00Standard Jet DB\x00\x01\x00\x00", 5000, "database"),  # a real .mdb from the corpus
     (b"\x00\x01\x00\x00Standard ACE DB\x00", 5000, "database"),("caf\u00e9 ".encode(), 6, "text"), (b"SQLite format 3\x00" + b"\x00" * 10, 100, "database")],
)
def test_content_kind(head, size, kind):
    assert kinds.content_kind(head, size) == kind


def test_pdf_header_is_not_found_beyond_the_window():
    assert kinds.content_kind(b"x" * (kinds.PDF_HEADER_WINDOW + 10) + b"%PDF-", 2000) != "pdf"


def test_a_utf8_character_cut_by_the_window_is_still_text():
    assert kinds.content_kind(("a" * 10 + "\u00e9").encode()[:-1], 11) == "text"


@pytest.mark.parametrize(
    ("extension", "content", "disagree"),
    [("pdf", "pdf", False), ("pdf", "text", True), ("pdf", "zip_container", True), ("docx", "zip_container", False),
     ("epub", "zip_container", False), ("txt", "zip_container", True), ("mol", "text", False), ("xyz", "binary", True),
     ("weird", "binary", False), ("pdf", "empty", False)],
)
def test_kinds_disagree(extension, content, disagree):
    assert kinds.kinds_disagree(extension, content) is disagree
