"""The metadata commands end to end through the real command line, in process.

Covers the contract (one JSON envelope on stdout, exit codes, stable error codes), the opt-in switch for the network
(off means no socket is touched, proved by making the transport fail the test if it is called), and the whole
offline-to-online workflow a person would actually follow: extract, resolve, review, accept, explain, stats.
"""

from __future__ import annotations

import json

import pdfbuilders as b
import pytest
from support import write
from test_network import FakeCrossref, envelope

from knowledgevista import cli
from knowledgevista.network import transport
from knowledgevista.providers.crossref import CrossrefProvider

TITLE = "A Synthetic Study of Imaginary Lattices"
JOURNAL = "Cite This: J. Invented Results 2021, 12, 54-58"
DOI = "10.5555/kv.cli.0001"


@pytest.fixture
def tripwire(monkeypatch):
    """Any attempt to send a request fails the test. The proof that 'off' means off."""
    def fail(self, url, headers, timeout, max_bytes):
        raise AssertionError(f"a request was sent to {url}")

    monkeypatch.setattr(transport.UrllibTransport, "get", fail)


@pytest.fixture
def library(tmp_path):
    lib = tmp_path / "papers"
    lib.mkdir()
    b.native_pdf(lib / "kaya2022.pdf", title=TITLE, doi=DOI, journal=JOURNAL)
    b.native_pdf(lib / "nodoi.pdf", title="Another Study of Quite Different Matters Entirely", doi=None, authors="C. Writer", journal=JOURNAL)
    write(lib / "notes.txt", "not a pdf")
    return lib


def kv(capsys, tmp_path, *argv, expect=0):
    code = cli.main(["--json", "--catalog", str(tmp_path / "c.sqlite"), *argv])
    captured = capsys.readouterr()
    assert code == expect, f"{argv}: exit {code}\n{captured.out}\n{captured.err}"
    assert captured.out.count("\n") == 1, "stdout is exactly one JSON document"
    return json.loads(captured.out), captured.err


@pytest.fixture
def extracted(capsys, tmp_path, library):
    kv(capsys, tmp_path, "root", "add", str(library))
    kv(capsys, tmp_path, "scan")
    kv(capsys, tmp_path, "extract")
    return tmp_path


def records(envelope_, kind):
    return [r for r in envelope_["records"] if r["type"] == kind]


# ------------------------------------------------------------------------------------------------ config


def test_online_lookups_are_off_by_default_and_the_settings_round_trip(capsys, tmp_path):
    env, _ = kv(capsys, tmp_path, "config", "list")
    values = {r["key"]: r["value"] for r in env["records"]}
    assert values == {"online_lookup": False, "mailto": None, "request_budget": 1000}
    kv(capsys, tmp_path, "config", "set", "online_lookup", "true")
    kv(capsys, tmp_path, "config", "set", "mailto", "me@example.org")
    kv(capsys, tmp_path, "config", "set", "request_budget", "50")
    env, _ = kv(capsys, tmp_path, "config", "get", "mailto")
    assert env["records"][0]["value"] == "me@example.org"
    now = {r["key"]: r["value"] for r in kv(capsys, tmp_path, "config", "list")[0]["records"]}
    assert now == {"online_lookup": True, "mailto": "me@example.org", "request_budget": 50}
    kv(capsys, tmp_path, "config", "set", "mailto", "none")
    assert kv(capsys, tmp_path, "config", "get", "mailto")[0]["records"][0]["value"] is None


@pytest.mark.parametrize("argv", [("config", "set", "online_lookup", "maybe"), ("config", "set", "mailto", "not-an-email"),
                                  ("config", "set", "request_budget", "0"), ("config", "set", "request_budget", "lots"),
                                  ("config", "set", "nonsense", "1"), ("config", "get", "nonsense")])
def test_an_invalid_setting_is_refused_with_exit_2(capsys, tmp_path, argv):
    env, _ = kv(capsys, tmp_path, *argv, expect=2)
    assert env["errors"][0]["code"] == "KV_INVALID_ARGUMENTS"


def test_a_damaged_settings_file_means_offline_and_says_so(capsys, tmp_path):
    from knowledgevista import paths
    paths.settings_path().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_path().write_text("{ this is not json", encoding="utf-8")
    env, _ = kv(capsys, tmp_path, "config", "list")
    assert {r["key"]: r["value"] for r in env["records"]}["online_lookup"] is False
    assert env["warnings"][0]["code"] == "KV_SETTINGS_IGNORED"
    paths.settings_path().write_text(json.dumps({"online_lookup": "true"}), encoding="utf-8")  # a string is not the boolean true
    assert {r["key"]: r["value"] for r in kv(capsys, tmp_path, "config", "list")[0]["records"]}["online_lookup"] is False


# ------------------------------------------------------------------------------------------------ the switch


def test_online_resolve_is_refused_while_the_switch_is_off_and_no_request_is_made(capsys, tmp_path, extracted, tripwire):
    env, _ = kv(capsys, tmp_path, "resolve", "--online", expect=1)
    assert env["ok"] is False and env["errors"][0]["code"] == "KV_NETWORK_DISABLED"
    assert "kv config set online_lookup true" in env["errors"][0]["message"]


def test_plain_resolve_never_sends_a_request_even_with_the_switch_on(capsys, tmp_path, extracted, tripwire):
    kv(capsys, tmp_path, "config", "set", "online_lookup", "true")
    env, _ = kv(capsys, tmp_path, "resolve", "--accept-safe")
    assert env["ok"] and env["records"][0]["online"]["enabled"] is False


def test_resolve_before_extract_says_there_is_nothing_to_read(capsys, tmp_path, library):
    kv(capsys, tmp_path, "root", "add", str(library))
    kv(capsys, tmp_path, "scan")
    env, _ = kv(capsys, tmp_path, "resolve", expect=1)
    assert env["errors"][0]["code"] == "KV_NOT_EXTRACTED"


# ------------------------------------------------------------------------------------------------ the offline workflow


def test_resolve_review_accept_explain_stats_doctor_end_to_end(capsys, tmp_path, extracted, tripwire):
    env, err = kv(capsys, tmp_path, "resolve")
    summary = env["records"][0]
    assert summary["type"] == "summary" and summary["documents"] == 2 and summary["doi_classes"] == {"own": 1, "none": 1}
    assert summary["accepted"] == {"total": 0} and summary["review_queue_items"] > 0
    assert "resolving 1/2" in err and "resolving" not in json.dumps(env)

    queue, _ = kv(capsys, tmp_path, "review", "list")
    assert queue["complete"] is True
    items = records(queue, "item")
    doi_item = next(i for i in items if i["field"] == "doi")
    assert doi_item["value"] == DOI and doi_item["review"] == "safe" and doi_item["label"] == "kaya2022.pdf"

    explained, _ = kv(capsys, tmp_path, "explain", "kaya2022.pdf")
    assert explained["records"][0]["metadata"]["values"] == [] and explained["records"][0]["metadata"]["candidates"]

    accepted, _ = kv(capsys, tmp_path, "review", "accept", "--safe")
    assert {r["field"] for r in records(accepted, "accepted")} >= {"doi", "title"} and accepted["ok"]

    explained, _ = kv(capsys, tmp_path, "explain", "kaya2022.pdf")
    values = {v["field"]: v for v in explained["records"][0]["metadata"]["values"]}
    assert values["doi"]["value"] == DOI and values["doi"]["accepted_by"] == "rule:safe_batch_v1" and values["doi"]["locked"] is False

    stats, _ = kv(capsys, tmp_path, "stats")
    meta = stats["records"][0]["metadata"]
    assert meta["pdf_documents"] == 2 and meta["accepted"]["doi"] == 1 and meta["doi_coverage"] == 0.5
    assert sum(meta["titles_without"].values()) + meta["accepted"]["title"] == 2  # every PDF has a title or a stated reason
    assert set(meta["titles_without"]) <= {"candidates_awaiting_review", "no_title_found", "no_text_layer", "not_extracted", "not_resolved_yet"}

    doctor, _ = kv(capsys, tmp_path, "doctor")
    assert doctor["ok"] and doctor["records"][0]["categories_checked"][-1] == "metadata"


def test_a_second_resolve_changes_nothing_through_the_cli(capsys, tmp_path, extracted):
    kv(capsys, tmp_path, "resolve", "--accept-safe")
    first = kv(capsys, tmp_path, "stats")[0]["records"][0]["health"]["catalog_revision"]
    env, _ = kv(capsys, tmp_path, "resolve", "--accept-safe")
    assert env["records"][0]["accepted"] == {"total": 0} and set(env["records"][0]["local"]) <= {"unchanged", "decided"}
    assert kv(capsys, tmp_path, "stats")[0]["records"][0]["health"]["catalog_revision"] == first


def test_list_requests_shows_exactly_what_would_leave_the_machine_and_sends_nothing(capsys, tmp_path, extracted, tripwire):
    kv(capsys, tmp_path, "resolve")
    env, _ = kv(capsys, tmp_path, "resolve", "--list-requests")
    requests = records(env, "request")
    assert {(r["sends"], r["value"]) for r in requests} == {("doi", DOI), ("title", "Another Study of Quite Different Matters Entirely")}
    assert env["records"][0]["requests"] == 2 and env["ok"]


def test_review_reject_keeps_the_proposal_and_review_accept_needs_something_to_accept(capsys, tmp_path, extracted):
    kv(capsys, tmp_path, "resolve")
    items = records(kv(capsys, tmp_path, "review", "list", "--field", "doi")[0], "item")
    candidate = items[0]["candidate_ids"][0]
    env, _ = kv(capsys, tmp_path, "review", "reject", candidate[:10])
    assert env["records"][0] == {"type": "rejected", "candidate_id": candidate, "changed": True}
    assert records(kv(capsys, tmp_path, "review", "list", "--field", "doi")[0], "item") == []
    rejected = records(kv(capsys, tmp_path, "review", "list", "--field", "doi", "--status", "rejected")[0], "item")
    assert len(rejected) == 1  # kept, not deleted
    env, _ = kv(capsys, tmp_path, "review", "accept", expect=2)
    assert env["errors"][0]["code"] == "KV_INVALID_ARGUMENTS"
    env, _ = kv(capsys, tmp_path, "review", "accept", "00000000", expect=1)
    assert env["errors"][0]["code"] == "KV_NOT_FOUND"


def test_review_list_pages_and_says_when_it_is_incomplete(capsys, tmp_path, extracted):
    kv(capsys, tmp_path, "resolve")
    total = kv(capsys, tmp_path, "review", "list", "--limit", "1")[0]["records"][0]["total"]
    first, _ = kv(capsys, tmp_path, "review", "list", "--limit", "1")
    assert total > 1 and first["complete"] is False and len(records(first, "item")) == 1
    last, _ = kv(capsys, tmp_path, "review", "list", "--limit", "1", "--offset", str(total - 1))
    assert last["complete"] is True and len(records(last, "item")) == 1


def test_metadata_set_lock_clear_and_the_locked_refusal(capsys, tmp_path, extracted):
    kv(capsys, tmp_path, "resolve")
    env, _ = kv(capsys, tmp_path, "metadata", "set", "kaya2022.pdf", "year", "2022")
    assert env["records"][0]["value"] == "2022" and env["records"][0]["locked"] is True
    env, _ = kv(capsys, tmp_path, "metadata", "set", "kaya2022.pdf", "year", "twenty", expect=2)
    assert env["errors"][0]["code"] == "KV_INVALID_ARGUMENTS"
    titles = records(kv(capsys, tmp_path, "review", "list", "--field", "title")[0], "item")
    candidate = next(i for i in titles if i["label"] == "kaya2022.pdf")["candidate_ids"][0]
    kv(capsys, tmp_path, "review", "accept", candidate)
    kv(capsys, tmp_path, "metadata", "lock", "kaya2022.pdf", "title")
    kv(capsys, tmp_path, "metadata", "set", "kaya2022.pdf", "title", "A Title I Typed Over It, Deliberately", "--no-lock")
    env, _ = kv(capsys, tmp_path, "metadata", "clear", "kaya2022.pdf", "year")
    assert env["records"][0]["value"] is None
    env, _ = kv(capsys, tmp_path, "metadata", "unlock", "kaya2022.pdf", "year", expect=1)  # nothing to unlock: not found, not silent
    assert env["errors"][0]["code"] == "KV_NOT_FOUND"


def test_accepting_into_a_locked_field_is_refused_with_its_own_code(capsys, tmp_path, extracted):
    kv(capsys, tmp_path, "resolve")
    kv(capsys, tmp_path, "metadata", "set", "kaya2022.pdf", "doi", "10.5555/kv.cli.9999")
    doi = records(kv(capsys, tmp_path, "review", "list", "--field", "doi")[0], "item")[0]["candidate_ids"][0]
    env, _ = kv(capsys, tmp_path, "review", "accept", doi, expect=1)
    assert env["errors"][0]["code"] == "KV_METADATA_LOCKED"


# ------------------------------------------------------------------------------------------------ the online path via the CLI


@pytest.fixture
def crossref(monkeypatch):
    server = FakeCrossref()
    monkeypatch.setattr(cli, "CrossrefProvider", lambda fetcher, cache=None: CrossrefProvider(fetcher, cache=cache, base_url=server.url))
    yield server
    server.close()


def test_online_resolve_through_the_cli_fills_the_metadata_and_reports_the_requests(capsys, tmp_path, extracted, crossref):
    kv(capsys, tmp_path, "config", "set", "online_lookup", "true")
    kv(capsys, tmp_path, "config", "set", "mailto", "me@example.org")
    crossref.script["/works/"] = [(200, {}, envelope({
        "DOI": DOI.upper(), "title": [TITLE], "author": [{"given": "A.", "family": "Examplar"}, {"given": "B.", "family": "Placeholder"}],
        "issued": {"date-parts": [[2021]]}, "container-title": ["Journal of Invented Results"], "type": "journal-article", "volume": "12"}), 0)]
    crossref.script["/works?"] = [(200, {}, json.dumps({"status": "ok", "message-type": "work-list", "message": {"items": []}}).encode(), 0)]
    env, err = kv(capsys, tmp_path, "resolve", "--online", "--accept-safe")
    summary = env["records"][0]
    assert env["ok"] and env["complete"] is True and summary["online"]["enabled"] is True
    assert summary["online"]["states"].get("success") == 1 and summary["online"]["verdicts"] == {"exact": 1}
    assert "online: DOIs and titles will be sent to api.crossref.org" in err and "at most 1000 requests" in err
    explained, _ = kv(capsys, tmp_path, "explain", "kaya2022.pdf")
    values = {v["field"]: v["value"] for v in explained["records"][0]["metadata"]["values"]}
    assert values["year"] == "2021" and values["container"] == "Journal of Invented Results" and values["volume"] == "12"
    assert all("mailto=me%40example.org" in path or "mailto" not in path for path, _ in crossref.hits)
    assert not any("kaya2022" in path or "papers" in path for path, _ in crossref.hits)


def test_a_provider_that_asks_us_to_wait_stops_the_run_incomplete_but_not_failed(capsys, tmp_path, extracted, crossref):
    kv(capsys, tmp_path, "config", "set", "online_lookup", "true")
    crossref.script["/works"] = [(429, {"Retry-After": "3600"}, b"slow down", 0)]
    env, _ = kv(capsys, tmp_path, "resolve", "--online", "--accept-safe")
    assert env["ok"] is True and env["complete"] is False
    assert env["warnings"][0]["code"] == "KV_PROVIDER_STOPPED" and "3600" in env["warnings"][0]["message"]
    values = {v["field"] for v in kv(capsys, tmp_path, "explain", "kaya2022.pdf")[0]["records"][0]["metadata"]["values"]}
    assert "doi" in values  # the local evidence still earned its safe DOI; the outage changed nothing


def test_max_requests_bounds_the_run(capsys, tmp_path, extracted, crossref):
    kv(capsys, tmp_path, "config", "set", "online_lookup", "true")
    crossref.script["/works"] = [(404, {}, b"not found", 0)]
    env, err = kv(capsys, tmp_path, "resolve", "--online", "--max-requests", "1")
    assert "at most 1 requests" in err and len(crossref.hits) == 1
    assert env["complete"] is False and "budget" in env["records"][0]["online"]["stopped"]
