"""The window's job layer: work leaves the interface thread, writes are serial, the newest request wins, cancelling is cooperative."""

from __future__ import annotations

import logging
import threading
import time

import pytest
from guisupport import pump, wait_for
from lab import Lab

from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.gui import jobs as J
from knowledgevista.services import metadata

GATE_TIMEOUT = 10


@pytest.fixture
def manager(qapp, tmp_path):
    made = J.JobManager(tmp_path / "unused.sqlite")
    yield made
    assert made.shutdown(), "a worker was still running at the end of the test"


def gated():
    """(a job function that waits until released, the event that releases it, the event that says it began)."""
    release, began = threading.Event(), threading.Event()

    def fn(ctx):
        began.set()
        assert release.wait(GATE_TIMEOUT), "the test never released the job"
        return "done"

    return fn, release, began


# ---------------------------------------------------------------------------------------------------------- where work runs


def test_a_job_runs_off_the_interface_thread_and_reports_on_it(manager):
    main = threading.current_thread()
    seen = {}

    def fn(ctx):
        seen["worker"] = threading.current_thread()
        return 41 + 1

    def done(job):
        seen["callback"] = threading.current_thread()
        seen["result"] = job.result

    job = manager.submit("t", "t", fn, on_done=done)
    wait_for(lambda: job.state == J.SUCCEEDED, what="the job to succeed")
    assert seen["worker"] is not main and seen["callback"] is main and seen["result"] == 42


def test_the_job_object_tells_the_whole_story(manager):
    job = manager.submit("scan", "Scan the library", lambda ctx: "ok", lane=J.WRITE)
    assert job.kind == "scan" and job.title == "Scan the library" and job.lane == J.WRITE
    wait_for(lambda: job.state == J.SUCCEEDED, what="success")
    assert job.queued_at <= job.started_at <= job.finished_at and job.error is None and manager.jobs()[-1] is job


# ---------------------------------------------------------------------------------------------------------- the lanes


def test_writes_run_one_at_a_time_in_the_order_they_were_asked(manager):
    events, lock = [], threading.Lock()

    def make(name):
        def fn(ctx):
            with lock:
                events.append(("start", name))
            time.sleep(0.05)
            with lock:
                events.append(("end", name))
        return fn

    for name in "abc":
        manager.submit("w", name, make(name), lane=J.WRITE)
    assert manager.wait_idle(10000)
    assert events == [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b"), ("start", "c"), ("end", "c")]


def test_reads_run_at_the_same_time(manager):
    """Two reads meet at a barrier: if the lane ran them one after the other, the first would wait alone and time out."""
    barrier = threading.Barrier(2, timeout=GATE_TIMEOUT)
    manager.submit("r", "one", lambda ctx: barrier.wait(), lane=J.READ)
    manager.submit("r", "two", lambda ctx: barrier.wait(), lane=J.READ)
    assert manager.wait_idle(15000)
    assert [j.state for j in manager.jobs()] == [J.SUCCEEDED, J.SUCCEEDED]


def test_a_read_does_not_wait_behind_a_long_write(manager):
    fn, release, began = gated()
    write = manager.submit("w", "long write", fn, lane=J.WRITE)
    assert began.wait(GATE_TIMEOUT)
    read = manager.submit("r", "quick read", lambda ctx: "answer", lane=J.READ)
    wait_for(lambda: read.state == J.SUCCEEDED, what="a read to finish while a write is running")
    assert write.state == J.RUNNING
    release.set()
    wait_for(lambda: write.state == J.SUCCEEDED, what="the write")


def test_only_the_write_lane_may_open_the_catalog_for_writing(manager, tmp_path):
    job = manager.submit("r", "read that tries to write", lambda ctx: ctx.writer().__enter__(), lane=J.READ)
    wait_for(lambda: job.state == J.FAILED, what="the refusal")
    assert "only a write-lane job" in job.error


# ---------------------------------------------------------------------------------------------------------- newest wins


def test_only_the_newest_request_on_a_channel_may_deliver(manager):
    """Three searches asked in a row finish in the WRONG order. The first two are out of date by then and must not be shown."""
    delivered = []
    gates = [threading.Event() for _ in range(3)]
    started = [threading.Event() for _ in range(3)]

    def make(n):
        def fn(ctx):
            started[n].set()
            assert gates[n].wait(GATE_TIMEOUT)
            return n
        return fn

    jobs = [manager.submit("search", f"s{n}", make(n), channel="search", on_done=lambda j: delivered.append(j.result)) for n in range(3)]
    assert all(s.wait(GATE_TIMEOUT) for s in started)
    gates[2].set()
    wait_for(lambda: jobs[2].state == J.SUCCEEDED, what="the newest")
    gates[0].set()
    gates[1].set()
    wait_for(lambda: jobs[0].state == J.SUPERSEDED and jobs[1].state == J.SUPERSEDED, what="the older ones to be marked out of date")
    assert delivered == [2], "a slow stale answer must never replace a fresh one"


def test_a_superseded_failure_is_not_shown_either(manager):
    gate, began = threading.Event(), threading.Event()
    shown = []

    def slow_bad(ctx):
        began.set()
        gate.wait(GATE_TIMEOUT)
        raise KvError(ErrorCode.QUERY_INVALID, "old query was malformed")

    old = manager.submit("search", "old", slow_bad, channel="search", on_done=lambda j: shown.append(j.error))
    assert began.wait(GATE_TIMEOUT)
    new = manager.submit("search", "new", lambda ctx: "fine", channel="search", on_done=lambda j: shown.append(j.result))
    wait_for(lambda: new.state == J.SUCCEEDED, what="the new search")
    gate.set()
    wait_for(lambda: old.state == J.SUPERSEDED, what="the old failure to be dropped")
    assert shown == ["fine"]


def test_different_channels_do_not_supersede_each_other(manager):
    a = manager.submit("x", "a", lambda ctx: 1, channel="list")
    b = manager.submit("x", "b", lambda ctx: 2, channel="detail")
    assert manager.wait_idle(10000)
    assert (a.state, b.state) == (J.SUCCEEDED, J.SUCCEEDED)


# ---------------------------------------------------------------------------------------------------------- stopping


def test_a_running_job_stops_at_its_next_check(manager):
    began = threading.Event()
    steps = []

    def fn(ctx):
        began.set()
        for n in range(10_000):
            ctx.check()
            steps.append(n)
            time.sleep(0.001)

    job = manager.submit("scan", "long", fn, lane=J.WRITE)
    assert began.wait(GATE_TIMEOUT)
    wait_for(lambda: len(steps) > 3, what="some progress")
    assert manager.cancel(job.job_id) is True
    wait_for(lambda: job.state == J.CANCELLED, what="the job to stop")
    count = len(steps)
    pump(100)
    assert len(steps) == count, "it really stopped, not merely reported stopped"
    assert count < 10_000


def test_a_job_that_has_not_started_is_dropped_without_running(manager):
    fn, release, began = gated()
    manager.submit("w", "blocker", fn, lane=J.WRITE)
    assert began.wait(GATE_TIMEOUT)
    ran = []
    queued = manager.submit("w", "queued", lambda ctx: ran.append(1), lane=J.WRITE)
    assert queued.state == J.QUEUED and manager.cancel(queued.job_id)
    release.set()
    wait_for(lambda: queued.state == J.CANCELLED, what="the queued job to be dropped")
    assert ran == [] and manager.wait_idle(5000)


def test_should_stop_lets_a_service_stop_between_items(manager):
    """The services take `should_stop`; the context hands them one."""
    seen = []

    def fn(ctx):
        while not ctx.should_stop():
            time.sleep(0.001)
        seen.append("noticed")
        return "partial"

    job = manager.submit("extract", "x", fn, lane=J.WRITE)
    wait_for(lambda: job.state == J.RUNNING, what="start")
    manager.cancel(job.job_id)
    wait_for(lambda: job.state == J.SUCCEEDED, what="finish")
    assert seen == ["noticed"] and job.result == "partial", "a job that stops by itself and returns what it did is a success, not a cancellation"


def test_cancelling_a_finished_or_unknown_job_is_a_no_op(manager):
    job = manager.submit("x", "x", lambda ctx: 1)
    assert manager.wait_idle(5000)
    assert manager.cancel(job.job_id) is False and manager.cancel(99_999) is False


def test_shutdown_cancels_and_waits(qapp, tmp_path):
    manager = J.JobManager(tmp_path / "x.sqlite")
    began = threading.Event()

    def fn(ctx):
        began.set()
        while True:
            ctx.check()
            time.sleep(0.001)

    job = manager.submit("w", "forever", fn, lane=J.WRITE)
    assert began.wait(GATE_TIMEOUT)
    assert manager.shutdown(5000) is True
    assert job.state == J.CANCELLED


# ---------------------------------------------------------------------------------------------------------- failure


def test_an_unexpected_failure_is_shown_and_logged(manager, caplog):
    def boom(ctx):
        raise ZeroDivisionError("division by zero")

    with caplog.at_level(logging.WARNING, logger="knowledgevista.gui.jobs"):
        job = manager.submit("x", "x", boom)
        wait_for(lambda: job.state == J.FAILED, what="failure")
    assert job.error == "ZeroDivisionError: division by zero" and job.error_code is None
    assert any(r.levelno >= logging.ERROR and "failed" in r.getMessage() for r in caplog.records), "a fault must reach the log (and so the driver's ledger)"


def test_an_answer_the_library_gave_on_purpose_is_not_logged_as_a_fault(manager, caplog):
    def refused(ctx):
        raise KvError(ErrorCode.QUERY_INVALID, "The query is empty.")

    with caplog.at_level(logging.WARNING, logger="knowledgevista.gui.jobs"):
        job = manager.submit("search", "s", refused)
        wait_for(lambda: job.state == J.FAILED, what="refusal")
    assert (job.error, job.error_code) == ("The query is empty.", ErrorCode.QUERY_INVALID)
    assert not caplog.records


def test_a_callback_that_raises_is_logged_and_the_next_job_still_delivers(manager, caplog):
    got = []

    def bad(job):
        raise RuntimeError("callback bug")

    with caplog.at_level(logging.ERROR, logger="knowledgevista.gui.jobs"):
        first = manager.submit("x", "first", lambda ctx: 1, on_done=bad)
        second = manager.submit("x", "second", lambda ctx: 2, on_done=lambda j: got.append(j.result))
        assert manager.wait_idle(5000)
    assert first.state == J.SUCCEEDED and got == [2]
    assert any("callback" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------------------------------------- bookkeeping


def test_once_returns_the_running_job_instead_of_starting_a_second(manager):
    fn, release, began = gated()
    first = manager.submit("scan", "scan", fn, lane=J.WRITE, once=True)
    assert began.wait(GATE_TIMEOUT)
    second = manager.submit("scan", "scan again", lambda ctx: "never", lane=J.WRITE, once=True)
    assert second is first and len(manager.jobs()) == 1
    release.set()
    assert manager.wait_idle(5000)
    third = manager.submit("scan", "scan once more", lambda ctx: "runs", lane=J.WRITE, once=True)
    assert third is not first, "once only collapses requests while one is unfinished"
    assert manager.wait_idle(5000)


def test_busy_follows_the_write_lane_only(manager):
    changes = []
    manager.busyChanged.connect(changes.append)
    fn, release, began = gated()
    manager.submit("r", "read", lambda ctx: 1, lane=J.READ)
    assert manager.wait_idle(5000) and changes == [] and manager.busy is False
    manager.submit("w", "write", fn, lane=J.WRITE)
    assert began.wait(GATE_TIMEOUT) and manager.busy is True
    release.set()
    assert manager.wait_idle(5000)
    wait_for(lambda: changes == [True, False], what="busy to go on and off")


def test_idle_is_announced_only_when_nothing_is_unfinished(manager):
    fn, release, began = gated()
    seen = []
    manager.idle.connect(lambda: seen.append(len(manager.active())))
    manager.submit("w", "slow", fn, lane=J.WRITE)
    assert began.wait(GATE_TIMEOUT)
    manager.submit("r", "quick", lambda ctx: 1)
    wait_for(lambda: any(j.title == "quick" and j.state == J.SUCCEEDED for j in manager.jobs()), what="the quick read")
    pump(50)
    assert seen == [], "a job is still running, so the manager is not idle"
    release.set()
    assert manager.wait_idle(5000)
    wait_for(lambda: seen, what="idle")
    assert seen == [0]


def test_progress_reaches_the_job_and_is_throttled(manager):
    seen = []
    manager.jobChanged.connect(lambda job: seen.append(job.progress))

    def fn(ctx):
        for n in range(500):
            ctx.progress(f"item {n}")
        time.sleep(0.12)
        ctx.progress("last")

    job = manager.submit("x", "x", fn)
    assert manager.wait_idle(5000)
    assert "last" in seen and any(p.startswith("item") for p in seen)
    assert sum(1 for p in seen if p.startswith("item")) < 100, "a flood of messages must not become a flood of screen updates"
    assert job.state == J.SUCCEEDED


def test_finished_jobs_are_kept_only_up_to_a_limit(manager):
    for n in range(J.HISTORY + 15):
        manager.submit("x", str(n), lambda ctx: None)
    assert manager.wait_idle(20000)
    assert len(manager.jobs()) == J.HISTORY and manager.jobs()[-1].title == str(J.HISTORY + 14)


# ---------------------------------------------------------------------------------------------------------- connections


def test_a_reader_is_one_snapshot_and_cannot_write(qapp, tmp_path):
    lab = Lab(tmp_path)
    manager = J.JobManager(lab.catalog)
    try:
        seen = {}
        gate, began = threading.Event(), threading.Event()
        document = lab.doc("papers/aqueous.pdf")

        def read(ctx):
            with ctx.reader() as (conn, index):
                seen["before"] = conn.execute("SELECT catalog_revision FROM library").fetchone()[0]
                began.set()
                assert gate.wait(GATE_TIMEOUT)
                seen["after"] = conn.execute("SELECT catalog_revision FROM library").fetchone()[0]
                seen["index"] = index is not None
                try:
                    conn.execute("UPDATE library SET catalog_revision = 0")
                except Exception as exc:  # noqa: BLE001
                    seen["write"] = str(exc)

        job = manager.submit("r", "r", read)
        assert began.wait(GATE_TIMEOUT)
        metadata.set_value(lab.conn, document, "title", "Changed While Reading", lock=False)  # a writer commits mid-read
        gate.set()
        wait_for(lambda: job.state == J.SUCCEEDED, what="the read")
        assert seen["after"] == seen["before"], "everything inside one reader block sees one revision"
        assert "readonly" in seen["write"] and seen["index"] is True
        assert lab.env.revision() > seen["before"]
    finally:
        manager.shutdown()
        lab.close()


def test_a_writer_commits_for_the_next_reader(qapp, tmp_path):
    lab = Lab(tmp_path)
    manager = J.JobManager(lab.catalog)
    try:
        document = lab.doc("papers/aqueous.pdf")

        def write(ctx):
            with ctx.writer() as (conn, index):
                assert index is None
                metadata.set_value(conn, document, "title", "From A Job", lock=False)

        manager.submit("w", "w", write, lane=J.WRITE)
        assert manager.wait_idle(10000)

        def read(ctx):
            with ctx.reader() as (conn, _):
                return conn.execute("SELECT value FROM metadata_value WHERE document_id = ? AND field = 'title'", (document,)).fetchone()[0]

        job = manager.submit("r", "r", read)
        wait_for(lambda: job.state == J.SUCCEEDED, what="read")
        assert job.result == "From A Job"
    finally:
        manager.shutdown()
        lab.close()


def test_idle_means_every_callback_has_already_run(manager):
    """A job is 'unfinished' until the interface thread has applied its outcome and run its callback. If a worker could mark itself
    finished first, wait_idle (and a script that waits for idle, then reads the screen) would see the old screen."""
    delivered = []
    for n in range(40):
        manager.submit("x", str(n), lambda ctx: None, on_done=lambda job: delivered.append(job.title))
    assert manager.wait_idle(10000)
    assert len(delivered) == 40


def test_find_active_sees_a_job_until_its_callback_has_run(manager):
    started, release = threading.Event(), threading.Event()
    seen_active = []

    def fn(ctx):
        started.set()
        release.wait(GATE_TIMEOUT)

    manager.submit("scan", "s", fn, lane=J.WRITE, on_done=lambda job: seen_active.append(manager.find_active("scan")))
    assert started.wait(GATE_TIMEOUT)
    release.set()
    assert manager.wait_idle(5000)
    assert seen_active == [None], "by the time the callback runs the job is finished, so a follow-up scan may be queued from it"
