"""Script the running app, for checks that need the REAL window (Tk skeleton).

Copied into a NEW project by TokenSave Manager. Rename `MYAPP_DRIVE`, then grow
the steps for your own widgets. The evidence machinery -- what the app said about
itself, the verdict, the report, the exit code -- is `drive_ledger.py` beside this
file and is toolkit-neutral; read `README.md` first for why each rule exists.

    MYAPP_DRIVE=<script.json>  python app.py

    [
      {"do": "click",        "text": "Save"},
      {"do": "expect",       "id": "saved", "check": "widget_text",
                              "text": "Saved", "within_ms": 3000},
      {"do": "expect_clean", "settle_ms": 1000},
      {"do": "log_report"},
      {"do": "quit"}
    ]

Wire it in two places in your `App.__init__`:

    import debug_drive
    debug_drive.begin(self)              # FIRST thing a Tk root allows
    ...                                  # build the window
    self._driver = debug_drive.start_if_requested(self)   # LAST, and keep it

Drive the window from INSIDE the process. Never with the mouse: it needs focus for
every step, makes the machine unusable for the run, and a stray click lands in
another application.
"""

from __future__ import annotations

import atexit
import json
import os
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import drive_ledger

_DRIVE_SCRIPT = os.environ.get("MYAPP_DRIVE")
_LEDGER = None
_DEFAULT_AFTER_MS = 400


def _say(message: str) -> None:
    """Print without the console's encoding being able to stop the run.

    A cp1252 console cannot encode a check mark, and the UnicodeEncodeError would
    be raised inside a step, escape the timer chain, and look exactly like the app
    hanging: a diagnostic dying on the glyphs of the thing it diagnoses.
    """
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(message.encode(enc, "replace").decode(enc), flush=True)


def begin(app=None) -> None:
    """Listen from launch, so startup diagnostics are on the record."""
    global _LEDGER
    if not _DRIVE_SCRIPT or _LEDGER is not None:
        return
    _LEDGER = drive_ledger.Ledger()
    _LEDGER.install()
    if app is not None:
        _LEDGER.install_tk(app)


def start_if_requested(app):
    if not _DRIVE_SCRIPT:
        return None
    try:
        steps = json.loads(Path(_DRIVE_SCRIPT).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _say("drive: cannot read %s: %s" % (_DRIVE_SCRIPT, exc))
        return None
    if not isinstance(steps, list):
        _say("drive: %s is not a JSON list of steps" % _DRIVE_SCRIPT)
        return None
    driver = Driver(app, steps, ledger=_LEDGER, script=_DRIVE_SCRIPT)
    driver.start()
    return driver


def _walk(widget):
    yield widget
    try:
        children = widget.winfo_children()
    except tk.TclError:
        return
    for child in children:
        yield from _walk(child)


def _text(widget) -> str:
    try:
        return str(widget.cget("text"))
    except (tk.TclError, AttributeError):
        return ""


class Driver:
    def __init__(self, app, steps, *, ledger=None, script=None) -> None:
        self._app, self._steps, self._index = app, steps, 0
        # Never None: a hand-built driver gets a ledger that listens to nothing.
        self._ledger = ledger if ledger is not None else drive_ledger.Ledger()
        self._script = script
        self._run_id = drive_ledger.new_run_id()
        self._started = time.time()
        self._hold, self._hold_ms = None, 500

    def start(self) -> None:
        self._ledger.start_drive()
        atexit.register(self._finalize, "exit")   # only if there is no `quit`
        self._app.after(600, self._run_next)

    def _run_next(self) -> None:
        """Do a step AND arm the next. `step()` alone arms nothing, which is
        what a test stepping by hand wants (a timer leaked per step corrupted a
        whole suite once)."""
        if self._hold is not None:
            if self._hold():
                self._app.after(self._hold_ms, self._run_next)
                return
            self._hold, self._hold_ms = None, 500
        if not self.step():
            return
        last = self._steps[self._index - 1]
        self._app.after(int(last.get("after_ms", _DEFAULT_AFTER_MS)), self._run_next)

    def step(self) -> bool:
        if self._index >= len(self._steps):
            return False
        step = self._steps[self._index]
        self._index += 1
        name = str(step.get("do", ""))
        handler = getattr(self, "_do_" + name, None)
        if handler is None:
            self._ledger.step_failed(self._index, name, "unknown step")
            return True
        try:
            handler(step)
        except Exception as exc:                              # noqa: BLE001
            # A failed step is a failure OF THE RUN, not a printed remark, and
            # it must not strand the `quit` that ends the run.
            self._ledger.step_failed(self._index, name, str(exc), exc)
        return True

    # -- steps: grow these for your own widgets -----------------------------

    def _do_wait(self, step) -> None:
        pass

    def _do_click(self, step) -> None:
        want = str(step.get("text", "")).strip().lower()
        for widget in _walk(self._app):
            if hasattr(widget, "invoke") and want and want in _text(widget).lower():
                label = _text(widget)          # read BEFORE: invoke may destroy it
                widget.invoke()
                _say("drive: click -> %r" % label)
                return
        raise LookupError("no button matching %r" % want)

    def _check(self, step):
        check, text = str(step.get("check", "")), str(step.get("text", ""))
        if check == "widget_text":
            hit = next((_text(w) for w in _walk(self._app)
                        if text and text.lower() in _text(w).lower()), "")
            return bool(hit), hit
        if check == "diagnostic_contains":   # the LIVE ledger, never a file
            return self._ledger.contains(text), self._ledger.summary()
        raise ValueError("unknown check %r" % check)

    def _do_expect(self, step) -> None:
        """Assert state, polling: Tk state settles asynchronously. Failure is
        permanent -- a later success never erases it."""
        ident = str(step.get("id") or step.get("check"))
        within = int(step.get("within_ms", 0))
        began = time.monotonic()

        def poll() -> bool:
            elapsed = int((time.monotonic() - began) * 1000)
            try:
                ok, actual = self._check(step)
            except Exception as exc:                          # noqa: BLE001
                self._ledger.expect(ident, False, expected=step, actual=None,
                                    elapsed_ms=elapsed, reason="check raised: %s" % exc)
                return False
            if ok:
                self._ledger.expect(ident, True, expected=step, actual=actual,
                                    elapsed_ms=elapsed)
                return False
            if elapsed >= within:
                self._ledger.expect(ident, False, expected=step, actual=actual,
                                    elapsed_ms=elapsed,
                                    reason="not satisfied within %d ms" % within)
                return False
            return True

        if poll():
            self._hold, self._hold_ms = poll, 100

    def _do_expect_clean(self, step) -> None:
        """Nothing wrong so far AND quiet for `settle_ms`. Without the window, a
        callback that throws 200 ms after this check passes it, and the run is
        still not clean."""
        settle = int(step.get("settle_ms", 1000))
        began = time.monotonic()

        def poll() -> bool:
            elapsed = int((time.monotonic() - began) * 1000)
            if self._ledger.drive_diagnostics():
                self._ledger.expect("expect_clean", False, expected="no diagnostics",
                                    actual=self._ledger.summary(), elapsed_ms=elapsed,
                                    reason=self._ledger.summary())
                return False
            if elapsed >= settle:
                self._ledger.expect("expect_clean", True, expected="no diagnostics",
                                    actual="none", elapsed_ms=elapsed)
                return False
            return True

        if poll():
            self._hold, self._hold_ms = poll, 100

    def _do_log_report(self, step) -> None:
        _say("drive: startup -- " + self._ledger.summary(drive_ledger.STARTUP))
        _say("drive: drive   -- " + self._ledger.summary(drive_ledger.DRIVE))

    # -- ending -------------------------------------------------------------

    def _finalize(self, reason: str) -> dict:
        """Uninstall hooks, snapshot, write the report ONCE, before any exit."""
        first = not self._ledger.finalized
        report = self._ledger.finalize(lambda: drive_ledger.build_report(
            self._ledger, script=self._script, run_id=self._run_id,
            started=self._started, reason=reason, shots=[]))
        if first and self._script:
            path = str(Path(self._script).with_suffix(".report.json"))
            try:
                drive_ledger.write_report_atomically(path, report)
                _say("drive: report -> %s" % path)
            except (OSError, ValueError, TypeError) as exc:
                _say("drive: could not write %s: %s" % (path, exc))
        return report

    def _do_quit(self, step) -> None:
        code = 0 if self._finalize("quit")["passed"] else 1
        _say("drive: quit (exit %d)" % code)
        if self._script:      # a hand-built driver must never exit the test runner
            timer = threading.Timer(0.2, self._exit_now, args=(code,))
            timer.daemon = True
            timer.start()
        try:
            self._app.destroy()
        except tk.TclError:
            pass

    @staticmethod
    def _exit_now(code: int) -> None:
        # os._exit skips interpreter shutdown (and flushing), and is the only way
        # to end a run a background thread would otherwise hold open.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        os._exit(code)
