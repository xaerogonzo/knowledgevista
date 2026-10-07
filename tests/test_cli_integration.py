"""`locate`, `open`, cursors and limits, and deterministic output, end to end through `kv` as a script or OpenChem would run it."""

from __future__ import annotations

import json
import os

import pdfbuilders as b
import pytest
from support import tree_hashes, write
from test_cli import run, run_json

from knowledgevista import cli_integration
from knowledgevista.db.catalog import open_catalog
from knowledgevista.domain.candidate import CandidateSpec, evidence_key
from knowledgevista.services import metadata, relations
from knowledgevista.services.relations import RelationSpec


@pytest.fixture
def world(capsys, tmp_path):
    lib = tmp_path / "papers"
    lib.mkdir()
    b.labelled_pdf(lib / "book.pdf")
    for name in "abcdef":
        write(lib / f"{name}.txt", f"document {name}")
    write(lib / "x" / "dup.txt", "gamma")
    write(lib / "y" / "dup.txt", "delta")
    write(lib / "one" / "same.txt", "twin")
    write(lib / "two" / "same.txt", "twin")
    run_json(capsys, tmp_path, "root", "add", str(lib))
    run_json(capsys, tmp_path, "scan")
    run_json(capsys, tmp_path, "extract")
    return tmp_path


def doc(capsys, tmp, name) -> str:
    return run_json(capsys, tmp, "explain", name)[1]["records"][0]["document_id"]


def with_catalog(tmp, write_fn):
    conn = open_catalog(tmp / "c.sqlite", create=False)
    try:
        return write_fn(conn)
    finally:
        conn.close()


# ------------------------------------------------------------------------------------------------ locate and open


def test_locate_answers_with_the_current_path_and_leaves_the_library_alone(capsys, world):
    before = tree_hashes(world / "papers")
    code, env, _ = run_json(capsys, world, "locate", "a.txt")
    (found,) = env["records"]
    assert code == 0 and env["command"] == "locate" and found["status"] == "available" and found["locations"][0]["on_disk"] is True
    assert os.path.normcase(found["locations"][0]["absolute_path"]) == os.path.normcase(str(world / "papers" / "a.txt"))
    assert run_json(capsys, world, "locate", found["uri"])[1]["records"][0]["document_id"] == found["document_id"]
    assert run_json(capsys, world, "locate", found["artifact_id"][:10])[1]["records"][0]["artifact_id"] == found["artifact_id"]
    assert tree_hashes(world / "papers") == before


def test_locate_exit_codes_follow_the_contract(capsys, world):
    code, env, _ = run_json(capsys, world, "locate", "dup.txt")
    assert code == 1 and env["errors"][0]["code"] == "KV_AMBIGUOUS" and len(env["errors"][0]["details"]["candidates"]) == 2
    code, env, _ = run_json(capsys, world, "locate", "missing.pdf")
    assert code == 1 and env["errors"][0]["code"] == "KV_NOT_FOUND"
    code, env, _ = run_json(capsys, world, "locate", "knowledgevista://document/xyz")
    assert code == 2 and env["errors"][0]["code"] == "KV_INVALID_ARGUMENTS"
    code, env, _ = run_json(capsys, world, "locate")
    assert code == 2 and env["errors"][0]["code"] == "KV_INVALID_ARGUMENTS"


def test_locate_notices_a_file_that_vanished_and_the_no_disk_check_flag_stops_looking(capsys, world):
    (world / "papers" / "a.txt").unlink()
    _, env, _ = run_json(capsys, world, "locate", "a.txt")
    assert env["records"][0]["status"] == "missing" and env["records"][0]["locations"][0]["on_disk"] is False
    _, env, _ = run_json(capsys, world, "locate", "a.txt", "--no-disk-check")
    assert env["records"][0]["status"] == "available" and env["records"][0]["locations"][0]["on_disk"] is None


def test_locate_says_when_a_name_only_matched_the_past_and_when_an_id_was_merged_away(capsys, world):
    (world / "papers" / "b.txt").rename(world / "papers" / "renamed.txt")
    run_json(capsys, world, "scan")
    _, env, _ = run_json(capsys, world, "locate", "b.txt")
    assert env["records"][0]["historical"] is True and [w["code"] for w in env["warnings"]] == ["KV_HISTORICAL_MATCH"]
    keep, gone = doc(capsys, world, "c.txt"), doc(capsys, world, "d.txt")
    run_json(capsys, world, "document", "merge", keep, gone)
    _, env, _ = run_json(capsys, world, "locate", gone)
    assert env["records"][0]["document_id"] == keep and env["warnings"][0]["code"] == "KV_DOCUMENT_MERGED"
    assert env["warnings"][0]["details"] == {"requested_document_id": gone, "document_id": keep}


def test_open_resolves_and_launches_through_one_replaceable_function(capsys, world, monkeypatch):
    launched = []
    monkeypatch.setattr(cli_integration, "_launch", launched.append)
    code, env, _ = run_json(capsys, world, "open", "a.txt")
    (record,) = env["records"]
    assert code == 0 and launched == [record["path"]] and record["launched"] is True and record["page_targeted"] is False and record["requested_page"] is None
    code, env, _ = run_json(capsys, world, "open", "book.pdf", "--pdf-page", "5")
    assert env["records"][0]["requested_page"] == {"pdf_page": 5, "printed_label": None} and len(launched) == 2
    code, env, _ = run_json(capsys, world, "open", "book.pdf", "--label", "iii", "--no-launch")
    assert env["records"][0]["launched"] is False and env["records"][0]["requested_page"]["printed_label"] == "iii" and len(launched) == 2


def test_open_refuses_what_it_cannot_reach_and_bad_page_arguments(capsys, world, monkeypatch):
    launched = []
    monkeypatch.setattr(cli_integration, "_launch", launched.append)
    (world / "papers" / "a.txt").unlink()
    code, env, _ = run_json(capsys, world, "open", "a.txt")
    assert code == 1 and env["errors"][0]["code"] == "KV_FILE_MISSING" and env["errors"][0]["details"]["locate"]["status"] == "missing"
    with_catalog(world, lambda conn: conn.execute("UPDATE root SET status = 'unavailable'"))
    code, env, _ = run_json(capsys, world, "open", "b.txt")
    assert code == 1 and env["errors"][0]["code"] == "KV_ROOT_UNAVAILABLE"
    assert run_json(capsys, world, "open", "b.txt", "--pdf-page", "1", "--label", "1")[0] == 2
    assert run_json(capsys, world, "open", "b.txt", "--pdf-page", "0")[0] == 2
    assert launched == []


# ------------------------------------------------------------------------------------------------ cursors


def pages(capsys, tmp, command, *args, limit=2, kind=None, max_pages=40):
    """Every page of a listing, followed through next_cursor, as a flat list of records of one type."""
    seen, token = [], None
    for _ in range(max_pages):
        extra = ["--cursor", token] if token else []
        code, env, _ = run_json(capsys, tmp, *command, *args, "--limit", str(limit), *extra)
        assert code == 0, env
        seen += [r for r in env["records"] if kind is None or r["type"] == kind]
        token = env["next_cursor"]
        assert (token is None) == (env["complete"] is True)
        if token is None:
            return seen
    raise AssertionError("never ended")


def test_a_view_is_paged_through_its_cursor_without_gaps_or_repeats(capsys, world):
    whole = run_json(capsys, world, "view", "new", "--limit", "1000")[1]
    all_items = [r["document_id"] for r in whole["records"] if r["type"] == "item"]
    assert len(all_items) >= 8 and whole["next_cursor"] is None and whole["complete"] is True
    paged = [r["document_id"] for r in pages(capsys, world, ("view", "new"), kind="item", limit=3)]
    assert paged == all_items


def test_a_collection_listing_and_proposals_page_the_same_way(capsys, world):
    ids = [doc(capsys, world, f"{n}.txt") for n in "abcdef"]
    run_json(capsys, world, "collection", "create", "Shelf", *ids)
    members = [r["document_id"] for r in pages(capsys, world, ("collection", "show", "Shelf"), kind="member", limit=2)]
    assert members == sorted(ids)

    def propose(conn):
        for i, other in enumerate(ids[1:]):
            relations.upsert_candidate(conn, RelationSpec("document", "related_to", ids[0], other, f"k{i}", {"why": i}, "low"), None, "t")
    with_catalog(world, propose)
    first = run_json(capsys, world, "relations", "list", "--limit", "2")[1]
    assert first["complete"] is False and first["next_cursor"]
    listed = [r["candidate_id"] for r in pages(capsys, world, ("relations", "list"), kind="proposal", limit=2)]
    everything = [r["candidate_id"] for r in run_json(capsys, world, "relations", "list", "--limit", "100")[1]["records"] if r["type"] == "proposal"]
    assert listed == everything and len(listed) == 5


def test_the_review_queue_pages_through_its_cursor(capsys, world):
    ids = [doc(capsys, world, f"{n}.txt") for n in "abcd"]

    def candidates(conn):
        for i, document_id in enumerate(ids):
            artifact = conn.execute("SELECT artifact_id FROM document_artifact WHERE document_id = ?", (document_id,)).fetchone()[0]
            metadata.upsert_candidate(conn, document_id, artifact, CandidateSpec("title", f"A Proposed Title Number {i} Of The Queue", "observed", "layout_title", evidence_key(f"t{i}")), None, "t")
    with_catalog(world, candidates)
    items = pages(capsys, world, ("review", "list"), kind="item", limit=1)
    assert len(items) == 4 and len({i["candidate_ids"][0] for i in items}) == 4
    assert [i["candidate_ids"] for i in items] == [i["candidate_ids"] for i in run_json(capsys, world, "review", "list", "--limit", "10")[1]["records"] if i["type"] == "item"]


def test_search_and_saved_search_page_through_a_cursor_in_one_order(capsys, world):
    whole = [(h["artifact_id"], h["pdf_page"]) for h in run_json(capsys, world, "search", "marker", "--limit", "50")[1]["records"] if h["type"] == "hit"]
    assert len(whole) == 10
    assert [(h["artifact_id"], h["pdf_page"]) for h in pages(capsys, world, ("search", "marker"), kind="hit", limit=4)] == whole
    run_json(capsys, world, "search", "marker", "--save", "markers")
    assert [(h["artifact_id"], h["pdf_page"]) for h in pages(capsys, world, ("saved", "run", "markers"), kind="hit", limit=3)] == whole


def test_a_cursor_goes_stale_when_the_catalog_changes_and_starting_again_works(capsys, world):
    token = run_json(capsys, world, "view", "new", "--limit", "2")[1]["next_cursor"]
    run_json(capsys, world, "tag", "add", "anything", doc(capsys, world, "a.txt"))  # a meaningful write moves the revision
    code, env, _ = run_json(capsys, world, "view", "new", "--limit", "2", "--cursor", token)
    assert code == 1 and env["errors"][0]["code"] == "KV_CURSOR_STALE" and env["errors"][0]["details"]["cursor_revision"] < env["errors"][0]["details"]["catalog_revision"]
    assert run_json(capsys, world, "view", "new", "--limit", "2")[0] == 0


def test_a_cursor_belongs_to_one_listing_and_cannot_be_mixed_with_offset_or_invented(capsys, world):
    token = run_json(capsys, world, "view", "new", "--limit", "2")[1]["next_cursor"]
    code, env, _ = run_json(capsys, world, "view", "inbox", "--limit", "2", "--cursor", token)  # another view, same command
    assert code == 2 and env["errors"][0]["code"] == "KV_INVALID_ARGUMENTS"
    code, env, _ = run_json(capsys, world, "relations", "list", "--cursor", token)  # another command
    assert code == 2 and env["errors"][0]["code"] == "KV_INVALID_ARGUMENTS"
    code, env, _ = run_json(capsys, world, "review", "list", "--cursor", token, "--offset", "1")
    assert code == 2 and "not both" in env["errors"][0]["message"]
    code, env, _ = run_json(capsys, world, "view", "new", "--cursor", "not-a-cursor")
    assert code == 2 and env["errors"][0]["code"] == "KV_INVALID_ARGUMENTS"
    code, env, _ = run_json(capsys, world, "search", "marker", "--cursor", token)
    assert code == 2


def test_limits_are_between_one_and_a_thousand_and_are_refused_not_clamped(capsys, world):
    for command in (("view", "new"), ("search", "marker"), ("relations", "list"), ("review", "list"), ("view", "inbox")):
        for bad in ("0", "-1", "1001"):
            code, env, _ = run_json(capsys, world, *command, "--limit", bad)
            assert code == 2 and env["errors"][0]["code"] == "KV_INVALID_ARGUMENTS" and env["errors"][0]["details"]["maximum"] == 1000, (command, bad)
    assert run_json(capsys, world, "view", "new", "--limit", "1000")[0] == 0


def test_the_old_offset_still_works_where_it_always_did(capsys, world):
    ids = [doc(capsys, world, f"{n}.txt") for n in "abc"]

    def propose(conn):
        for i, other in enumerate(ids[1:]):
            relations.upsert_candidate(conn, RelationSpec("document", "related_to", ids[0], other, f"k{i}", {"why": i}, "low"), None, "t")
    with_catalog(world, propose)
    everything = [r["candidate_id"] for r in run_json(capsys, world, "relations", "list")[1]["records"] if r["type"] == "proposal"]
    second = [r["candidate_id"] for r in run_json(capsys, world, "relations", "list", "--offset", "1")[1]["records"] if r["type"] == "proposal"]
    assert second == everything[1:] and run_json(capsys, world, "relations", "list", "--offset", "-1")[0] == 2


# ------------------------------------------------------------------------------------------------ determinism


@pytest.mark.parametrize("command", [("locate", "a.txt"), ("capabilities",), ("view", "inbox"), ("relations", "list"), ("dupes",), ("search", "marker"),
                                     ("explain", "a.txt"), ("collection", "list"), ("tag", "list"), ("root", "list"), ("stats",)])
def test_a_read_command_prints_the_same_bytes_twice(capsys, world, command):
    args = ["--json", "--catalog", str(world / "c.sqlite"), *command]
    code, first, _ = run(capsys, *args)
    code2, second, _ = run(capsys, *args)
    assert code == code2 == 0 and first == second and json.loads(first)["ok"] is True


# ------------------------------------------------------------------------------------------------ forged cursors


def forged(world, command, signature, offset, after):
    from knowledgevista.db.catalog import revision
    from knowledgevista.services import cursor as cursormod

    current = with_catalog(world, revision)
    return cursormod.encode(cursormod.Cursor(command, signature, current, offset, after))


def test_a_cursor_whose_previous_item_is_not_the_one_it_names_is_stale_even_at_the_right_revision(capsys, world):
    from knowledgevista.index.search import parse_query
    from knowledgevista.services import cursor as cursormod
    from knowledgevista.services import search_page

    items = [r["document_id"] for r in run_json(capsys, world, "view", "new", "--limit", "1000")[1]["records"] if r["type"] == "item"]
    good = forged(world, "view", cursormod.signature_of("new"), 2, items[1])
    assert run_json(capsys, world, "view", "new", "--limit", "2", "--cursor", good)[0] == 0
    bad = forged(world, "view", cursormod.signature_of("new"), 2, "somebody-else")
    code, env, _ = run_json(capsys, world, "view", "new", "--limit", "2", "--cursor", bad)
    assert code == 1 and env["errors"][0]["code"] == "KV_CURSOR_STALE" and "order" in env["errors"][0]["message"]
    sig = search_page.query_signature(parse_query("marker"))
    code, env, _ = run_json(capsys, world, "search", "marker", "--limit", "2", "--cursor", forged(world, "search", sig, 2, "not-a-hit"))
    assert code == 1 and env["errors"][0]["code"] == "KV_CURSOR_STALE"


def test_nobody_pages_past_the_window_and_a_listing_says_to_narrow_instead(capsys, world):
    from knowledgevista.index.search import parse_query
    from knowledgevista.services import cursor as cursormod
    from knowledgevista.services import search_page

    deep = forged(world, "view", cursormod.signature_of("new"), cursormod.MAX_WINDOW - 5, None)
    code, env, _ = run_json(capsys, world, "view", "new", "--limit", "10", "--cursor", deep)
    assert code == 2 and "narrow" in env["errors"][0]["message"]
    sig = search_page.query_signature(parse_query("marker"))
    code, env, _ = run_json(capsys, world, "search", "marker", "--limit", "10", "--cursor", forged(world, "search", sig, cursormod.MAX_WINDOW - 5, None))
    assert code == 2 and "narrow" in env["errors"][0]["message"]
