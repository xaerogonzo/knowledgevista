"""The CLI contract (docs/CLI_CONTRACT.md): stdout purity, exit codes, envelopes, error codes, docs in sync."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from support import tree_hashes, write

from knowledgevista import cli, errors
from knowledgevista.envelope import JSON_SCHEMA_VERSION, Envelope

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def lib(tmp_path):
    base = tmp_path / "papers"
    write(base / "kaya2022.pdf", b"%PDF-1.4 kaya")
    write(base / "sub" / "same.txt", "dup")
    write(base / "other" / "same.txt", "dup")
    return base


def run(capsys, *argv):
    """Run `kv` in process. Returns (exit code, stdout, stderr)."""
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(capsys, tmp_path, *argv):
    code, out, err = run(capsys, "--json", "--catalog", str(tmp_path / "c.sqlite"), *argv)
    return code, json.loads(out), err


# --- the envelope -----------------------------------------------------------------------------------------------


def test_stdout_is_exactly_one_json_document_and_progress_goes_to_stderr(capsys, tmp_path, lib):
    run_json(capsys, tmp_path, "root", "add", str(lib))
    code, out, err = run(capsys, "--json", "--catalog", str(tmp_path / "c.sqlite"), "scan")
    assert code == 0
    assert out.count("\n") == 1, "exactly one line of JSON"
    envelope = json.loads(out)  # parses as a whole: nothing else shared the stream
    assert "hashing" in err and "hashing" not in out


def test_envelope_shape_is_stable(capsys, tmp_path, lib):
    run_json(capsys, tmp_path, "root", "add", str(lib))
    code, env, _ = run_json(capsys, tmp_path, "scan")
    assert code == 0
    assert list(env) == ["schema_version", "command", "ok", "complete", "records", "warnings", "errors", "next_cursor"]
    assert env["schema_version"] == JSON_SCHEMA_VERSION == 1
    assert env["command"] == "scan" and env["ok"] is True and env["errors"] == [] and env["complete"] is True
    assert isinstance(env["records"], list) and env["records"][0]["new_artifacts"] == 2


def test_records_is_always_a_list_even_when_empty(capsys, tmp_path):
    empty = tmp_path / "empty-lib"
    empty.mkdir()
    code, env, _ = run_json(capsys, tmp_path, "root", "add", str(empty))  # a root with nothing in it is still fine
    code, env, _ = run_json(capsys, tmp_path, "root", "list")
    assert isinstance(env["records"], list)
    code, env, _ = run_json(capsys, tmp_path, "scan")
    assert env["ok"] and env["records"][0]["files_seen"] == 0


def test_json_is_ascii_so_it_survives_any_console_encoding(capsys, tmp_path):
    base = tmp_path / "lib"
    write(base / "café 田中.txt", "x")
    run_json(capsys, tmp_path, "root", "add", str(base))
    run_json(capsys, tmp_path, "scan")
    code, out, _ = run(capsys, "--json", "--catalog", str(tmp_path / "c.sqlite"), "explain", "x" and "café 田中.txt")
    assert out.isascii() and code == 0
    assert json.loads(out)["records"][0]["artifacts"][0]["locations"][0]["path"] == "café 田中.txt"


def test_global_options_work_before_or_after_the_command(capsys, tmp_path, lib):
    run(capsys, "--catalog", str(tmp_path / "c.sqlite"), "root", "add", str(lib))
    before = run(capsys, "--json", "--catalog", str(tmp_path / "c.sqlite"), "stats")
    after = run(capsys, "stats", "--json", "--catalog", str(tmp_path / "c.sqlite"))
    assert before[0] == after[0] == 0 and json.loads(before[1])["command"] == json.loads(after[1])["command"] == "stats"


# --- errors and exit codes --------------------------------------------------------------------------------------


def test_invalid_arguments_under_json_is_an_envelope_with_exit_2_and_no_usage_dump(capsys):
    code, out, err = run(capsys, "--json", "--bogus")
    env = json.loads(out)
    assert code == errors.EXIT_INVALID == 2
    assert env["ok"] is False and env["errors"][0]["code"] == errors.ErrorCode.INVALID_ARGUMENTS
    assert "usage" not in err.lower()


def test_missing_subcommand_and_missing_required_argument_are_invalid_arguments(capsys):
    for argv in (["--json"], ["--json", "explain"], ["--json", "root"]):
        code, out, _ = run(capsys, *argv)
        assert code == 2 and json.loads(out)["errors"][0]["code"] == "KV_INVALID_ARGUMENTS", argv


def test_not_found_is_exit_1_with_a_stable_code(capsys, tmp_path, lib):
    run_json(capsys, tmp_path, "root", "add", str(lib))
    run_json(capsys, tmp_path, "scan")
    code, env, _ = run_json(capsys, tmp_path, "explain", "no-such-file.pdf")
    assert code == 1 and env["ok"] is False
    assert env["errors"][0]["code"] == "KV_NOT_FOUND" and env["errors"][0]["details"]["reference"] == "no-such-file.pdf"


def test_ambiguity_lists_every_candidate_and_never_picks_one(capsys, tmp_path):
    base = tmp_path / "lib"
    write(base / "a" / "report.pdf", b"%PDF-1.4 one")
    write(base / "b" / "report.pdf", b"%PDF-1.4 two")
    run_json(capsys, tmp_path, "root", "add", str(base))
    run_json(capsys, tmp_path, "scan")
    code, env, _ = run_json(capsys, tmp_path, "explain", "report.pdf")
    assert code == 1 and env["errors"][0]["code"] == "KV_AMBIGUOUS"
    assert sorted(c["path"] for c in env["errors"][0]["details"]["candidates"]) == ["a/report.pdf", "b/report.pdf"]


def test_a_typo_in_the_catalog_path_is_an_error_not_a_new_empty_library(capsys, tmp_path):
    code, out, _ = run(capsys, "--json", "--catalog", str(tmp_path / "typo.sqlite"), "stats")
    assert code == 1 and json.loads(out)["errors"][0]["code"] == "KV_CATALOG_MISSING"
    assert not (tmp_path / "typo.sqlite").exists(), "a read command must not create a catalog"


def test_scan_of_only_unavailable_roots_fails_with_root_unavailable_and_changes_nothing(capsys, tmp_path, lib):
    run_json(capsys, tmp_path, "root", "add", str(lib))
    run_json(capsys, tmp_path, "scan")
    os.rename(lib, tmp_path / "away")
    code, env, _ = run_json(capsys, tmp_path, "scan")
    assert code == 1 and env["errors"][0]["code"] == "KV_ROOT_UNAVAILABLE"
    assert any(w["code"] == "KV_ROOT_UNAVAILABLE" for w in env["warnings"])
    os.rename(tmp_path / "away", lib)
    code, env, _ = run_json(capsys, tmp_path, "scan")
    assert code == 0 and env["records"][0]["went_missing"] == 0


def test_overlapping_root_is_refused_with_its_own_code(capsys, tmp_path, lib):
    run_json(capsys, tmp_path, "root", "add", str(lib))
    code, env, _ = run_json(capsys, tmp_path, "root", "add", str(lib / "sub"))
    assert code == 1 and env["errors"][0]["code"] == "KV_ROOT_OVERLAP"


def test_a_catalog_from_a_newer_version_is_reported_and_left_alone(capsys, tmp_path, lib):
    run_json(capsys, tmp_path, "root", "add", str(lib))
    import sqlite3
    c = sqlite3.connect(tmp_path / "c.sqlite")
    c.execute("INSERT INTO schema_migration VALUES (999, 'future', 'x', '9.9')")
    c.commit()
    c.close()
    code, env, _ = run_json(capsys, tmp_path, "stats")
    assert code == 1 and env["errors"][0]["code"] == "KV_CATALOG_TOO_NEW"


def test_an_unexpected_exception_becomes_an_internal_error_envelope_never_a_traceback(capsys, tmp_path, lib, monkeypatch):
    run_json(capsys, tmp_path, "root", "add", str(lib))
    monkeypatch.setattr("knowledgevista.services.stats.library_stats", lambda conn: 1 / 0)
    code, out, err = run(capsys, "--json", "--catalog", str(tmp_path / "c.sqlite"), "stats")
    assert code == 1 and json.loads(out)["errors"][0]["code"] == "KV_INTERNAL"
    assert "Traceback" not in out + err


# --- the commands -----------------------------------------------------------------------------------------------


def test_verify_exit_codes_and_findings(capsys, tmp_path, lib):
    run_json(capsys, tmp_path, "root", "add", str(lib))
    run_json(capsys, tmp_path, "scan")
    code, env, _ = run_json(capsys, tmp_path, "verify")
    assert code == 0 and env["records"][0]["ok"] == 3
    stamp = os.stat(lib / "kaya2022.pdf")
    (lib / "kaya2022.pdf").write_bytes(b"%PDF-1.4 kayb")
    os.utime(lib / "kaya2022.pdf", ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    code, env, _ = run_json(capsys, tmp_path, "verify")
    assert code == 1 and env["errors"][0]["code"] == "KV_FILE_CHANGED"
    assert [r["path"] for r in env["records"] if r["type"] == "mismatch"] == ["kaya2022.pdf"]
    os.remove(lib / "sub" / "same.txt")
    code, env, _ = run_json(capsys, tmp_path, "verify")
    assert {e["code"] for e in env["errors"]} == {"KV_FILE_CHANGED", "KV_FILE_MISSING"}


def test_doctor_summary_names_the_categories_it_checked(capsys, tmp_path, lib):
    run_json(capsys, tmp_path, "root", "add", str(lib))
    run_json(capsys, tmp_path, "scan")
    code, env, _ = run_json(capsys, tmp_path, "doctor")
    summary = env["records"][0]
    assert code == 0 and summary["type"] == "summary" and summary["categories_checked"] == ["filesystem", "catalog", "extraction", "search", "metadata", "relationships"]


def test_doctor_exits_1_when_it_finds_catalog_errors(capsys, tmp_path, lib):
    run_json(capsys, tmp_path, "root", "add", str(lib))
    run_json(capsys, tmp_path, "scan")
    import sqlite3
    c = sqlite3.connect(tmp_path / "c.sqlite")
    c.execute("INSERT INTO document (document_id, created_at) VALUES ('orphan-doc', 'x')")
    c.commit()
    c.close()
    code, env, _ = run_json(capsys, tmp_path, "doctor")
    assert code == 1 and env["errors"][0]["code"] == "KV_DOCTOR_FOUND_PROBLEMS"
    assert any(r.get("code") == "KVD_NO_CANONICAL" for r in env["records"])


def test_the_whole_workflow_never_modifies_the_source_tree(capsys, tmp_path, lib):
    before = tree_hashes(lib)
    for argv in (["root", "add", str(lib)], ["scan"], ["scan", "--full"], ["stats"], ["doctor"], ["verify"], ["explain", "kaya2022.pdf"], ["root", "list"]):
        run_json(capsys, tmp_path, *argv)
    assert tree_hashes(lib) == before


def test_human_output_is_readable_text_and_the_module_runs_as_a_program(tmp_path, lib):
    env = {**os.environ, "KNOWLEDGEVISTA_HOME": str(tmp_path / "home"), "PYTHONIOENCODING": "utf-8"}
    run_kv = lambda *a: subprocess.run([sys.executable, "-m", "knowledgevista", *a], capture_output=True, text=True, encoding="utf-8", env=env)  # noqa: E731
    assert run_kv("root", "add", str(lib), "--label", "papers").returncode == 0
    scanned = run_kv("scan")
    assert scanned.returncode == 0 and "papers: 3 files" in scanned.stdout
    explained = run_kv("explain", "kaya2022.pdf")
    assert "available" in explained.stdout and "papers/kaya2022.pdf" in explained.stdout
    assert run_kv("--version").stdout.startswith("KnowledgeVista")
    failed = run_kv("explain", "nothing.pdf")
    assert failed.returncode == 1 and "KV_NOT_FOUND" in failed.stderr


# --- the contract document stays in sync with the code ----------------------------------------------------------


def test_every_error_code_is_unique_prefixed_and_documented():
    contract = (ROOT / "docs" / "CLI_CONTRACT.md").read_text(encoding="utf-8")
    codes = errors.all_codes()
    assert len(codes) == len([n for n in vars(errors.ErrorCode) if not n.startswith("_")]), "two names share one value"
    assert all(code.startswith("KV_") for code in codes)
    undocumented = [c for c in codes if f"`{c}`" not in contract]
    assert not undocumented, f"in the code but not in docs/CLI_CONTRACT.md: {undocumented}"
    documented = set(re.findall(r"\| `(KV_[A-Z_]+)` \|", contract))
    assert documented == set(codes), f"documented but not in the code: {documented - set(codes)}"


def test_every_command_is_documented_and_every_documented_command_exists():
    contract = (ROOT / "docs" / "CLI_CONTRACT.md").read_text(encoding="utf-8")
    parser = cli.build_parser()
    top = next(a for a in parser._actions if a.dest == "command")
    real = set()
    for name, command in top.choices.items():
        nested = next((a for a in command._actions if a.__class__.__name__ == "_SubParsersAction"), None)
        real |= {f"{name} {action}" for action in nested.choices} if nested else {name}
    names = "|".join(sorted((re.escape(n) for n in real), key=len, reverse=True))
    documented = {m.group(1) for row in contract.splitlines() if (m := re.match(rf"\| `({names})[ `]", row))}
    assert documented == real, f"commands drifted: code {sorted(real)} vs docs {sorted(documented)}"


def test_exit_codes_are_the_documented_ones():
    assert (errors.EXIT_OK, errors.EXIT_FAILURE, errors.EXIT_INVALID) == (0, 1, 2)
    assert errors.KvError(errors.ErrorCode.INVALID_ARGUMENTS, "x").exit_code == 2
    assert errors.KvError(errors.ErrorCode.QUERY_INVALID, "x").exit_code == 2
    assert errors.KvError(errors.ErrorCode.NOT_FOUND, "x").exit_code == 1


def test_envelope_to_json_round_trips_and_defaults_are_not_shared():
    a, b = Envelope("x"), Envelope("y")
    a.records.append({"k": 1})
    assert b.records == [], "mutable defaults must not be shared between envelopes"
    assert json.loads(a.to_json())["records"] == [{"k": 1}]


def test_a_root_that_contains_the_catalog_is_refused(capsys, tmp_path):
    code, env, _ = run_json(capsys, tmp_path, "root", "add", str(tmp_path))  # the catalog is tmp_path/c.sqlite
    assert code == 1 and env["errors"][0]["code"] == "KV_ROOT_OVERLAP" and "catalog" in env["errors"][0]["message"]
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert run_json(capsys, tmp_path, "root", "add", str(other))[0] == 0
