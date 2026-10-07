"""Planning: what is proposed, what is refused, and that making a plan changes nothing."""

from __future__ import annotations

import json

import pytest
from organizer_support import AUTHORS, TITLE, describe, document, library, plan_for, registered, tree

from knowledgevista.db.catalog import transaction
from knowledgevista.domain import plan as planmod
from knowledgevista.domain.candidate import CandidateSpec, evidence_key
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import metadata, organizer_plan, relations, roots


def by_old(plan):
    return {i.old_path: i for i in plan.items}


def refused(code, fn, *args, **kw):
    with pytest.raises(KvError) as caught:
        fn(*args, **kw)
    assert caught.value.code == code, caught.value.message
    return caught.value


def test_a_document_with_an_accepted_title_is_renamed_in_place_and_the_reason_says_where_the_name_came_from(tmp_path):
    env = library(tmp_path, {"papers/cm4c01978.pdf": "one", "papers/notes.txt": "two"})
    describe(env, "papers/cm4c01978.pdf")
    plan = plan_for(env)
    item = by_old(plan)["papers/cm4c01978.pdf"]
    assert (item.operation, item.status, item.new_path) == ("rename", "planned", f"papers/Examplar (2021) - {TITLE}.pdf")
    assert item.confidence == "high" and item.risk == "low" and item.source == "manual" and "accepted title" in item.reason
    assert {k: item.snapshot[k] for k in ("title", "year", "layout")} == {"title": TITLE, "year": "2021", "layout": "in_place"}
    assert json.loads(item.snapshot["authors"]) == [{"family": "Examplar", "given": "A.", "name": None}]
    assert item.expected_sha256 == item.artifact_id and item.expected_size == 3


def test_a_document_without_an_accepted_title_is_left_alone_and_a_proposal_never_names_a_file(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    doc = document(env, "a.pdf")
    with transaction(env.conn):
        metadata.upsert_candidate(env.conn, doc, env.one("SELECT artifact_id FROM document_artifact WHERE document_id = ?", doc),
                                  CandidateSpec("title", "A Proposed Title That Nobody Accepted", "observed", "layout_title", evidence_key("t")), None, "t")
    item = by_old(plan_for(env))["a.pdf"]
    assert (item.operation, item.status, item.new_path, item.reason) == ("unchanged", "unchanged", "a.pdf", "no accepted title: left as it is")


def test_a_file_already_named_as_the_policy_would_name_it_is_unchanged(tmp_path):
    env = library(tmp_path, {f"Examplar (2021) - {TITLE}.pdf": "one"})
    describe(env, f"Examplar (2021) - {TITLE}.pdf")
    item = plan_for(env).items[0]
    assert (item.operation, item.status, item.reason) == ("unchanged", "unchanged", "already named as the policy would name it")


def test_the_by_year_layout_moves_files_into_year_folders_and_says_move_or_rename_plus_move(tmp_path):
    env = library(tmp_path, {"papers/x.pdf": "one", "papers/y.pdf": "two"})
    describe(env, "papers/x.pdf")
    describe(env, "papers/y.pdf", title="Another Title For The Second", year=None)
    items = by_old(plan_for(env, layout="by_year"))
    assert (items["papers/x.pdf"].operation, items["papers/x.pdf"].new_path) == ("rename+move", f"2021/Examplar (2021) - {TITLE}.pdf")
    assert items["papers/y.pdf"].new_path == "unknown-year/Examplar - Another Title For The Second.pdf" and items["papers/y.pdf"].risk == "medium"


def test_only_roots_that_allow_organizing_are_planned_and_naming_a_root_that_does_not_is_an_error_that_says_how(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"}, allow=False)
    describe(env, "a.pdf")
    error = refused(ErrorCode.ROOT_NOT_ORGANIZABLE, organizer_plan.organizable_roots, env.conn, None)
    assert "kv root allow-organize" in error.message
    label = roots.list_roots(env.conn)[0].label
    assert "kv root allow-organize" in refused(ErrorCode.ROOT_NOT_ORGANIZABLE, organizer_plan.organizable_roots, env.conn, [label]).message
    env.conn.execute("UPDATE root SET allow_organize = 1")
    assert [r.label for r in organizer_plan.organizable_roots(env.conn, None)] == [label] == [r.label for r in organizer_plan.organizable_roots(env.conn, [label])]
    env.conn.execute("UPDATE root SET status = 'unavailable'")
    refused(ErrorCode.ROOT_UNAVAILABLE, organizer_plan.organizable_roots, env.conn, [label])
    refused(ErrorCode.ROOT_NOT_ORGANIZABLE, organizer_plan.organizable_roots, env.conn, None)  # an offline root is not planned for


def test_two_documents_that_would_share_a_name_both_get_their_artifact_tag(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "b.pdf": "two"})
    describe(env, "a.pdf")
    describe(env, "b.pdf")
    items = list(plan_for(env).items)
    news = sorted(i.new_path for i in items)
    assert len(set(n.lower() for n in news)) == 2 and all("[" in n for n in news)
    assert all(i.artifact_id[:8] in i.new_path for i in items)


def test_a_destination_occupied_by_an_unrelated_file_is_blocked_with_the_reason_never_overwritten(tmp_path):
    taken = f"Examplar (2021) - {TITLE}.pdf"
    env = library(tmp_path, {"a.pdf": "one", taken: "somebody else's file"})
    describe(env, "a.pdf")
    item = by_old(plan_for(env))["a.pdf"]
    assert item.status == "blocked" and item.new_path == taken and "already at that path" in item.blocked_reason


def test_a_file_on_disk_the_catalog_does_not_know_yet_also_blocks(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    (env.lib / f"Examplar (2021) - {TITLE}.pdf").write_text("arrived after the scan")
    item = by_old(plan_for(env))["a.pdf"]
    assert item.status == "blocked" and "on disk" in item.blocked_reason
    assert by_old(plan_for(env, check_disk=False))["a.pdf"].status == "planned"  # the catalog alone cannot see it


def test_a_destination_held_by_a_file_this_plan_moves_away_is_not_a_block(tmp_path):
    first = f"Examplar (2021) - {TITLE}.pdf"
    env = library(tmp_path, {first: "one", "second.pdf": "two"})
    describe(env, first, title="A Different Title For The First", year="2020")  # the first file will leave the name the second wants
    describe(env, "second.pdf")
    items = by_old(plan_for(env))
    assert items["second.pdf"].status == "planned" and items["second.pdf"].new_path == first and items[first].status == "planned"


def test_a_merged_away_document_has_no_files_of_its_own_and_a_documents_filter_narrows_the_plan(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "b.pdf": "two", "c.pdf": "three"})
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        describe(env, name, title=f"Title Of {name} Here")
    relations.merge(env.conn, document(env, "a.pdf"), document(env, "b.pdf"))
    plan = plan_for(env)
    assert {i.old_path for i in plan.items} == {"a.pdf", "b.pdf", "c.pdf"}  # b is an alternate copy inside a's document, still a file to name
    only = plan_for(env, documents={document(env, "c.pdf")})
    assert [i.old_path for i in only.items] == ["c.pdf"]


def test_exact_copies_at_two_paths_are_two_items_with_distinct_names(tmp_path):
    env = library(tmp_path, {"one/same.pdf": "twin", "two/same.pdf": "twin"})
    describe(env, "one/same.pdf")
    items = by_old(plan_for(env))
    assert items["one/same.pdf"].new_path.startswith("one/") and items["two/same.pdf"].new_path.startswith("two/")
    assert items["one/same.pdf"].new_path.rpartition("/")[2] == items["two/same.pdf"].new_path.rpartition("/")[2]  # same name in different folders is fine


def test_confidence_and_risk_follow_who_accepted_the_title(tmp_path):
    env = library(tmp_path, {"stated.pdf": "one", "ruled.pdf": "two"})
    describe(env, "stated.pdf")
    describe(env, "ruled.pdf", title="Title Accepted By A Rule Not A Person")
    env.conn.execute("UPDATE metadata_value SET accepted_by = 'rule:safe_batch_v1', source = 'layout_title' WHERE document_id = ? AND field = 'title'", (document(env, "ruled.pdf"),))
    items = by_old(plan_for(env))
    assert (items["stated.pdf"].confidence, items["stated.pdf"].risk) == ("high", "low")
    assert (items["ruled.pdf"].confidence, items["ruled.pdf"].risk, items["ruled.pdf"].source) == ("medium", "medium", "layout_title")
    moved = by_old(plan_for(env, layout="by_year"))
    assert (moved["stated.pdf"].risk, moved["ruled.pdf"].risk) == ("medium", "high")


def test_a_case_only_difference_is_a_rename_flagged_as_such(tmp_path):
    name = f"examplar (2021) - {TITLE}.pdf"
    env = library(tmp_path, {name: "one"})
    describe(env, name)
    item = plan_for(env).items[0]
    assert item.operation == "rename" and item.new_path == f"Examplar (2021) - {TITLE}.pdf" and "case or Unicode form only" in item.reason and item.status == "planned"


def test_the_same_catalog_and_files_give_the_same_plan(tmp_path):
    env = library(tmp_path, {f"d{n}/f{n}.pdf": f"content {n}" for n in range(6)})
    for n in range(6):
        describe(env, f"d{n}/f{n}.pdf", title=f"Distinct Title Number {n} Of The Series", year=str(2000 + n))
    a, b = plan_for(env), plan_for(env)
    assert [i.as_dict() for i in a.items] == [i.as_dict() for i in b.items] and a.options == b.options


def test_planning_changes_neither_the_library_nor_the_catalog(tmp_path):
    env = library(tmp_path, {"a.pdf": "one", "sub/b.pdf": "two"})
    describe(env, "a.pdf")
    before, revision, rows = tree(env.lib), env.revision(), env.one("SELECT COUNT(*) FROM location")
    plan_for(env)
    plan_for(env, layout="by_year")
    assert tree(env.lib) == before and env.revision() == revision and env.one("SELECT COUNT(*) FROM location") == rows
    assert env.one("SELECT COUNT(*) FROM plan_registry") == 0 and env.one("SELECT COUNT(*) FROM operation") == 0


def test_a_written_plan_is_registered_frozen_and_never_written_into_the_library(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    describe(env, "a.pdf")
    plan = plan_for(env)
    path = registered(env, tmp_path, plan)
    assert path.is_file() and planmod.loads(path.read_text(encoding="utf-8")).plan_id == plan.plan_id
    row = env.rows("SELECT * FROM plan_registry")[0]
    assert (row["plan_id"], row["item_count"], row["planned_count"], row["naming_policy"]) == (plan.plan_id, 1, 1, "naming-1") and row["plan_hash"] == planmod.hash_of(plan.body())
    refused(ErrorCode.DESTINATION_EXISTS, organizer_plan.write_plan, env.conn, plan_for(env), path)  # a plan is frozen: a new plan is a new file
    inside = refused(ErrorCode.INVALID_ARGUMENTS, organizer_plan.write_plan, env.conn, plan_for(env), env.lib / "plan.json")
    assert "outside the library" in inside.message and not (env.lib / "plan.json").exists()


def test_the_layout_must_be_a_known_one(tmp_path):
    env = library(tmp_path, {"a.pdf": "one"})
    refused(ErrorCode.INVALID_ARGUMENTS, organizer_plan.build_plan, env.conn, roots=roots.list_roots(env.conn), layout="by_colour")


def test_hostile_titles_never_produce_an_unusable_destination(tmp_path):
    env = library(tmp_path, {f"f{n}.pdf": f"content {n}" for n in range(6)})
    hostile = ["CON", "a:b/c\\d", "x" * 400, "trailing.", "‮evil.fdp", "NUL.v2"]
    for n, title in enumerate(hostile):
        metadata.set_value(env.conn, document(env, f"f{n}.pdf"), "title", title + " padding to be a title")
    plan = plan_for(env, layout="by_year")
    assert all(i.status in ("planned", "unchanged") for i in plan.items)
    planmod.validate(json.loads(planmod.dumps(plan)))  # every destination passes the validator
