"""The CLI contract for extract / search / show / import, and that doctor and stats report extraction health."""

from __future__ import annotations

import json
import sqlite3

import pdfbuilders as b
import pytest
from support import write
from test_cli import run, run_json
from test_index import legacy_index

from knowledgevista import errors


@pytest.fixture
def lib(tmp_path):
    base = tmp_path / "papers"
    base.mkdir()
    b.native_pdf(base / "native.pdf")
    b.labelled_pdf(base / "book.pdf")
    b.scanned_pdf(base / "scan.pdf")
    b.malformed_pdf(base / "broken.pdf")
    return base


@pytest.fixture
def ready(capsys, tmp_path, lib):
    run_json(capsys, tmp_path, "root", "add", str(lib))
    run_json(capsys, tmp_path, "scan")
    return tmp_path


def test_extract_reports_each_outcome_and_the_memory_limit_in_force(capsys, ready):
    code, env, err = run_json(capsys, ready, "extract")
    summary = env["records"][0]
    assert code == 0 and summary["type"] == "summary" and (summary["extracted"], summary["complete"], summary["failed"]) == (4, 3, 1)
    assert summary["limiter"].startswith(("windows-job-object", "RLIMIT_AS"))
    assert "extracting 1/4" in err, "progress goes to stderr"
    assert any(w["code"] == "KV_NON_PDF_NOT_EXTRACTED" for w in env["warnings"]) is False, "no non-PDF files in this library"


def test_search_envelope_has_a_summary_with_coverage_then_hits(capsys, ready):
    run_json(capsys, ready, "extract")
    code, env, _ = run_json(capsys, ready, "search", "aqueous solubility")
    assert code == 0 and env["ok"] and env["complete"] is True
    summary, *hits = env["records"]
    assert summary["type"] == "summary" and summary["hits"] == 2 and summary["query"]["language_version"] == 2
    assert summary["coverage"]["searchable"] == 3 and summary["coverage"]["no_text_layer"] == 1 and summary["coverage"]["extraction_failed"] == 1
    assert [h["type"] for h in hits] == ["hit", "hit"] and hits[0]["anchor"]["pdf_page"] == 2
    assert any(w["code"] == "KV_SEARCH_COVERAGE" for w in env["warnings"]), "a hit list over a partly unsearchable library says so"


def test_a_search_with_no_hits_still_reports_what_it_could_not_see(capsys, ready):
    run_json(capsys, ready, "extract")
    code, env, _ = run_json(capsys, ready, "search", "zzzznothing")
    assert code == 0 and env["records"][0]["hits"] == 0
    assert "not proof of absence" in next(w for w in env["warnings"] if w["code"] == "KV_SEARCH_COVERAGE")["message"]


def test_searching_before_anything_is_extracted_is_an_error_not_an_empty_success(capsys, ready):
    code, env, _ = run_json(capsys, ready, "search", "aqueous")
    assert code == 1 and not env["ok"] and env["errors"][0]["code"] == "KV_NOTHING_SEARCHABLE"
    assert env["errors"][0]["details"]["coverage"]["not_yet_extracted"] == 4


def test_truncation_sets_complete_false(capsys, ready):
    run_json(capsys, ready, "extract")
    code, env, _ = run_json(capsys, ready, "search", "marker", "--limit", "2")
    assert code == 0 and env["complete"] is False and env["records"][0]["truncated"] is True
    assert len([r for r in env["records"] if r["type"] == "hit"]) == 2


def test_an_empty_query_is_exit_2_with_query_invalid(capsys, ready):
    code, env, _ = run_json(capsys, ready, "search", " | ")
    assert code == 2 and env["errors"][0]["code"] == "KV_QUERY_INVALID"


def test_search_filters_pass_through(capsys, ready):
    run_json(capsys, ready, "extract")
    _, env, _ = run_json(capsys, ready, "search", "page | marker", "--file", "book")
    assert {h["paths"][0]["path"] for h in env["records"] if h["type"] == "hit"} == {"book.pdf"}
    _, env, _ = run_json(capsys, ready, "search", "aqueous", "--near", "298", "--also", "compound")
    assert env["records"][0]["query"]["near"] == ["298"] and env["records"][0]["hits"] == 2


def test_show_by_label_and_by_position_and_the_errors(capsys, ready):
    run_json(capsys, ready, "extract")
    code, env, _ = run_json(capsys, ready, "show", "book.pdf", "--label", "iii")
    assert code == 0 and env["records"][0]["pdf_page"] == 3 and env["records"][0]["printed_label"] == "iii"
    code, env, _ = run_json(capsys, ready, "show", "book.pdf", "--pdf-page", "5")
    assert env["records"][0]["printed_label"] == "1"
    code, env, _ = run_json(capsys, ready, "show", "book.pdf")  # neither: argparse requires one
    assert code == 2 and env["errors"][0]["code"] == "KV_INVALID_ARGUMENTS"
    code, env, _ = run_json(capsys, ready, "show", "book.pdf", "--pdf-page", "1", "--label", "i")
    assert code == 2, "the two addressing modes are mutually exclusive"
    code, env, _ = run_json(capsys, ready, "show", "book.pdf", "--pdf-page", "99")
    assert code == 1 and env["errors"][0]["code"] == "KV_NOT_FOUND"


def test_show_before_extract_is_not_extracted(capsys, ready):
    code, env, _ = run_json(capsys, ready, "show", "native.pdf", "--pdf-page", "1")
    assert code == 1 and env["errors"][0]["code"] == "KV_NOT_EXTRACTED"


def test_extract_without_pymupdf_says_how_to_install_it(capsys, ready, monkeypatch):
    monkeypatch.setattr("knowledgevista.extract.profile.installed_pymupdf_version", lambda: None)
    code, env, _ = run_json(capsys, ready, "extract")
    assert code == 1 and env["errors"][0]["code"] == "KV_DEPENDENCY_MISSING" and "extract" in env["errors"][0]["message"]


def test_stats_and_doctor_cover_extraction_and_search(capsys, ready):
    _, env, _ = run_json(capsys, ready, "stats")
    assert env["records"][0]["search"]["not_yet_extracted"] == 4
    _, env, _ = run_json(capsys, ready, "doctor")
    assert env["records"][0]["categories_checked"] == ["filesystem", "catalog", "extraction", "search", "metadata", "relationships", "operations"]
    assert any(r.get("code") == "KVD_NOT_EXTRACTED" for r in env["records"])
    run_json(capsys, ready, "extract")
    _, env, _ = run_json(capsys, ready, "stats")
    assert env["records"][0]["search"]["searchable"] == 3
    code, env, _ = run_json(capsys, ready, "doctor")
    assert code == 0 and any(r.get("code") == "KVD_FAILED_EXTRACTION" for r in env["records"])


def test_doctor_reports_a_damaged_index_as_an_error_with_the_remedy(capsys, ready):
    run_json(capsys, ready, "extract")
    index = ready / "c.cache" / "extractions.sqlite"
    connection = sqlite3.connect(index)
    connection.execute("DELETE FROM page_fts WHERE rowid = (SELECT MIN(rowid) FROM page_fts)")
    connection.commit()
    connection.close()
    code, env, _ = run_json(capsys, ready, "doctor")
    finding = next(r for r in env["records"] if r.get("code") == "KVD_INDEX_INCONSISTENT")
    assert code == 1 and "kv extract --force" in finding["message"]


def test_import_openchem_index_end_to_end(capsys, ready, tmp_path):
    from knowledgevista.db.catalog import open_catalog
    conn = open_catalog(ready / "c.sqlite", create=False, read_only=True)
    sha = conn.execute("SELECT a.artifact_id, a.size FROM artifact a JOIN location l USING (artifact_id) WHERE l.relative_path = 'native.pdf'").fetchone()
    conn.close()
    legacy = legacy_index(tmp_path / "legacy.sqlite", [("whatever/name.pdf", sha[0], sha[1], ["legacy words about sparingly soluble RDX", "second"], "")])
    code, env, _ = run_json(capsys, ready, "import", "openchem-index", str(legacy))
    assert code == 0 and env["command"] == "import openchem-index" and env["records"][0]["imported"] == 1
    code, env, _ = run_json(capsys, ready, "search", "sparingly soluble")
    hit = next(r for r in env["records"] if r["type"] == "hit")
    assert hit["extraction"]["provisional"] is True and hit["paths"][0]["path"] == "native.pdf", "found at its CURRENT path, not the legacy one"
    assert env["records"][0]["coverage"]["provisional_imported"] == 1
    code, env, _ = run_json(capsys, ready, "extract")
    assert env["records"][0]["provisional_imported"] == 1, "extract leaves provisional text alone by default"
    code, env, _ = run_json(capsys, ready, "extract", "--rebuild-imported")
    assert code == 0 and env["records"][0]["extracted"] >= 1
    code, env, _ = run_json(capsys, ready, "search", "sparingly soluble")
    assert [r for r in env["records"] if r["type"] == "hit"] == [], "the native text replaced the imported text"


def test_import_of_a_missing_file_is_a_structured_error(capsys, ready, tmp_path):
    code, env, _ = run_json(capsys, ready, "import", "openchem-index", str(tmp_path / "none.sqlite"))
    assert code == 1 and env["errors"][0]["code"] == "KV_NOT_FOUND"


def test_human_output_for_search_is_readable(capsys, tmp_path, lib):
    cat = str(tmp_path / "c.sqlite")
    run(capsys, "--catalog", cat, "root", "add", str(lib))
    run(capsys, "--catalog", cat, "scan")
    run(capsys, "--catalog", cat, "extract")
    code, out, _ = run(capsys, "--catalog", cat, "search", "aqueous solubility")
    assert code == 0 and "native.pdf  p2" in out and "[[aqueous solubility]]" in out and "2 page(s)" in out
    code, out, _ = run(capsys, "--catalog", cat, "show", "book.pdf", "--label", "1")
    assert "pdf page 5 of 10 (printed label 1)" in out and "navigation aid" in out


def test_the_new_error_codes_are_documented():
    from pathlib import Path
    contract = (Path(__file__).resolve().parent.parent / "docs" / "CLI_CONTRACT.md").read_text(encoding="utf-8")
    for code in (errors.ErrorCode.DEPENDENCY_MISSING, errors.ErrorCode.NOT_EXTRACTED, errors.ErrorCode.NOTHING_SEARCHABLE, errors.ErrorCode.EXTRACTION_FAILED):
        assert f"`{code}`" in contract
