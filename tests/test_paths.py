from __future__ import annotations

from knowledgevista import paths


def test_home_override_moves_all_three_storage_classes(isolated_home):
    assert paths.data_dir() == isolated_home / "data"
    assert paths.cache_dir() == isolated_home / "cache"
    assert paths.config_dir() == isolated_home / "config"
    assert paths.catalog_path() == isolated_home / "data" / "catalog.sqlite"


def test_storage_classes_are_distinct_directories(isolated_home):
    # The cache is deletable and the catalog is not; sharing a directory would let a cache prune eat user state.
    assert len({paths.data_dir(), paths.cache_dir(), paths.config_dir()}) == 3


def test_environment_is_read_per_call_not_at_import(tmp_path, monkeypatch):
    first = tmp_path / "a"
    second = tmp_path / "b"
    monkeypatch.setenv(paths.HOME_ENV, str(first))
    assert paths.catalog_path().is_relative_to(first)
    monkeypatch.setenv(paths.HOME_ENV, str(second))
    assert paths.catalog_path().is_relative_to(second)


def test_default_location_is_not_the_override(monkeypatch):
    monkeypatch.delenv(paths.HOME_ENV, raising=False)
    assert paths.data_dir().name in {"data", "KnowledgeVista"}
    assert "kv-home" not in str(paths.data_dir())
