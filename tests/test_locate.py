"""`locate`: where it is NOW, from a hash, an id, a name or a reference, checked against the disk and never guessing."""

from __future__ import annotations

import os
import shutil

import pytest
from support import make_env

from knowledgevista.domain import reference as refmod
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import locate, metadata, relations


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path, {"a.txt": "alpha", "sub/b.txt": "beta", "one/same.txt": "twin", "two/same.txt": "twin", "x/dup.txt": "gamma", "y/dup.txt": "delta"})
    e.scan()
    return e


def artifact(env, path) -> str:
    return env.one("SELECT artifact_id FROM location WHERE relative_path = ? AND ended_at IS NULL", path)


def document(env, path) -> str:
    return env.one("SELECT document_id FROM document_artifact WHERE artifact_id = ?", artifact(env, path))


def refused(code, fn, *args, **kwargs):
    with pytest.raises(KvError) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == code, caught.value.message
    return caught.value


def test_a_hash_a_prefix_an_id_a_name_and_a_reference_all_find_the_same_file(env):
    a, doc = artifact(env, "a.txt"), document(env, "a.txt")
    wanted = os.path.join(str(env.lib), "a.txt")
    for text, how in ((a, "artifact"), (a[:12], "artifact_prefix"), (doc, "document_id"), ("a.txt", "file_name"), (f"knowledgevista://artifact/{a}", "uri"),
                      (f"knowledgevista://document/{doc}", "uri")):
        found = locate.locate(env.conn, text)
        assert (found["document_id"], found["artifact_id"], found["matched_by"]) == (doc, a, how), text
        assert found["status"] == "available" and found["available"] is True
        (location,) = found["locations"]
        assert os.path.normcase(location["absolute_path"]) == os.path.normcase(wanted) and location["on_disk"] is True and location["state"] == "active"
        assert found["uri"] == f"knowledgevista://document/{doc}" or found["uri"].startswith(f"knowledgevista://document/{doc}/")


def test_a_page_reference_is_echoed_with_its_page_and_kept_in_the_uri(env):
    a, doc = artifact(env, "a.txt"), document(env, "a.txt")
    found = locate.locate(env.conn, f"knowledgevista://artifact/{a}/page/12")
    assert found["page"] == {"pdf_page": 12, "printed_label": None} and found["uri"] == f"knowledgevista://document/{doc}/page/12"
    found = locate.locate(env.conn, f"knowledgevista://document/{doc}/label/iii")
    assert found["page"] == {"pdf_page": None, "printed_label": "iii"} and found["uri"].endswith("/label/iii")
    assert "page" not in locate.locate(env.conn, a)


def test_the_catalog_is_not_trusted_over_the_disk(env):
    os.remove(env.lib / "a.txt")  # removed after the last scan: the catalog still says active
    found = locate.locate(env.conn, "a.txt")
    assert found["locations"][0]["state"] == "active" and found["locations"][0]["on_disk"] is False
    assert found["status"] == "missing" and found["available"] is False and found["document_available"] is False
    assert locate.locate(env.conn, "a.txt", check_disk=False)["locations"][0]["on_disk"] is None  # not checked is not "false"
    assert locate.locate(env.conn, "a.txt", check_disk=False)["status"] == "available"  # and the catalog alone says so


def test_a_file_moved_by_hand_is_found_after_a_scan_by_its_hash_and_the_old_name_says_where_it_went(env):
    a, doc = artifact(env, "a.txt"), document(env, "a.txt")
    shutil.move(env.lib / "a.txt", env.lib / "renamed.txt")
    env.scan()
    by_hash = locate.locate(env.conn, a)
    assert by_hash["document_id"] == doc and by_hash["locations"][0]["relative_path"] == "renamed.txt" and by_hash["status"] == "available"
    by_old_name = locate.locate(env.conn, "a.txt")  # only a PAST location matches this name
    assert by_old_name["historical"] is True and by_old_name["locations"][0]["relative_path"] == "renamed.txt"


def test_an_offline_root_is_reported_as_such_and_nothing_is_checked(env):
    env.conn.execute("UPDATE root SET status = 'unavailable'")
    found = locate.locate(env.conn, "a.txt")
    assert found["status"] == "root_offline" and found["available"] is False
    assert found["locations"][0]["on_disk"] is None and found["locations"][0]["root_status"] == "unavailable"


def test_one_file_at_two_paths_lists_both_and_is_not_ambiguous(env):
    found = locate.locate(env.conn, artifact(env, "one/same.txt"))
    assert sorted(loc["relative_path"] for loc in found["locations"]) == ["one/same.txt", "two/same.txt"] and found["status"] == "available"
    assert len(found["artifacts"]) == 1


def test_a_name_shared_by_two_documents_is_ambiguous_with_every_candidate(env):
    error = refused(ErrorCode.AMBIGUOUS, locate.locate, env.conn, "dup.txt")
    assert {c["document_id"] for c in error.details["candidates"]} == {document(env, "x/dup.txt"), document(env, "y/dup.txt")}


def test_unknown_things_are_not_found_and_a_malformed_reference_is_an_argument_error(env):
    refused(ErrorCode.NOT_FOUND, locate.locate, env.conn, "nothing.pdf")
    refused(ErrorCode.NOT_FOUND, locate.locate, env.conn, "knowledgevista://artifact/" + "e" * 64)
    refused(ErrorCode.INVALID_ARGUMENTS, locate.locate, env.conn, "knowledgevista://document/short")
    refused(ErrorCode.INVALID_ARGUMENTS, locate.locate, env.conn, "   ")


def test_a_merged_away_document_id_still_resolves_to_its_survivor_and_says_so(env):
    keep, absorbed = document(env, "a.txt"), document(env, "sub/b.txt")
    relations.merge(env.conn, keep, absorbed)
    found = locate.locate(env.conn, absorbed)
    assert found["document_id"] == keep and found["requested_document_id"] == absorbed and found["retired_document"] is True
    assert found["artifact_id"] == artifact(env, "sub/b.txt") and found["locations"][0]["relative_path"] == "sub/b.txt"
    assert [a["artifact_id"] for a in found["artifacts"]] == [artifact(env, "a.txt"), artifact(env, "sub/b.txt")]  # canonical first
    plain = locate.locate(env.conn, keep)
    assert "requested_document_id" not in plain and "retired_document" not in plain


def test_a_document_with_two_artifacts_reports_each_and_the_one_asked_for_leads(env):
    keep, absorbed = document(env, "a.txt"), document(env, "sub/b.txt")
    relations.merge(env.conn, keep, absorbed)
    by_second = locate.locate(env.conn, artifact(env, "sub/b.txt"))
    assert by_second["artifact_id"] == artifact(env, "sub/b.txt") and by_second["locations"][0]["relative_path"] == "sub/b.txt"
    by_document = locate.locate(env.conn, keep)
    assert by_document["artifact_id"] == artifact(env, "a.txt") and {a["canonical"] for a in by_document["artifacts"]} == {True, False}


def test_accepted_metadata_is_included_and_proposals_are_not(env):
    doc = document(env, "a.txt")
    metadata.set_value(env.conn, doc, "title", "An Accepted Title For This Document")
    metadata.set_value(env.conn, doc, "doi", "10.1234/abc")
    assert locate.locate(env.conn, doc)["year"] is None  # unknown is absent, never an empty string
    metadata.set_value(env.conn, doc, "year", "2021")
    found = locate.locate(env.conn, doc)
    assert (found["title"], found["doi"], found["year"]) == ("An Accepted Title For This Document", "10.1234/abc", "2021")


def test_locate_changes_nothing(env):
    before = (env.revision(), env.conn.execute("SELECT COUNT(*) FROM location_event").fetchone()[0], env.snapshot())
    for text in (artifact(env, "a.txt"), "a.txt", document(env, "sub/b.txt")):
        locate.locate(env.conn, text)
    assert (env.revision(), env.conn.execute("SELECT COUNT(*) FROM location_event").fetchone()[0], env.snapshot()) == before


def test_the_uri_in_the_answer_parses_back_to_the_document(env):
    found = locate.locate(env.conn, "a.txt")
    assert refmod.parse(found["uri"]).id == found["document_id"] and refmod.parse(found["artifact_uri"]).id == found["artifact_id"]
