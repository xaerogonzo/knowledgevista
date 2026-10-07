"""The in-app driver: a scripted run of the real window that ends in a verdict, and the proof that the verdict can be no.

The tour (drive/library_tour.json) runs `kv gui` as a SEPARATE PROCESS, offscreen, against a scratch home and a generated library,
exactly as a person would run it with a display. What it asserts is read off the real widgets and off the catalog, and the exit status
is the verdict. Alongside it are the runs that must FAIL: a check that cannot fail is not a check (drive_template/README.md).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pdfbuilders as b
import pytest
from guisupport import require_qt

require_qt()

ROOT = Path(__file__).resolve().parent.parent
TOUR = ROOT / "drive" / "library_tour.json"


def make_library(folder: Path) -> Path:
    (folder / "papers").mkdir(parents=True)
    (folder / "books").mkdir()
    (folder / "notes").mkdir()
    b.native_pdf(folder / "papers" / "aqueous.pdf", title="Aqueous Solubility of Invented Esters", doi="10.5555/kv.drive.0001", body_pages=5)
    b.native_pdf(folder / "papers" / "second.pdf", title="Partition Coefficients of Imaginary Amides", doi="10.5555/kv.drive.0002", body_pages=3)
    b.labelled_pdf(folder / "books" / "book.pdf")
    (folder / "notes" / "readme.txt").write_text("A plain note about invented esters.\n", encoding="utf-8")
    return folder


def environment(tmp_path: Path, library: Path | None = None, **extra) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KNOWLEDGEVISTA", "KV_", "QT_"))}
    env.update({"QT_QPA_PLATFORM": "offscreen", "KNOWLEDGEVISTA_HOME": str(tmp_path / "home"), "KNOWLEDGEVISTA_DRIVE_TIMEOUT_S": "150",
                "KV_DRIVE_SHOTS": str(tmp_path / "shots"), "PYTHONIOENCODING": "utf-8"})
    if library is not None:
        env["KV_DRIVE_LIBRARY"] = str(library)
    env.update(extra)
    return env


def run_gui(script: Path, env: dict[str, str], *extra_args: str, timeout: int = 240) -> tuple[subprocess.CompletedProcess, dict | None]:
    env = {**env, "KNOWLEDGEVISTA_DRIVE": str(script)}
    done = subprocess.run([sys.executable, "-m", "knowledgevista", "gui", *extra_args], env=env, capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    report_path = script.with_suffix(".report.json")
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else None
    return done, report


def write_script(tmp_path: Path, steps: list[dict], name: str = "script.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(steps), encoding="utf-8")
    return path


def failed_ids(report: dict) -> list[str]:
    return [e["id"] for e in report["expectations"] if not e["passed"]]


def start_steps(library: Path) -> list[dict]:
    return [{"do": "add_root", "path": str(library), "label": "L"}, {"do": "wait_jobs"}]


@pytest.fixture
def library(tmp_path):
    return make_library(tmp_path / "library")


# ---------------------------------------------------------------------------------------------------------- the tour passes


def test_the_library_tour_passes_and_leaves_the_library_alone(tmp_path, library):
    before = {p.relative_to(library).as_posix(): p.read_bytes() for p in library.rglob("*") if p.is_file()}
    script = tmp_path / "tour.json"
    script.write_text(TOUR.read_text(encoding="utf-8"), encoding="utf-8")
    done, report = run_gui(script, environment(tmp_path, library))
    assert report is not None, done.stdout + done.stderr
    assert done.returncode == 0 and report["passed"] is True, json.dumps([e for e in report["expectations"] if not e["passed"]] + report["steps_failed"], indent=1)
    asked = sum(1 for step in json.loads(TOUR.read_text(encoding="utf-8")) if step["do"] in ("expect", "expect_clean"))
    assert report["steps_failed"] == [] and len(report["expectations"]) == asked >= 25, "every expectation in the script was actually evaluated"
    assert all(e["passed"] for e in report["expectations"])
    assert report["finished_because"] == "quit" and report["drive_diagnostics"] == []
    assert {p.relative_to(library).as_posix(): p.read_bytes() for p in library.rglob("*") if p.is_file()} == before, "the tour moved, renamed or changed nothing"
    for shot in ("evidence.png", "rename.png", "tour.png"):
        assert (tmp_path / "shots" / shot).stat().st_size > 1000, shot


def test_a_second_process_finds_the_state_the_first_one_left(tmp_path, library):
    """The restart that matters: not the same process opening a new window, but `kv gui` started again against the same home."""
    first = write_script(tmp_path, start_steps(library) + [
        {"do": "extract"}, {"do": "wait_jobs", "within_ms": 120000}, {"do": "resolve"}, {"do": "wait_jobs", "within_ms": 120000},
        {"do": "select_review", "paper": "papers/aqueous.pdf", "field": "Title"}, {"do": "press", "widget": "acceptButton"}, {"do": "wait_jobs"},
        {"do": "tab", "name": "documents"}, {"do": "select", "title": "Aqueous Solubility of Invented Esters"}, {"do": "wait_jobs"},
        {"do": "press", "widget": "tagButton"}, {"do": "dialog", "name": "tagDialog", "set": {"choiceCombo": "Shelf"}, "press": "Tag"}, {"do": "wait_jobs"},
        {"do": "filter", "text": "aqueous"}, {"do": "search", "text": "aqueous solubility"}, {"do": "wait_jobs"}, {"do": "tab", "name": "review"},
        {"do": "quit"}], "first.json")
    env = environment(tmp_path, library)
    done, report = run_gui(first, env)
    assert done.returncode == 0 and report["passed"], done.stdout + done.stderr + json.dumps(report["steps_failed"])
    state = json.loads((tmp_path / "home" / "config" / "gui-state.json").read_text(encoding="utf-8"))
    assert (state["tab"], state["filter_text"], state["search_text"]) == ("review", "aqueous", "aqueous solubility") and state["document_id"]

    second = write_script(tmp_path, [
        {"do": "wait_jobs"},
        {"do": "expect", "id": "the tab", "check": "tab", "equals": "review"},
        {"do": "expect", "id": "the filter", "check": "filter_text", "equals": "aqueous"},
        {"do": "expect", "id": "the search box", "check": "search_text", "equals": "aqueous solubility"},
        {"do": "expect", "id": "the remembered search ran again", "check": "rows", "table": "hitTable", "at_least": 1, "within_ms": 8000},
        {"do": "expect", "id": "the selected document", "check": "detail_title", "equals": "Aqueous Solubility of Invented Esters", "within_ms": 8000},
        {"do": "expect", "id": "the accepted title is in the catalog", "check": "catalog", "sql": "SELECT value FROM metadata_value WHERE field = 'title'",
         "equals": "Aqueous Solubility of Invented Esters"},
        {"do": "expect", "id": "the tag", "check": "sidebar_has", "includes": ["Shelf  (1)"]},
        {"do": "expect", "id": "the folder", "check": "sidebar_has", "includes": []},
        {"do": "quit"}], "second.json")
    done, report = run_gui(second, env)
    assert done.returncode == 0 and report["passed"], json.dumps([e for e in report["expectations"] if not e["passed"]] + report["steps_failed"], indent=1)


# ---------------------------------------------------------------------------------------------------------- proof the verdict can be no


def test_a_false_expectation_fails_the_run_and_is_listed(tmp_path, library):
    script = write_script(tmp_path, start_steps(library) + [
        {"do": "expect", "id": "titles are nonsense", "check": "titles", "equals": ["nonsense"], "within_ms": 500},
        {"do": "expect", "id": "four documents", "check": "rows", "table": "docTable", "equals": 4},
        {"do": "quit"}])
    done, report = run_gui(script, environment(tmp_path))
    assert done.returncode == 1 and report["passed"] is False
    assert failed_ids(report) == ["titles are nonsense"], "the true expectation passed and the false one is named"
    assert any(e["id"] == "four documents" and e["passed"] for e in report["expectations"])
    assert "not satisfied within 500 ms" in next(e for e in report["expectations"] if not e["passed"])["failure_reason"]


def test_a_failure_is_permanent_even_if_the_condition_later_comes_true(tmp_path, library):
    script = write_script(tmp_path, [
        {"do": "expect", "id": "too early", "check": "rows", "table": "docTable", "equals": 4, "within_ms": 100},
        {"do": "add_root", "path": str(library)}, {"do": "wait_jobs"},
        {"do": "expect", "id": "now true", "check": "rows", "table": "docTable", "equals": 4},
        {"do": "quit"}])
    done, report = run_gui(script, environment(tmp_path))
    assert done.returncode == 1 and failed_ids(report) == ["too early"]
    assert any(e["id"] == "now true" and e["passed"] for e in report["expectations"]), "a later success does not erase the earlier failure"


def test_an_unknown_step_and_an_impossible_step_fail_the_run(tmp_path, library):
    script = write_script(tmp_path, [{"do": "teleport"}, {"do": "press", "widget": "noSuchButton"}, {"do": "select", "title": "no such document"}, {"do": "quit"}])
    done, report = run_gui(script, environment(tmp_path))
    assert done.returncode == 1
    reasons = [s["detail"]["reason"] for s in report["steps_failed"]]
    assert len(reasons) == 3 and reasons[0] == "unknown step" and "noSuchButton" in reasons[1] and "no such document" in reasons[2]
    assert report["finished_because"] == "quit", "failed steps do not stop the script from reaching its quit"


def test_pressing_a_disabled_button_fails_the_step(tmp_path, library):
    script = write_script(tmp_path, [{"do": "press", "widget": "acceptButton"}, {"do": "quit"}])
    done, report = run_gui(script, environment(tmp_path))
    assert done.returncode == 1 and "is disabled" in report["steps_failed"][0]["detail"]["reason"]


def test_expect_clean_fails_when_the_application_says_something(tmp_path, library):
    script = write_script(tmp_path, [{"do": "log", "level": "warning", "message": "the application said something"}, {"do": "expect_clean", "settle_ms": 200}, {"do": "quit"}])
    done, report = run_gui(script, environment(tmp_path))
    assert done.returncode == 1 and failed_ids(report) == ["expect_clean"]
    assert "the application said something" in report["expectations"][0]["failure_reason"]


def test_expect_clean_passes_when_the_only_diagnostic_is_excused(tmp_path, library):
    script = write_script(tmp_path, [{"do": "log", "message": "a known notice"}, {"do": "expect_clean", "settle_ms": 200, "allow": ["known notice"]}, {"do": "quit"}])
    done, report = run_gui(script, environment(tmp_path))
    assert done.returncode == 0 and report["passed"] and report["drive_diagnostics"], "excused is not hidden: it stays in the report"


def test_an_error_logged_by_the_application_is_in_the_report_even_without_expect_clean(tmp_path, library):
    script = write_script(tmp_path, [{"do": "log", "level": "error", "message": "something broke"}, {"do": "quit"}])
    done, report = run_gui(script, environment(tmp_path))
    assert any("something broke" in m for e in report["drive_diagnostics"] for m in e["messages"])


def test_a_run_that_never_finishes_is_stopped_and_fails(tmp_path, library):
    script = write_script(tmp_path, [{"do": "sleep", "ms": 600000}, {"do": "quit"}])
    started = time.monotonic()
    done, report = run_gui(script, environment(tmp_path, KNOWLEDGEVISTA_DRIVE_TIMEOUT_S="4"), timeout=60)
    assert done.returncode == 1 and time.monotonic() - started < 40
    assert report["finished_because"] == "timeout" and "did not finish within 4 s" in report["steps_failed"][0]["detail"]["reason"]


def test_a_script_that_ends_without_quit_fails_and_still_leaves_a_verdict(tmp_path, library):
    script = write_script(tmp_path, [{"do": "expect", "id": "pointless", "check": "tab", "equals": "documents"}])
    done, report = run_gui(script, environment(tmp_path), timeout=60)
    assert done.returncode == 1 and report is not None and report["expectations"][0]["passed"]
    assert report["steps_failed"][0]["detail"]["reason"] == "the script ended without a quit step", "forgetting quit is a mistake in the script, so it fails"


# ---------------------------------------------------------------------------------------------------------- safety


def test_a_script_refuses_to_drive_the_real_library(tmp_path):
    """No KNOWLEDGEVISTA_HOME and the default location: this would be the maintainer's own library. Refused BEFORE anything is created."""
    env = environment(tmp_path)
    del env["KNOWLEDGEVISTA_HOME"]
    env["LOCALAPPDATA"], env["APPDATA"] = str(tmp_path / "local"), str(tmp_path / "roaming")
    env["XDG_DATA_HOME"], env["XDG_CACHE_HOME"], env["XDG_CONFIG_HOME"] = (str(tmp_path / n) for n in ("xdata", "xcache", "xconfig"))
    script = write_script(tmp_path, [{"do": "quit"}])
    done, report = run_gui(script, env)
    assert done.returncode == 2 and "REAL library" in done.stderr + done.stdout and report is None
    assert not (tmp_path / "local").exists() and not (tmp_path / "xdata").exists(), "a refused run creates nothing"


def test_a_script_may_drive_a_catalog_it_is_pointed_at(tmp_path, library):
    env = environment(tmp_path)
    del env["KNOWLEDGEVISTA_HOME"]
    env["LOCALAPPDATA"], env["APPDATA"] = str(tmp_path / "local"), str(tmp_path / "roaming")
    env["XDG_DATA_HOME"], env["XDG_CACHE_HOME"], env["XDG_CONFIG_HOME"] = (str(tmp_path / n) for n in ("xdata", "xcache", "xconfig"))
    script = write_script(tmp_path, [{"do": "quit"}])
    done, report = run_gui(script, env, "--catalog", str(tmp_path / "explicit.sqlite"))
    assert done.returncode == 0 and report["passed"]


def test_a_driven_run_never_starts_a_viewer(tmp_path, library):
    """The shell is a recording one: `open` is recorded and nothing is launched (the tour asserts what was recorded)."""
    script = write_script(tmp_path, start_steps(library) + [
        {"do": "select", "title": "papers/aqueous.pdf"}, {"do": "wait_jobs"}, {"do": "press", "widget": "openButton"}, {"do": "wait_jobs"},
        {"do": "expect", "id": "recorded", "check": "shell", "kind": "open", "endswith_any": "aqueous.pdf"}, {"do": "quit"}])
    done, report = run_gui(script, environment(tmp_path))
    assert done.returncode == 0 and report["passed"], json.dumps(report["steps_failed"])


# ---------------------------------------------------------------------------------------------------------- in-process pieces


def test_the_ledger_is_the_templates_byte_for_byte():
    template = ROOT / "drive_template" / "drive_ledger.py"
    assert template.exists(), "the template this module is a copy of must be in the repository"
    assert (ROOT / "src" / "knowledgevista" / "gui" / "drive_ledger.py").read_bytes() == template.read_bytes()


def test_an_exception_in_a_slot_reaches_the_ledger(qapp):
    """The channel the verdict depends on: PySide prints a slot's exception and carries on, so only the hook makes it a failure."""
    from PySide6.QtCore import QTimer
    from guisupport import pump

    from knowledgevista.gui import drive_ledger as L

    ledger = L.Ledger()
    ledger.install()
    try:
        ledger.start_drive()
        QTimer.singleShot(0, lambda: 1 / 0)
        pump(100)
        assert ledger.failed and any(e.kind == L.UNCAUGHT_EXCEPTION and "ZeroDivisionError" in e.messages[0] for e in ledger.in_phase(L.DRIVE))
    finally:
        ledger.uninstall()


def test_a_qt_warning_reaches_the_ledger(qapp):
    from PySide6.QtCore import QObject, qWarning

    from knowledgevista.gui import drive
    from knowledgevista.gui import drive_ledger as L

    ledger = L.Ledger()
    drive._LEDGER = ledger  # noqa: SLF001 - what begin() does, without needing a script
    previous = drive.qInstallMessageHandler(drive._qt_message)  # noqa: SLF001
    drive._PREVIOUS_QT_HANDLER = previous  # noqa: SLF001
    try:
        ledger.start_drive()
        qWarning("QObject: a Qt warning from the test")
        assert ledger.contains("a Qt warning from the test")
        assert QObject is not None
    finally:
        drive._end_listening()  # noqa: SLF001
        drive._LEDGER = None  # noqa: SLF001
