"""The extraction supervisor: it must ALWAYS return, whatever the worker or the file does.

The fake worker (tests/fakeworker.py) is a real process, so a hang, a crash and a memory failure here are real ones.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pdfbuilders as b
import pymupdf
import pytest

from knowledgevista.extract import client
from knowledgevista.extract.client import ExtractionSession

FAKE = [sys.executable, str(Path(__file__).with_name("fakeworker.py"))]


def fake_session(monkeypatch, config: dict, **kwargs) -> ExtractionSession:
    monkeypatch.setenv("KV_FAKE", json.dumps(config))
    kwargs.setdefault("page_timeout", 1.5)
    kwargs.setdefault("open_timeout", 5)
    return ExtractionSession(command=FAKE, **kwargs)


def texts(result):
    return {p.n: p.text for p in result.pages}


# --- the real worker ---------------------------------------------------------------------------------------------


def test_real_worker_extracts_text_labels_and_image_counts(tmp_path):
    native = b.native_pdf(tmp_path / "n.pdf")
    labelled = b.labelled_pdf(tmp_path / "l.pdf")
    scan = b.scanned_pdf(tmp_path / "s.pdf")
    with ExtractionSession() as session:
        one, two, three = session.extract(str(native)), session.extract(str(labelled)), session.extract(str(scan))
    assert (one.status, one.page_count) == ("complete", 3)
    assert "Imaginary Lattices" in one.pages[0].text and "aqueous solubility" in one.pages[1].text
    assert [p.label for p in two.pages][:5] == ["i", "ii", "iii", "iv", "1"], "the printed label is read from the PDF"
    assert [p.label for p in one.pages] == [None, None, None], "an unlabelled PDF yields None, never an empty string"
    assert [(p.text.strip(), p.images) for p in three.pages] == [("", 1)] * 3, "a scan: no text, one image per page"


@pytest.mark.parametrize("make", ["malformed", "junk", "encrypted", "missing"])
def test_unreadable_files_become_failed_results_with_a_reason_never_an_exception(tmp_path, make):
    if make == "malformed":
        path = b.malformed_pdf(tmp_path / "x.pdf")
    elif make == "junk":
        path = tmp_path / "x.pdf"
        path.write_bytes(b"this is not a pdf")
    elif make == "encrypted":
        b.native_pdf(tmp_path / "plain.pdf")
        with pymupdf.open(tmp_path / "plain.pdf") as doc:
            doc.save(tmp_path / "x.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
        path = tmp_path / "x.pdf"
    else:
        path = tmp_path / "does-not-exist.pdf"
    with ExtractionSession() as session:
        result = session.extract(str(path))
        assert result.status == "failed" and result.pages == [] and result.error
        again = session.extract(str(b.native_pdf(tmp_path / "ok.pdf")))
    assert again.status == "complete", "one bad file must not poison the worker for the next"
    if make == "encrypted":
        assert "encrypted" in result.error


def test_one_worker_serves_many_files_and_is_replaced_on_schedule(tmp_path, monkeypatch):
    path = str(b.native_pdf(tmp_path / "n.pdf"))
    with ExtractionSession() as session:
        session.extract(path)
        first = session._worker.process.pid
        session.extract(path)
        assert session._worker.process.pid == first, "the expensive import of PyMuPDF is paid once, not per file"
    monkeypatch.setattr(client, "RESTART_EVERY_FILES", 2)
    with ExtractionSession() as session:
        session.extract(path)
        first = session._worker.process.pid
        session.extract(path)
        session.extract(path)
        assert session._worker.process.pid != first, "a long-lived native process is replaced so a slow leak cannot accumulate"


def test_closing_the_session_kills_the_worker(tmp_path):
    session = ExtractionSession()
    session.extract(str(b.native_pdf(tmp_path / "n.pdf")))
    process = session._worker.process
    assert process.poll() is None
    session.close()
    assert process.poll() is not None


# --- per-page failure handling -----------------------------------------------------------------------------------


def test_a_page_that_fails_inside_the_worker_costs_only_that_page(monkeypatch, tmp_path):
    with fake_session(monkeypatch, {"pages": 5, "error_page": 2}) as session:
        result = session.extract(str(tmp_path / "a.pdf"))
    assert result.status == "partial" and result.restarts == 0
    assert [p.error is not None for p in result.pages] == [False, True, False, False, False]
    assert "simulated page failure" in result.pages[1].error


def test_a_hung_page_is_timed_out_and_only_that_page_is_lost(monkeypatch, tmp_path):
    started = time.monotonic()
    with fake_session(monkeypatch, {"pages": 10, "hang_page": 4}, page_timeout=1.0) as session:
        result = session.extract(str(tmp_path / "a.pdf"))
    assert time.monotonic() - started < 20, "a hang must not hang the caller"
    assert result.status == "partial" and result.restarts == 1
    assert [p.n for p in result.pages if p.error] == [4]
    assert "timed out" in result.pages[3].error
    assert all(p.text for p in result.pages if p.n != 4), "pages after the poison page were still read, from a fresh worker"
    assert len(result.pages) == 10


def test_a_crash_is_recorded_with_the_workers_own_last_words(monkeypatch, tmp_path):
    with fake_session(monkeypatch, {"pages": 8, "crash_page": 6}) as session:
        result = session.extract(str(tmp_path / "a.pdf"))
    assert result.status == "partial" and [p.n for p in result.pages if p.error] == [6]
    assert "exited (3)" in result.pages[5].error and "simulated crash" in result.pages[5].error
    assert result.pages[7].text, "extraction resumed after the crash"


def test_garbage_on_the_protocol_pipe_is_ignored(monkeypatch, tmp_path):
    with fake_session(monkeypatch, {"pages": 6, "garbage": True}) as session:
        result = session.extract(str(tmp_path / "a.pdf"))
    assert result.status == "complete" and all(p.text for p in result.pages)


def test_a_worker_that_dies_before_opening_gives_a_failed_result_and_the_next_file_still_works(monkeypatch, tmp_path):
    with fake_session(monkeypatch, {"pages": 3, "poison": "bad"}) as session:
        broken = session.extract(str(tmp_path / "bad.pdf"))
        fine = session.extract(str(tmp_path / "good.pdf"))
    assert broken.status == "failed" and broken.page_count == 0 and "exited (9)" in broken.error
    assert fine.status == "complete", "the dead worker was replaced"


def test_too_many_failures_stop_extraction_and_account_for_every_page(monkeypatch, tmp_path):
    # Every page hangs the worker. After max_failures restarts the rest are recorded as "not reached".
    with fake_session(monkeypatch, {"pages": 12, "hang_all": True}, page_timeout=0.5, max_failures=1) as session:
        result = session.extract(str(tmp_path / "a.pdf"))
    assert len(result.pages) == 12, "every page 1..N has a row, so a partial file says exactly where it is incomplete"
    assert result.status == "failed", "no page was read at all"
    assert [("timed out" in p.error) for p in result.pages[:2]] == [True, True]
    assert all("not reached" in p.error for p in result.pages[2:])
    assert any("too many worker failures" in note for note in result.notes)


def test_a_file_that_exceeds_its_time_budget_is_stopped_with_a_note(monkeypatch, tmp_path):
    with fake_session(monkeypatch, {"pages": 50, "slow_page_s": 0.2}, file_timeout=1.0, page_timeout=5) as session:
        result = session.extract(str(tmp_path / "a.pdf"))
    assert result.status == "partial" and len(result.pages) == 50
    assert any("not reached" in (p.error or "") for p in result.pages)
    assert result.seconds < 10


@pytest.mark.skipif(sys.platform != "win32", reason="the memory cap is exercised through the Windows Job Object")
def test_a_worker_that_exhausts_memory_is_stopped_and_the_parent_is_unharmed(monkeypatch, tmp_path):
    with fake_session(monkeypatch, {"pages": 6, "alloc_mb_page": [3, 900]}, memory_limit_mb=300) as session:
        result = session.extract(str(tmp_path / "a.pdf"))
        assert session.limiter_note.startswith("windows-job-object") and "NOT ENFORCED" not in session.limiter_note
    assert result.status == "partial" and [p.n for p in result.pages if p.error] == [3]
    assert "MemoryError" in result.pages[2].error, "the worker's own error reached the record"
    assert result.pages[5].text, "pages after the memory failure were read from a fresh worker"
    assert os.getpid()


def test_a_decompression_heavy_pdf_is_stopped_by_the_page_timeout_and_does_not_poison_the_next_file(tmp_path):
    """A 230 KB file whose content stream expands to ~240 MB of operators. Measured on the development machine:
    PyMuPDF streams it, so it neither fails nor grows in memory but takes ~6 s for one page. The protection that
    matters is therefore the page timeout, and this proves it works on a real heavy file, not a fake: the page is
    abandoned, the worker replaced, and the next file is unaffected."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "heavy page marker")
    doc.update_stream(page.get_contents()[0], b"q Q " * 60_000_000 + b"BT /F0 12 Tf (still readable) Tj ET", compress=True)
    path = tmp_path / "heavy.pdf"
    doc.save(path, deflate=True)
    assert path.stat().st_size < 400_000, "the oracle must be alive: a small file that expands about a thousand-fold"

    started = time.monotonic()
    with ExtractionSession(page_timeout=0.7, open_timeout=30) as session:
        heavy = session.extract(str(path))
        after = session.extract(str(b.native_pdf(tmp_path / "ok.pdf")))
    assert time.monotonic() - started < 60
    assert heavy.status == "failed" and heavy.restarts == 1
    assert "timed out" in heavy.pages[0].error
    assert after.status == "complete", "the replacement worker reads the next file normally"


def test_the_workers_last_words_are_awaited_even_when_the_stderr_reader_is_slow(monkeypatch, tmp_path):
    """EOF on stdout can arrive before the stderr reader has drained the crash message. The supervisor must wait for
    it, or the failure record loses the only explanation there is. A deliberately slow reader makes the race certain."""
    original = client._Worker._read_stderr

    def slow(self):
        time.sleep(0.4)
        original(self)

    monkeypatch.setattr(client._Worker, "_read_stderr", slow)
    with fake_session(monkeypatch, {"pages": 8, "crash_page": 6}) as session:
        result = session.extract(str(tmp_path / "a.pdf"))
    assert "simulated crash" in result.pages[5].error
