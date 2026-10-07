"""Virtual organisation: collections, tags, saved searches and the computed system views. None of it may touch a file."""

from __future__ import annotations

import os

import pytest
from support import make_env, tree_hashes

from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.index.search import QUERY_LANGUAGE_VERSION, Query, parse_query
from knowledgevista.services import metadata, organize, relations


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path, {"a.txt": "alpha", "b.txt": "beta", "c.txt": "gamma"})
    e.scan()
    return e


def doc(env, name) -> str:
    return env.one("SELECT da.document_id FROM location l JOIN document_artifact da ON da.artifact_id = l.artifact_id WHERE l.relative_path = ? AND l.ended_at IS NULL", name)


def raises(code, fn, *args, **kwargs):
    with pytest.raises(KvError) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == code, caught.value.message
    return caught.value


# ------------------------------------------------------------------------------------------------ collections


def test_a_collection_holds_documents_and_documents_can_be_in_several(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    first = organize.new_collection(env.conn, "To Read", [a, b])
    second = organize.new_collection(env.conn, "Energetics", [a])
    assert first["added"] == 2 and second["added"] == 1
    assert organize.collections_of(env.conn, a) == ["Energetics", "To Read"] and organize.collections_of(env.conn, b) == ["To Read"]
    listed = {c["name"]: c["members"] for c in organize.list_collections(env.conn)}
    assert listed == {"Energetics": 1, "To Read": 2}
    _, members = organize.collection_members(env.conn, "to read")
    assert members == sorted([a, b])


def test_collection_names_compare_without_regard_to_case_or_spacing_and_cannot_be_empty(env):
    organize.new_collection(env.conn, "To Read")
    for clash in ("to read", "TO  READ", "  to\tread "):
        raises(ErrorCode.INVALID_ARGUMENTS, organize.new_collection, env.conn, clash)
    raises(ErrorCode.INVALID_ARGUMENTS, organize.new_collection, env.conn, "   ")
    raises(ErrorCode.INVALID_ARGUMENTS, organize.new_collection, env.conn, "x" * 201)
    assert env.one("SELECT COUNT(*) FROM collection") == 1


def test_adding_is_idempotent_removing_reports_what_it_did_and_the_documents_are_untouched(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    organize.new_collection(env.conn, "Papers", [a])
    first = organize.add_to_collection(env.conn, "papers", [a, b])
    assert first["added"] == 1 and first["already_members"] == 1  # a was already in; b is new
    again = organize.add_to_collection(env.conn, "papers", [a, b])
    assert again["added"] == 0 and again["already_members"] == 2
    assert organize.remove_from_collection(env.conn, "Papers", [b])["removed"] == 1
    assert organize.remove_from_collection(env.conn, "Papers", [b])["removed"] == 0
    assert env.one("SELECT COUNT(*) FROM document") == 3  # a collection holds documents; removing one removes the membership only


def test_a_collection_is_found_by_name_or_by_id_prefix_and_never_by_guess(env):
    made = organize.new_collection(env.conn, "Handbook Chapters")
    assert organize.find_collection(env.conn, "handbook chapters")["collection_id"] == made["collection_id"]
    assert organize.find_collection(env.conn, made["collection_id"][:10])["collection_id"] == made["collection_id"]
    raises(ErrorCode.NOT_FOUND, organize.find_collection, env.conn, "no such collection")
    raises(ErrorCode.NOT_FOUND, organize.find_collection, env.conn, "deadbeef0")


def test_a_retired_collection_is_gone_from_view_its_name_is_free_again_and_nothing_was_deleted(env):
    a = doc(env, "a.txt")
    organize.new_collection(env.conn, "Old", [a])
    organize.retire_collection(env.conn, "Old")
    assert organize.list_collections(env.conn) == [] and organize.collections_of(env.conn, a) == []
    assert env.one("SELECT COUNT(*) FROM collection") == 1 and env.one("SELECT COUNT(*) FROM collection_member") == 1  # still recorded
    organize.new_collection(env.conn, "Old")  # the name is reusable
    raises(ErrorCode.NOT_FOUND, organize.retire_collection, env.conn, "Missing")


def test_a_merged_away_document_cannot_be_added_and_does_not_count_as_a_member(env):
    a, b, c = doc(env, "a.txt"), doc(env, "b.txt"), doc(env, "c.txt")
    organize.new_collection(env.conn, "Papers", [b, c])
    relations.merge(env.conn, a, b)
    error = raises(ErrorCode.INVALID_ARGUMENTS, organize.add_to_collection, env.conn, "Papers", [b])
    assert "merged into" in error.message
    assert {c["name"]: c["members"] for c in organize.list_collections(env.conn)} == {"Papers": 2}  # c, and a (who inherited b's membership)
    assert set(organize.collection_members(env.conn, "Papers")[1]) == {a, c}


def test_nothing_in_a_collection_touches_a_file(env):
    before = tree_hashes(env.lib)
    mtimes = {p.name: p.stat().st_mtime_ns for p in env.lib.iterdir()}
    organize.new_collection(env.conn, "X", [doc(env, "a.txt")])
    organize.add_tags(env.conn, [doc(env, "a.txt")], ["t"])
    assert tree_hashes(env.lib) == before and {p.name: p.stat().st_mtime_ns for p in env.lib.iterdir()} == mtimes
    assert sorted(os.listdir(env.lib)) == ["a.txt", "b.txt", "c.txt"]


# ------------------------------------------------------------------------------------------------ tags


def test_tags_compare_without_regard_to_case_and_spacing_but_punctuation_matters(env):
    a = doc(env, "a.txt")
    result = organize.add_tags(env.conn, [a], ["Energetics", "energetics", " ENERGETICS ", "C", "C++"])
    assert result["added"] == 3 and organize.tags_of(env.conn, a) == ["C", "C++", "Energetics"]  # "C" and "C++" are different tags
    assert organize.add_tags(env.conn, [a], ["energetics"])["added"] == 0
    assert organize.remove_tags(env.conn, [a], ["ENERGETICS"])["removed"] == 1
    assert organize.tags_of(env.conn, a) == ["C", "C++"]


def test_tag_listing_counts_documents_and_ignores_merged_away_ones(env):
    a, b, c = doc(env, "a.txt"), doc(env, "b.txt"), doc(env, "c.txt")
    organize.add_tags(env.conn, [a, b, c], ["common"])
    organize.add_tags(env.conn, [b], ["rare"])
    assert {t["tag"]: t["documents"] for t in organize.list_tags(env.conn)} == {"common": 3, "rare": 1}
    relations.merge(env.conn, a, b)  # b is retired; its tags moved to a
    assert {t["tag"]: t["documents"] for t in organize.list_tags(env.conn)} == {"common": 2, "rare": 1}
    raises(ErrorCode.INVALID_ARGUMENTS, organize.add_tags, env.conn, [b], ["x"])
    raises(ErrorCode.NOT_FOUND, organize.add_tags, env.conn, ["f" * 32], ["x"])
    raises(ErrorCode.INVALID_ARGUMENTS, organize.add_tags, env.conn, [a], [""])


# ------------------------------------------------------------------------------------------------ saved searches


def test_a_saved_search_stores_the_parsed_query_and_runs_the_same_one_back(env):
    query = parse_query("solub* | dissolution tag:energetics year:2015-2020", near=["aqueous"], within=12, also=["298 K"], path_contains="papers/")
    saved = organize.save_search(env.conn, "Solubility work", query)
    row, back = organize.get_saved_search(env.conn, "solubility  WORK")
    assert back == query and saved["replaced"] is False
    assert row["language_version"] == QUERY_LANGUAGE_VERSION == 2
    assert [("tag", "energetics"), ("year", "2015-2020")] == list(back.filters)
    assert "result" not in row.keys() and "hits" not in row["query_json"]  # a query, never a list of results


def test_saving_over_a_name_needs_replace_and_replacing_keeps_one_live_search(env):
    organize.save_search(env.conn, "S", parse_query("alpha"))
    raises(ErrorCode.INVALID_ARGUMENTS, organize.save_search, env.conn, "s", parse_query("beta"))
    assert organize.save_search(env.conn, "s", parse_query("beta"), replace=True)["replaced"] is True
    assert organize.get_saved_search(env.conn, "S")[1].alternatives == ("beta",)
    assert len(organize.list_saved_searches(env.conn)) == 1


def test_a_saved_version_one_query_still_loads_and_a_newer_language_is_refused_not_guessed(env):
    organize.save_search(env.conn, "Old", parse_query("alpha"))
    env.conn.execute("UPDATE saved_search SET language_version = 1, query_json = ? WHERE name = 'Old'",
                     ('{"alternatives": ["alpha"], "near": [], "within": 30, "also": [], "path_contains": null, "language_version": 1}',))
    assert organize.get_saved_search(env.conn, "Old")[1] == Query(("alpha",), language_version=1)  # no filters field: still understood
    env.conn.execute("UPDATE saved_search SET language_version = 99 WHERE name = 'Old'")
    error = raises(ErrorCode.QUERY_INVALID, organize.get_saved_search, env.conn, "Old")
    assert "newer than this program" in error.message


def test_a_retired_saved_search_is_gone_from_view_and_remains_recorded(env):
    organize.save_search(env.conn, "S", parse_query("alpha"))
    organize.retire_saved_search(env.conn, "s")
    assert organize.list_saved_searches(env.conn) == [] and env.one("SELECT COUNT(*) FROM saved_search") == 1
    raises(ErrorCode.NOT_FOUND, organize.get_saved_search, env.conn, "S")
    raises(ErrorCode.NOT_FOUND, organize.retire_saved_search, env.conn, "S")


# ------------------------------------------------------------------------------------------------ system views


def test_every_view_is_a_query_and_a_document_leaves_it_when_the_reason_is_gone(env):
    a = doc(env, "a.txt")
    assert a in {i["document_id"] for i in organize.run_view(env.conn, None, "untagged")}
    organize.add_tags(env.conn, [a], ["t"])
    assert a not in {i["document_id"] for i in organize.run_view(env.conn, None, "untagged")}
    assert a in {i["document_id"] for i in organize.run_view(env.conn, None, "uncollected")}
    organize.new_collection(env.conn, "C", [a])
    assert a not in {i["document_id"] for i in organize.run_view(env.conn, None, "uncollected")}
    # Nothing about "inbox" was stored: the views have no tables of their own.
    assert env.one("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%inbox%' OR name LIKE '%view%'") == 0


def test_the_new_view_is_what_resolve_has_not_seen(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    assert {i["document_id"] for i in organize.run_view(env.conn, None, "new")} == {a, b, doc(env, "c.txt")}
    metadata.set_value(env.conn, a, "title", "A Title That Marks It As Seen By Someone")
    assert a not in {i["document_id"] for i in organize.run_view(env.conn, None, "new")}


def test_the_missing_view_follows_the_files_and_the_root(env):
    os.remove(env.lib / "b.txt")
    env.scan()
    missing = organize.run_view(env.conn, None, "missing")
    assert [i["document_id"] for i in missing] == [doc(env, "b.txt")] and "no reachable copy" in missing[0]["detail"]


def test_the_duplicates_view_and_exact_copy_groups_see_one_file_at_two_paths(tmp_path):
    e = make_env(tmp_path, {"one/same.txt": "identical bytes", "two/same.txt": "identical bytes", "other.txt": "different"})
    e.scan()
    groups = organize.exact_copy_groups(e.conn)
    assert len(groups) == 1 and groups[0]["copies"] == 2 and {p["path"] for p in groups[0]["paths"]} == {"one/same.txt", "two/same.txt"}
    assert [i["document_id"] for i in organize.run_view(e.conn, None, "duplicates")] == [groups[0]["document_id"]]
    assert e.one("SELECT COUNT(*) FROM document") == 2  # one item, two paths: not two documents


def test_the_inbox_lists_each_document_once_under_the_first_reason_in_the_plans_order(env):
    os.remove(env.lib / "c.txt")
    env.scan()
    inbox = organize.run_view(env.conn, None, "inbox")
    ids = [i["document_id"] for i in inbox]
    assert len(ids) == len(set(ids)) == 3  # once each, though every document is also "new"
    assert {i["document_id"]: i["reason"] for i in inbox}[doc(env, "c.txt")] == "missing"  # missing outranks new
    assert [i["reason"] for i in inbox] == sorted((i["reason"] for i in inbox), key=["unresolved", "ambiguous", "missing", "new"].index)


def test_an_unknown_view_names_the_ones_that_exist(env):
    error = raises(ErrorCode.NOT_FOUND, organize.run_view, env.conn, None, "nonsense")
    assert set(error.details["views"]) == set(organize.VIEWS)
