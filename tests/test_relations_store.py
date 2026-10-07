"""Relations, proposals, merge and split: the rules that keep identity safe.

The four shapes the plan says must stay separate each have a test that checks WHERE the relation is recorded (artifact level
or document level) and what it does and does not mean. Every refusal has a test, and every "nothing is deleted" has an oracle:
the rows are counted before and after.
"""

from __future__ import annotations

import json

import pytest
from support import make_env

from knowledgevista.db.catalog import transaction
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import metadata, organize, relations
from knowledgevista.services.relations import RelationSpec


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path, {"a.txt": "alpha", "b.txt": "beta", "c.txt": "gamma", "d.txt": "delta"})
    e.scan()
    return e


def doc(env, name) -> str:
    return env.one("SELECT da.document_id FROM location l JOIN document_artifact da ON da.artifact_id = l.artifact_id WHERE l.relative_path = ? AND l.ended_at IS NULL", name)


def art(env, name) -> str:
    return env.one("SELECT artifact_id FROM location WHERE relative_path = ? AND ended_at IS NULL", name)


def propose(env, kind, source, target="", *, level="document", key="k", members=None, confidence="high", evidence=None):
    with transaction(env.conn):
        return relations.upsert_candidate(env.conn, RelationSpec(level, kind, source, target, key, evidence or {"why": 1}, confidence, members), None, "t-1")


def cand(env, kind) -> str:
    return env.one("SELECT candidate_id FROM relation_candidate WHERE kind = ?", kind)


def count(env, table) -> int:
    return env.one(f"SELECT COUNT(*) FROM {table}")


# ------------------------------------------------------------------------------------- the four shapes stay separate


def test_two_artifacts_of_one_document_is_a_merge_not_a_relation(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    artifacts_before = count(env, "artifact")
    result = relations.merge(env.conn, a, b, reason="same article, re-downloaded")
    assert result["artifacts_moved"] == [art(env, "b.txt")] and count(env, "artifact") == artifacts_before  # no artifact id changed or was lost
    rows = env.rows("SELECT * FROM document_artifact WHERE document_id = ? ORDER BY canonical DESC", a)
    assert [r["artifact_id"] for r in rows] == [art(env, "a.txt"), art(env, "b.txt")]
    assert (rows[0]["canonical"], rows[1]["canonical"], rows[1]["role"]) == (1, 0, "alternate_copy")
    assert count(env, "document_relation") == 0 and count(env, "artifact_relation") == 0  # a merge records no relation


def test_two_documents_version_of_is_a_document_relation_and_the_artifacts_stay_apart(env):
    relation_id, created = relations.add_relation_by_user(env.conn, "document", "version_of", doc(env, "a.txt"), doc(env, "b.txt"), note="preprint -> published")
    assert created
    row = env.rows("SELECT * FROM document_relation WHERE relation_id = ?", relation_id)[0]
    assert (row["kind"], row["source_id"], row["target_id"], row["accepted_by"]) == ("version_of", doc(env, "a.txt"), doc(env, "b.txt"), "user")
    assert count(env, "artifact_relation") == 0 and doc(env, "a.txt") != doc(env, "b.txt")  # still two documents, still two artifacts


def test_two_documents_supplement_of_points_from_the_supplement_to_the_paper(env):
    relation_id, _ = relations.add_relation_by_user(env.conn, "document", "supplement_of", doc(env, "b.txt"), doc(env, "a.txt"))
    row = env.rows("SELECT * FROM document_relation WHERE relation_id = ?", relation_id)[0]
    assert (row["source_id"], row["target_id"]) == (doc(env, "b.txt"), doc(env, "a.txt"))
    explained = relations.relations_of_document(env.conn, doc(env, "a.txt"))["document"][0]
    assert (explained["kind"], explained["direction"], explained["other_document"]) == ("supplement_of", "incoming", doc(env, "b.txt"))


def test_an_artifact_derivative_of_an_artifact_is_an_artifact_relation_and_no_document_changes(env):
    documents_before = count(env, "document")
    relation_id, _ = relations.add_relation_by_user(env.conn, "artifact", "derivative_of", art(env, "b.txt"), art(env, "a.txt"), note="an OCR'd copy")
    assert env.rows("SELECT kind, source_id, target_id FROM artifact_relation WHERE relation_id = ?", relation_id)[0][:] == ("derivative_of", art(env, "b.txt"), art(env, "a.txt"))
    assert count(env, "document_relation") == 0 and count(env, "document") == documents_before
    assert relations.relations_of_document(env.conn, doc(env, "a.txt"))["artifact"][0]["direction"] == "incoming"


@pytest.mark.parametrize("level, kind", [("document", "derivative_of"), ("document", "equivalent_to"), ("artifact", "supplement_of"), ("artifact", "part_of"), ("group", "part_of"), ("document", "nonsense")])
def test_a_kind_at_the_wrong_level_is_refused(env, level, kind):
    with pytest.raises(KvError) as caught:
        relations.add_relation_by_user(env.conn, level, kind, doc(env, "a.txt"), doc(env, "b.txt"))
    assert caught.value.code == ErrorCode.INVALID_ARGUMENTS
    assert count(env, "document_relation") == 0 and count(env, "artifact_relation") == 0


# ------------------------------------------------------------------------------------- relation rules


def test_a_relation_to_itself_and_to_nothing_is_refused(env):
    a = doc(env, "a.txt")
    with pytest.raises(KvError) as caught:
        relations.add_relation_by_user(env.conn, "document", "related_to", a, a)
    assert caught.value.code == ErrorCode.INVALID_ARGUMENTS
    with pytest.raises(KvError) as caught:
        relations.add_relation_by_user(env.conn, "document", "related_to", a, "f" * 32)
    assert caught.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(KvError) as caught:
        relations.add_relation_by_user(env.conn, "artifact", "duplicate_of", art(env, "a.txt"), "f" * 64)
    assert caught.value.code == ErrorCode.NOT_FOUND


def test_a_hierarchy_that_would_loop_is_refused_directly_and_through_a_chain(env):
    a, b, c = doc(env, "a.txt"), doc(env, "b.txt"), doc(env, "c.txt")
    relations.add_relation_by_user(env.conn, "document", "part_of", a, b)
    relations.add_relation_by_user(env.conn, "document", "part_of", b, c)
    for source, target in ((b, a), (c, a), (c, b)):
        with pytest.raises(KvError) as caught:
            relations.add_relation_by_user(env.conn, "document", "part_of", source, target)
        assert caught.value.code == ErrorCode.INVALID_ARGUMENTS and "ancestor" in caught.value.message
    relations.add_relation_by_user(env.conn, "document", "related_to", b, a)  # related_to is not a hierarchy: no loop to fear


def test_a_symmetric_relation_is_one_row_whichever_end_is_named_first(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    first, created = relations.add_relation_by_user(env.conn, "document", "related_to", a, b)
    again, created_again = relations.add_relation_by_user(env.conn, "document", "related_to", b, a)
    assert created and not created_again and first == again and count(env, "document_relation") == 1
    relations.add_relation_by_user(env.conn, "document", "supplement_of", a, b)
    with pytest.raises(KvError) as caught:  # direction matters for supplement_of, and the opposite direction cannot also hold
        relations.add_relation_by_user(env.conn, "document", "supplement_of", b, a)
    assert caught.value.code == ErrorCode.INVALID_ARGUMENTS


def test_a_relation_is_retracted_not_deleted_and_can_be_made_again(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    relation_id, _ = relations.add_relation_by_user(env.conn, "document", "version_of", a, b)
    revision = env.revision()
    assert relations.retract_relation(env.conn, relation_id[:10]) is True and env.revision() == revision + 1
    assert relations.retract_relation(env.conn, relation_id) is False  # already retracted
    row = env.rows("SELECT * FROM document_relation WHERE relation_id = ?", relation_id)[0]
    assert row["retracted_at"] is not None and row["retracted_by"] == "user"  # still recorded
    assert relations.relations_of_document(env.conn, a)["document"] == []
    new_id, created = relations.add_relation_by_user(env.conn, "document", "version_of", a, b)
    assert created and new_id != relation_id and count(env, "document_relation") == 2
    with pytest.raises(KvError) as caught:
        relations.retract_relation(env.conn, "0" * 12)
    assert caught.value.code == ErrorCode.NOT_FOUND


# ------------------------------------------------------------------------------------- proposals


def test_a_proposal_is_one_per_evidence_and_a_second_finding_writes_nothing(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    assert propose(env, "version_of", a, b, key="title") == "new"
    snapshot = [tuple(r) for r in env.rows("SELECT * FROM relation_candidate")]
    assert propose(env, "version_of", a, b, key="title") == "unchanged"
    assert [tuple(r) for r in env.rows("SELECT * FROM relation_candidate")] == snapshot
    assert propose(env, "version_of", a, b, key="other evidence") == "new" and count(env, "relation_candidate") == 2
    assert propose(env, "version_of", a, b, key="title", evidence={"why": 2}) == "updated"


def test_a_symmetric_proposal_is_the_same_whichever_way_it_is_found(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    propose(env, "same_document", a, b)
    assert propose(env, "same_document", b, a) == "unchanged" and count(env, "relation_candidate") == 1


def test_a_decision_is_not_reopened_and_stale_proposals_are_revived_when_found_again(env):
    a, b, c = doc(env, "a.txt"), doc(env, "b.txt"), doc(env, "c.txt")
    propose(env, "related_to", a, b)
    propose(env, "related_to", a, c)
    relations.reject_candidate(env.conn, env.one("SELECT candidate_id FROM relation_candidate WHERE target_id = ?", b if b > a else a))
    with transaction(env.conn):
        stale = relations.mark_stale(env.conn, set(), None)
    assert stale == 1  # the rejected one is decided and untouched
    assert propose(env, "related_to", a, c) == "revived"
    assert propose(env, "related_to", a, b) == "decided"
    assert {r["status"] for r in env.rows("SELECT status FROM relation_candidate")} == {"proposed", "rejected"}


def test_accepting_a_document_proposal_records_the_relation_with_where_it_came_from(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    propose(env, "supplement_of", b, a, confidence="high")
    result = relations.accept_candidate(env.conn, cand(env, "supplement_of"), actor="user")
    assert result["effect"] == "relation"
    row = env.rows("SELECT * FROM document_relation WHERE relation_id = ?", result["relation_id"])[0]
    assert (row["kind"], row["accepted_from_candidate"], row["accepted_by"]) == ("supplement_of", cand(env, "supplement_of"), "user")
    assert env.one("SELECT status FROM relation_candidate") == "accepted"
    assert relations.accept_candidate(env.conn, cand(env, "supplement_of"))["effect"] == "already_accepted" and count(env, "document_relation") == 1


def test_accepting_an_artifact_proposal_records_an_artifact_relation(env):
    propose(env, "equivalent_to", art(env, "a.txt"), art(env, "b.txt"), level="artifact", key="identical-text", confidence="exact")
    relations.accept_candidate(env.conn, cand(env, "equivalent_to"), actor="user")
    assert count(env, "artifact_relation") == 1 and count(env, "document_relation") == 0 and count(env, "document") == 4


def test_accepting_same_document_merges_and_the_survivor_is_the_one_with_more_metadata_unless_told(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    metadata.set_value(env.conn, b, "title", "A Title That Only B Has Accepted")
    propose(env, "same_document", a, b, key="shared-doi")
    result = relations.accept_candidate(env.conn, cand(env, "same_document"), actor="user")
    assert result["kept"] == b and result["absorbed"] == a  # b has the accepted value, so b survives
    assert env.one("SELECT merged_into FROM document WHERE document_id = ?", a) == b
    c, d = doc(env, "c.txt"), doc(env, "d.txt")
    propose(env, "same_document", c, d, key="shared-doi")
    told = relations.accept_candidate(env.conn, env.one("SELECT candidate_id FROM relation_candidate WHERE status = 'proposed'"), actor="user", keep=d)
    assert told["kept"] == d
    propose(env, "same_document", doc(env, "b.txt"), c, key="x")  # b survives; c was absorbed already, but this one is new evidence for a new pair
    with pytest.raises(KvError):
        relations.accept_candidate(env.conn, env.one("SELECT candidate_id FROM relation_candidate WHERE status = 'proposed'"), keep="e" * 32)


def test_a_merge_repoints_open_proposals_to_the_survivor_because_their_evidence_still_holds(env):
    a, b, c = doc(env, "a.txt"), doc(env, "b.txt"), doc(env, "c.txt")
    propose(env, "supplement_of", c, a)
    relations.merge(env.conn, b, a)  # a is retired; b survives
    row = env.rows("SELECT * FROM relation_candidate")[0]
    assert (row["status"], row["source_id"], row["target_id"]) == ("proposed", c, b)  # it now says what it always meant, about the survivor
    relations.accept_candidate(env.conn, cand(env, "supplement_of"))
    assert env.rows("SELECT source_id, target_id FROM document_relation")[0][:] == (c, b)


def test_a_proposal_that_only_said_these_two_are_one_is_accepted_by_the_merge_itself(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    propose(env, "same_document", a, b, key="shared-doi")
    propose(env, "same_document", a, b, key="identical-text")  # a second piece of evidence for the same pair
    relations.merge(env.conn, a, b)
    assert {r["status"] for r in env.rows("SELECT status FROM relation_candidate")} == {"accepted"}
    assert {r["decided_by"] for r in env.rows("SELECT decided_by FROM relation_candidate")} == {"merge:user"}


def test_a_group_of_copies_can_be_merged_one_proposal_at_a_time_whichever_copy_survives_each_step(tmp_path):
    e = make_env(tmp_path, {f"copy{i}.txt": f"bytes {i}" for i in range(8)})
    e.scan()
    ids = [doc(e, f"copy{i}.txt") for i in range(8)]
    first = min(ids)
    metadata.set_value(e.conn, ids[3], "title", "The Copy That Has Accepted Metadata So It Wins")  # the survivor changes mid-way
    for other in sorted(set(ids) - {first}):  # the detector's star: the first member against each of the others
        propose(e, "same_document", first, other, key="identical-text", confidence="exact")
    artifacts_before = count(e, "artifact")
    for candidate_id in [r["candidate_id"] for r in e.rows("SELECT candidate_id FROM relation_candidate ORDER BY candidate_id")]:
        if e.one("SELECT status FROM relation_candidate WHERE candidate_id = ?", candidate_id) == "proposed":
            relations.accept_candidate(e.conn, candidate_id)
    live = e.rows("SELECT document_id FROM document WHERE retired_at IS NULL AND document_id IN (%s)" % ",".join("?" * 8), *ids)
    assert len(live) == 1 and live[0]["document_id"] == ids[3]
    assert e.one("SELECT COUNT(*) FROM document_artifact WHERE document_id = ?", ids[3]) == 8 and count(e, "artifact") == artifacts_before
    assert {r["status"] for r in e.rows("SELECT status FROM relation_candidate")} == {"accepted"}


def test_a_stale_proposal_is_refused_with_the_way_to_refresh_it(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    propose(env, "related_to", a, b)
    with transaction(env.conn):
        relations.mark_stale(env.conn, set(), None)
    with pytest.raises(KvError) as caught:
        relations.accept_candidate(env.conn, cand(env, "related_to"))
    assert caught.value.code == ErrorCode.INVALID_ARGUMENTS and "kv relate" in caught.value.message


def test_a_collection_proposal_makes_the_collection_and_reuses_one_of_that_name(env):
    members = [doc(env, n) for n in ("a.txt", "b.txt", "c.txt")]
    propose(env, "collection", "container:book", level="group", key="container:book", members=members,
            evidence={"name": "Handbook of Imaginary Things", "parent_doi": "10.5555/book"})
    result = relations.accept_candidate(env.conn, cand(env, "collection"))
    assert result["effect"] == "collection" and result["added"] == 3 and result["reused_existing"] is False
    collection = env.rows("SELECT * FROM collection")[0]
    assert (collection["name"], collection["source_doi"]) == ("Handbook of Imaginary Things", "10.5555/book")
    propose(env, "collection", "container:book2", level="group", key="container:book2", members=[members[0], doc(env, "d.txt")],
            evidence={"name": "handbook of imaginary things"})
    again = relations.accept_candidate(env.conn, env.one("SELECT candidate_id FROM relation_candidate WHERE status = 'proposed'"))
    assert again["reused_existing"] is True and again["added"] == 1 and count(env, "collection") == 1


def test_reject_keeps_the_proposal_and_an_accepted_one_cannot_be_rejected(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    propose(env, "related_to", a, b)
    assert relations.reject_candidate(env.conn, cand(env, "related_to")) is True
    assert relations.reject_candidate(env.conn, cand(env, "related_to")) is False and count(env, "relation_candidate") == 1
    propose(env, "version_of", a, b)
    relations.accept_candidate(env.conn, cand(env, "version_of"))
    with pytest.raises(KvError) as caught:
        relations.reject_candidate(env.conn, cand(env, "version_of"))
    assert caught.value.code == ErrorCode.INVALID_ARGUMENTS


def test_proposal_ids_resolve_by_prefix_and_never_by_guess(env):
    a, b, c = doc(env, "a.txt"), doc(env, "b.txt"), doc(env, "c.txt")
    propose(env, "related_to", a, b)
    propose(env, "related_to", a, c)
    first = env.one("SELECT candidate_id FROM relation_candidate LIMIT 1")
    assert relations.resolve_candidate_id(env.conn, first[:10]) == first
    for bad, code in (("short", ErrorCode.INVALID_ARGUMENTS), ("zzzzzzzzzz", ErrorCode.INVALID_ARGUMENTS), ("0" * 12, ErrorCode.NOT_FOUND)):
        with pytest.raises(KvError) as caught:
            relations.resolve_candidate_id(env.conn, bad)
        assert caught.value.code == code


# ------------------------------------------------------------------------------------- merge


def test_a_merge_deletes_nothing_and_leaves_the_history_of_both_sides(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    before = {t: count(env, t) for t in ("document", "artifact", "location", "document_artifact")}
    relations.merge(env.conn, a, b, reason="test")
    assert {t: count(env, t) for t in before} == before  # not one row gone
    retired = env.rows("SELECT * FROM document WHERE document_id = ?", b)[0]
    assert retired["retired_at"] is not None and retired["merged_into"] == a
    events = {r["document_id"]: (r["event"], json.loads(r["detail"])) for r in env.rows("SELECT * FROM document_event")}
    assert events[b][0] == "merged_into" and events[b][1]["into"] == a and events[b][1]["artifacts"] == [art(env, "b.txt")]
    assert events[a][0] == "absorbed" and events[a][1]["from"] == b and events[a][1]["reason"] == "test"


def test_a_merge_carries_over_what_the_survivor_lacks_and_never_overrides_what_it_has(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    metadata.set_value(env.conn, a, "title", "The Survivors Own Accepted Title Here")
    metadata.set_value(env.conn, b, "title", "A Different Title On The Absorbed One")
    metadata.set_value(env.conn, b, "year", "2020")
    organize.add_tags(env.conn, [b], ["energetics"])
    organize.new_collection(env.conn, "Papers", [b])
    result = relations.merge(env.conn, a, b)
    values = {f: v["value"] for f, v in metadata.get_values(env.conn, a).items()}
    assert values == {"title": "The Survivors Own Accepted Title Here", "year": "2020"} and result["fields_carried_over"] == ["year"]
    assert organize.tags_of(env.conn, a) == ["energetics"] and organize.collections_of(env.conn, a) == ["Papers"]
    assert metadata.history(env.conn, a)[-1]["reason"].startswith("carried over from the merged document")
    assert metadata.get_values(env.conn, b)["title"]["value"].startswith("A Different Title")  # the absorbed document keeps its own record


def test_a_merge_repoints_relations_and_folds_the_ones_that_would_relate_a_document_to_itself(env):
    a, b, c = doc(env, "a.txt"), doc(env, "b.txt"), doc(env, "c.txt")
    pointed, _ = relations.add_relation_by_user(env.conn, "document", "supplement_of", b, c)
    between, _ = relations.add_relation_by_user(env.conn, "document", "related_to", a, b)
    relations.merge(env.conn, a, b)
    assert env.rows("SELECT source_id, target_id, retracted_at FROM document_relation WHERE relation_id = ?", pointed)[0][:2] == (a, c)
    folded = env.rows("SELECT retracted_at, note FROM document_relation WHERE relation_id = ?", between)[0]
    assert folded["retracted_at"] is not None and "folded by a merge" in folded["note"]  # a and b are one now: no relation to itself


def test_merge_refusals(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    with pytest.raises(KvError) as caught:
        relations.merge(env.conn, a, a)
    assert caught.value.code == ErrorCode.INVALID_ARGUMENTS
    with pytest.raises(KvError) as caught:
        relations.merge(env.conn, a, "f" * 32)
    assert caught.value.code == ErrorCode.NOT_FOUND
    relations.merge(env.conn, a, b)
    for keep, absorb in ((a, b), (b, a)):
        with pytest.raises(KvError) as caught:
            relations.merge(env.conn, keep, absorb)
        assert caught.value.code == ErrorCode.INVALID_ARGUMENTS


def test_the_canonical_artifact_can_be_chosen_and_there_is_always_exactly_one(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    relations.merge(env.conn, a, b)
    assert relations.set_canonical(env.conn, a, art(env, "b.txt"), reason="the cleaner copy") is True
    assert relations.set_canonical(env.conn, a, art(env, "b.txt")) is False
    assert env.one("SELECT COUNT(*) FROM document_artifact WHERE document_id = ? AND canonical = 1", a) == 1
    assert env.one("SELECT artifact_id FROM document_artifact WHERE document_id = ? AND canonical = 1", a) == art(env, "b.txt")
    with pytest.raises(KvError) as caught:
        relations.set_canonical(env.conn, a, art(env, "c.txt"))
    assert caught.value.code == ErrorCode.NOT_FOUND


def test_a_merge_moves_the_revision_only_when_something_changed(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    revision = env.revision()
    relations.merge(env.conn, a, b)
    assert env.revision() == revision + 1


# ------------------------------------------------------------------------------------- split


def test_split_after_merge_revives_the_original_document_under_its_original_id(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    metadata.set_value(env.conn, b, "title", "B Had This Title Before The Merge Happened")
    relations.merge(env.conn, a, b)
    result = relations.split_document(env.conn, a, art(env, "b.txt"))
    assert result["document_id"] == b and result["revived"] is True
    assert doc(env, "b.txt") == b and doc(env, "a.txt") == a
    revived = env.rows("SELECT retired_at, merged_into FROM document WHERE document_id = ?", b)[0]
    assert revived["retired_at"] is None and revived["merged_into"] is None
    assert metadata.get_values(env.conn, b)["title"]["value"].startswith("B Had This Title")  # its record was never lost
    assert env.one("SELECT canonical FROM document_artifact WHERE artifact_id = ?", art(env, "b.txt")) == 1
    kinds = [r["event"] for r in env.rows("SELECT event FROM document_event ORDER BY event_id")]
    assert kinds == ["merged_into", "absorbed", "revived", "split_off"]  # the whole story is there


def test_split_makes_a_new_document_when_the_artifact_does_not_map_back_to_a_retired_one(env):
    a, c, d = doc(env, "a.txt"), doc(env, "c.txt"), doc(env, "d.txt")
    relations.merge(env.conn, c, d)  # c holds c.txt and d.txt; d is retired
    relations.merge(env.conn, a, c)  # a holds all three; c is retired, and its event says it carried TWO artifacts
    documents = count(env, "document")
    result = relations.split_document(env.conn, a, art(env, "c.txt"))
    assert result["revived"] is False and result["document_id"] not in (a, c, d) and count(env, "document") == documents + 1
    assert doc(env, "c.txt") == result["document_id"] and env.one("SELECT retired_at IS NOT NULL FROM document WHERE document_id = ?", c) == 1  # c stays retired
    assert [r["event"] for r in env.rows("SELECT event FROM document_event WHERE document_id = ? ORDER BY event_id", result["document_id"])] == ["created_by_split"]


def test_split_revives_a_document_merged_away_before_its_survivor_was_itself_merged(env):
    a, c, d = doc(env, "a.txt"), doc(env, "c.txt"), doc(env, "d.txt")
    relations.merge(env.conn, c, d)
    relations.merge(env.conn, a, c)  # d now names a, the live survivor
    result = relations.split_document(env.conn, a, art(env, "d.txt"))
    assert result["revived"] is True and result["document_id"] == d


def test_split_requires_two_artifacts_and_the_artifact_must_belong(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    with pytest.raises(KvError) as caught:
        relations.split_document(env.conn, a, art(env, "a.txt"))
    assert caught.value.code == ErrorCode.INVALID_ARGUMENTS and "only one artifact" in caught.value.message
    relations.merge(env.conn, a, b)
    with pytest.raises(KvError) as caught:
        relations.split_document(env.conn, a, art(env, "c.txt"))
    assert caught.value.code == ErrorCode.NOT_FOUND


def test_splitting_off_the_canonical_artifact_promotes_another(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    relations.merge(env.conn, a, b)
    relations.split_document(env.conn, a, art(env, "a.txt"))  # a's own canonical goes out; a becomes a new document, b's artifact stays
    remaining = env.rows("SELECT artifact_id, canonical FROM document_artifact WHERE document_id = ?", a)
    assert [(r["artifact_id"], r["canonical"]) for r in remaining] == [(art(env, "b.txt"), 1)]
    new_doc = doc(env, "a.txt")
    assert new_doc != a and env.one("SELECT COUNT(*) FROM document_artifact WHERE document_id = ? AND canonical = 1", new_doc) == 1


def test_merge_then_split_restores_every_artifact_id_and_leaves_both_documents_whole(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    owner_before = {r["artifact_id"]: r["document_id"] for r in env.rows("SELECT artifact_id, document_id FROM document_artifact")}
    relations.merge(env.conn, a, b)
    relations.split_document(env.conn, a, art(env, "b.txt"))
    owner_after = {r["artifact_id"]: r["document_id"] for r in env.rows("SELECT artifact_id, document_id FROM document_artifact")}
    assert owner_after == owner_before  # exactly where everything started, ids included
    assert count(env, "document_event") == 4 and count(env, "document") == 4


# ------------------------------------------------------------------------------------- doctor


def test_doctor_reports_each_injected_relationship_defect_by_its_own_code(env):
    from knowledgevista.services import doctor
    a, b, c = doc(env, "a.txt"), doc(env, "b.txt"), doc(env, "c.txt")
    assert [f for f in doctor.run_doctor(env.conn) if f.category == "relationships"] == []
    env.conn.execute("PRAGMA foreign_keys = OFF")  # these defects are what a corrupted file looks like
    env.conn.execute("INSERT INTO relation_run (run_id, started_at, status, matcher_version) VALUES ('r1', '2026-01-01T00:00:00Z', 'running', 'x')")
    env.conn.execute("UPDATE document SET retired_at = '2026-01-01T00:00:00Z' WHERE document_id = ?", (c,))  # retired, no survivor, still holds its artifact
    for x, y in ((a, b), (b, a)):  # a cycle written around the service's own check
        env.conn.execute("INSERT INTO document_relation (relation_id, kind, source_id, target_id, accepted_by, accepted_at) VALUES (?, 'part_of', ?, ?, 'user', 't')", (f"rel{x[:6]}", x, y))
    env.conn.execute("INSERT INTO document_relation (relation_id, kind, source_id, target_id, accepted_by, accepted_at) VALUES ('relret', 'related_to', ?, ?, 'user', 't')", (a, c))
    codes = {f.code for f in doctor.run_doctor(env.conn) if f.category == "relationships"}
    assert codes == {"KVD_STALE_RELATION_RUN", "KVD_RETIRED_WITHOUT_SURVIVOR", "KVD_RETIRED_HOLDS_ARTIFACTS", "KVD_RELATION_CYCLE", "KVD_RELATION_TO_RETIRED"}


def test_a_chain_of_merges_leaves_every_retired_document_pointing_at_a_live_one_and_split_still_revives_the_first(env):
    a, b, c = doc(env, "a.txt"), doc(env, "b.txt"), doc(env, "c.txt")
    relations.merge(env.conn, a, b)  # b -> a
    relations.merge(env.conn, c, a)  # a -> c, and b must now say c, not a retired document
    assert env.one("SELECT merged_into FROM document WHERE document_id = ?", b) == c
    assert env.one("SELECT merged_into FROM document WHERE document_id = ?", a) == c
    from knowledgevista.services import doctor
    assert [f for f in doctor.run_doctor(env.conn) if f.code in ("KVD_MERGE_CHAIN", "KVD_RETIRED_WITHOUT_SURVIVOR")] == []
    revived = relations.split_document(env.conn, c, art(env, "b.txt"))
    assert revived["document_id"] == b and revived["revived"] is True  # b comes back under its own id, from inside the survivor of two merges


def test_a_retired_documents_old_proposals_may_point_at_artifacts_that_moved_and_doctor_does_not_call_that_a_defect(env):
    from knowledgevista.db.catalog import transaction as tx
    from knowledgevista.domain.candidate import CandidateSpec, evidence_key
    from knowledgevista.services import doctor
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    with tx(env.conn):
        metadata.upsert_candidate(env.conn, b, art(env, "b.txt"), CandidateSpec("title", "Title Of The Absorbed Document Here", "observed", "layout_title", evidence_key("t")), None, "t")
    relations.merge(env.conn, a, b)  # b's artifact now belongs to a
    assert [f for f in doctor.run_doctor(env.conn) if f.code == "KVD_CANDIDATE_WRONG_ARTIFACT"] == []
