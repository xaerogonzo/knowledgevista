"""The window is an optional extra: the core must install and run without Qt, and `kv gui` must say what is missing rather than crash.

These tests do not need PySide6 and must pass without it (the `core-only-build` CI job installs none of the extras).
"""

from __future__ import annotations

import json
import subprocess
import sys

from knowledgevista import cli, gui


def test_importing_the_command_line_imports_no_qt():
    """`kv --version` in a core-only install: the parser knows `gui` but never imports the toolkit until it is run."""
    code = ("import sys; import knowledgevista.cli, knowledgevista.cli_gui, knowledgevista.gui, knowledgevista.services.library_view, "
            "knowledgevista.services.opener, knowledgevista.domain.evidence_view, knowledgevista.domain.health_view; "
            "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('PySide6', 'shiboken6')); "
            "print(bad); sys.exit(1 if bad else 0)")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, f"a core module imported Qt: {done.stdout}{done.stderr}"


def test_the_read_model_and_its_text_are_core_not_gui():
    """A caller without Qt (the command line, the MCP server) can use the same read model the window shows."""
    from knowledgevista.domain import evidence_view, health_view
    from knowledgevista.services import library_view

    assert callable(library_view.list_documents) and callable(evidence_view.render_why) and callable(health_view.health_text)


def test_gui_without_pyside_says_what_to_install(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(gui, "qt_available", lambda: False)
    code = cli.main(["--json", "--catalog", str(tmp_path / "c.sqlite"), "gui"])
    envelope = json.loads(capsys.readouterr().out)
    assert code == 1 and envelope["ok"] is False
    error = envelope["errors"][0]
    assert error["code"] == "KV_DEPENDENCY_MISSING" and "knowledgevista[gui]" in error["message"] and error["details"] == {"extra": "gui", "package": "PySide6"}
    assert not (tmp_path / "c.sqlite").exists(), "asking for a window that cannot open must not create a library"


def test_qt_available_agrees_with_the_import():
    try:
        import PySide6.QtWidgets  # noqa: F401
    except ImportError:
        assert gui.qt_available() is False
    else:
        assert gui.qt_available() is True
