"""Shared fixtures.

`isolated_home` is autouse: a test must never read or write the maintainer's real catalog or cache, and
nothing here should depend on whose machine it runs on. The environment is read on every call by
`knowledgevista.paths`, so setting it per test is enough.
"""

from __future__ import annotations

import pytest

from knowledgevista import paths


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "kv-home"
    monkeypatch.setenv(paths.HOME_ENV, str(home))
    return home
