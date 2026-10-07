"""What a driven run saw, kept rather than printed.

The driver used to say what each step did on stdout and keep nothing. A logged
warning, an exception inside a Tk callback, a worker thread dying -- all of it
went to a log file or a console nobody compared with anything, so a run that
"finished" proved only that the script reached its last line. This module is the
memory the driver lacked: a **ledger** of what the application reported about
itself, the **expectations** a script asserted, and a **report** written when the
run ends.

It knows nothing about widgets, `App` or configuration. The driver hands it
facts and (once) the object whose `report_callback_exception` it should listen
to; it keeps them.

**Every hook is fail-open.** Each wrapper records inside its own `try` and then
ALWAYS calls the original. A diagnostic that can change what the application
does when it fails is worse than the silence it replaces.

**Restoring is conditional.** `uninstall` puts a hook back only if the current
value is still the wrapper we installed. If something else replaced it in the
meantime, that newer hook is left alone and a `HOOK_DRIFT` entry is recorded --
blindly restoring would silently delete somebody else's legitimate change.

**Everything that mutates state takes one lock.** Records arrive from the Tk
thread, worker threads, `logging`, and `atexit`; nothing here assumes one thread.

**Startup is kept apart from the drive.** Capture begins before the window is
built, so a launch warning is on the record, tagged `startup` so it cannot make
every `expect_clean` fail for ever -- but a startup *exception* still fails the
run, and nothing is silently cleared.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable

#: The report's format. A machine-readable artefact gets a version from day
#: one, because the first reader written against it outlives the first layout.
REPORT_SCHEMA = 1

#: What kind of thing an entry is, so a reader can tell whether the
#: application misbehaved or the script asked for the wrong thing.
APPLICATION_WARNING = "APPLICATION_WARNING"
UNCAUGHT_EXCEPTION = "UNCAUGHT_EXCEPTION"
WORKER_FAILURE = "WORKER_FAILURE"
EXPECTATION_FAILURE = "EXPECTATION_FAILURE"
STEP_FAILURE = "STEP_FAILURE"
HOOK_DRIFT = "HOOK_DRIFT"

#: Kinds that fail a run. A warning fails one only when a script says
#: `expect_clean`; drift is information about the harness, not the application.
FAILING_KINDS = (UNCAUGHT_EXCEPTION, EXPECTATION_FAILURE, STEP_FAILURE)

STARTUP = "startup"
DRIVE = "drive"

COMMIT_KNOWN = "KNOWN"
COMMIT_UNKNOWN = "UNKNOWN"

#: How many distinct messages one fingerprint keeps.
_MESSAGES_KEPT = 5


@dataclass
class Entry:
    """One kind of report, aggregated over how often it happened."""

    kind: str
    channel: str
    severity: str
    phase: str
    fingerprint: str
    count: int = 0
    first: float = 0.0
    last: float = 0.0
    messages: "list[str]" = field(default_factory=list)
    traceback: "str | None" = None
    #: Step context, set for a STEP_FAILURE so the report is a postmortem.
    detail: "dict[str, Any]" = field(default_factory=dict)

    def as_dict(self) -> "dict[str, Any]":
        return {
            "kind": self.kind, "channel": self.channel,
            "severity": self.severity, "phase": self.phase,
            "fingerprint": self.fingerprint, "count": self.count,
            "first": self.first, "last": self.last,
            "messages": list(self.messages), "traceback": self.traceback,
            "detail": dict(self.detail),
        }


def _fingerprint(source: str, exception: str, frame: str, message: str) -> str:
    text = message.strip()
    first_line = text.splitlines()[0] if text else ""
    return "%s|%s|%s|%s" % (source, exception, frame, first_line)


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


class _LogHandler(logging.Handler):
    """Listens; never formats for output and never raises into the app."""

    def __init__(self, ledger: "Ledger") -> None:
        super().__init__(level=logging.WARNING)
        self._ledger = ledger

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            exception, trace = "", None
            if record.exc_info and record.exc_info[0] is not None:
                exception = record.exc_info[0].__name__
                trace = "".join(traceback.format_exception(*record.exc_info))
            worker = "worker" in record.name or "thread" in record.threadName.lower()
            self._ledger.record(
                WORKER_FAILURE if worker and record.levelno >= logging.ERROR
                else APPLICATION_WARNING,
                "logging", record.levelname,
                _fingerprint(record.name, exception,
                             "%s:%s" % (record.pathname, record.lineno), text),
                text, trace)
        except Exception:                                    # noqa: BLE001
            pass  # a diagnostic must never raise into the application


class Ledger:
    """The ledger, its expectations, and the hooks that fill it."""

    def __init__(self) -> None:
        self.phase = STARTUP
        self.entries: "dict[tuple[str, str], Entry]" = {}
        self.expectations: "list[dict[str, Any]]" = []
        # Re-entrant: a hook may fire while another record is being made on the
        # same thread (a warning raised while formatting a message).
        self._lock = threading.RLock()
        self._installed = False
        #: name -> (original, wrapper). Conditional restore compares to wrapper.
        self._hooks: "dict[str, tuple[Any, Any]]" = {}
        self._handler: "_LogHandler | None" = None
        self._tk: "tuple[Any, bool, Any, Any] | None" = None
        #: Increments on every record, so a window can ask "anything new?".
        self.sequence = 0
        self._final: "dict[str, Any] | None" = None
        self._final_lock = threading.Lock()

    # -- hooks --------------------------------------------------------------

    def install(self) -> None:
        """Start listening on the process-wide channels. Idempotent."""
        with self._lock:
            if self._installed:
                return
            self._installed = True
        self._handler = _LogHandler(self)
        logging.getLogger().addHandler(self._handler)

        original = sys.excepthook

        def excepthook(kind, value, tb):
            try:
                self._exception("sys.excepthook", kind, value, tb)
            except Exception:                                # noqa: BLE001
                pass
            original(kind, value, tb)

        sys.excepthook = excepthook
        self._hooks["sys.excepthook"] = (original, excepthook)

        original_thread = threading.excepthook

        def thread_hook(args):
            try:
                if args.exc_type is not SystemExit:
                    self._exception("threading.excepthook", args.exc_type,
                                    args.exc_value, args.exc_traceback,
                                    kind_override=WORKER_FAILURE)
            except Exception:                                # noqa: BLE001
                pass
            original_thread(args)

        threading.excepthook = thread_hook
        self._hooks["threading.excepthook"] = (original_thread, thread_hook)

        original_warn = warnings.showwarning

        def showwarning(message, category, filename, lineno, file=None, line=None):
            try:
                text = str(message)
                self.record(
                    APPLICATION_WARNING, "warnings", "WARNING",
                    _fingerprint(category.__name__, "",
                                 "%s:%s" % (filename, lineno), text),
                    "%s: %s" % (category.__name__, text))
            except Exception:                                # noqa: BLE001
                pass
            original_warn(message, category, filename, lineno, file, line)

        warnings.showwarning = showwarning
        self._hooks["warnings.showwarning"] = (original_warn, showwarning)

    def install_tk(self, app: Any) -> None:
        """Listen to Tk callback exceptions on `app`, then chain to its handler.

        Records whether the original was an INSTANCE attribute or inherited from
        the class, because restoring "the same object" is not restoring the same
        state: an inherited bound method must be restored by deleting our
        instance attribute, not by pinning a bound copy onto the instance.
        """
        with self._lock:
            if self._tk is not None:
                return
        original = app.report_callback_exception
        had_instance_attr = "report_callback_exception" in getattr(app, "__dict__", {})

        def report_callback_exception(exc, val, tb):
            try:
                self._exception("tk.callback", exc, val, tb)
            except Exception:                                # noqa: BLE001
                pass
            return original(exc, val, tb)

        app.report_callback_exception = report_callback_exception
        with self._lock:
            self._tk = (app, had_instance_attr, original, report_callback_exception)

    def uninstall(self) -> None:
        """Put every hook back where it is still ours. Idempotent."""
        with self._lock:
            if not self._installed and self._tk is None:
                return
            self._installed = False
            hooks, self._hooks = self._hooks, {}
            tk_state, self._tk = self._tk, None
        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)
            self._handler = None
        targets = {
            "sys.excepthook": (lambda: sys.excepthook,
                               lambda v: setattr(sys, "excepthook", v)),
            "threading.excepthook": (lambda: threading.excepthook,
                                     lambda v: setattr(threading, "excepthook", v)),
            "warnings.showwarning": (lambda: warnings.showwarning,
                                     lambda v: setattr(warnings, "showwarning", v)),
        }
        for name, (original, wrapper) in hooks.items():
            get, put = targets[name]
            if get() is wrapper:
                put(original)
            else:
                self._drift(name)
        if tk_state is not None:
            app, had_instance_attr, original, wrapper = tk_state
            if getattr(app, "report_callback_exception", None) is wrapper:
                if had_instance_attr:
                    app.report_callback_exception = original
                else:
                    try:
                        del app.report_callback_exception
                    except AttributeError:
                        pass
            else:
                self._drift("tk.callback")

    def _drift(self, name: str) -> None:
        self.record(HOOK_DRIFT, "harness", "WARNING",
                    _fingerprint("drift", "", name, name),
                    "%s was replaced while the ledger was installed; left as found"
                    % name)

    # -- recording ----------------------------------------------------------

    def _exception(self, channel: str, kind, value, tb,
                   kind_override: "str | None" = None) -> None:
        frames = traceback.extract_tb(tb)
        top = "%s:%s" % (frames[-1].filename, frames[-1].lineno) if frames else ""
        name = getattr(kind, "__name__", str(kind))
        self.record(
            kind_override or UNCAUGHT_EXCEPTION, channel, "CRITICAL",
            _fingerprint(channel, name, top, str(value)),
            "%s: %s" % (name, value),
            "".join(traceback.format_exception(kind, value, tb)))

    def record(self, kind: str, channel: str, severity: str, fingerprint: str,
               message: str, trace: "str | None" = None,
               detail: "dict[str, Any] | None" = None) -> None:
        """Add one occurrence. Safe from any thread."""
        now = time.time()
        with self._lock:
            key = (self.phase, fingerprint)
            entry = self.entries.get(key)
            if entry is None:
                entry = Entry(kind, channel, severity, self.phase, fingerprint,
                              first=now, traceback=trace,
                              detail=dict(detail or {}))
                self.entries[key] = entry
            entry.count += 1
            entry.last = now
            self.sequence += 1
            if message not in entry.messages and len(entry.messages) < _MESSAGES_KEPT:
                entry.messages.append(message)

    def step_failed(self, index: int, action: str, reason: str,
                    exc: "BaseException | None" = None) -> None:
        """A step that could not run is a failure of the run, not a remark."""
        detail = {"step_index": index, "step": action, "reason": reason,
                  "exception": type(exc).__name__ if exc is not None else None,
                  "timestamp": time.time()}
        self.record(STEP_FAILURE, "driver", "ERROR",
                    _fingerprint("step", "", "%s#%s" % (action, index), reason),
                    "step %s (%s): %s" % (index, action, reason),
                    detail=detail)

    def start_drive(self) -> None:
        """Startup is over; what follows belongs to the script."""
        with self._lock:
            self.phase = DRIVE

    # -- expectations -------------------------------------------------------

    def expect(self, ident: str, passed: bool, *, expected: Any, actual: Any,
               elapsed_ms: int, reason: str = "") -> None:
        """Record one expectation. Failure is permanent: also an entry, so
        `failed` stays true however the run continues."""
        with self._lock:
            self.expectations.append({
                "id": ident, "passed": bool(passed), "elapsed_ms": elapsed_ms,
                "expected": _jsonable(expected), "actual": _jsonable(actual),
                "failure_reason": "" if passed else reason,
            })
        if not passed:
            self.record(EXPECTATION_FAILURE, "expect", "ERROR",
                        _fingerprint("expect", "", ident, reason),
                        "%s: %s" % (ident, reason))

    # -- reading ------------------------------------------------------------

    def in_phase(self, phase: str) -> "list[Entry]":
        with self._lock:
            return [e for e in self.entries.values() if e.phase == phase]

    def drive_diagnostics(self) -> "list[Entry]":
        """What `expect_clean` looks at: what the APPLICATION said during the
        drive. A failed expectation or step is the script's own finding and
        drift is the harness's; counting either would make one failure
        report itself twice."""
        return [e for e in self.in_phase(DRIVE)
                if e.kind not in (HOOK_DRIFT, EXPECTATION_FAILURE, STEP_FAILURE)]

    def contains(self, needle: str) -> bool:
        """Live, in-memory: does any recorded message contain `needle`?"""
        with self._lock:
            return any(needle in m for e in self.entries.values()
                       for m in e.messages)

    def summary(self, phase: str = DRIVE) -> str:
        entries = self.in_phase(phase)
        if not entries:
            return "no diagnostics during %s" % phase
        parts = ["%dx %s [%s] %s" % (e.count, e.kind, e.channel,
                                     (e.messages[0] if e.messages else "")[:80])
                 for e in entries]
        return "%d distinct diagnostic(s) during %s: %s" % (
            len(entries), phase, "; ".join(parts))

    @property
    def failed(self) -> bool:
        """Whether the run should exit non-zero.

        An uncaught exception (in EITHER phase -- a launch crash is not
        excused), a failed expectation, or a failed step. A mere warning fails
        a run only when the script says `expect_clean`.
        """
        with self._lock:
            return any(e.kind in FAILING_KINDS for e in self.entries.values())

    @property
    def passed(self) -> bool:
        with self._lock:
            return not self.failed and all(e["passed"] for e in self.expectations)

    # -- ending -------------------------------------------------------------

    def finalize(self, build: "Callable[[], dict[str, Any]]") -> "dict[str, Any]":
        """Snapshot once. A second caller (quit, then atexit) gets the first
        result instead of a second, different report."""
        # A separate lock from the one hooks take: `build` may run git for up to
        # three seconds, and no application thread should wait on that to log.
        with self._final_lock:
            if self._final is None:
                self.uninstall()
                self._final = build()
            return self._final

    @property
    def finalized(self) -> bool:
        return self._final is not None


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def commit_info(git_exe: str, cwd: str) -> "tuple[str | None, str]":
    """The source commit as (sha, state). Evidence, never a dependency.

    Git absent, a detached HEAD or a frozen build all answer UNKNOWN and the run
    carries on: unknown is not a pass, but it is not a failure either.
    """
    if not git_exe or getattr(sys, "frozen", False):
        return None, COMMIT_UNKNOWN
    try:
        out = subprocess.run(
            [git_exe, "-C", cwd, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None, COMMIT_UNKNOWN
    sha = out.stdout.strip()
    if out.returncode == 0 and len(sha) >= 7:
        return sha, COMMIT_KNOWN
    return None, COMMIT_UNKNOWN


def script_hash(path: "str | None") -> "str | None":
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except OSError:
        return None


def build_report(ledger: Ledger, *, script: "str | None", run_id: str,
                 started: float, reason: str, shots: "list[str]",
                 git_exe: str = "", cwd: str = "") -> "dict[str, Any]":
    """The whole record of one run, as plain data."""
    sha, sha_state = commit_info(git_exe, cwd)
    with ledger._lock:                                       # noqa: SLF001
        steps_failed = [e.as_dict() for e in ledger.entries.values()
                        if e.kind == STEP_FAILURE]
        return {
            "report_schema": REPORT_SCHEMA,
            "run_id": run_id,
            "pid": os.getpid(),
            "script": script,
            "script_hash": script_hash(script),
            "commit_sha": sha,
            "commit_sha_state": sha_state,
            "python": sys.version.split()[0],
            "started_at": started,
            "finished_at": time.time(),
            "finished_because": reason,
            "passed": ledger.passed,
            "expectations": list(ledger.expectations),
            "steps_failed": steps_failed,
            "startup_diagnostics": [e.as_dict() for e in ledger.in_phase(STARTUP)],
            "drive_diagnostics": [e.as_dict() for e in ledger.in_phase(DRIVE)],
            "shots": list(shots),
        }


def write_report_atomically(path: str, report: "dict[str, Any]") -> None:
    """Write `report` so a killed run never leaves a half-written file.

    A temporary file beside the target, flushed to disk, then `os.replace`d: the
    reader sees the old file or the whole new one. Not routed through any
    captured channel, so a failure here cannot become a new diagnostic.
    """
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
