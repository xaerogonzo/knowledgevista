"""Pure text logic, the extraction store, the importer and the search query."""

from __future__ import annotations

import sqlite3
import sys

import pytest
from support import make_env, write

from knowledgevista.domain import text
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.extract.profile import IMPORTED_PROFILE, IMPORTED_SOURCE, NATIVE_SOURCE, current_profile, make_profile
from knowledgevista.index import search as qs
from knowledgevista.index import store
from knowledgevista.index.importer import import_openchem_index

D = "10."  # assembled so a DOI-shaped literal never appears whole in the source

# --- text logic (ported from the OpenChem indexer, with its cases) -------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Journal of X. https://doi.org/" + D + "1021/acs.jced.5c00573.", D + "1021/acs.jced.5c00573"),
        ("doi:" + D + "1002/prep.70064)", D + "1002/prep.70064"),
        ("no identifier here", ""),
        ("", ""),
        ("see " + D + "1038/s42004-025-01544-9, and also", D + "1038/s42004-025-01544-9"),
    ],
)
def test_find_doi_trims_the_sentence_around_it(body, expected):
    assert text.find_doi(body) == expected


def test_scan_detection_and_page_states():
    assert text.is_scanned(chars=40, pages=200) and not text.is_scanned(chars=900_000, pages=200)
    assert not text.is_scanned(chars=0, pages=0), "an unreadable file is an error, not a scan"
    assert text.page_state(500, 0) == "text_native"
    assert text.page_state(10, 0) == "text_sparse"
    assert text.page_state(0, 2) == "image_only", "no text but pictures: the signature of a scan"
    assert text.page_state(0, 0) == "blank", "no text and no pictures is nothing, not a scan"
    assert text.page_state(500, 0, error=True) == "failed"
    assert text.imported_page_state(0) == "unknown", "the legacy index cannot tell a blank page from a scanned one"


# --- the profile ---------------------------------------------------------------------------------------------------


def test_profile_changes_with_the_extractor_version_and_only_with_it():
    a, b, again = make_profile("1.0"), make_profile("2.0"), make_profile("1.0")
    assert a.profile_id == again.profile_id and a.profile_id != b.profile_id
    assert a.options_hash == b.options_hash, "an upstream upgrade is not an options change"


def test_profile_needs_pymupdf_but_only_when_asked(monkeypatch):
    monkeypatch.setattr("knowledgevista.extract.profile.installed_pymupdf_version", lambda: None)
    with pytest.raises(KvError) as caught:
        current_profile()
    assert caught.value.code == ErrorCode.DEPENDENCY_MISSING and "extract" in caught.value.message


# --- the store -----------------------------------------------------------------------------------------------------


def record(artifact: str = "a" * 64, pages: list[store.PageRecord] | None = None, **kw) -> store.ExtractionRecord:
    pages = pages if pages is not None else [store.PageRecord(1, "alpha beta", None, "text_native"), store.PageRecord(2, "gamma delta", "ii", "text_native")]
    base = dict(artifact_id=artifact, source=NATIVE_SOURCE, extractor="x", extractor_version="1", format_version=1, options_hash="o",
                profile_id="p1", status="complete", page_count=len(pages), chars=sum(len(p.text) for p in pages),
                legacy_scanned=False, first_pages_doi=None, first_text=None, pages=pages)
    base.update(kw)
    return store.ExtractionRecord(**base)


@pytest.fixture
def idx(tmp_path):
    connection = store.open_index(tmp_path / "x.sqlite", create=True)
    yield connection
    connection.close()


def test_store_round_trips_a_record_and_its_text(idx):
    store.replace_extraction(idx, record())
    extraction = store.get_extraction(idx, "a" * 64)
    assert extraction["page_count"] == 2 and extraction["source"] == NATIVE_SOURCE
    page = idx.execute("SELECT * FROM page WHERE pdf_page = 2").fetchone()
    assert page["printed_label"] == "ii" and store.page_text(idx, page["page_id"]) == "gamma delta"
    assert store.consistency_problems(idx) == []


def test_replacing_an_extraction_replaces_everything_and_issues_new_page_ids(idx):
    store.replace_extraction(idx, record())
    before = [r["page_id"] for r in idx.execute("SELECT page_id FROM page ORDER BY pdf_page")]
    store.replace_extraction(idx, record(pages=[store.PageRecord(1, "epsilon", None, "text_native")]))
    after = [r["page_id"] for r in idx.execute("SELECT page_id FROM page")]
    assert len(after) == 1 and not set(after) & set(before), "page ids are never reused: they are internal keys of one extraction"
    assert idx.execute("SELECT COUNT(*) FROM page_fts").fetchone()[0] == 1
    assert store.consistency_problems(idx) == []


def test_a_failed_replacement_leaves_the_old_extraction_intact(idx):
    store.replace_extraction(idx, record())
    broken = record(pages=[store.PageRecord(1, "new text", None, "text_native"), store.PageRecord(1, "duplicate page number", None, "text_native")])
    with pytest.raises(sqlite3.IntegrityError):
        store.replace_extraction(idx, broken)
    assert not idx.in_transaction
    assert [r["text"] for r in idx.execute("SELECT text FROM page_fts ORDER BY rowid")] == ["alpha beta", "gamma delta"], "atomic: all old or all new"
    assert store.consistency_problems(idx) == []


def test_pages_without_text_have_no_full_text_row_but_a_state(idx):
    store.replace_extraction(idx, record(pages=[store.PageRecord(1, "", None, "image_only"), store.PageRecord(2, "", None, "failed", "boom")]))
    assert idx.execute("SELECT COUNT(*) FROM page_fts").fetchone()[0] == 0
    assert [r["text_state"] for r in idx.execute("SELECT text_state FROM page ORDER BY pdf_page")] == ["image_only", "failed"]
    assert store.consistency_problems(idx) == []


def test_empty_label_is_refused_by_the_schema(idx):
    with pytest.raises(sqlite3.IntegrityError):
        store.replace_extraction(idx, record(pages=[store.PageRecord(1, "x", "", "text_native")]))


def test_consistency_check_detects_each_kind_of_damage(idx):
    store.replace_extraction(idx, record())
    assert store.consistency_problems(idx) == [], "the oracle must be alive: clean first"
    idx.execute("DELETE FROM page_fts WHERE rowid = (SELECT MIN(page_id) FROM page)")
    assert any("full-text rows" in p for p in store.consistency_problems(idx))
    idx.execute("INSERT INTO page_fts (rowid, text) VALUES (99999, 'orphan')")
    assert any("belong to no page" in p for p in store.consistency_problems(idx))
    idx.execute("UPDATE extraction SET page_count = 7")
    assert any("page count" in p for p in store.consistency_problems(idx))


def test_a_store_from_another_schema_version_is_rebuilt_not_migrated(tmp_path):
    path = tmp_path / "x.sqlite"
    first = store.open_index(path, create=True)
    store.replace_extraction(first, record())
    first.execute("PRAGMA user_version = 999")
    first.close()
    assert store.open_index(path, create=False) is None, "a read without create sees 'nothing extracted', not a crash"
    assert store.open_index(path, create=True, read_only=True) is None
    rebuilt = store.open_index(path, create=True)
    assert store.get_extraction(rebuilt, "a" * 64) is None, "derived data is discarded, not migrated"
    rebuilt.close()


def test_a_file_that_is_not_a_database_is_replaced_when_writing_and_ignored_when_reading(tmp_path):
    path = tmp_path / "x.sqlite"
    path.write_bytes(b"this is not sqlite at all, just text pretending")
    assert store.open_index(path, create=False) is None
    index = store.open_index(path, create=True)
    assert index is not None and store.consistency_problems(index) == []
    index.close()


def test_missing_store_opens_as_none_unless_created(tmp_path):
    assert store.open_index(tmp_path / "nope.sqlite", create=False) is None
    assert store.open_index(tmp_path / "sub" / "made.sqlite", create=True) is not None


# --- the search query ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("typed", ["2,4-DNT", "NEAR(a b)", 'a" OR "b', "x AND y", "-negated", "col:umn", "(paren"])
def test_nothing_typed_becomes_a_search_operator(typed):
    expression = qs.compile_fts(qs.parse_query(typed))
    assert expression.startswith('"') and expression.endswith('"')
    assert expression.count('"') - 2 * typed.count('"') == 2


def test_a_trailing_star_is_a_prefix_and_nothing_else_is_an_operator():
    assert qs.compile_fts(qs.parse_query("PETN", near=["solub*"])) == 'NEAR("PETN" "solub"*, 30)'
    assert qs.compile_fts(qs.parse_query("a*b")) == '"a*b"'


def test_alternatives_proximity_and_also_are_the_only_structure():
    assert qs.compile_fts(qs.parse_query("PGDN | propylene glycol dinitrate")) == '"PGDN" OR "propylene glycol dinitrate"'
    assert qs.compile_fts(qs.parse_query("PETN", near=["solubility"], within=40)) == 'NEAR("PETN" "solubility", 40)'
    assert qs.compile_fts(qs.parse_query("RDX", also=["water"])) == '("RDX") AND "water"'


@pytest.mark.parametrize("bad", ["", "   ", " | ", "*", " * | "])
def test_an_empty_or_meaningless_query_is_an_error_never_zero_hits(bad):
    with pytest.raises(KvError) as caught:
        qs.parse_query(bad)
    assert caught.value.code == ErrorCode.QUERY_INVALID and caught.value.exit_code == 2


def test_within_must_be_positive_and_the_language_version_is_recorded():
    with pytest.raises(KvError):
        qs.parse_query("x", within=0)
    assert qs.parse_query("x").language_version == qs.QUERY_LANGUAGE_VERSION == 2


def test_an_engine_level_parse_failure_is_a_query_error_not_a_crash(idx, monkeypatch):
    monkeypatch.setattr(qs, "compile_fts", lambda query: "(((")
    with pytest.raises(KvError) as caught:
        qs.run_search(idx, qs.parse_query("x"), limit=5)
    assert caught.value.code == ErrorCode.QUERY_INVALID


def _fill(idx):
    store.replace_extraction(idx, record("a" * 64, pages=[
        store.PageRecord(1, "propylene glycol dinitrate has a solubility of 1.6 g/L", None, "text_native"),
        store.PageRecord(2, "unrelated words", "9", "text_native")]))
    store.replace_extraction(idx, record("b" * 64, pages=[store.PageRecord(1, "RDX is sparingly soluble in water", None, "text_native")]))


def test_a_prefix_finds_the_longer_word_and_a_bare_stem_does_not(idx):
    _fill(idx)
    stem = qs.run_search(idx, qs.parse_query("propylene glycol dinitrate", near=["solub"]), limit=10)
    prefix = qs.run_search(idx, qs.parse_query("propylene glycol dinitrate", near=["solub*"]), limit=10)
    assert stem == [] and [(h.artifact_id[0], h.pdf_page) for h in prefix] == [("a", 1)]


def test_results_are_ordered_deterministically_and_truncation_is_detectable(idx):
    for letter in "dcba":  # inserted in reverse so an insertion-order result would differ from artifact order
        store.replace_extraction(idx, record(letter * 64, pages=[store.PageRecord(1, "identical words here", None, "text_native")]))
    hits = qs.run_search(idx, qs.parse_query("identical words"), limit=3)
    assert len(hits) == 4, "limit + 1 rows come back: the extra one is how the caller knows it was truncated"
    assert [h.artifact_id[0] for h in hits] == ["a", "b", "c", "d"], "equal relevance: ties break by artifact"
    assert hits == qs.run_search(idx, qs.parse_query("identical words"), limit=3)


def test_artifact_scope_limits_results(idx):
    _fill(idx)
    only_b = qs.run_search(idx, qs.parse_query("RDX | dinitrate"), limit=10, artifact_scope={"b" * 64})
    assert {h.artifact_id[0] for h in only_b} == {"b"}
    assert qs.run_search(idx, qs.parse_query("RDX"), limit=10, artifact_scope=set()) == []


def test_snippets_mark_the_match(idx):
    _fill(idx)
    assert "[[sparingly]]" in qs.run_search(idx, qs.parse_query("sparingly"), limit=1)[0].snippet


# --- the importer --------------------------------------------------------------------------------------------------


LEGACY_SCHEMA = """
CREATE TABLE files (path TEXT PRIMARY KEY, sha256 TEXT, size INTEGER, mtime_ns INTEGER, pages INTEGER, chars INTEGER,
                    scanned INTEGER, doi TEXT, first_text TEXT, indexed_at REAL, error TEXT);
CREATE VIRTUAL TABLE pages USING fts5(path UNINDEXED, page UNINDEXED, text, tokenize = 'unicode61 remove_diacritics 2');
"""


def legacy_index(path, rows):
    """rows: (path, sha256, size, [page texts], error). Mirrors tools/library_index.py's schema."""
    connection = sqlite3.connect(path)
    connection.executescript(LEGACY_SCHEMA)
    for rel, sha, size, pages, error in rows:
        chars = sum(len(t) for t in pages)
        connection.execute("INSERT INTO files VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, 0, ?)",
                           (rel, sha, size, len(pages), chars, int(len(pages) > 0 and chars / len(pages) < 100), "10.5555/x" if pages else "", (pages[0] if pages else "")[:600], error))
        for number, body in enumerate(pages, start=1):
            connection.execute("INSERT INTO pages (path, page, text) VALUES (?, ?, ?)", (rel, number, body))
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def scanned(tmp_path):
    env = make_env(tmp_path, {"a.pdf": b"%PDF-1.4 aaaa", "b.pdf": b"%PDF-1.4 bbbbbb", "copy_of_a.pdf": b"%PDF-1.4 aaaa"})
    env.scan()
    env.index = store.open_index(tmp_path / "idx.sqlite", create=True)
    return env


def sha_of(env, rel):
    return env.location(rel)["artifact_id"]


def test_import_brings_in_matching_documents_as_provisional_text(scanned, tmp_path):
    env = scanned
    legacy = legacy_index(tmp_path / "legacy.sqlite", [
        ("a.pdf", sha_of(env, "a.pdf"), 13, ["first page words", "second page words"], ""),
        ("b.pdf", sha_of(env, "b.pdf"), 15, ["only page"], "")])
    report = import_openchem_index(env.conn, env.index, legacy)
    assert (report.legacy_files, report.imported, report.pages, report.unmatched) == (2, 2, 3, 0)
    extraction = store.get_extraction(env.index, sha_of(env, "a.pdf"))
    assert (extraction["source"], extraction["profile_id"], extraction["status"]) == (IMPORTED_SOURCE, IMPORTED_PROFILE, "complete")
    assert extraction["first_pages_doi"] == "10.5555/x"
    assert env.index.execute("SELECT COUNT(*) FROM page WHERE printed_label IS NOT NULL").fetchone()[0] == 0, "the legacy index has no labels"
    assert qs.run_search(env.index, qs.parse_query("second page"), limit=5)[0].source == IMPORTED_SOURCE
    assert store.consistency_problems(env.index) == []


def test_a_row_whose_hash_is_not_in_the_catalog_is_unmatched_and_imports_nothing(scanned, tmp_path):
    legacy = legacy_index(tmp_path / "legacy.sqlite", [("ghost.pdf", "f" * 64, 99, ["ghost text"], ""), ("nohash.pdf", None, 9, ["x"], "")])
    report = import_openchem_index(scanned.conn, scanned.index, legacy)
    assert (report.imported, report.unmatched) == (0, 2) and report.unmatched_examples == ["ghost.pdf", "nohash.pdf"]
    assert scanned.index.execute("SELECT COUNT(*) FROM extraction").fetchone()[0] == 0


def test_the_legacy_path_is_never_trusted_as_a_location(scanned, tmp_path):
    # The legacy index says this hash lives at an invented path. Only the catalog decides where a file is.
    legacy = legacy_index(tmp_path / "legacy.sqlite", [("invented/elsewhere/name.pdf", sha_of(scanned, "b.pdf"), 15, ["text"], "")])
    import_openchem_index(scanned.conn, scanned.index, legacy)
    assert scanned.one("SELECT COUNT(*) FROM location WHERE relative_path LIKE 'invented%'") == 0


def test_size_mismatch_error_rows_and_page_count_disagreement_are_skipped(scanned, tmp_path):
    legacy = legacy_index(tmp_path / "legacy.sqlite", [
        ("a.pdf", sha_of(scanned, "a.pdf"), 999, ["x"], ""),
        ("b.pdf", sha_of(scanned, "b.pdf"), 15, ["x"], "RuntimeError: damaged")])
    report = import_openchem_index(scanned.conn, scanned.index, legacy)
    assert (report.imported, report.skipped_size_mismatch, report.skipped_legacy_error) == (0, 1, 1)
    liar = sqlite3.connect(legacy)
    liar.execute("UPDATE files SET size = 13, error = '' WHERE path = 'a.pdf'")
    liar.execute("UPDATE files SET pages = 5 WHERE path = 'a.pdf'")
    liar.commit()
    liar.close()
    report = import_openchem_index(scanned.conn, scanned.index, legacy)
    assert report.imported == 0 and report.skipped_legacy_error >= 1, "a row that disagrees with its own pages is not trusted"


def test_two_legacy_paths_with_the_same_hash_import_once(scanned, tmp_path):
    sha = sha_of(scanned, "a.pdf")
    legacy = legacy_index(tmp_path / "legacy.sqlite", [("a.pdf", sha, 13, ["one"], ""), ("copy_of_a.pdf", sha, 13, ["one"], "")])
    report = import_openchem_index(scanned.conn, scanned.index, legacy)
    assert (report.imported, report.skipped_duplicate_hash) == (1, 1)


def test_import_is_idempotent_and_never_overwrites_native_text(scanned, tmp_path):
    sha = sha_of(scanned, "a.pdf")
    store.replace_extraction(scanned.index, record(sha, pages=[store.PageRecord(1, "native words", None, "text_native")]))
    legacy = legacy_index(tmp_path / "legacy.sqlite", [("a.pdf", sha, 13, ["legacy words"], ""), ("b.pdf", sha_of(scanned, "b.pdf"), 15, ["b words"], "")])
    first = import_openchem_index(scanned.conn, scanned.index, legacy)
    again = import_openchem_index(scanned.conn, scanned.index, legacy)
    assert (first.imported, first.skipped_native_exists) == (1, 1)
    assert (again.imported, again.skipped_already_imported, again.skipped_native_exists) == (0, 1, 1)
    assert qs.run_search(scanned.index, qs.parse_query("native"), limit=5) and not qs.run_search(scanned.index, qs.parse_query("legacy"), limit=5)


def test_a_missing_or_foreign_file_is_a_clear_error(scanned, tmp_path):
    with pytest.raises(KvError) as caught:
        import_openchem_index(scanned.conn, scanned.index, tmp_path / "nope.sqlite")
    assert caught.value.code == ErrorCode.NOT_FOUND
    other = tmp_path / "other.sqlite"
    sqlite3.connect(other).executescript("CREATE TABLE t (x)")
    with pytest.raises(KvError) as caught:
        import_openchem_index(scanned.conn, scanned.index, other)
    assert caught.value.code == ErrorCode.INVALID_ARGUMENTS
    garbage = tmp_path / "garbage.sqlite"
    garbage.write_bytes(b"not a database " * 50)
    with pytest.raises(KvError) as caught:
        import_openchem_index(scanned.conn, scanned.index, garbage)
    assert caught.value.code == ErrorCode.INVALID_ARGUMENTS


def test_import_does_not_modify_the_legacy_file(scanned, tmp_path):
    legacy = legacy_index(tmp_path / "legacy.sqlite", [("a.pdf", sha_of(scanned, "a.pdf"), 13, ["x"], "")])
    before = legacy.read_bytes()
    import_openchem_index(scanned.conn, scanned.index, legacy)
    assert legacy.read_bytes() == before


def test_import_reads_interleaved_legacy_rows_correctly_too(scanned, tmp_path):
    """A re-index in the legacy tool can leave one file's rows split by another's. The fast path (a rowid range) must not
    be trusted then, or pages from the neighbouring file would be imported under the wrong document."""
    a, b = sha_of(scanned, "a.pdf"), sha_of(scanned, "b.pdf")
    legacy = legacy_index(tmp_path / "legacy.sqlite", [("a.pdf", a, 13, ["a one", "a two"], ""), ("b.pdf", b, 15, ["b one"], "")])
    connection = sqlite3.connect(legacy)
    connection.execute("DELETE FROM pages WHERE path = 'a.pdf' AND page = 2")
    connection.execute("INSERT INTO pages (path, page, text) VALUES ('a.pdf', 2, 'a two')")  # now after b's row: a's rows are split
    connection.commit()
    connection.close()
    report = import_openchem_index(scanned.conn, scanned.index, legacy)
    assert report.imported == 2
    texts = lambda sha: [r[0] for r in scanned.index.execute(  # noqa: E731
        "SELECT f.text FROM page p JOIN page_fts f ON f.rowid = p.page_id JOIN extraction e USING (extraction_id) WHERE e.artifact_id = ? ORDER BY p.pdf_page", (sha,))]
    assert texts(a) == ["a one", "a two"] and texts(b) == ["b one"]
