"""The window's job layer: the one place work leaves the interface thread.

Nothing in the window starts a thread of its own. Anything that touches the catalog, the extraction store or the disk is a `Job`
submitted here, runs on a worker, and reports back on the interface thread through queued signals. Three rules make that safe:

  * Two LANES. The `write` lane has ONE thread, because the catalog has one logical writer (scan, extract, resolve, a collection,
    an accepted proposal): writes run one at a time in the order they were asked. The `read` lane has a few threads and uses read-only
    connections, which coexist with the writer under WAL.
  * Every job opens its OWN connections. A sqlite3 connection belongs to the thread that made it; nothing is shared across threads.
  * A CHANNEL is "the latest request wins". Typing a search, then another, then another, leaves three jobs; only the newest may
    change the screen. An older one that finishes later is marked `superseded` and its callback never runs, so a slow stale answer
    cannot replace a fresh one.

Cancelling is cooperative: a job checks `should_stop()` / `check()` between items. It is never killed, because killing a thread in
the middle of a write is how a catalog is left half-changed. A job that has not started yet is dropped without running.
"""

from __future__ import annotations

import itertools
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, QRunnable, QThreadPool, Signal, Slot

from knowledgevista import paths
from knowledgevista.db.catalog import open_catalog, open_catalog_strict
from knowledgevista.errors import KvError
from knowledgevista.index.store import open_index

log = logging.getLogger("knowledgevista.gui.jobs")

QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED, SUPERSEDED = "queued", "running", "succeeded", "failed", "cancelled", "superseded"
UNFINISHED = (QUEUED, RUNNING)

WRITE, READ = "write", "read"
#: Several reads at once is fine (WAL); more than this only competes for the disk.
READ_THREADS = 3
#: Progress messages closer together than this are dropped: a scan can report thousands, and the screen can read ten a second.
PROGRESS_INTERVAL_S = 0.05
#: Finished jobs kept for the Jobs panel.
HISTORY = 60


class JobCancelled(Exception):
    """Raised inside a job by `JobContext.check()` when a person asked it to stop."""


@dataclass
class Job:
    job_id: int
    kind: str
    title: str
    lane: str
    channel: str | None = None
    state: str = QUEUED
    progress: str = ""
    result: Any = None
    error: str | None = None
    error_code: str | None = None
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    stop: threading.Event = field(default_factory=threading.Event, repr=False)
    on_done: Callable[[Job], None] | None = field(default=None, repr=False)

    @property
    def unfinished(self) -> bool:
        return self.state in UNFINISHED

    @property
    def cancel_requested(self) -> bool:
        return self.stop.is_set()


class JobContext:
    """What a job's function is given: progress, cancellation, and its own connections."""

    def __init__(self, job: Job, catalog: Path, bridge: _Bridge):
        self.job = job
        self.catalog = catalog
        self._bridge = bridge
        self._last_progress = 0.0

    def progress(self, message: str) -> None:
        now = time.monotonic()
        if now - self._last_progress >= PROGRESS_INTERVAL_S:
            self._last_progress = now
            self._bridge.progress.emit(self.job.job_id, str(message))

    def should_stop(self) -> bool:
        return self.job.stop.is_set()

    def check(self, *_ignored: Any) -> None:
        """Raise `JobCancelled` if a person asked this job to stop. Takes and ignores arguments so it can be a progress callback."""
        if self.job.stop.is_set():
            raise JobCancelled()

    @contextmanager
    def reader(self) -> Iterator[tuple[sqlite3.Connection, sqlite3.Connection | None]]:
        """(catalog, extraction store) for reading, as ONE snapshot of the catalog: every statement in the block sees the same
        revision, so a list and its counts cannot disagree. Neither connection can write, and opening never migrates."""
        conn = open_catalog_strict(self.catalog)
        index = open_index(paths.index_path(self.catalog), create=False, read_only=True)
        conn.execute("BEGIN")
        try:
            yield conn, index
        finally:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            conn.close()
            if index is not None:
                index.close()

    @contextmanager
    def writer(self, *, index: bool = False) -> Iterator[tuple[sqlite3.Connection, sqlite3.Connection | None]]:
        """(catalog, extraction store or None) for writing. Only the write lane may call this."""
        if self.job.lane != WRITE:
            raise RuntimeError("only a write-lane job may open the catalog for writing")
        conn = open_catalog(self.catalog, create=False)
        store = open_index(paths.index_path(self.catalog), create=True) if index else None
        try:
            yield conn, store
        finally:
            conn.close()
            if store is not None:
                store.close()


class _Bridge(QObject):
    """The queued path from a worker thread back to the interface thread. Signals emitted on a worker reach slots on this object's
    thread, in order."""

    started = Signal(int)
    progress = Signal(int, str)
    done = Signal(int)


class _Task(QRunnable):
    """Runs a job's function on a worker and leaves its OUTCOME here. It never touches the `Job`: every state change happens on the
    interface thread, in the slots below, so "unfinished" is true until the moment the completion callback has run."""

    def __init__(self, job: Job, fn: Callable[[JobContext], Any], context: JobContext, bridge: _Bridge):
        super().__init__()
        self.job, self.fn, self.context, self.bridge = job, fn, context, bridge
        self.outcome: tuple[str, Any, str | None, str | None] = (FAILED, None, "the job never ran", None)
        self.setAutoDelete(True)

    def run(self) -> None:
        job = self.job
        if job.stop.is_set():
            self.outcome = (CANCELLED, None, None, None)  # asked to stop before it began: it never starts
        else:
            self.bridge.started.emit(job.job_id)
            try:
                self.outcome = (SUCCEEDED, self.fn(self.context), None, None)  # a stop asked for after its last check came too late to matter
            except JobCancelled:
                self.outcome = (CANCELLED, None, None, None)
            except KvError as exc:  # an answer the library gave on purpose (a malformed query, a locked value): shown, not a fault
                self.outcome = (FAILED, None, exc.message, str(exc.code))
            except Exception as exc:  # noqa: BLE001 - a worker must report, never die silently
                self.outcome = (FAILED, None, f"{type(exc).__name__}: {exc}", None)
                log.exception("job %s (%s) failed", job.job_id, job.kind)
        self.bridge.done.emit(job.job_id)


class JobManager(QObject):
    jobAdded = Signal(object)
    jobChanged = Signal(object)
    jobFinished = Signal(object)  # succeeded, failed or cancelled; not superseded
    busyChanged = Signal(bool)  # any WRITE job unfinished: the window shows it as work in progress
    idle = Signal()  # nothing unfinished in either lane

    def __init__(self, catalog: Path | str, parent: QObject | None = None):
        super().__init__(parent)
        self.catalog = Path(catalog)
        self._ids = itertools.count(1)
        self._jobs: dict[int, Job] = {}
        self._order: list[int] = []
        self._latest: dict[str, int] = {}
        self._tasks: dict[int, _Task] = {}
        self._pools = {WRITE: QThreadPool(self), READ: QThreadPool(self)}
        self._pools[WRITE].setMaxThreadCount(1)
        self._pools[READ].setMaxThreadCount(READ_THREADS)
        self._bridge = _Bridge(self)
        self._bridge.started.connect(self._on_started)
        self._bridge.progress.connect(self._on_progress)
        self._bridge.done.connect(self._on_done)
        self._was_busy = False

    # -- submitting ------------------------------------------------------------------------------------------------------

    def submit(self, kind: str, title: str, fn: Callable[[JobContext], Any], *, lane: str = READ, channel: str | None = None,
               on_done: Callable[[Job], None] | None = None, once: bool = False) -> Job:
        """Queue `fn(ctx)`. With `once`, an unfinished job of the same `kind` is returned instead of queueing a second (pressing Scan
        twice scans once). With a `channel`, only the newest job of that channel is allowed to deliver its result."""
        if lane not in self._pools:
            raise ValueError(f"unknown lane {lane!r}")
        if once:
            running = self.find_active(kind)
            if running is not None:
                return running
        job = Job(next(self._ids), kind, title, lane, channel, on_done=on_done)
        self._jobs[job.job_id] = job
        self._order.append(job.job_id)
        if channel is not None:
            self._latest[channel] = job.job_id
        task = _Task(job, fn, JobContext(job, self.catalog, self._bridge), self._bridge)
        self._tasks[job.job_id] = task
        self.jobAdded.emit(job)
        self._notify_busy()
        self._pools[lane].start(task)
        return job

    # -- asking ----------------------------------------------------------------------------------------------------------

    def find_active(self, kind: str) -> Job | None:
        return next((j for j in self._jobs.values() if j.kind == kind and j.unfinished), None)

    def jobs(self) -> list[Job]:
        """Oldest first."""
        return [self._jobs[i] for i in self._order]

    def active(self) -> list[Job]:
        return [j for j in self.jobs() if j.unfinished]

    @property
    def busy(self) -> bool:
        return any(j.unfinished and j.lane == WRITE for j in self._jobs.values())

    # -- stopping --------------------------------------------------------------------------------------------------------

    def cancel(self, job_id: int) -> bool:
        job = self._jobs.get(job_id)
        if job is None or not job.unfinished:
            return False
        job.stop.set()
        self.jobChanged.emit(job)  # so the panel can say "stopping" now, not when the job finally notices
        return True

    def cancel_all(self) -> int:
        return sum(1 for job in self.active() if self.cancel(job.job_id))

    def wait_idle(self, timeout_ms: int = 15000) -> bool:
        """Run the event loop until nothing is unfinished. For tests and the scripted driver; the window never blocks on this."""
        deadline = time.monotonic() + timeout_ms / 1000
        while self.active():
            if time.monotonic() > deadline:
                return False
            QCoreApplication.processEvents()
            time.sleep(0.005)
        QCoreApplication.processEvents()
        return True

    def shutdown(self, timeout_ms: int = 10000) -> bool:
        """Ask everything to stop and wait for the workers. Returns whether they all finished in time."""
        self.cancel_all()
        finished = all(pool.waitForDone(timeout_ms) for pool in self._pools.values())
        QCoreApplication.processEvents()
        return finished

    # -- worker signals, on the interface thread ---------------------------------------------------------------------------

    @Slot(int)
    def _on_started(self, job_id: int) -> None:
        job = self._jobs.get(job_id)
        if job is not None and job.state == QUEUED:
            job.state, job.started_at = RUNNING, time.time()
            self.jobChanged.emit(job)

    @Slot(int, str)
    def _on_progress(self, job_id: int, message: str) -> None:
        job = self._jobs.get(job_id)
        if job is not None and job.unfinished:
            job.progress = message
            self.jobChanged.emit(job)

    @Slot(int)
    def _on_done(self, job_id: int) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        task = self._tasks.pop(job_id)
        job.state, job.result, job.error, job.error_code = task.outcome
        job.finished_at = time.time()
        if job.channel is not None and self._latest.get(job.channel) != job_id and job.state in (SUCCEEDED, FAILED):
            job.state = SUPERSEDED  # a newer request on this channel exists: this answer is out of date and is dropped
        self.jobChanged.emit(job)
        if job.state != SUPERSEDED:
            if job.on_done is not None:
                try:
                    job.on_done(job)
                except Exception:  # noqa: BLE001 - a broken callback must be seen, and must not stop the next job's
                    log.exception("the callback for job %s (%s) raised", job.job_id, job.kind)
            self.jobFinished.emit(job)
        self._trim()
        self._notify_busy()
        if not self.active():
            self.idle.emit()

    def _trim(self) -> None:
        finished = [i for i in self._order if not self._jobs[i].unfinished]
        for job_id in finished[: max(0, len(finished) - HISTORY)]:
            self._order.remove(job_id)
            del self._jobs[job_id]

    def _notify_busy(self) -> None:
        busy = self.busy
        if busy != self._was_busy:
            self._was_busy = busy
            self.busyChanged.emit(busy)
