"""The resolver over the REAL HTTP client, cache and pacing, against a loopback stand-in for Crossref.

What the scripted-provider tests cannot show: that a killed or interrupted run does not repeat a request that was
answered (the cache is the resume mechanism), that an outage never poisons the cache, and exactly what bytes of a
request leave the machine.
"""

from __future__ import annotations

import json

import pdfbuilders as b
import pytest
from support import make_env
from test_network import FakeCrossref, envelope

from knowledgevista.extract.client import ExtractionSession
from knowledgevista.index import metacache, store
from knowledgevista.network.policy import Fetcher, NetworkPolicy
from knowledgevista.network.transport import UrllibTransport
from knowledgevista.providers.crossref import CrossrefProvider
from knowledgevista.services import metadata
from knowledgevista.services.extract import extract_library
from knowledgevista.services.resolve_metadata import resolve_library

TITLE1, TITLE2 = "A Synthetic Study of Imaginary Lattices", "Another Study of Quite Different Matters Entirely"
DOI1, DOI2 = "10.5555/kv.net.one", "10.5555/kv.net.two"


def record(doi, title, families=("Examplar", "Placeholder")) -> bytes:
    return envelope({
        "DOI": doi.upper(), "title": [title], "author": [{"given": "A.", "family": f, "sequence": "first"} for f in families],
        "issued": {"date-parts": [[2021, 7, 31]]}, "container-title": ["Journal of Invented Results"], "publisher": "Invented Press",
        "type": "journal-article", "volume": "12", "page": "54-58",
    })


class World:
    def __init__(self, tmp_path, pdfs):
        self.server = FakeCrossref()
        self.env = make_env(tmp_path, {})
        for name, kwargs in pdfs.items():
            b.native_pdf(self.env.lib / name, journal="Cite This: J. Invented Results 2021, 12, 54-58", **kwargs)
        self.env.scan()
        self.index = store.open_index(tmp_path / "idx" / "extractions.sqlite", create=True)
        self.cache = metacache.open_metacache(tmp_path / "idx" / "metadata.sqlite", create=True)
        self.session = ExtractionSession()
        extract_library(self.env.conn, self.index, session=self.session)

    def run(self, mailto=None):
        fetch = Fetcher(NetworkPolicy(online=True, mailto=mailto, min_interval=0.0, floor_interval=0.0, max_retries=0, timeout=3.0),
                        UrllibTransport(), sleep=lambda s: None)
        provider = CrossrefProvider(fetch, cache=self.cache, base_url=self.server.url)
        return resolve_library(self.env.conn, self.index, self.cache, session=self.session, provider=provider, online=True, accept_safe=True)

    def close(self):
        self.session.close()
        self.index.close()
        self.cache.close()
        self.server.close()


@pytest.fixture
def make_world(tmp_path):
    made = []

    def build(pdfs):
        made.append(World(tmp_path, pdfs))
        return made[-1]

    yield build
    for world in made:
        world.close()


@pytest.fixture
def world(make_world):
    w = make_world({"secret-name-42.pdf": {"title": TITLE1, "doi": DOI1}, "other.pdf": {"title": TITLE2, "doi": DOI2}})
    yield w.env, w.server, w.run, w.cache


def accepted(env, name):
    document = env.one("SELECT da.document_id FROM location l JOIN document_artifact da ON da.artifact_id = l.artifact_id "
                       "WHERE l.relative_path = ? AND l.ended_at IS NULL", name)
    return {f: v["value"] for f, v in metadata.get_values(env.conn, document).items()}


def test_a_run_stopped_by_an_outage_resumes_without_repeating_what_was_answered(world):
    env, server, run, cache = world
    server.script["/works/10.5555/kv.net.one"] = [(200, {}, record(DOI1, TITLE1), 0)]
    server.script["/works/10.5555/kv.net.two"] = [(500, {}, b"down", 0)]
    first = run()
    # The confirmed document has the provider's fields; the one the provider could not answer for keeps only what its own
    # evidence earned (a safe printed DOI and a title read two ways) and has nothing from the provider.
    assert accepted(env, "secret-name-42.pdf")["year"] == "2021" and "year" not in accepted(env, "other.pdf")
    assert first["online"]["states"].get("transient_error") == 1
    assert cache.execute("SELECT COUNT(*) FROM response").fetchone()[0] == 1  # the success is cached; the failure is not
    one_hits = sum(path.startswith("/works/10.5555/kv.net.one") for path, _ in server.hits)

    server.script["/works/10.5555/kv.net.two"] = [(200, {}, record(DOI2, TITLE2), 0)]  # the outage ends
    second = run()
    assert accepted(env, "other.pdf")["year"] == "2021" and accepted(env, "other.pdf")["title"] == TITLE2
    assert sum(path.startswith("/works/10.5555/kv.net.one") for path, _ in server.hits) == one_hits  # not asked again
    assert second["online"]["cache_hits"] >= 1

    before = {n: accepted(env, n) for n in ("secret-name-42.pdf", "other.pdf")}
    hits = len(server.hits)
    settled = run()
    assert {n: accepted(env, n) for n in before} == before and len(server.hits) == hits  # a settled library asks nothing
    assert settled["online"]["requests"] == 0 and settled["online"]["cache_hits"] >= 2  # and the report says so


def test_a_failure_in_the_middle_never_alters_what_was_already_accepted(world):
    env, server, run, cache = world
    server.script["/works/10.5555/kv.net.one"] = [(200, {}, record(DOI1, TITLE1), 0)]
    server.script["/works/10.5555/kv.net.two"] = [(200, {}, record(DOI2, TITLE2), 0)]
    run()
    settled = {n: accepted(env, n) for n in ("secret-name-42.pdf", "other.pdf")}
    server.script["/works/"] = [(503, {"Retry-After": "1"}, b"maintenance", 0)]  # everything is down from now on
    cache.execute("DELETE FROM response")  # forget every answer, so the next run must really ask the server that is down
    run()
    assert {n: accepted(env, n) for n in settled} == settled


def test_what_is_sent_is_a_doi_and_a_courtesy_address_and_nothing_from_the_library(world, tmp_path):
    env, server, run, _ = world
    server.script["/works/"] = [(404, {}, b"Resource not found.", 0)]
    run(mailto="me@example.org")
    assert server.hits
    for path, headers in server.hits:
        sent = path + json.dumps(headers)
        assert path.startswith("/works/10.5555/kv.net.") or "query.bibliographic" in path
        for private in ("secret-name-42", "other.pdf", str(env.lib), str(tmp_path), "Cite This", "Examplar"):
            assert private not in sent, f"{private!r} left the machine in {path}"
        assert "mailto:me@example.org" in headers["User-Agent"]


def test_the_title_search_request_carries_only_the_title(make_world, tmp_path):
    w = make_world({"private-file-name.pdf": {"title": TITLE1, "doi": None, "authors": "A. Examplar and B. Placeholder"}})
    w.server.script["/works?"] = [(200, {}, json.dumps({"status": "ok", "message-type": "work-list", "message": {"items": [
        json.loads(record(DOI1, TITLE1))["message"]]}}).encode(), 0)]
    report = w.run(mailto="me@example.org")
    assert [path for path, _ in w.server.hits if path.startswith("/works?")] and len(w.server.hits) == 1  # one search, no second lookup
    path, headers = w.server.hits[0]
    assert "query.bibliographic=A+Synthetic+Study+of+Imaginary+Lattices" in path
    for private in ("private-file-name", str(w.env.lib), str(tmp_path), "Cite This", "Placeholder"):
        assert private not in path + json.dumps(headers)
    assert accepted(w.env, "private-file-name.pdf")["doi"] == DOI1 and report["online"]["title_matches"] == 1
