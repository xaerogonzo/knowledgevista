"""The metadata store and the batch rule: proposals vs accepted values, locks, history, and what a rerun may not touch."""

from __future__ import annotations

import json

import pytest
from support import make_env

from knowledgevista.domain.candidate import CandidateSpec, evidence_key
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import metadata, review

DOI = "10.5555/kv.store.0001"
TITLE = "Aqueous Solubility of Invented Nitrate Esters"


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path, {"a.txt": "alpha", "b.txt": "beta"})
    e.scan()
    return e


def doc_ids(env) -> list[str]:
    return [r["document_id"] for r in env.rows("SELECT d.document_id FROM document d ORDER BY d.document_id")]


def artifact_of(env, document_id) -> str:
    return env.one("SELECT artifact_id FROM document_artifact WHERE document_id = ?", document_id)


def spec(field="title", value=TITLE, source="layout_title", review_level="required", **kw) -> CandidateSpec:
    return CandidateSpec(field=field, value=value, origin="observed", source=source, evidence_key=evidence_key(field, source, value),
                         evidence=kw.pop("evidence", {"n": 1}), review=review_level, **kw)


def propose(env, document_id, candidate, run=None) -> str:
    from knowledgevista.db.catalog import transaction
    with transaction(env.conn):
        return metadata.upsert_candidate(env.conn, document_id, artifact_of(env, document_id), candidate, run, "test-1")


def cid(env, document_id, field="title", source="layout_title") -> str:
    return env.one("SELECT candidate_id FROM metadata_candidate WHERE document_id = ? AND field = ? AND source = ?", document_id, field, source)


# ------------------------------------------------------------------------------------------------ proposals


def test_the_same_evidence_is_one_proposal_and_a_second_finding_writes_nothing(env):
    a = doc_ids(env)[0]
    assert propose(env, a, spec()) == "new"
    before = env.rows("SELECT * FROM metadata_candidate")
    assert propose(env, a, spec()) == "unchanged"
    assert [tuple(r) for r in env.rows("SELECT * FROM metadata_candidate")] == [tuple(r) for r in before]  # not even updated_at moved


def test_changed_evidence_for_the_same_proposal_updates_it_and_keeps_its_identity(env):
    a = doc_ids(env)[0]
    propose(env, a, spec(evidence={"n": 1}))
    first = cid(env, a)
    assert propose(env, a, spec(evidence={"n": 2}, confidence="high")) == "updated"
    assert cid(env, a) == first and json.loads(env.one("SELECT evidence_json FROM metadata_candidate")) == {"n": 2}


def test_a_decided_candidate_is_not_reopened_by_finding_it_again(env):
    a = doc_ids(env)[0]
    propose(env, a, spec())
    metadata.reject_candidate(env.conn, cid(env, a))
    assert propose(env, a, spec(confidence="exact")) == "decided"
    assert env.one("SELECT status FROM metadata_candidate") == "rejected"


def test_mark_stale_touches_only_the_named_sources_and_never_a_decided_candidate(env):
    a = doc_ids(env)[0]
    from knowledgevista.db.catalog import transaction
    propose(env, a, spec(source="layout_title"))
    propose(env, a, spec(value="Other Title Entirely Different", source="pdf_info_title"))
    propose(env, a, spec(field="year", value="2021", source="crossref"))
    metadata.accept_candidate(env.conn, cid(env, a, "title", "pdf_info_title"), actor="user")
    with transaction(env.conn):
        stale = metadata.mark_stale(env.conn, a, ["layout_title", "pdf_info_title"], set(), None)
    assert stale == 1  # the layout title; the accepted info title and the crossref year are left alone
    statuses = {r["source"]: r["status"] for r in env.rows("SELECT source, status FROM metadata_candidate")}
    assert statuses == {"layout_title": "stale", "pdf_info_title": "accepted", "crossref": "proposed"}


def test_a_stale_candidate_found_again_is_revived(env):
    a = doc_ids(env)[0]
    from knowledgevista.db.catalog import transaction
    propose(env, a, spec())
    with transaction(env.conn):
        metadata.mark_stale(env.conn, a, ["layout_title"], set(), None)
    assert propose(env, a, spec()) == "revived" and env.one("SELECT status FROM metadata_candidate") == "proposed"


# ------------------------------------------------------------------------------------------------ accepting


def test_accepting_writes_the_value_the_history_and_moves_the_revision(env):
    a = doc_ids(env)[0]
    propose(env, a, spec(field="doi", value=DOI, source="pdf_text_doi", classification="own"))
    revision = env.revision()
    result = metadata.accept_candidate(env.conn, cid(env, a, "doi", "pdf_text_doi"), actor="user")
    assert result.changed and result.value == DOI and env.revision() == revision + 1
    value = metadata.get_values(env.conn, a)["doi"]
    assert (value["value"], value["origin"], value["source"], value["accepted_by"], value["locked"]) == (DOI, "observed", "pdf_text_doi", "user", 0)
    (entry,) = metadata.history(env.conn, a)
    assert (entry["old_value"], entry["new_value"], entry["actor"]) == (None, DOI, "user")
    assert env.one("SELECT status FROM metadata_candidate") == "accepted"


def test_accepting_the_same_value_again_changes_nothing(env):
    a = doc_ids(env)[0]
    propose(env, a, spec())
    metadata.accept_candidate(env.conn, cid(env, a), actor="user")
    revision, history = env.revision(), len(metadata.history(env.conn, a))
    snapshot = [tuple(r) for r in env.rows("SELECT * FROM metadata_candidate")]
    again = metadata.accept_candidate(env.conn, cid(env, a), actor="user")
    assert not again.changed and env.revision() == revision
    assert len(metadata.history(env.conn, a)) == history
    assert [tuple(r) for r in env.rows("SELECT * FROM metadata_candidate")] == snapshot


def test_other_proposals_with_the_same_value_are_accepted_with_it_and_different_values_are_not(env):
    a = doc_ids(env)[0]
    propose(env, a, spec(source="layout_title"))
    propose(env, a, spec(source="pdf_info_title"))
    propose(env, a, spec(value="A Different Title That Disagrees With The Other", source="pdf_xmp_title"))
    metadata.accept_candidate(env.conn, cid(env, a, "title", "layout_title"), actor="user")
    statuses = {r["source"]: r["status"] for r in env.rows("SELECT source, status FROM metadata_candidate")}
    assert statuses == {"layout_title": "accepted", "pdf_info_title": "accepted", "pdf_xmp_title": "proposed"}


def test_accepting_a_different_value_replaces_it_and_the_history_keeps_the_old_one_and_its_source(env):
    a = doc_ids(env)[0]
    propose(env, a, spec(value="First Title Which Is Wrong Or Outdated", source="pdf_info_title"))
    propose(env, a, spec(source="crossref"))
    metadata.accept_candidate(env.conn, cid(env, a, "title", "pdf_info_title"), actor="user")
    result = metadata.accept_candidate(env.conn, cid(env, a, "title", "crossref"), actor="rule:safe_batch_v1")
    assert result.changed and result.replaced == "First Title Which Is Wrong Or Outdated"
    first, second = metadata.history(env.conn, a)
    assert (second["old_value"], second["old_source"], second["new_value"], second["actor"]) == (
        "First Title Which Is Wrong Or Outdated", "pdf_info_title", TITLE, "resolver")
    assert metadata.get_values(env.conn, a)["title"]["accepted_by"] == "rule:safe_batch_v1"


def test_a_locked_value_is_not_replaced_not_by_a_rule_and_not_by_the_users_own_accept(env):
    a = doc_ids(env)[0]
    metadata.set_value(env.conn, a, "title", "The Title I Typed Myself, Carefully")
    propose(env, a, spec())
    for actor in ("rule:safe_batch_v1", "user"):
        with pytest.raises(KvError) as caught:
            metadata.accept_candidate(env.conn, cid(env, a), actor=actor)
        assert caught.value.code == ErrorCode.METADATA_LOCKED
    assert metadata.get_values(env.conn, a)["title"]["value"] == "The Title I Typed Myself, Carefully"
    assert env.one("SELECT status FROM metadata_candidate") == "proposed"  # the failed accept rolled back completely


def test_a_proposal_equal_to_a_locked_value_may_be_accepted_because_nothing_is_replaced(env):
    a = doc_ids(env)[0]
    metadata.set_value(env.conn, a, "title", TITLE)
    propose(env, a, spec())
    result = metadata.accept_candidate(env.conn, cid(env, a), actor="rule:safe_batch_v1")
    assert not result.changed and metadata.get_values(env.conn, a)["title"]["origin"] == "assigned"


def test_reject_keeps_the_proposal_and_refuses_to_reject_the_accepted_value(env):
    a = doc_ids(env)[0]
    propose(env, a, spec())
    assert metadata.reject_candidate(env.conn, cid(env, a)) is True
    assert metadata.reject_candidate(env.conn, cid(env, a)) is False
    assert env.one("SELECT COUNT(*) FROM metadata_candidate") == 1  # never deleted
    propose(env, a, spec(value="Another Title Worth Accepting, Perhaps", source="pdf_info_title"))
    metadata.accept_candidate(env.conn, cid(env, a, "title", "pdf_info_title"), actor="user")
    with pytest.raises(KvError) as caught:
        metadata.reject_candidate(env.conn, cid(env, a, "title", "pdf_info_title"))
    assert caught.value.code == ErrorCode.INVALID_ARGUMENTS


def test_unknown_candidate_is_not_found(env):
    with pytest.raises(KvError) as caught:
        metadata.accept_candidate(env.conn, "0" * 32, actor="user")
    assert caught.value.code == ErrorCode.NOT_FOUND


# ------------------------------------------------------------------------------------------------ assigning


@pytest.mark.parametrize("field, raw, stored", [
    ("doi", "https://doi.org/10.5555/KV.Typed.0001.", "10.5555/kv.typed.0001"),
    ("year", " 2021 ", "2021"),
    ("title", "  Spaces   collapse \n here ", "Spaces collapse here"),
    ("isbn", "978-3-16-148410-0", "9783161484100"),
    ("arxiv", "arXiv:2105.12345v2", "2105.12345"),
])
def test_a_typed_value_is_normalised_like_any_other(env, field, raw, stored):
    a = doc_ids(env)[0]
    assert metadata.set_value(env.conn, a, field, raw) is True
    value = metadata.get_values(env.conn, a)[field]
    assert (value["value"], value["origin"], value["source"], value["locked"], value["accepted_by"]) == (stored, "assigned", "manual", 1, "user")


@pytest.mark.parametrize("field, raw", [
    ("doi", "not a doi"), ("year", "20x1"), ("year", "1066"), ("year", "3000"), ("title", "   "), ("title", ""),
    ("isbn", "978-3-16-148410-1"), ("isbn", "0-306-40615-3"), ("arxiv", "12345"), ("authors", ""), ("colour", "red"),
])
def test_an_invalid_or_empty_value_is_refused_and_nothing_is_stored(env, field, raw):
    a = doc_ids(env)[0]
    with pytest.raises(KvError) as caught:
        metadata.set_value(env.conn, a, field, raw)
    assert caught.value.code == ErrorCode.INVALID_ARGUMENTS
    assert env.one("SELECT COUNT(*) FROM metadata_value") == 0 and env.one("SELECT COUNT(*) FROM metadata_history") == 0


def test_authors_are_stored_as_structure_and_displayed_readably(env):
    a = doc_ids(env)[0]
    metadata.set_value(env.conn, a, "authors", "Examplar; Placeholder")
    stored = json.loads(metadata.get_values(env.conn, a)["authors"]["value"])
    assert stored == [{"family": None, "given": None, "name": "Examplar"}, {"family": None, "given": None, "name": "Placeholder"}]
    from knowledgevista.domain import fields
    assert fields.authors_display(metadata.get_values(env.conn, a)["authors"]["value"]) == "Examplar; Placeholder"


def test_setting_a_value_for_an_unknown_document_is_not_found(env):
    with pytest.raises(KvError) as caught:
        metadata.set_value(env.conn, "f" * 32, "title", TITLE)
    assert caught.value.code == ErrorCode.NOT_FOUND


def test_the_user_can_replace_their_own_locked_value_and_confirm_a_found_one(env):
    a = doc_ids(env)[0]
    metadata.set_value(env.conn, a, "title", "First Statement By The User Here")
    assert metadata.set_value(env.conn, a, "title", "Second Statement By The User Here") is True
    assert metadata.set_value(env.conn, a, "title", "Second Statement By The User Here") is False
    propose(env, a, spec(field="year", value="2020", source="crossref"))
    metadata.accept_candidate(env.conn, cid(env, a, "year", "crossref"), actor="rule:safe_batch_v1")
    assert metadata.set_value(env.conn, a, "year", "2020") is True  # same value, now stated by a person: it becomes theirs
    year = metadata.get_values(env.conn, a)["year"]
    assert (year["origin"], year["locked"], year["accepted_by"]) == ("assigned", 1, "user")


def test_clearing_a_value_rejects_what_produced_it_so_a_rule_cannot_bring_it_back(env):
    a = doc_ids(env)[0]
    propose(env, a, spec(review_level="safe"))
    metadata.accept_candidate(env.conn, cid(env, a), actor="rule:safe_batch_v1")
    assert metadata.clear_value(env.conn, a, "title") is True
    assert "title" not in metadata.get_values(env.conn, a)
    assert env.one("SELECT status FROM metadata_candidate") == "rejected"
    batch = review.accept_safe_for_document(env.conn, a)
    assert batch.accepted == [] and "title" not in metadata.get_values(env.conn, a)
    assert [h["new_value"] for h in metadata.history(env.conn, a)] == [TITLE, None]


def test_lock_and_unlock(env):
    a = doc_ids(env)[0]
    with pytest.raises(KvError) as caught:
        metadata.set_lock(env.conn, a, "title", True)
    assert caught.value.code == ErrorCode.NOT_FOUND
    propose(env, a, spec())
    metadata.accept_candidate(env.conn, cid(env, a), actor="user")
    assert metadata.set_lock(env.conn, a, "title", True) is True and metadata.set_lock(env.conn, a, "title", True) is False
    with pytest.raises(KvError):
        metadata.clear_value(env.conn, a, "title", actor="rule:safe_batch_v1")
    assert metadata.set_lock(env.conn, a, "title", False) is True


# ------------------------------------------------------------------------------------------------ the batch rule


def test_the_rule_accepts_a_safe_candidate_and_records_the_rule_as_the_decider(env):
    a = doc_ids(env)[0]
    propose(env, a, spec(field="doi", value=DOI, source="pdf_text_doi", classification="own", review_level="safe"))
    batch = review.accept_safe_for_document(env.conn, a)
    assert [(x.field, x.value) for x in batch.accepted] == [("doi", DOI)]
    assert env.one("SELECT decided_by FROM metadata_candidate") == review.RULE == "rule:safe_batch_v1"


def test_the_rule_never_accepts_a_candidate_that_is_not_safe(env):
    a = doc_ids(env)[0]
    propose(env, a, spec(review_level="required", confidence="exact"))
    assert review.accept_safe_for_document(env.conn, a).accepted == []


def test_the_rule_accepts_nothing_when_safe_candidates_disagree(env):
    a = doc_ids(env)[0]
    propose(env, a, spec(field="doi", value=DOI, source="pdf_text_doi", classification="own", review_level="safe"))
    propose(env, a, spec(field="doi", value="10.5555/kv.store.0002", source="crossref", classification="own", review_level="safe"))
    batch = review.accept_safe_for_document(env.conn, a)
    assert batch.accepted == [] and batch.skipped[0]["reason"] == "safe candidates disagree"
    assert "doi" not in metadata.get_values(env.conn, a)


def test_the_best_sourced_of_agreeing_safe_candidates_is_recorded_and_all_are_accepted(env):
    a = doc_ids(env)[0]
    propose(env, a, spec(field="doi", value=DOI, source="pdf_text_doi", classification="own", review_level="safe"))
    propose(env, a, spec(field="doi", value=DOI, source="crossref", classification="own", review_level="safe"))
    review.accept_safe_for_document(env.conn, a)
    assert metadata.get_values(env.conn, a)["doi"]["source"] == "crossref"
    assert {r["status"] for r in env.rows("SELECT status FROM metadata_candidate")} == {"accepted"}


def test_the_rule_does_not_replace_a_different_accepted_value(env):
    a = doc_ids(env)[0]
    metadata.set_value(env.conn, a, "year", "2019", lock=False)
    propose(env, a, spec(field="year", value="2021", source="crossref", review_level="safe"))
    batch = review.accept_safe_for_document(env.conn, a)
    assert batch.accepted == [] and batch.skipped[0]["reason"] == "differs from the accepted value"
    assert metadata.get_values(env.conn, a)["year"]["value"] == "2019"


def test_the_rule_may_upgrade_its_own_value_to_the_same_letters_from_a_better_source_and_only_then(env):
    a = doc_ids(env)[0]
    propose(env, a, spec(value="CU2O SOLUBILITY IN AQUEOUS MEDIA", source="layout_title", review_level="safe"))
    review.accept_safe_for_document(env.conn, a)
    propose(env, a, spec(value="Cu2O Solubility in Aqueous Media", source="crossref", review_level="safe"))
    batch = review.accept_safe_for_document(env.conn, a)
    assert [x.value for x in batch.accepted] == ["Cu2O Solubility in Aqueous Media"]
    assert [h["new_value"] for h in metadata.history(env.conn, a)] == ["CU2O SOLUBILITY IN AQUEOUS MEDIA", "Cu2O Solubility in Aqueous Media"]
    propose(env, a, spec(value="A Completely Different Title For This Document", source="crossref", review_level="safe"))
    assert review.accept_safe_for_document(env.conn, a).accepted == []  # different letters: a person decides


def test_a_persons_value_is_never_upgraded_by_a_rule_even_to_the_same_letters(env):
    a = doc_ids(env)[0]
    metadata.set_value(env.conn, a, "title", "CU2O SOLUBILITY IN AQUEOUS MEDIA", lock=False)
    propose(env, a, spec(value="Cu2O Solubility in Aqueous Media", source="crossref", review_level="safe"))
    assert review.accept_safe_for_document(env.conn, a).accepted == []


def test_a_locked_value_makes_the_rule_leave_it_alone_quietly_for_the_same_words_and_with_a_reason_for_others(env):
    a = doc_ids(env)[0]
    metadata.set_value(env.conn, a, "title", "CU2O SOLUBILITY IN AQUEOUS MEDIA")
    propose(env, a, spec(value="Cu2O Solubility in Aqueous Media", source="crossref", review_level="safe"))
    same_words = review.accept_safe_for_document(env.conn, a)
    assert same_words.accepted == [] and same_words.skipped == []  # nothing to decide, nothing to report
    assert metadata.get_values(env.conn, a)["title"]["value"] == "CU2O SOLUBILITY IN AQUEOUS MEDIA"
    b = doc_ids(env)[1]  # a second document: the same lock, but the proposal says different words
    metadata.set_value(env.conn, b, "title", "CU2O SOLUBILITY IN AQUEOUS MEDIA")
    propose(env, b, spec(value="A Completely Different Title For This Document", source="layout_title", review_level="safe"))
    different = review.accept_safe_for_document(env.conn, b)
    assert different.accepted == [] and different.skipped[0]["reason"] == "differs from the accepted value"
    assert metadata.get_values(env.conn, b)["title"]["value"] == "CU2O SOLUBILITY IN AQUEOUS MEDIA"


def test_the_same_title_spelled_with_different_punctuation_is_one_title_not_a_disagreement(env):
    """Measured on a real library: 44 documents had a layout title with a non-breaking hyphen and file metadata with an ASCII one."""
    a = doc_ids(env)[0]
    layout = "Zorbite‑QL: an open‑source program for enumerating ionization states"
    plain = "Zorbite-QL: an open-source program for enumerating ionization states"
    propose(env, a, spec(value=layout, source="layout_title", review_level="safe"))
    propose(env, a, spec(value=plain, source="pdf_info_title", review_level="safe"))
    batch = review.accept_safe_for_document(env.conn, a)
    assert batch.skipped == [] and [x.field for x in batch.accepted] == ["title"]
    assert metadata.get_values(env.conn, a)["title"]["value"] == layout  # the better-sourced spelling
    assert {r["status"] for r in env.rows("SELECT status FROM metadata_candidate")} == {"accepted"}  # both spellings, accepted together
    assert review.queue(env.conn)[1] == 0


def test_a_proposal_that_says_what_the_accepted_value_says_is_not_in_the_queue_and_a_real_difference_is(env):
    a = doc_ids(env)[0]
    metadata.set_value(env.conn, a, "title", "Zorbite-QL: an open-source program for enumerating ionization states", lock=False)
    propose(env, a, spec(value="Zorbite‑QL: an open‑source program for enumerating ionization states", source="layout_title"))
    assert review.queue(env.conn)[1] == 0
    propose(env, a, spec(value="Something Else Entirely About Another Subject Altogether", source="pdf_xmp_title"))
    (item,), total = review.queue(env.conn)
    assert total == 1 and item["differs_from_accepted"].startswith("Zorbite")


# ------------------------------------------------------------------------------------------------ the queue


def test_the_queue_groups_agreeing_sources_into_one_item_and_orders_by_urgency(env):
    a, b = doc_ids(env)
    propose(env, a, spec(source="layout_title", confidence="medium", priority=20))
    propose(env, a, spec(source="pdf_info_title", confidence="high", priority=20))
    propose(env, b, spec(field="doi", value=DOI, source="pdf_text_doi", classification="ambiguous", confidence="ambiguous", priority=10))
    items, total = review.queue(env.conn)
    assert total == 2 and items[0]["field"] == "doi" and items[0]["priority"] == 10
    title = items[1]
    assert title["sources"] == ["layout_title", "pdf_info_title"] and len(title["candidate_ids"]) == 2 and title["confidence"] == "high"


def test_the_queue_shows_what_the_accepted_value_is_when_a_proposal_differs(env):
    a = doc_ids(env)[0]
    metadata.set_value(env.conn, a, "year", "2019", lock=False)
    propose(env, a, spec(field="year", value="2021", source="crossref"))
    (item,), _ = review.queue(env.conn)
    assert item["differs_from_accepted"] == "2019"


def test_the_queue_excludes_decided_and_set_aside_unless_asked_and_pages(env):
    a = doc_ids(env)[0]
    propose(env, a, spec(field="doi", value=DOI, source="pdf_text_doi", status="set_aside", classification="foreign"))
    propose(env, a, spec(value="Another Title Which Is Proposed Here", source="pdf_info_title"))
    assert review.queue(env.conn)[1] == 1
    assert review.queue(env.conn, include=("proposed", "set_aside"))[1] == 2
    assert review.queue(env.conn, include=("proposed", "set_aside"), limit=1)[0][0]["field"] in ("doi", "title")
    assert review.queue(env.conn, field_="doi", include=("set_aside",))[1] == 1
    assert review.queue(env.conn, review="safe")[1] == 0


def test_candidate_ids_resolve_by_prefix_and_ambiguity_is_never_guessed(env):
    a, b = doc_ids(env)
    propose(env, a, spec())
    propose(env, b, spec())
    first = cid(env, a)
    assert review.resolve_candidate_id(env.conn, first[:10]) == first
    for bad in ("short", "zzzzzzzzzz"):
        with pytest.raises(KvError) as caught:
            review.resolve_candidate_id(env.conn, bad)
        assert caught.value.code == ErrorCode.INVALID_ARGUMENTS
    with pytest.raises(KvError) as caught:
        review.resolve_candidate_id(env.conn, "0" * 12)
    assert caught.value.code == ErrorCode.NOT_FOUND


# ------------------------------------------------------------------------------------------------ doctor and accounting


def metadata_findings(env):
    from knowledgevista.services import doctor
    return {f.code: f for f in doctor.run_doctor(env.conn) if f.category == "metadata"}


def test_doctor_is_silent_about_metadata_on_a_healthy_catalog(env):
    a = doc_ids(env)[0]
    propose(env, a, spec(field="doi", value=DOI, source="pdf_text_doi", classification="own"))
    metadata.accept_candidate(env.conn, cid(env, a, "doi", "pdf_text_doi"), actor="user")
    assert metadata_findings(env) == {}


def test_doctor_reports_each_injected_metadata_defect_by_its_own_code(env):
    a, b = doc_ids(env)
    metadata.set_value(env.conn, a, "doi", DOI)
    metadata.set_value(env.conn, b, "doi", DOI)  # two documents, one DOI
    env.conn.execute("PRAGMA foreign_keys = OFF")  # the defects below are what a corrupted file looks like, so write them around the guards
    env.conn.execute("UPDATE metadata_value SET value = '10.5555/KV.NotCanonical' WHERE document_id = ?", (a,))  # written around the service
    env.conn.execute("INSERT INTO resolve_run (run_id, started_at, status, app_version, matcher_version, online, accept_safe) "
                     "VALUES ('r1', '2026-01-01T00:00:00Z', 'running', '0', 'x', 0, 0)")
    propose(env, a, spec())
    env.conn.execute("UPDATE metadata_value SET source_candidate_id = 'does-not-exist' WHERE document_id = ?", (b,))
    env.conn.execute("UPDATE metadata_candidate SET artifact_id = ?", (artifact_of(env, b),))  # evidence from another document's artifact
    found = metadata_findings(env)
    assert set(found) == {"KVD_STALE_RESOLVE_RUN", "KVD_VALUE_NOT_CANONICAL", "KVD_VALUE_WITHOUT_SOURCE", "KVD_CANDIDATE_WRONG_ARTIFACT"}
    assert found["KVD_VALUE_NOT_CANONICAL"].severity == "error" and found["KVD_STALE_RESOLVE_RUN"].severity == "warning"
    env.conn.execute("UPDATE metadata_value SET value = ? WHERE document_id = ?", (DOI, a))
    assert metadata_findings(env)["KVD_DOI_COLLISION"].severity == "warning"  # never merged, only reported


def test_every_pdf_without_an_accepted_title_is_in_exactly_one_stated_state(tmp_path):
    import pdfbuilders as b
    from knowledgevista.index import store
    from knowledgevista.services.metadata_report import TITLE_REASONS, title_states
    e = make_env(tmp_path, {})
    for name in ("accepted", "awaiting", "scan", "nothing", "unread"):
        b.native_pdf(e.lib / f"{name}.pdf", title=f"Title For The Document Called {name} Here")
    e.scan()
    index = store.open_index(tmp_path / "idx.sqlite", create=True)
    doc = {n: e.one("SELECT da.document_id FROM location l JOIN document_artifact da ON da.artifact_id = l.artifact_id WHERE l.relative_path = ?", f"{n}.pdf")
           for n in ("accepted", "awaiting", "scan", "nothing", "unread")}
    art = {n: artifact_of(e, d) for n, d in doc.items()}

    def extraction(name, scanned=False):
        store.replace_extraction(index, store.ExtractionRecord(
            artifact_id=art[name], source="native", extractor="x", extractor_version="1", format_version=1, options_hash="-", profile_id="p",
            status="complete", page_count=1, chars=10, legacy_scanned=scanned, first_pages_doi=None, first_text=None,
            pages=[store.PageRecord(1, "text", None, "text_native")]))

    for name in ("accepted", "awaiting", "scan", "nothing"):
        extraction(name, scanned=(name == "scan"))
    states = title_states(e.conn, index)
    assert states[doc["unread"]] == "not_extracted"  # no extraction yet, whatever else is true
    assert states[doc["nothing"]] == "not_resolved_yet"  # resolve has never run
    e.conn.execute("INSERT INTO resolve_run (run_id, started_at, status, app_version, matcher_version, online, accept_safe) "
                   "VALUES ('r1', '2026-01-01T00:00:00Z', 'completed', '0', 'x', 0, 0)")
    metadata.set_value(e.conn, doc["accepted"], "title", "An Accepted Title For This One")
    from knowledgevista.db.catalog import transaction
    with transaction(e.conn):
        metadata.upsert_candidate(e.conn, doc["awaiting"], art["awaiting"], spec(), "r1", "t")
    states = title_states(e.conn, index)
    assert states == {doc["accepted"]: "accepted", doc["awaiting"]: "candidates_awaiting_review", doc["scan"]: "no_text_layer",
                      doc["nothing"]: "no_title_found", doc["unread"]: "not_extracted"}
    assert set(states.values()) - {"accepted"} <= set(TITLE_REASONS)  # every state that is not a title is a stated reason
    index.close()


def test_a_better_sourced_proposal_with_different_letters_never_replaces_a_rule_accepted_value(env):
    a = doc_ids(env)[0]
    propose(env, a, spec(value="The Title The Layout Read From The Page", source="layout_title", review_level="safe"))
    review.accept_safe_for_document(env.conn, a)
    propose(env, a, spec(value="A Completely Different Title From A Better Source", source="crossref", review_level="safe"))
    batch = review.accept_safe_for_document(env.conn, a)
    assert batch.accepted == [] and batch.skipped[0]["reason"] == "differs from the accepted value"
    assert metadata.get_values(env.conn, a)["title"]["value"] == "The Title The Layout Read From The Page"


def test_a_safe_doi_that_is_not_classified_own_is_never_accepted_by_the_rule(env):
    a = doc_ids(env)[0]
    propose(env, a, spec(field="doi", value=DOI, source="pdf_text_doi", classification="ambiguous", review_level="safe"))
    assert review.accept_safe_for_document(env.conn, a).accepted == [] and "doi" not in metadata.get_values(env.conn, a)
