"""Supervise the extraction worker: spawn it, bound it, read its pages, and survive its failures.

The contract with the rest of the program is `ExtractionSession.extract(path) -> FileResult`, which ALWAYS returns:
a hang, a crash, a memory-limit kill, garbage on the pipe or a file that will not open all become a result with a
reason, never an exception and never a stuck caller.

Failure handling is per PAGE, because that is the unit of loss:
  * a page that raises inside the worker is recorded as failed and the next page is still read;
  * if the worker hangs or dies WHILE producing page k, page k is recorded as failed, the worker is replaced, and
    extraction resumes at page k+1, so one poison page does not cost the other 299;
  * after `max_failures` such restarts the remaining pages are recorded as "not reached" and the file is `partial`.
Every page 1..N gets a row, so a partly extracted file says exactly where it is incomplete (a search over it can be
qualified: "this document is only partially indexed").
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from knowledgevista.extract import limits

DEFAULT_COMMAND = [sys.executable, "-m", "knowledgevista.extract.worker"]
RESTART_EVERY_FILES = 100  # a long-lived native-code process is replaced on a schedule so a slow leak cannot accumulate


@dataclass
class PageResult:
    n: int
    text: str = ""
    label: str | None = None
    images: int = 0
    error: str | None = None


@dataclass
class FileResult:
    status: str  # complete | partial | failed
    page_count: int = 0
    pages: list[PageResult] = field(default_factory=list)
    error: str | None = None
    repaired: bool = False
    restarts: int = 0
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)


class _Worker:
    """One worker process plus the threads that read its pipes (a blocking read cannot be given a timeout on Windows)."""

    def __init__(self, command: list[str], memory_limit_mb: int):
        self.files_served = 0
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, **limits.popen_kwargs(memory_limit_mb),
        )
        self.limiter = limits.attach(self.process, memory_limit_mb)
        self.events: queue.Queue[dict | None] = queue.Queue()
        self.stderr_tail: deque[str] = deque(maxlen=12)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        self._stderr_thread.start()

    def _read_stdout(self) -> None:
        try:
            for line in self.process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    self.stderr_tail.append("(non-protocol output) " + line.strip()[:120])
                    continue
                if isinstance(event, dict):
                    self.events.put(event)
        except (OSError, ValueError):
            pass
        self.events.put(None)  # EOF: the process is gone

    def _read_stderr(self) -> None:
        try:
            for line in self.process.stderr:
                self.stderr_tail.append(line.strip()[:200])
        except (OSError, ValueError):
            pass

    def send(self, request: dict[str, Any]) -> bool:
        try:
            self.process.stdin.write(json.dumps(request, ensure_ascii=True) + "\n")
            self.process.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def next_event(self, timeout: float) -> tuple[str, dict | None]:
        """('event', dict) | ('eof', None) | ('timeout', None)"""
        try:
            event = self.events.get(timeout=max(0.01, timeout))
        except queue.Empty:
            return "timeout", None
        return ("eof", None) if event is None else ("event", event)

    def why_it_ended(self) -> str:
        # EOF on the pipe can arrive before the process is reaped, and the last stderr lines before the reader has
        # drained them; wait briefly for both, or the record says "exited (None)" and loses the worker's own words.
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        self._stderr_thread.join(timeout=1)
        code = self.process.poll()
        tail = " | ".join(line for line in self.stderr_tail if line)[-240:]
        return f"worker exited ({code})" + (f": {tail}" if tail else "")

    def kill(self) -> None:
        try:
            self.process.kill()
        except OSError:
            pass
        self.limiter.close()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        self.send({"cmd": "exit"})
        self.kill()


class ExtractionSession:
    """Extract many files with one reusable worker. Use as a context manager so the worker never outlives the caller."""

    def __init__(
        self,
        *,
        command: list[str] | None = None,
        memory_limit_mb: int = limits.DEFAULT_MEMORY_LIMIT_MB,
        open_timeout: float = 120.0,
        page_timeout: float = 60.0,
        file_timeout: float = 1800.0,
        max_failures: int = 5,
    ):
        self.command = command or DEFAULT_COMMAND
        self.memory_limit_mb = memory_limit_mb
        self.open_timeout, self.page_timeout = open_timeout, page_timeout
        self.file_timeout, self.max_failures = file_timeout, max_failures
        self._worker: _Worker | None = None
        self.limiter_note: str | None = None

    def __enter__(self) -> ExtractionSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._worker is not None:
            self._worker.close()
            self._worker = None

    def _ensure_worker(self) -> _Worker:
        if self._worker is not None and (self._worker.process.poll() is not None or self._worker.files_served >= RESTART_EVERY_FILES):
            self._worker.close()
            self._worker = None
        if self._worker is None:
            self._worker = _Worker(self.command, self.memory_limit_mb)
            self.limiter_note = f"{self._worker.limiter.mechanism}: {self._worker.limiter.note}" + ("" if self._worker.limiter.enforced else " [NOT ENFORCED]")
        return self._worker

    def _discard_worker(self) -> None:
        if self._worker is not None:
            self._worker.kill()
            self._worker = None

    def extract(self, path: str) -> FileResult:
        started = time.monotonic()
        deadline = started + self.file_timeout
        result = FileResult("failed")
        pages: dict[int, PageResult] = {}
        start, failures = 1, 0
        try:
            while True:
                worker = self._ensure_worker()
                worker.files_served += 1
                if not worker.send({"cmd": "extract", "path": path, "start": start}):
                    self._discard_worker()
                    return self._finish(result, pages, started, "the worker could not be reached")
                kind, event = worker.next_event(min(self.open_timeout, deadline - time.monotonic()))
                while kind == "event" and event.get("type") not in ("open", "open_error"):
                    kind, event = worker.next_event(min(self.open_timeout, deadline - time.monotonic()))
                if kind != "event":
                    reason = "timed out opening the file" if kind == "timeout" else worker.why_it_ended()
                    self._discard_worker()
                    if result.page_count:  # we had pages already: this restart failed to reopen, so give up on the rest
                        return self._finish(result, pages, started, f"could not reopen after a failure ({reason})")
                    return self._finish(result, pages, started, reason)
                if event["type"] == "open_error":
                    return self._finish(result, pages, started, event["error"])
                result.page_count = int(event["pages"])
                result.repaired = result.repaired or bool(event.get("repaired"))

                last = start - 1
                ended = False
                while True:
                    kind, event = worker.next_event(min(self.page_timeout, deadline - time.monotonic()))
                    if kind == "event":
                        if event.get("type") == "page":
                            n = int(event["n"])
                            pages[n] = PageResult(n, event.get("text", ""), event.get("label"), int(event.get("images", 0)), event.get("error"))
                            last = max(last, n)
                        elif event.get("type") == "end":
                            ended = True
                            break
                        continue
                    # The worker hung or died while producing page last+1.
                    reason = f"timed out after {self.page_timeout:g}s" if kind == "timeout" else worker.why_it_ended()
                    self._discard_worker()
                    failed_page = last + 1
                    if failed_page <= result.page_count:
                        pages[failed_page] = PageResult(failed_page, error=f"page not extracted: {reason}")
                    result.restarts += 1
                    failures += 1
                    start = failed_page + 1
                    break
                if ended or start > result.page_count:
                    break
                if failures > self.max_failures or time.monotonic() >= deadline:
                    result.notes.append("stopped early: " + ("too many worker failures" if failures > self.max_failures else "the per-file time limit was reached"))
                    break
            return self._finish(result, pages, started, None)
        except Exception as exc:  # noqa: BLE001 - the contract is "always returns"
            self._discard_worker()
            return self._finish(result, pages, started, f"supervisor error: {type(exc).__name__}: {exc}")

    def _finish(self, result: FileResult, pages: dict[int, PageResult], started: float, error: str | None) -> FileResult:
        result.seconds = time.monotonic() - started
        result.error = error
        count = result.page_count
        if count == 0:
            result.status, result.pages = "failed", []
            result.error = result.error or "no pages were read"
            return result
        for n in range(1, count + 1):
            if n not in pages:
                pages[n] = PageResult(n, error="not reached: " + (error or "extraction stopped early"))
        result.pages = [pages[n] for n in range(1, count + 1)]
        bad = sum(1 for page in result.pages if page.error)
        result.status = "failed" if bad == count else ("partial" if bad else "complete")
        if error and result.status == "complete":
            result.status = "partial"
        return result
