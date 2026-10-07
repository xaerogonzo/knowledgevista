from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("license_inventory", _ROOT / "tools" / "license_inventory.py")
tool = importlib.util.module_from_spec(_SPEC)
sys.modules["license_inventory"] = tool
_SPEC.loader.exec_module(tool)

# The exact strings the real packages declare (measured 2026-10-06 from their installed metadata).
PYMUPDF = "Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License"
PYMUPDF_LAYOUT = "Dual Licensed - Polyform Noncommercial or Artifex Commercial License"

LOCK = tomllib.loads(
    """
[[package]]
name = "knowledgevista"
version = "0.0.1"
dependencies = [{ name = "plain" }]

[package.optional-dependencies]
extract = [{ name = "PyMuPDF" }]
reflow = [{ name = "pymupdf4llm" }]

[[package]]
name = "plain"
version = "1"

[[package]]
name = "pymupdf"
version = "1"

[[package]]
name = "pymupdf4llm"
version = "1"
dependencies = [{ name = "pymupdf" }, { name = "pymupdf-layout" }]

[[package]]
name = "pymupdf-layout"
version = "1"
dependencies = [{ name = "deep" }]

[[package]]
name = "deep"
version = "1"
"""
)

LICENSES = {
    "plain": "MIT",
    "pymupdf": PYMUPDF,
    "pymupdf4llm": PYMUPDF,
    "pymupdf-layout": PYMUPDF_LAYOUT,
    "deep": "BSD-3-Clause",
}


# --- classification ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        (PYMUPDF, "ok"),  # "or Artifex COMMERCIAL License" must not trip the noncommercial rule
        (PYMUPDF_LAYOUT, "blocked"),  # the real one that started this tool
        ("MIT", "ok"),
        ("BSD-3-Clause", "ok"),
        ("Apache Software License", "ok"),
        ("GNU Lesser General Public License v3 (LGPLv3)", "ok"),
        ("Proprietary", "blocked"),
        ("CC-BY-NC non-commercial", "blocked"),
        ("Some bespoke text nobody has classified", "unknown"),
        ("", "unknown"),
    ],
)
def test_classify(text, verdict):
    assert tool.classify(text) == verdict


# --- the lockfile closure ---------------------------------------------------------------------------------------


def test_core_closure_contains_only_required_dependencies():
    assert tool.closure(LOCK, set()) == {"plain"}


def test_extra_pulls_in_its_transitive_closure_and_normalises_names():
    assert tool.closure(LOCK, {"extract"}) == {"plain", "pymupdf"}
    assert tool.closure(LOCK, {"reflow"}) == {"plain", "pymupdf4llm", "pymupdf", "pymupdf-layout", "deep"}


def test_unknown_extra_is_an_error_not_an_empty_set():
    with pytest.raises(ValueError):
        tool.closure(LOCK, {"nope"})


def test_blocked_dependency_two_levels_down_is_found_only_when_its_extra_is_selected():
    core = tool.inventory(LOCK, set(), LICENSES.get)
    assert tool.failures(core) == [], "the default install must be clean"

    reflow = tool.inventory(LOCK, {"reflow"}, LICENSES.get)
    bad = tool.failures(reflow)
    assert [(row.name, row.verdict) for row in bad] == [("pymupdf-layout", "blocked")], (
        "the Polyform package is two levels down (extra -> pymupdf4llm -> pymupdf-layout); a direct-dependency "
        "scan would not see it"
    )


def test_a_package_that_is_not_installed_is_reported_not_skipped():
    rows = tool.inventory(LOCK, {"extract"}, {"plain": "MIT"}.get)
    assert {row.name: row.verdict for row in rows} == {"plain": "ok", "pymupdf": "not-installed"}
    assert tool.failures(rows), "an unreadable licence must fail the check, not pass it"


# --- this repository --------------------------------------------------------------------------------------------


def test_this_repositorys_default_install_has_no_blocked_dependency():
    lock = tomllib.loads((_ROOT / "uv.lock").read_text(encoding="utf-8"))
    rows = tool.inventory(lock, set())
    assert tool.failures(rows) == [], rows


def test_this_repositorys_extract_and_gui_extras_are_clean_when_installed():
    lock = tomllib.loads((_ROOT / "uv.lock").read_text(encoding="utf-8"))
    rows = tool.inventory(lock, {"extract"})
    assert any(row.name == "pymupdf" for row in rows), "the oracle must be alive: pymupdf is in the extract closure"
    assert tool.failures(rows) == [], rows


def test_reflow_extra_stays_empty_until_its_licence_problem_is_resolved():
    # pyproject.toml documents why; this stops a well-meaning edit from wiring pymupdf4llm back in unnoticed.
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["optional-dependencies"]["reflow"] == []
