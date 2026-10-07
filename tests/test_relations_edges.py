"""The edges of milestone 4 that a mutation sweep found nothing watching.

Each test here was written because a deliberately planted fault survived the suite (docs/RELATIONS.md): a prefix that matches
two proposals, a cycle already in the data, a lock lost in a merge, a proposal repointed onto another's evidence, and the pure
evidence functions at their boundaries. The relate-level tests build `Doc` values directly, because what they pin is the
decision rule, not the PDF it was read from.
"""

from __future__ import annotations

import json

import pytest
from support import make_env

from knowledgevista.db.catalog import transaction
from knowledgevista.domain.relation_evidence import is_correction, pairs, supplement_markers, text_fingerprint
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.index.search import extract_filters
from knowledgevista.services import metadata, organize, relate, relations
from knowledgevista.services.relate import Doc
from knowledgevista.services.relations import RelationSpec


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path, {"a.txt": "alpha", "b.txt": "beta", "c.txt": "gamma", "d.txt": "delta"})
    e.scan()
    return e


def doc(env, name) -> str:
    return env.one("SELECT da.document_id FROM location l JOIN document_artifact da ON da.artifact_id = l.artifact_id WHERE l.relative_path = ? AND l.ended_at IS NULL", name)


def propose(env, kind, source, target="", *, level="document", key="k", members=None, confidence="high"):
    with transaction(env.conn):
        relations.upsert_candidate(env.conn, RelationSpec(level, kind, source, target, key, {"why": 1}, confidence, members), None, "t-1")


def status_of(env, kind, source, target) -> str:
    return env.one("SELECT status FROM relation_candidate WHERE kind = ? AND source_id = ? AND target_id = ?", kind, source, target)


# ------------------------------------------------------------------------------------------------ the store


def test_a_prefix_that_matches_two_proposals_is_ambiguous_not_the_first_one(env):
    a, b, c = doc(env, "a.txt"), doc(env, "b.txt"), doc(env, "c.txt")
    propose(env, "related_to", a, b)
    propose(env, "related_to", a, c)
    first, second = [r[0] for r in env.rows("SELECT candidate_id FROM relation_candidate ORDER BY candidate_id")]
    env.conn.execute("UPDATE relation_candidate SET candidate_id = ? WHERE candidate_id = ?", ("aaaaaaaa" + "1" * 24, first))
    env.conn.execute("UPDATE relation_candidate SET candidate_id = ? WHERE candidate_id = ?", ("aaaaaaaa" + "2" * 24, second))
    env.conn.commit()
    with pytest.raises(KvError) as caught:
        relations.resolve_candidate_id(env.conn, "aaaaaaaa")
    assert caught.value.code == ErrorCode.AMBIGUOUS and len(caught.value.details["candidates"]) == 2
    assert relations.resolve_candidate_id(env.conn, "aaaaaaaa1") == "aaaaaaaa" + "1" * 24


def test_a_cycle_already_in_the_data_does_not_hang_the_check_for_a_new_one(env):
    a, b, c = doc(env, "a.txt"), doc(env, "b.txt"), doc(env, "c.txt")
    for source, target in ((b, c), (c, b)):  # a corrupt catalog, or two writers: never made through add_relation
        env.conn.execute("INSERT INTO document_relation (relation_id, kind, source_id, target_id, accepted_by, accepted_at) VALUES (?, 'part_of', ?, ?, 'user', 'now')",
                         (source[:8] + target[:8], source, target))
    env.conn.commit()
    assert relations._would_cycle(env.conn, "part_of", a, b) is False  # terminates, and a is not reachable from b
    assert relations._would_cycle(env.conn, "part_of", b, c) is True


def test_a_retracted_relation_no_longer_forbids_the_reverse(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    relation_id, _ = relations.add_relation_by_user(env.conn, "document", "part_of", a, b)
    with pytest.raises(KvError):
        relations.add_relation_by_user(env.conn, "document", "part_of", b, a)  # a cycle while it stands
    assert relations.retract_relation(env.conn, relation_id) is True
    relations.add_relation_by_user(env.conn, "document", "part_of", b, a)  # the first is taken back: no cycle


def test_a_lock_survives_a_merge_with_the_value_it_protects(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    metadata.set_value(env.conn, b, "title", "A Title The Person Stated Themselves", lock=True)
    relations.merge(env.conn, a, b)
    carried = metadata.get_values(env.conn, a)["title"]
    assert carried["value"] == "A Title The Person Stated Themselves" and carried["locked"]


def test_a_proposal_that_would_land_on_another_proposals_evidence_goes_stale_instead(env):
    a, b, c = doc(env, "a.txt"), doc(env, "b.txt"), doc(env, "c.txt")
    propose(env, "supplement_of", a, b)  # a is a supplement of b, and (same evidence) of c
    propose(env, "supplement_of", a, c)
    relations.merge(env.conn, c, b)  # b is now c: the first proposal would be the second
    assert status_of(env, "supplement_of", a, b) == "stale" and status_of(env, "supplement_of", a, c) == "proposed"


def test_a_group_proposal_names_the_survivor_not_the_retired_member(env):
    a, b, c, d = (doc(env, f"{n}.txt") for n in "abcd")
    propose(env, "collection", "parent-doi:10.1/x", level="group", members=[b, c, d])
    relations.merge(env.conn, a, b)
    members = json.loads(env.one("SELECT members_json FROM relation_candidate WHERE kind = 'collection'"))
    assert members == sorted([a, c, d])


def test_keep_must_be_one_of_the_two_documents_being_merged(env):
    a, b, c = doc(env, "a.txt"), doc(env, "b.txt"), doc(env, "c.txt")
    propose(env, "same_document", a, b)
    candidate = env.one("SELECT candidate_id FROM relation_candidate")
    with pytest.raises(KvError) as caught:
        relations.accept_candidate(env.conn, candidate, keep=c)
    assert caught.value.code == ErrorCode.INVALID_ARGUMENTS and "one of the two" in caught.value.message
    assert env.one("SELECT COUNT(*) FROM document WHERE retired_at IS NOT NULL") == 0


def test_a_collection_made_from_a_group_leaves_out_a_retired_member(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    relations.merge(env.conn, a, b)
    made = organize.create_collection_from_group(env.conn, "A group", [a, b], source_doi=None, actor="user")
    assert organize.collection_members(env.conn, made["collection_id"])[1] == [a]


def test_the_ambiguous_view_includes_a_low_confidence_relation_proposal_and_leaves_out_a_retired_end(env):
    a, b, c = doc(env, "a.txt"), doc(env, "b.txt"), doc(env, "c.txt")
    propose(env, "related_to", a, b, confidence="low")
    assert sorted(i["document_id"] for i in organize.run_view(env.conn, None, "ambiguous")) == sorted([a, b])
    relations.merge(env.conn, c, b)
    ids = sorted(i["document_id"] for i in organize.run_view(env.conn, None, "ambiguous"))
    assert ids == sorted([a, c])  # b is retired and its proposal now names the survivor c; the retired document is never listed


def test_a_path_that_ended_is_not_a_copy(tmp_path):
    import os

    e = make_env(tmp_path, {"one/same.txt": "identical bytes", "two/same.txt": "identical bytes"})
    e.scan()
    assert len(organize.exact_copy_groups(e.conn)) == 1
    os.remove(e.lib / "two" / "same.txt")
    e.scan()
    assert organize.exact_copy_groups(e.conn) == []  # one live path is not a duplicate, whatever the history says


def test_the_detector_does_not_examine_a_retired_document(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    relations.merge(env.conn, a, b)
    assert b not in relate.load_documents(env.conn) and a in relate.load_documents(env.conn)


# ------------------------------------------------------------------------------------------------ the detector's rules


def make(document_id, *, title=None, doi=None, stem=None, text="", **values) -> Doc:
    d = Doc(document_id, [f"art-{document_id}"], f"art-{document_id}", stem, first_text=text)
    if title:
        d.values["title"] = title
    if doi:
        d.values["doi"] = doi
    d.values.update(values)
    return d


PAPER_TITLE = "Aqueous Solubility of Invented Esters"
PAPER = make("p", title=PAPER_TITLE, doi="10.5555/p")


def test_a_file_name_alone_nominates_a_supplement_but_decides_nothing():
    only_title = make("s", stem="paper_si", text=f"Some other front matter that mentions {PAPER_TITLE} once")
    assert relate._supplements({"p": PAPER, "s": only_title}) == []  # one signal and no announcement: not enough
    both = make("s", stem="paper_si", text=f"Front matter of {PAPER_TITLE} doi 10.5555/p")
    (found,) = relate._supplements({"p": PAPER, "s": both})
    assert (found.source_id, found.target_id, found.confidence) == ("s", "p", "medium")  # two signals: proposed, but not 'high'


def test_a_supplement_is_not_proposed_as_the_main_paper_of_another_supplement():
    first = make("s1", title="Supporting Information", text="Supporting Information for something. doi 10.5555/second", doi="10.5555/first")
    second = make("s2", title="Supporting Information", text="Supporting Information for another. doi 10.5555/first", doi="10.5555/second")
    assert relate._supplements({"s1": first, "s2": second}) == []


def test_two_documents_under_one_doi_with_two_titles_are_two_candidates_for_a_merge_not_a_parent_work():
    one = make("x", title="A Quite Long First Title Here", doi="10.1/shared")
    two = make("y", title="A Wholly Different Second Title", doi="10.1/shared")
    specs, parents = relate._same_document_by_doi({"x": one, "y": two}, set(), {})
    assert parents == {} and [(s.kind, s.confidence) for s in specs] == [("same_document", "medium")]


def test_one_artifact_is_not_a_duplicate_of_itself():
    only = make("x", title="A Quite Long First Title Here")
    assert relate._identical_text({"x": only}, {"art-x": "fingerprint"}) == []


def test_one_doi_is_one_publication_even_if_one_copy_is_a_preprint():
    published = make("x", title=PAPER_TITLE, doi="10.1/same")
    preprint = make("y", title=PAPER_TITLE, doi="10.1/same", arxiv="2101.00001")
    assert relate._versions({"x": published, "y": preprint}) == []


def test_the_duplicate_report_files_a_proposal_without_text_evidence_under_same_publication(env):
    a, b = doc(env, "a.txt"), doc(env, "b.txt")
    with transaction(env.conn):
        relations.upsert_candidate(env.conn, RelationSpec("document", "same_document", a, b, "shared-doi", {"doi": "10.1/x"}, "high"), None, "t")
    report = relate.duplicates_report(env.conn)
    assert len(report["same_publication"]) == 1 and report["identical_text"] == []


# ------------------------------------------------------------------------------------------------ pure evidence


def test_a_text_fingerprint_ignores_spacing_and_the_order_pages_were_given_in():
    one = text_fingerprint([(1, "alpha beta gamma delta " * 30), (2, "epsilon zeta eta theta " * 30)])
    assert one is not None
    assert text_fingerprint([(1, "alphabetagammadelta\n" * 30), (2, "epsilon-zeta  eta,theta " * 30)]) == one
    assert text_fingerprint([(2, "epsilon zeta eta theta " * 30), (1, "alpha beta gamma delta " * 30)]) == one
    assert text_fingerprint([(1, "alpha beta gamma delta " * 30), (2, "epsilon zeta eta theta " * 3)]) != one
    assert text_fingerprint([(1, "too short")]) is None


def test_a_small_group_is_paired_fully_and_only_a_large_one_is_starred():
    assert pairs(["c", "a", "b"]) == [("a", "b"), ("a", "c"), ("b", "c")]
    star = pairs(list("abcdefgh"))
    assert star == [("a", other) for other in "bcdefgh"]


def test_a_supplement_is_announced_on_the_first_page_not_deep_in_the_text():
    assert supplement_markers("Supporting Information for a paper", None)["says_so"] is True
    assert supplement_markers("x " * 1000 + "Supporting Information", None)["says_so"] is False  # a mention far down the page is a mention


def test_a_correction_begins_that_way_and_a_mention_later_on_does_not_make_one():
    assert is_correction(None, "Reply to the comment on our paper") is True
    assert is_correction("Response to the editor", "") is True
    assert is_correction(None, "word " * 100 + "Reply to something") is False


def test_a_filter_name_inside_a_word_is_text():
    assert extract_filters("foo-tag:x pre.year:2020") == ("foo-tag:x pre.year:2020", ())
    assert extract_filters("tag:x") == ("", (("tag", "x"),))
