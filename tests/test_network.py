"""The network layer and the Crossref client.

Two kinds of test, on purpose. The retry, pacing and budget RULES run against a scripted transport with injected
clock and sleep (instant and exact). The TRANSPORT itself runs against a real HTTP server on the loopback interface, so
timeouts, refused connections, truncated bodies and header handling are the real library's behaviour, not a mock's.
Nothing here touches the internet.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from knowledgevista import __version__
from knowledgevista.index import metacache
from knowledgevista.network.policy import Fetcher, LookupState, NetworkPolicy
from knowledgevista.network.transport import HttpResponse, TransportError, UrllibTransport
from knowledgevista.providers.crossref import CrossrefProvider, normalise_work

DOI = "10.5555/kv.net.0001"
WORK = {
    "DOI": DOI.upper(), "title": ["Cu<sub>2</sub>O Solubility: An <i>Invented</i> Study"], "subtitle": [],
    "author": [{"given": "Alexandra", "family": "Examplar", "sequence": "first"}, {"name": "The Placeholder Consortium"}],
    "issued": {"date-parts": [[2021, 7, 31]]}, "published-print": {"date-parts": [[2021, 8]]},
    "container-title": ["Journal of Invented Results"], "publisher": "Invented Press", "type": "journal-article",
    "volume": "12", "issue": "3", "page": "54-58", "score": 61.2,
}


def envelope(message: dict, kind: str = "work") -> bytes:
    return json.dumps({"status": "ok", "message-type": kind, "message": message}).encode()


class Scripted:
    """A transport that plays back a list of outcomes (an HttpResponse, or a TransportError to raise), repeating the last."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def get(self, url, headers, timeout, max_bytes):
        self.calls.append((url, dict(headers), timeout))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Clock:
    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def fetcher(*outcomes, **policy) -> tuple[Fetcher, Scripted, Clock]:
    clock, transport = Clock(), Scripted(*outcomes)
    defaults = dict(online=True, min_interval=1.0, max_retries=2, request_budget=100, backoff_base=1.0)
    return Fetcher(NetworkPolicy(**{**defaults, **policy}), transport, clock=clock.time, wall=clock.time, sleep=clock.sleep, rng=lambda: 0.5), transport, clock


def ok(body: bytes = b"{}", **headers) -> HttpResponse:
    return HttpResponse(200, {k.lower(): v for k, v in headers.items()}, body)


def status(code: int, **headers) -> HttpResponse:
    return HttpResponse(code, {k.lower(): v for k, v in headers.items()}, b"")


# ------------------------------------------------------------------------------------------- the rules (scripted)


def test_nothing_is_sent_unless_online_lookups_are_on():
    f, transport, _ = fetcher(ok(), online=False)
    outcome = f.get("https://api.example.org/x")
    assert outcome.state == LookupState.NOT_ATTEMPTED and "switched off" in outcome.detail
    assert transport.calls == [] and f.requests_made == 0


def test_only_https_is_sent_except_to_the_loopback_interface():
    f, transport, _ = fetcher(ok())
    with pytest.raises(ValueError):
        f.get("http://api.example.org/x")
    with pytest.raises(ValueError):
        f.get("ftp://127.0.0.1/x")
    assert f.get("http://127.0.0.1:9/x").state == LookupState.SUCCESS and len(transport.calls) == 1


def test_the_user_agent_names_the_program_and_carries_the_mailto_only_when_the_user_set_one():
    f, transport, _ = fetcher(ok())
    f.get("https://api.example.org/x")
    anonymous = transport.calls[0][1]["User-Agent"]
    assert anonymous.startswith(f"KnowledgeVista/{__version__} (") and "mailto" not in anonymous
    f2, transport2, _ = fetcher(ok(), mailto="me@example.org")
    f2.get("https://api.example.org/x")
    assert "mailto:me@example.org" in transport2.calls[0][1]["User-Agent"]


def test_requests_are_paced_and_the_pace_is_learned_from_the_servers_own_headers():
    f, _, clock = fetcher(ok(**{"x-rate-limit-limit": "5", "x-rate-limit-interval": "1s"}))
    f.get("https://api.example.org/1")
    assert f.interval == pytest.approx(0.25)  # 1s / 5 * 1.25 safety, read from the response, not written in the code
    f.get("https://api.example.org/2")
    f.get("https://api.example.org/3")
    assert clock.sleeps == [pytest.approx(0.25), pytest.approx(0.25)]


def test_without_rate_limit_headers_the_conservative_starting_pace_stays():
    f, _, clock = fetcher(ok())
    for number in range(3):
        f.get(f"https://api.example.org/{number}")
    assert f.interval == 1.0 and clock.sleeps == [pytest.approx(1.0), pytest.approx(1.0)]


def test_a_server_that_claims_a_huge_allowance_is_still_paced_by_the_floor():
    f, _, _ = fetcher(ok(**{"x-rate-limit-limit": "1000", "x-rate-limit-interval": "1s"}), floor_interval=0.2)
    f.get("https://api.example.org/1")
    assert f.interval == pytest.approx(0.2)


def test_404_is_no_match_and_not_a_failure():
    f, _, _ = fetcher(status(404))
    assert f.get("https://api.example.org/x").state == LookupState.NO_MATCH


@pytest.mark.parametrize("code", [401, 403])
def test_unauthorized_is_reported_and_never_retried(code):
    f, transport, _ = fetcher(status(code))
    assert f.get("https://api.example.org/x").state == LookupState.UNAUTHORIZED
    assert len(transport.calls) == 1


def test_a_rate_limit_is_waited_out_with_the_servers_retry_after_then_retried():
    f, transport, clock = fetcher(status(429, **{"Retry-After": "7"}), ok())
    outcome = f.get("https://api.example.org/x")
    assert outcome.state == LookupState.SUCCESS and len(transport.calls) == 2
    assert 7.0 in clock.sleeps


def test_a_retry_after_longer_than_the_run_will_wait_gives_up_at_once():
    f, transport, clock = fetcher(status(429, **{"Retry-After": "3600"}), ok(), max_wait=60)
    outcome = f.get("https://api.example.org/x")
    assert outcome.state == LookupState.RATE_LIMITED and outcome.retry_after == 3600
    assert len(transport.calls) == 1 and max(clock.sleeps, default=0) < 60  # it did not sleep for an hour


def test_a_retry_after_http_date_is_understood():
    f, transport, clock = fetcher(status(429, **{"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}), ok(), max_wait=60)
    clock.now = 1445412475.0  # five seconds before that date, in epoch seconds
    assert f.get("https://api.example.org/x").state == LookupState.SUCCESS and len(transport.calls) == 2
    assert any(s == pytest.approx(5.0) for s in clock.sleeps)


def test_server_errors_back_off_with_jitter_then_succeed_or_report_transient():
    f, transport, clock = fetcher(status(500), status(502), ok())
    assert f.get("https://api.example.org/x").state == LookupState.SUCCESS and len(transport.calls) == 3
    backoffs = [s for s in clock.sleeps if s >= 1.0 and s != 1.0]
    assert 1.0 in clock.sleeps or backoffs  # exponential: base * 2**attempt * (0.5 + rng()) with rng fixed at 0.5 -> 1.0, 2.0
    assert 2.0 in clock.sleeps
    g, t2, _ = fetcher(status(500))
    assert g.get("https://api.example.org/x").state == LookupState.TRANSIENT_ERROR and len(t2.calls) == 3


def test_503_is_provider_unavailable_after_the_retries():
    f, transport, _ = fetcher(status(503))
    assert f.get("https://api.example.org/x").state == LookupState.PROVIDER_UNAVAILABLE and len(transport.calls) == 3


def test_a_client_error_is_not_retried_and_not_a_no_match():
    f, transport, _ = fetcher(status(400))
    outcome = f.get("https://api.example.org/x")
    assert outcome.state == LookupState.PROVIDER_UNAVAILABLE and len(transport.calls) == 1


@pytest.mark.parametrize("kind, state", [("timeout", LookupState.TRANSIENT_ERROR), ("dns", LookupState.OFFLINE),
                                         ("connection", LookupState.OFFLINE), ("tls", LookupState.PROVIDER_UNAVAILABLE)])
def test_transport_failures_map_to_distinct_states(kind, state):
    f, transport, _ = fetcher(TransportError(kind, "boom"))
    assert f.get("https://api.example.org/x").state == state
    assert len(transport.calls) == (1 if kind == "tls" else 3)  # a certificate problem is not retried


def test_the_request_budget_bounds_a_run_and_retries_count_against_it():
    f, transport, _ = fetcher(status(500), request_budget=2, max_retries=5)
    outcome = f.get("https://api.example.org/x")
    assert outcome.state == LookupState.NOT_ATTEMPTED and "budget" in outcome.detail
    assert len(transport.calls) == 2 and f.budget_left == 0
    assert f.get("https://api.example.org/y").state == LookupState.NOT_ATTEMPTED and len(transport.calls) == 2


def test_an_oversized_response_is_refused_not_parsed():
    f, _, _ = fetcher(HttpResponse(200, {}, b"x" * 10, truncated=True))
    outcome = f.get("https://api.example.org/x")
    assert outcome.state == LookupState.PROVIDER_UNAVAILABLE and "larger than" in outcome.detail


def test_no_answer_states_are_never_a_definite_answer():
    assert {s for s in LookupState if not s.could_not_answer} == {LookupState.SUCCESS, LookupState.NO_MATCH}


# ------------------------------------------------------------------------------------------- a real server


class FakeCrossref:
    """A loopback HTTP server. `script[path_prefix]` is a list of (status, headers, body, delay) played in order."""

    def __init__(self):
        self.hits: list[tuple[str, dict]] = []
        self.script: dict[str, list] = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                owner.hits.append((self.path, dict(self.headers)))
                steps = next((v for k, v in owner.script.items() if self.path.startswith(k)), [(404, {}, b"", 0)])
                code, headers, body, delay = steps.pop(0) if len(steps) > 1 else steps[0]
                if delay:
                    threading.Event().wait(delay)
                try:
                    self.send_response(code)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except ConnectionError:
                    pass

            def log_message(self, *args):  # silence
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def crossref():
    server = FakeCrossref()
    yield server
    server.close()


def real_provider(server, tmp_path, *, cache=True, **policy):
    defaults = dict(online=True, min_interval=0.0, floor_interval=0.0, max_retries=1, backoff_base=0.0, timeout=2.0)
    fetch = Fetcher(NetworkPolicy(**{**defaults, **policy}), UrllibTransport(), sleep=lambda s: None)
    store = metacache.open_metacache(tmp_path / "meta.sqlite", create=True) if cache else None
    return CrossrefProvider(fetch, cache=store, base_url=server.url), store


def test_a_doi_lookup_over_a_real_socket_returns_the_normalised_work(crossref, tmp_path):
    crossref.script["/works/"] = [(200, {"x-rate-limit-limit": "5", "x-rate-limit-interval": "1s"}, envelope(WORK), 0)]
    provider, _ = real_provider(crossref, tmp_path, mailto="me@example.org")
    result = provider.lookup_doi(DOI)
    assert result.state == LookupState.SUCCESS and not result.from_cache
    work = result.work
    assert work["doi"] == DOI and work["title"] == "Cu2O Solubility: An Invented Study"  # JATS markup removed
    assert work["year"] == 2021 and work["years"] == {"issued": 2021, "print": 2021, "online": None}
    assert work["authors"][0] == {"family": "Examplar", "given": "Alexandra", "name": None}
    assert work["authors"][1]["name"] == "The Placeholder Consortium"
    assert (work["container"], work["publisher"], work["type"], work["pages"]) == ("Journal of Invented Results", "Invented Press", "journal-article", "54-58")
    path, headers = crossref.hits[0]
    assert path.startswith(f"/works/{DOI}") and "mailto=me%40example.org" in path
    assert headers["User-Agent"].startswith("KnowledgeVista/") and "mailto:me@example.org" in headers["User-Agent"]


def test_no_mailto_means_no_mailto_anywhere_in_the_request(crossref, tmp_path):
    crossref.script["/works/"] = [(200, {}, envelope(WORK), 0)]
    provider, _ = real_provider(crossref, tmp_path)
    provider.lookup_doi(DOI)
    path, headers = crossref.hits[0]
    assert "mailto" not in path and "mailto" not in headers["User-Agent"]


def test_a_second_lookup_is_served_from_the_cache_without_a_request(crossref, tmp_path):
    crossref.script["/works/"] = [(200, {}, envelope(WORK), 0)]
    provider, _ = real_provider(crossref, tmp_path)
    assert not provider.lookup_doi(DOI).from_cache
    again = provider.lookup_doi(DOI.upper())  # the same DOI, differently spelled
    assert again.from_cache and again.work["title"].startswith("Cu2O") and len(crossref.hits) == 1


def test_no_match_is_cached_so_an_unknown_doi_is_not_asked_for_again(crossref, tmp_path):
    crossref.script["/works/"] = [(404, {}, b"Resource not found.", 0)]
    provider, _ = real_provider(crossref, tmp_path)
    assert provider.lookup_doi(DOI).state == LookupState.NO_MATCH
    again = provider.lookup_doi(DOI)
    assert again.state == LookupState.NO_MATCH and again.from_cache and len(crossref.hits) == 1


def test_a_failure_is_never_cached_so_the_next_run_asks_again(crossref, tmp_path):
    crossref.script["/works/"] = [(500, {}, b"oops", 0)]
    provider, cache = real_provider(crossref, tmp_path)
    assert provider.lookup_doi(DOI).state == LookupState.TRANSIENT_ERROR
    first_hits = len(crossref.hits)
    assert cache.execute("SELECT COUNT(*) FROM response").fetchone()[0] == 0
    crossref.script["/works/"] = [(200, {}, envelope(WORK), 0)]  # the outage ends
    recovered = provider.lookup_doi(DOI)
    assert recovered.state == LookupState.SUCCESS and len(crossref.hits) == first_hits + 1


def test_garbage_and_wrong_shaped_responses_are_provider_unavailable_and_not_cached(crossref, tmp_path):
    provider, cache = real_provider(crossref, tmp_path)
    for body in (b"<html>not json</html>", json.dumps({"status": "failed"}).encode(), envelope({"title": ["no DOI"]}),
                 envelope(WORK, kind="work-list")):
        crossref.script["/works/"] = [(200, {}, body, 0)]
        assert provider.lookup_doi(DOI).state == LookupState.PROVIDER_UNAVAILABLE
    assert cache.execute("SELECT COUNT(*) FROM response").fetchone()[0] == 0


def test_a_slow_server_times_out_as_transient_over_a_real_socket(crossref, tmp_path):
    crossref.script["/works/"] = [(200, {}, envelope(WORK), 1.5)]
    provider, _ = real_provider(crossref, tmp_path, timeout=0.2, max_retries=0)
    assert provider.lookup_doi(DOI).state == LookupState.TRANSIENT_ERROR


def test_a_closed_port_is_offline_over_a_real_socket(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # nothing listens here once the probe closes
    fetch = Fetcher(NetworkPolicy(online=True, min_interval=0.0, floor_interval=0.0, max_retries=0, timeout=10.0), UrllibTransport(), sleep=lambda s: None)  # Windows takes ~2 s to refuse
    provider = CrossrefProvider(fetch, base_url=f"http://127.0.0.1:{port}")
    assert provider.lookup_doi(DOI).state == LookupState.OFFLINE


def test_the_real_transport_caps_the_body_and_lowercases_header_names(crossref):
    crossref.script["/big"] = [(200, {"X-Mixed-Case": "yes"}, b"y" * 5000, 0)]
    response = UrllibTransport().get(crossref.url + "/big", {"User-Agent": "t"}, 2.0, 1000)
    assert len(response.body) == 1000 and response.truncated and response.headers["x-mixed-case"] == "yes"


def test_a_doi_with_brackets_reaches_the_server_in_its_own_spelling(crossref, tmp_path):
    odd = "10.5555/(kv)odd;1:2"
    crossref.script["/works/"] = [(404, {}, b"", 0)]
    provider, _ = real_provider(crossref, tmp_path)
    provider.lookup_doi(odd)
    assert crossref.hits[0][0].startswith("/works/10.5555/(kv)odd;1:2")


def test_a_title_search_returns_ranked_normalised_items_and_is_cached(crossref, tmp_path):
    second = {**WORK, "DOI": "10.5555/kv.net.0002", "title": ["A Different Paper Entirely"], "score": 20.0}
    crossref.script["/works?"] = [(200, {}, json.dumps({"status": "ok", "message-type": "work-list", "message": {"items": [WORK, second]}}).encode(), 0)]
    provider, _ = real_provider(crossref, tmp_path)
    title = "Cu2O Solubility: An Invented Study of Considerable Length"
    result = provider.search_title(title)
    assert result.state == LookupState.SUCCESS and [i["doi"] for i in result.items] == [DOI, "10.5555/kv.net.0002"]
    path = crossref.hits[0][0]
    assert "query.bibliographic=" in path and "rows=5" in path and "select=" in path
    assert provider.search_title(title).from_cache and len(crossref.hits) == 1


def test_a_title_too_short_to_search_for_sends_nothing(crossref, tmp_path):
    provider, _ = real_provider(crossref, tmp_path)
    short = provider.search_title("Introduction")
    assert short.state == LookupState.NO_MATCH and crossref.hits == [] and short.sent is False
    assert provider.lookup_doi("not a doi").sent is False and crossref.hits == []


def test_an_empty_result_list_is_a_cached_no_match(crossref, tmp_path):
    crossref.script["/works?"] = [(200, {}, json.dumps({"status": "ok", "message-type": "work-list", "message": {"items": []}}).encode(), 0)]
    provider, _ = real_provider(crossref, tmp_path)
    title = "A Title That Nobody Has Ever Published Anywhere"
    assert provider.search_title(title).state == LookupState.NO_MATCH
    assert provider.search_title(title).from_cache and len(crossref.hits) == 1


def test_normalise_work_handles_missing_pieces_without_inventing_any():
    assert normalise_work({"title": ["no doi"]}) is None
    bare = normalise_work({"DOI": "10.5555/KV.BARE"})
    assert bare["doi"] == "10.5555/kv.bare" and bare["title"] is None and bare["authors"] == [] and bare["year"] is None
    assert bare["container"] is None and not bare["preprint"]
    preprint = normalise_work({"DOI": "10.5555/kv.pre", "type": "posted-content", "published-online": {"date-parts": [[2019]]}})
    assert preprint["preprint"] and preprint["year"] == 2019
    nested = normalise_work({"DOI": "10.5555/kv.x", "author": ["not a dict", {"family": ""}, {"family": "Real", "given": "A."}]})
    assert nested["authors"] == [{"family": "Real", "given": "A.", "name": None}]


def test_the_metacache_expires_entries_and_refuses_to_cache_a_failure(tmp_path):
    store = metacache.open_metacache(tmp_path / "m.sqlite", create=True)
    with pytest.raises(ValueError):
        metacache.put_response(store, "k", provider="p", kind="doi", request="r", state="transient_error", payload=None, client_version="1")
    metacache.put_response(store, "k", provider="p", kind="doi", request="r", state="success", payload="{}", client_version="1")
    assert metacache.get_response(store, "k") is not None
    store.execute("UPDATE response SET expires_at = '2000-01-01T00:00:00.000000Z'")
    assert metacache.get_response(store, "k") is None  # expired: an old answer is not served
    assert metacache.purge_expired(store) == 1


def test_a_stale_metacache_schema_is_rebuilt_not_migrated(tmp_path):
    path = tmp_path / "m.sqlite"
    store = metacache.open_metacache(path, create=True)
    metacache.put_front(store, "a" * 64, "1.0", ok=True, payload="{}", error=None)
    store.execute("PRAGMA user_version = 99")
    store.close()
    assert metacache.open_metacache(path, create=True, read_only=True) is None  # read-only never destroys it
    rebuilt = metacache.open_metacache(path, create=True)
    assert rebuilt.execute("SELECT COUNT(*) FROM front").fetchone()[0] == 0


def test_a_no_match_expires_sooner_than_a_match_because_a_work_can_be_registered_tomorrow(tmp_path):
    store = metacache.open_metacache(tmp_path / "m.sqlite", create=True)
    metacache.put_response(store, "hit", provider="p", kind="doi", request="r", state="success", payload="{}", client_version="1")
    metacache.put_response(store, "miss", provider="p", kind="doi", request="r", state="no_match", payload=None, client_version="1")
    expires = {r["cache_key"]: r["expires_at"] for r in store.execute("SELECT cache_key, expires_at FROM response")}
    assert expires["miss"] < expires["hit"]
    assert metacache.NO_MATCH_TTL < metacache.SUCCESS_TTL


def test_a_title_search_result_also_answers_the_lookup_of_its_own_doi(crossref, tmp_path):
    crossref.script["/works?"] = [(200, {}, json.dumps({"status": "ok", "message-type": "work-list", "message": {"items": [WORK]}}).encode(), 0)]
    provider, _ = real_provider(crossref, tmp_path)
    provider.search_title("Cu2O Solubility: An Invented Study of Considerable Length")
    hits = len(crossref.hits)
    looked_up = provider.lookup_doi(DOI)
    assert looked_up.from_cache and looked_up.work["title"].startswith("Cu2O") and len(crossref.hits) == hits  # no second request
