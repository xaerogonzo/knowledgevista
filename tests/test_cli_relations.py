"""Relations, duplicates, merge/split, collections, tags, saved searches, views and search filters, end to end through `kv`.

Everything a person would type is typed here, in process, and every result is read as the JSON envelope a script would get.
"""

from __future__ import annotations

import json

import pdfbuilders as b
import pytest
from support import tree_hashes, write

from knowledgevista import cli

TITLE = "Aqueous Solubility of Invented Esters"
DOI = "10.5555/kv.cli.rel.1"
JOURNAL = "Cite This: J. Invented Results 2021, 12, 54-58"
BODY = 8


def kv(capsys, tmp_path, *argv, expect=0):
    code = cli.main(["--json", "--catalog", str(tmp_path / "c.sqlite"), *argv])
    captured = capsys.readouterr()
    assert code == expect, f"{argv}: exit {code}\n{captured.out}\n{captured.err}"
    assert captured.out.count("\n") == 1, "stdout is exactly one JSON document"
    return json.loads(captured.out)


def of(env, kind):
    return [r for r in env["records"] if r["type"] == kind]


@pytest.fixture
def world(capsys, tmp_path):
    lib = tmp_path / "papers"
    lib.mkdir()
    b.native_pdf(lib / "a.pdf", title=TITLE, doi=DOI, journal=JOURNAL, body_pages=BODY, producer="One")
    b.native_pdf(lib / "b.pdf", title=TITLE, doi=DOI, journal=JOURNAL, body_pages=BODY, producer="Two")  # same text, other bytes
    b.native_pdf(lib / "paper.pdf", title="Another Study of Quite Different Things", doi="10.5555/kv.cli.rel.2", journal=JOURNAL, body_pages=BODY + 1, authors="C. Writer")
    b.native_pdf(lib / "supp.pdf", title="Supporting Information", doi="10.5555/kv.cli.rel.2", journal="Supporting Information for: Another Study of Quite Different Things",
                 body_pages=BODY + 2, authors="C. Writer")
    write(lib / "x" / "same.txt", "identical bytes")
    write(lib / "y" / "same.txt", "identical bytes")
    kv(capsys, tmp_path, "root", "add", str(lib))
    kv(capsys, tmp_path, "scan")
    kv(capsys, tmp_path, "extract")
    kv(capsys, tmp_path, "metadata", "set", "paper.pdf", "doi", "10.5555/kv.cli.rel.2")
    kv(capsys, tmp_path, "metadata", "set", "paper.pdf", "title", "Another Study of Quite Different Things")
    kv(capsys, tmp_path, "metadata", "set", "supp.pdf", "doi", "10.5555/kv.cli.rel.2")
    kv(capsys, tmp_path, "metadata", "set", "supp.pdf", "title", "Supporting Information")
    return tmp_path, lib


def doc_id(capsys, tmp_path, name) -> str:
    return kv(capsys, tmp_path, "explain", name)["records"][0]["document_id"]


# ------------------------------------------------------------------------------------------------ relate and review


def test_relate_proposes_with_evidence_and_changes_nothing_else(capsys, world):
    tmp, lib = world
    before = tree_hashes(lib)
    stats_before = kv(capsys, tmp, "stats")["records"][0]["inventory"]["documents"]
    env = kv(capsys, tmp, "relate")
    summary = env["records"][0]
    assert env["ok"] and summary["type"] == "summary" and summary["fingerprinted"] >= 3
    assert summary["proposals"]["same_document"] >= 1 and summary["proposals"]["supplement_of"] == 1 and summary["exact_copy_groups"] == 1
    assert kv(capsys, tmp, "stats")["records"][0]["inventory"]["documents"] == stats_before  # nothing merged
    assert tree_hashes(lib) == before
    listed = kv(capsys, tmp, "relations", "list")
    kinds = {p["kind"] for p in of(listed, "proposal")}
    assert {"same_document", "equivalent_to", "supplement_of"} <= kinds and all(p["evidence"] for p in of(listed, "proposal"))
    assert listed["complete"] is True and listed["records"][0]["total"] == len(of(listed, "proposal"))


def test_a_second_relate_changes_nothing(capsys, world):
    tmp, _ = world
    kv(capsys, tmp, "relate")
    revision = kv(capsys, tmp, "stats")["records"][0]["health"]["catalog_revision"]
    again = kv(capsys, tmp, "relate")["records"][0]
    assert set(again["outcomes"]) == {"unchanged"} and kv(capsys, tmp, "stats")["records"][0]["health"]["catalog_revision"] == revision


def test_accepting_the_merge_makes_one_document_with_both_artifacts_and_explain_tells_the_story(capsys, world):
    tmp, _ = world
    kv(capsys, tmp, "relate")
    a, b_ = doc_id(capsys, tmp, "a.pdf"), doc_id(capsys, tmp, "b.pdf")
    merge = next(p for p in of(kv(capsys, tmp, "relations", "list", "--kind", "same_document"), "proposal") if {p["source"], p["target"]} == {a, b_})
    accepted = kv(capsys, tmp, "relations", "accept", merge["candidate_id"][:10])
    assert accepted["records"][0]["effect"] == "merged"
    survivor, retired = accepted["records"][0]["kept"], accepted["records"][0]["absorbed"]
    explained = kv(capsys, tmp, "explain", "a.pdf")["records"][0]
    assert explained["document_id"] == survivor and len(explained["artifacts"]) == 2
    assert [e["event"] for e in explained["relations"]["history"]] == ["absorbed"]
    stats = kv(capsys, tmp, "stats")["records"][0]
    assert stats["inventory"]["documents_merged_away"] == 1 and stats["organization"]["documents_with_several_artifacts"] == 1
    retired_view = kv(capsys, tmp, "explain", retired)["records"][0]
    assert retired_view["relations"]["merged_into"] == survivor and retired_view["artifacts"] == []
    assert kv(capsys, tmp, "doctor")["ok"]  # a merge leaves a catalog doctor is content with


def test_split_after_a_merge_gives_the_original_document_back(capsys, world):
    tmp, _ = world
    a, b_ = doc_id(capsys, tmp, "a.pdf"), doc_id(capsys, tmp, "b.pdf")
    merged = kv(capsys, tmp, "document", "merge", a, b_, "--reason", "same article")["records"][0]
    assert merged["kept"] == a
    artifact = kv(capsys, tmp, "explain", "b.pdf")["records"][0]["artifacts"]
    b_artifact = next(x["artifact_id"] for x in artifact if any(loc["path"] == "b.pdf" for loc in x["locations"]))
    split = kv(capsys, tmp, "document", "split", a, b_artifact[:12])["records"][0]
    assert split["document_id"] == b_ and split["revived"] is True
    assert doc_id(capsys, tmp, "b.pdf") == b_ and doc_id(capsys, tmp, "a.pdf") == a
    assert kv(capsys, tmp, "stats")["records"][0]["inventory"]["documents_merged_away"] == 0


def test_a_supplement_is_accepted_as_a_relation_and_both_ends_show_it(capsys, world):
    tmp, _ = world
    kv(capsys, tmp, "relate")
    proposal = next(p for p in of(kv(capsys, tmp, "relations", "list", "--kind", "supplement_of"), "proposal"))
    assert proposal["confidence"] == "high"
    kv(capsys, tmp, "relations", "accept", proposal["candidate_id"])
    paper, supp = doc_id(capsys, tmp, "paper.pdf"), doc_id(capsys, tmp, "supp.pdf")
    on_paper = kv(capsys, tmp, "related", "paper.pdf")["records"][0]
    assert [(r["kind"], r["direction"], r["other_document"]) for r in on_paper["document"]] == [("supplement_of", "incoming", supp)]
    on_supp = kv(capsys, tmp, "related", "supp.pdf")["records"][0]
    assert [(r["kind"], r["direction"], r["other_document"]) for r in on_supp["document"]] == [("supplement_of", "outgoing", paper)]
    assert paper != supp and kv(capsys, tmp, "relations", "list", "--kind", "supplement_of")["records"][0]["total"] == 0  # accepted: no longer waiting


def test_stating_a_relation_yourself_and_taking_it_back(capsys, world):
    tmp, _ = world
    env = kv(capsys, tmp, "relations", "add", "version_of", "a.pdf", "paper.pdf", "--note", "a draft of it")
    relation_id = env["records"][0]["relation_id"]
    assert env["records"][0]["created"] is True
    assert kv(capsys, tmp, "relations", "add", "version_of", "a.pdf", "paper.pdf")["records"][0]["created"] is False
    bad = kv(capsys, tmp, "relations", "add", "version_of", "paper.pdf", "a.pdf", expect=2)  # would be its own ancestor
    assert bad["errors"][0]["code"] == "KV_INVALID_ARGUMENTS" and "ancestor" in bad["errors"][0]["message"]
    wrong_level = kv(capsys, tmp, "relations", "add", "derivative_of", "a.pdf", "paper.pdf", expect=2)
    assert wrong_level["errors"][0]["code"] == "KV_INVALID_ARGUMENTS"
    artifact = kv(capsys, tmp, "relations", "add", "derivative_of", "a.pdf", "paper.pdf", "--artifacts")
    assert artifact["records"][0]["level"] == "artifact"
    assert kv(capsys, tmp, "relations", "remove", relation_id[:10])["records"][0]["changed"] is True
    assert kv(capsys, tmp, "related", "a.pdf")["records"][0]["document"] == []


def test_reject_keeps_the_proposal_out_of_the_default_list_and_in_the_record(capsys, world):
    tmp, _ = world
    kv(capsys, tmp, "relate")
    first = of(kv(capsys, tmp, "relations", "list"), "proposal")[0]
    assert kv(capsys, tmp, "relations", "reject", first["candidate_id"])["records"][0]["changed"] is True
    assert first["candidate_id"] not in {p["candidate_id"] for p in of(kv(capsys, tmp, "relations", "list"), "proposal")}
    assert first["candidate_id"] in {p["candidate_id"] for p in of(kv(capsys, tmp, "relations", "list", "--status", "rejected"), "proposal")}
    kv(capsys, tmp, "relate")  # finding the evidence again does not reopen it
    assert first["candidate_id"] not in {p["candidate_id"] for p in of(kv(capsys, tmp, "relations", "list"), "proposal")}


def test_dupes_reports_the_four_levels_apart(capsys, world):
    tmp, _ = world
    kv(capsys, tmp, "relate")
    env = kv(capsys, tmp, "dupes")
    summary = env["records"][0]
    assert summary["exact_bytes"] == 1 and summary["identical_text"] >= 2 and summary["documents_with_several_artifacts"] == 0
    exact = of(env, "exact_bytes")[0]
    assert exact["copies"] == 2 and {p["path"] for p in exact["paths"]} == {"x/same.txt", "y/same.txt"}


def test_relate_without_extracted_text_still_runs_and_says_what_it_could_not_do(capsys, tmp_path):
    lib = tmp_path / "papers"
    lib.mkdir()
    b.native_pdf(lib / "a.pdf", title=TITLE, doi=DOI, journal=JOURNAL)
    kv(capsys, tmp_path, "root", "add", str(lib))
    kv(capsys, tmp_path, "scan")
    env = kv(capsys, tmp_path, "relate")
    assert env["ok"] and env["warnings"][0]["code"] == "KV_NO_TEXT_INDEX" and env["records"][0]["fingerprinted"] == 0


# ------------------------------------------------------------------------------------------------ collections, tags, saved, views


def test_collections_tags_and_the_files_they_never_touch(capsys, world):
    tmp, lib = world
    before = tree_hashes(lib)
    kv(capsys, tmp, "collection", "create", "To Read", "a.pdf", "paper.pdf")
    assert kv(capsys, tmp, "collection", "add", "to read", "supp.pdf")["records"][0]["added"] == 1
    shown = kv(capsys, tmp, "collection", "show", "To Read")
    assert shown["records"][0]["members"] == 3 and {m["label"] for m in of(shown, "member")} == {"a.pdf", "paper.pdf", "supp.pdf"}
    assert kv(capsys, tmp, "collection", "create", "TO  READ", expect=2)["errors"][0]["code"] == "KV_INVALID_ARGUMENTS"
    kv(capsys, tmp, "tag", "add", "energetics", "a.pdf", "paper.pdf")
    assert {t["tag"]: t["documents"] for t in of(kv(capsys, tmp, "tag", "list"), "tag")} == {"energetics": 2}
    explained = kv(capsys, tmp, "explain", "a.pdf")["records"][0]["organization"]
    assert explained == {"collections": ["To Read"], "tags": ["energetics"]}
    assert kv(capsys, tmp, "collection", "remove", "To Read", "supp.pdf")["records"][0]["removed"] == 1
    assert kv(capsys, tmp, "collection", "delete", "To Read")["ok"]
    assert of(kv(capsys, tmp, "collection", "list"), "collection") == []
    assert tree_hashes(lib) == before


def test_a_search_can_be_saved_and_run_again_and_stores_the_query_not_the_results(capsys, world):
    tmp, _ = world
    kv(capsys, tmp, "tag", "add", "energetics", "paper.pdf")
    env = kv(capsys, tmp, "search", "aqueous solubility tag:energetics", "--save", "Solubility")
    assert of(env, "saved")[0]["name"] == "Solubility" and env["records"][0]["query"]["filters"] == [["tag", "energetics"]]
    listed = of(kv(capsys, tmp, "saved", "list"), "saved")
    assert listed[0]["query"]["alternatives"] == ["aqueous solubility"] and "hits" not in json.dumps(listed[0])
    first = kv(capsys, tmp, "saved", "run", "solubility")
    assert first["records"][0]["hits"] >= 1 and {h["paths"][0]["path"] for h in of(first, "hit")} == {"paper.pdf"}
    kv(capsys, tmp, "tag", "add", "energetics", "a.pdf")  # the library changed; the saved query meant the same thing, so the answer follows
    second = kv(capsys, tmp, "saved", "run", "Solubility")
    assert {h["paths"][0]["path"] for h in of(second, "hit")} == {"paper.pdf", "a.pdf"}
    assert kv(capsys, tmp, "search", "aqueous", "--save", "Solubility", expect=2)["errors"][0]["code"] == "KV_INVALID_ARGUMENTS"
    assert kv(capsys, tmp, "search", "aqueous", "--save", "Solubility", "--replace")["records"][-1]["replaced"] is True
    assert kv(capsys, tmp, "saved", "delete", "solubility")["ok"] and kv(capsys, tmp, "saved", "run", "solubility", expect=1)["errors"][0]["code"] == "KV_NOT_FOUND"


def test_views_list_and_run_and_leave_when_the_reason_is_gone(capsys, world):
    tmp, _ = world
    names = {v["name"] for v in of(kv(capsys, tmp, "view"), "view")}
    assert {"inbox", "unresolved", "missing", "duplicates", "new", "untagged", "uncollected", "ambiguous"} == names
    untagged = kv(capsys, tmp, "view", "untagged", "--limit", "2")
    assert untagged["complete"] is False and untagged["records"][0]["shown"] == 2 and untagged["records"][0]["total"] >= 5
    kv(capsys, tmp, "tag", "add", "t", "a.pdf")
    assert "a.pdf" not in {i["label"] for i in of(kv(capsys, tmp, "view", "untagged", "--limit", "50"), "item")}
    assert kv(capsys, tmp, "view", "nonsense", expect=1)["errors"][0]["code"] == "KV_NOT_FOUND"
    duplicates = of(kv(capsys, tmp, "view", "duplicates"), "item")
    assert {"x/same.txt", "y/same.txt"} & {i["label"] for i in duplicates}


# ------------------------------------------------------------------------------------------------ search filters


def test_filters_narrow_a_search_by_accepted_metadata_tags_and_collections(capsys, world):
    tmp, _ = world
    kv(capsys, tmp, "metadata", "set", "paper.pdf", "year", "2018")
    kv(capsys, tmp, "metadata", "set", "paper.pdf", "authors", "Writer; Other")
    kv(capsys, tmp, "metadata", "set", "a.pdf", "year", "2022")
    kv(capsys, tmp, "tag", "add", "keep", "paper.pdf")
    kv(capsys, tmp, "collection", "create", "Set", "a.pdf")

    def paths(*query):
        env = kv(capsys, tmp, "search", *query, "--limit", "50")
        return {h["paths"][0]["path"] for h in of(env, "hit")}, env["records"][0]

    everywhere, _ = paths("aqueous solubility")
    assert {"a.pdf", "b.pdf", "paper.pdf", "supp.pdf"} <= everywhere
    assert paths("aqueous solubility tag:keep")[0] == {"paper.pdf"}
    assert paths("aqueous solubility collection:set")[0] == {"a.pdf"}
    assert paths("aqueous solubility year:2015-2020")[0] == {"paper.pdf"}
    assert paths("aqueous solubility year:2022")[0] == {"a.pdf"}
    assert paths("aqueous solubility year:2019-")[0] == {"a.pdf"} and paths("aqueous solubility year:-2019")[0] == {"paper.pdf"}
    assert paths("aqueous solubility author:writer")[0] == {"paper.pdf"}
    assert paths("aqueous solubility doi:10.5555/KV.CLI.REL.2")[0] == {"paper.pdf", "supp.pdf"}
    assert paths("aqueous solubility tag:keep year:2022")[0] == set()  # all filters must hold
    nothing, summary = paths("aqueous solubility tag:nonexistent")
    assert nothing == set() and summary["scope"] == {"filters": [["tag", "nonexistent"]], "documents_matching": 0}  # "no hits" says why


def test_a_proposal_never_narrows_a_search_only_accepted_metadata_does(capsys, world):
    tmp, _ = world
    kv(capsys, tmp, "resolve")  # proposals only: nothing accepted by running it
    env = kv(capsys, tmp, "search", f"aqueous doi:{DOI}")
    assert of(env, "hit") == [] and env["records"][0]["scope"]["documents_matching"] == 0


def test_a_merged_away_document_is_not_a_filter_hit_and_text_that_only_looks_like_a_filter_is_text(capsys, world):
    tmp, _ = world
    a, b_ = doc_id(capsys, tmp, "a.pdf"), doc_id(capsys, tmp, "b.pdf")
    kv(capsys, tmp, "tag", "add", "dup", "a.pdf", "b.pdf")
    kv(capsys, tmp, "document", "merge", a, b_)
    env = kv(capsys, tmp, "search", "aqueous solubility tag:dup")
    assert env["records"][0]["scope"]["documents_matching"] == 1  # one live document now, though it has two files
    plain = kv(capsys, tmp, "search", "Cu(II): at pH:7.4 | solubility")
    assert plain["records"][0]["query"]["filters"] == [] and "Cu(II):" in plain["records"][0]["query"]["alternatives"][0]


@pytest.mark.parametrize("query", ["solubility year:", "solubility year:twenty", "solubility year:20", "solubility doi:notadoi", "solubility tag:"])
def test_a_malformed_filter_is_a_query_error_never_a_search_that_ignores_it(capsys, world, query):
    tmp, _ = world
    env = kv(capsys, tmp, "search", query, expect=2)
    assert env["errors"][0]["code"] == "KV_QUERY_INVALID"


def test_a_batch_skips_a_stale_proposal_and_says_so_while_a_lone_stale_one_is_refused(capsys, world):
    import sqlite3
    tmp, _ = world
    kv(capsys, tmp, "relate")
    supp = next(p for p in of(kv(capsys, tmp, "relations", "list", "--kind", "supplement_of"), "proposal"))["candidate_id"]
    merge = next(p for p in of(kv(capsys, tmp, "relations", "list", "--kind", "same_document"), "proposal"))["candidate_id"]
    conn = sqlite3.connect(tmp / "c.sqlite")
    conn.execute("UPDATE relation_candidate SET status = 'stale' WHERE candidate_id = ?", (merge,))
    conn.commit()
    conn.close()
    lone = kv(capsys, tmp, "relations", "accept", merge, expect=2)
    assert lone["errors"][0]["code"] == "KV_INVALID_ARGUMENTS" and "kv relate" in lone["errors"][0]["message"]
    env = kv(capsys, tmp, "relations", "accept", merge, supp)
    assert [r["kind"] for r in of(env, "accepted")] == ["supplement_of"]  # the live one went through
    assert [w["code"] for w in env["warnings"]] == ["KV_PROPOSAL_STALE"] and env["warnings"][0]["details"]["candidate_id"] == merge
    assert kv(capsys, tmp, "stats")["records"][0]["inventory"]["documents"] == 5  # and nothing merged
