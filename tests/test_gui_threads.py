"""No ad-hoc threads: the window's work leaves the interface thread in ONE place (gui/jobs.py).

A guard that scans source can pass for the wrong reason (it matches nothing, or it matches its own prose), so it proves it can fail: it
runs the same scan over a string that does break the rule, and it requires that the one allowed place really does contain what the rule
is about.
"""

from __future__ import annotations

import re
from pathlib import Path

GUI = Path(__file__).resolve().parent.parent / "src" / "knowledgevista" / "gui"

#: How a thread is made. QTimer is not one (it runs on the thread that owns it); the driver's watchdog is a harness, not application work.
THREAD_MAKERS = re.compile(r"\bQThread\b|\bQThreadPool\b|\bQRunnable\b|\bQtConcurrent\b|threading\.Thread\b|threading\.Timer\b|\bTimer\(|\bThread\(|concurrent\.futures|\bThreadPoolExecutor\b|\bmultiprocessing\b|\bsubprocess\.")
ALLOWED = {"jobs.py": "the job manager: the one place work leaves the interface thread",
           "drive.py": "the scripted-run harness: a watchdog thread and a child `kv` process, neither is application work",
           "drive_ledger.py": "the scripted-run harness's evidence machinery (the template's, byte for byte): locks, and git for the commit id",
           "shell.py": "starts the operating system's viewer (a child process the window never waits on)"}


def offenders(sources: dict[str, str]) -> dict[str, list[str]]:
    found = {}
    for name, text in sources.items():
        code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
        # Prose in docstrings may name these classes; a real use is code. Strip triple-quoted blocks before matching.
        code = re.sub(r'""".*?"""', "", code, flags=re.S)
        hits = sorted(set(THREAD_MAKERS.findall(code)))
        if hits:
            found[name] = hits
    return found


def gui_sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(GUI.glob("*.py"))}


def test_only_the_job_manager_starts_threads():
    unexpected = {name: hits for name, hits in offenders(gui_sources()).items() if name not in ALLOWED}
    assert unexpected == {}, f"{unexpected}: work must leave the interface thread through gui/jobs.py"


def test_the_guard_looks_at_the_right_files_and_the_allowed_one_really_uses_threads():
    sources = gui_sources()
    assert {"jobs.py", "window.py", "work.py", "models.py", "detail.py", "dialogs.py", "state.py", "text.py"} <= set(sources), "the scan sees the window's modules"
    found = offenders(sources)
    assert "QThreadPool" in found["jobs.py"] and "QRunnable" in found["jobs.py"], "jobs.py is where the pools live"
    assert set(ALLOWED) >= set(found), "nothing outside the allowed list is making threads"


def test_the_guard_can_say_no():
    assert offenders({"x.py": "import threading\nt = threading.Thread(target=f)\nt.start()"}) == {"x.py": ["threading.Thread"]}
    assert offenders({"x.py": "from PySide6.QtCore import QThread\nclass W(QThread): pass"}) == {"x.py": ["QThread"]}
    assert offenders({"x.py": "import subprocess\nsubprocess.run(['x'])"}) == {"x.py": ["subprocess."]}
    assert offenders({"x.py": '"""A QThread would be wrong here."""\n# QThread too\nx = 1'}) == {}, "prose is not a use"


JOB_CALL = re.compile(r"jobs\.submit\((.*?)\)\n", flags=re.S)
JOB_FUNCTION = re.compile(r"lambda ctx: ([^,]+(?:,[^,]+)*?)(?:,\s*lane|,\s*on_done|,\s*channel|,\s*once|$)")
WIDGET = re.compile(r"\b(dialog|self\.\w*(?:box|edit|table|combo|button|tabs|detail|sidebar))\b")


def widgets_in_jobs(text: str) -> list[str]:
    return [fn for call in JOB_CALL.findall(text) for fn in JOB_FUNCTION.findall(call) if WIDGET.search(fn)]


def test_widgets_are_never_touched_inside_a_job():
    """The bug this guards was real: a dialog's value read inside the job's lambda, after the dialog had been deleted. Jobs may close over
    plain values only, so no `submit(...)` call in the window may mention a widget inside its job function."""
    assert widgets_in_jobs((GUI / "window.py").read_text(encoding="utf-8")) == []


def test_that_guard_can_say_no():
    buggy = ('dialog.accepted.connect(lambda: self.jobs.submit("collection", "Add", lambda ctx: work.add_to_collection(ctx, dialog.value(), document_id),\n'
             '                                                 lane=J.WRITE, on_done=self._collected))\n')
    assert widgets_in_jobs(buggy) == ["work.add_to_collection(ctx, dialog.value(), document_id)"]
    assert widgets_in_jobs('self.jobs.submit("x", "x", lambda ctx: work.search(ctx, text), channel="search")\n') == []
    assert widgets_in_jobs('self.jobs.submit("x", "x", lambda ctx: work.detail(ctx, self.search_box.text()), channel="d")\n') != []
