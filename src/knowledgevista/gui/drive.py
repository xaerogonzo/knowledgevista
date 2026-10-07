"""The in-app driver: a scripted run of the REAL window that ends in a verdict.

    KNOWLEDGEVISTA_DRIVE=<script.json>  KNOWLEDGEVISTA_HOME=<scratch folder>  kv gui

A script is a JSON list of steps. They run inside the process, on the toolkit's own timer, through the window's real widgets: it types
into the real search box, selects rows in the real table, presses the real buttons, fills in the real dialogs. It never moves the mouse
and never sends an operating-system key, so it needs no focus, leaves the machine usable, and cannot land a click in another program.

A run is evidence, not a demonstration (drive_template/README.md): what the application said about itself (a log warning, an exception
in a slot, a worker that died, a Qt warning) is kept in a ledger and becomes the verdict, a failed `expect` is permanent, and the exit
status is non-zero when anything failed. `drive_ledger.py` is the template's, byte for byte (a test keeps it so); this module is the
Qt half the template's README describes.

Refuses to run against the real library: a script adds folders, scans and changes things. Set KNOWLEDGEVISTA_HOME to a scratch folder
(or pass --catalog), or say so on purpose with KNOWLEDGEVISTA_DRIVE_ALLOW_REAL=1.

Steps (see docs/GUI.md for the table and examples):
    add_root, scan, extract, resolve, wait_jobs, search, type, press, select, select_review, select_hit, scope, tab, filter, dialog,
    close_dialogs, external, restart, mark, shot, sleep, expect, expect_clean, log_report, quit
"""

from __future__ import annotations

import atexit
import json
import os
import string
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QtMsgType, QTimer, qInstallMessageHandler
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QAbstractButton, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QLabel, QLineEdit, QPlainTextEdit, QTableView,
                               QTreeWidgetItem)

from knowledgevista import paths
from knowledgevista.gui import drive_ledger as L
from knowledgevista.gui.models import ITEM_ROLE
from knowledgevista.gui.shell import RecordingShell
from knowledgevista.gui.text import audit

ENV_SCRIPT = "KNOWLEDGEVISTA_DRIVE"
ENV_HIDDEN = "KNOWLEDGEVISTA_DRIVE_HIDDEN"
ENV_ALLOW_REAL = "KNOWLEDGEVISTA_DRIVE_ALLOW_REAL"
ENV_TIMEOUT = "KNOWLEDGEVISTA_DRIVE_TIMEOUT_S"

DEFAULT_AFTER_MS = 100
HOLD_POLL_MS = 40
DEFAULT_TIMEOUT_S = 300

#: Notices the headless test platform prints about ITSELF (it has no window manager). They say nothing about this application, and a
#: verdict that failed on them would fail every run on the platform CI uses. Matched whole, never as a substring: a real warning that
#: merely mentions them is still recorded.
PLATFORM_NOTICES = frozenset({"This plugin does not support propagateSizeHints()"})

_LEDGER: L.Ledger | None = None
_PREVIOUS_QT_HANDLER: Any = None


def script_path() -> str | None:
    return os.environ.get(ENV_SCRIPT) or None


def hidden() -> bool:
    return os.environ.get(ENV_HIDDEN, "") not in ("", "0")


def say(message: str) -> None:
    """Print without the console's encoding being able to stop the run: a cp1252 console cannot encode a typographic quote, and the
    UnicodeEncodeError would be raised inside a step and look exactly like the application hanging."""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(message.encode(encoding, "replace").decode(encoding), flush=True)


# ------------------------------------------------------------------------------------------------------- listening from launch


def _qt_message(mode: QtMsgType, context: Any, message: str) -> None:
    try:
        if _LEDGER is not None and message not in PLATFORM_NOTICES and mode in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
            where = f"{getattr(context, 'file', None)}:{getattr(context, 'line', 0)}"
            _LEDGER.record(L.APPLICATION_WARNING, "qt", "WARNING" if mode == QtMsgType.QtWarningMsg else "ERROR",
                           L._fingerprint("qt", "", where, message), message)  # noqa: SLF001
    except Exception:  # noqa: BLE001 - a diagnostic must never raise into the application
        pass
    if _PREVIOUS_QT_HANDLER is not None:
        _PREVIOUS_QT_HANDLER(mode, context, message)
    else:
        print(message, file=sys.stderr, flush=True)


def begin() -> L.Ledger | None:
    """Listen from launch, so a startup warning is on the record (in its own phase, so it cannot fail `expect_clean` for ever)."""
    global _LEDGER, _PREVIOUS_QT_HANDLER
    if not script_path() or _LEDGER is not None:
        return _LEDGER
    _LEDGER = L.Ledger()
    _LEDGER.install()
    _PREVIOUS_QT_HANDLER = qInstallMessageHandler(_qt_message)
    return _LEDGER


def _end_listening() -> None:
    global _PREVIOUS_QT_HANDLER
    qInstallMessageHandler(_PREVIOUS_QT_HANDLER)
    _PREVIOUS_QT_HANDLER = None


def refuses_real_library(catalog: Path | str) -> str | None:
    """Why this run must not go ahead, or None. A script changes the library it runs against."""
    if os.environ.get(ENV_ALLOW_REAL):
        return None
    if os.environ.get(paths.HOME_ENV):
        return None  # everything lives under a folder the caller chose
    if Path(catalog) == paths.catalog_path():
        return (f"This would drive the REAL library ({catalog}). Set {paths.HOME_ENV} to a scratch folder, or pass --catalog, "
                f"or set {ENV_ALLOW_REAL}=1 if you mean it.")
    return None


def start_if_requested(app: Any, window: Any, catalog: Path | str | None = None) -> Driver | None:
    path = script_path()
    if not path:
        return None
    catalog = Path(catalog) if catalog else window.catalog
    why = refuses_real_library(catalog)
    if why:
        say(f"drive: {why}")
        os._exit(2)
    try:
        steps = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        say(f"drive: cannot read {path}: {exc}")
        os._exit(2)
    if not isinstance(steps, list):
        say(f"drive: {path} is not a JSON list of steps")
        os._exit(2)
    driver = Driver(app, window, _expand(steps), ledger=_LEDGER, script=path, catalog=catalog)
    driver.start()
    return driver


def _expand(value: Any) -> Any:
    """`${NAME}` in any string of the script becomes that environment variable, so one committed script can run against any scratch library."""
    if isinstance(value, str):
        return string.Template(value).safe_substitute(os.environ)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


# ------------------------------------------------------------------------------------------------------------------ the driver


class StepError(Exception):
    """A step that could not be done as written: reported as a failure of the run, never swallowed."""


class Driver:
    def __init__(self, app: Any, window: Any, steps: list[dict[str, Any]], *, ledger: L.Ledger | None = None, script: str | None = None,
                 catalog: Path | str | None = None):
        self.app, self.window, self.steps, self.index = app, window, steps, 0
        # Never None: a hand-built driver gets a ledger that listens to nothing.
        self.ledger = ledger if ledger is not None else L.Ledger()
        self.script, self.catalog = script, Path(catalog) if catalog else window.catalog
        self.run_id, self.started = L.new_run_id(), time.time()
        self.shots: list[str] = []
        self.marks: dict[str, int] = {}
        self.shell = RecordingShell()  # a driven run never starts a PDF viewer
        window.shell = self.shell
        self._hold: Callable[[], bool] | None = None
        self._hold_ms = HOLD_POLL_MS
        self._watchdog: threading.Timer | None = None

    # -- running ---------------------------------------------------------------------------------------------------------

    def start(self) -> None:
        self.ledger.start_drive()
        atexit.register(self._finalize, "exit")  # only matters if the script has no `quit`
        if self.script:
            timeout = float(os.environ.get(ENV_TIMEOUT, DEFAULT_TIMEOUT_S))
            self._watchdog = threading.Timer(timeout, self._timed_out, args=(timeout,))
            self._watchdog.daemon = True
            self._watchdog.start()
        self._arm(600)

    def _arm(self, ms: int) -> None:
        # The WINDOW is the context object: if it dies, Qt cancels the chain instead of running it against a freed window.
        QTimer.singleShot(ms, self.window, self._run_next)

    def _run_next(self) -> None:
        """Do a step AND arm the next. `step()` alone arms nothing, which is what a test stepping by hand wants: a timer left armed
        per step on a window that is only closed fires into a later test."""
        if self._hold is not None:
            if self._hold():
                self._arm(self._hold_ms)
                return
            self._hold, self._hold_ms = None, HOLD_POLL_MS
        if not self.step():
            return
        last = self.steps[self.index - 1]
        self._arm(int(last.get("after_ms", DEFAULT_AFTER_MS)))

    def step(self) -> bool:
        if self.index >= len(self.steps):
            # A script that forgot `quit` would leave the application running for ever. It is a mistake in the script, so it fails the run.
            self.ledger.step_failed(self.index + 1, "end", "the script ended without a quit step")
            self._do_quit({})
            return False
        step = self.steps[self.index]
        self.index += 1
        name = str(step.get("do", ""))
        handler = getattr(self, "_do_" + name, None)
        if handler is None:
            self.ledger.step_failed(self.index, name, "unknown step")
            return True
        try:
            handler(step)
        except Exception as exc:  # noqa: BLE001
            # A failed step is a failure OF THE RUN, not a printed remark, and it must not strand the `quit` that ends the run.
            self.ledger.step_failed(self.index, name, f"{type(exc).__name__}: {exc}", exc)
        return True

    def _wait(self, poll: Callable[[], bool], ms: int = HOLD_POLL_MS) -> None:
        """Hold the script until `poll()` says it is done (it returns True while still waiting)."""
        if poll():
            self._hold, self._hold_ms = poll, ms

    # -- finding things --------------------------------------------------------------------------------------------------

    def _roots(self) -> list[QObject]:
        return [self.window, *self.window._dialogs, *self.window._boxes]  # noqa: SLF001 - the driver is the window's test harness

    def _find(self, name: str, kind: type | None = None) -> Any:
        for root in self._roots():
            if root.objectName() == name and (kind is None or isinstance(root, kind)):
                return root
            found = root.findChild(kind or QObject, name)
            if found is not None:
                return found
        raise StepError(f"no widget named {name!r}" + (f" of type {kind.__name__}" if kind else ""))

    def _dialog(self, name: str) -> QDialog:
        for dialog in self.window._dialogs:  # noqa: SLF001
            if dialog.objectName() == name:
                return dialog
        raise StepError(f"no dialog named {name!r} is open (open: {[d.objectName() for d in self.window._dialogs]})")  # noqa: SLF001

    def _button(self, step: dict[str, Any]) -> QAbstractButton | Any:
        if "widget" in step:
            for root in self._roots():
                for button in [root, *root.findChildren(QAbstractButton)]:
                    if isinstance(button, QAbstractButton) and button.objectName() == step["widget"]:
                        return button
            action = self.window.findChild(QObject, step["widget"])
            if action is not None and hasattr(action, "trigger"):
                return action
            raise StepError(f"no button or action named {step['widget']!r}")
        wanted = str(step.get("text", "")).strip().lower()
        for root in self._roots():
            for button in root.findChildren(QAbstractButton):
                if wanted and button.text().replace("&", "").strip().lower() == wanted and button.isVisibleTo(root):
                    return button
        raise StepError(f"no button labelled {step.get('text')!r}")

    def _table(self, name: str) -> QTableView:
        table = getattr(self.window, {"docTable": "doc_table", "hitTable": "hit_table", "reviewTable": "review_table"}.get(name, name), None)
        if not isinstance(table, QTableView):
            raise StepError(f"no table named {name!r}")
        return table

    def _doc_row(self, step: dict[str, Any]) -> int:
        proxy = self.window.doc_proxy
        if "row" in step:
            return int(step["row"])
        wanted = step.get("title") or step.get("name")
        for r in range(proxy.rowCount()):
            row = proxy.index(r, 1).data(ITEM_ROLE)
            if wanted in (row.display_title, row.name):
                return r
        raise StepError(f"no document called {wanted!r} in the list ({[proxy.index(r, 1).data() for r in range(proxy.rowCount())]})")

    # -- steps: doing ----------------------------------------------------------------------------------------------------

    def _do_sleep(self, step: dict[str, Any]) -> None:
        end = time.monotonic() + int(step.get("ms", 500)) / 1000
        self._wait(lambda: time.monotonic() < end)

    def _do_wait_jobs(self, step: dict[str, Any]) -> None:
        """Until nothing is unfinished (every job's result is on screen). Failing to settle is a failure of the run."""
        within = int(step.get("within_ms", 60000))
        began = time.monotonic()

        def poll() -> bool:
            if not self.window.jobs.active():
                return False
            if (time.monotonic() - began) * 1000 > within:
                self.ledger.expect("wait_jobs", False, expected="no unfinished jobs", actual=[j.title for j in self.window.jobs.active()],
                                   elapsed_ms=within, reason=f"jobs were still running after {within} ms")
                return False
            return True

        self._wait(poll)

    def _do_press(self, step: dict[str, Any]) -> None:
        target = self._button(step)
        if hasattr(target, "isEnabled") and not target.isEnabled():
            raise StepError(f"{step.get('widget') or step.get('text')!r} is disabled")
        label = target.text() if hasattr(target, "text") else str(step.get("widget"))
        (target.click if isinstance(target, QAbstractButton) else target.trigger)()
        say(f"drive: press -> {label!r}")

    def _do_add_root(self, step: dict[str, Any]) -> None:
        """Through the real dialog: open it, type the folder, leave organizing as the script says, press the real button."""
        self.window.act_add.trigger()
        dialog = self._dialog("addRootDialog")
        self._type_into(dialog.path_edit, str(step["path"]))
        if step.get("label"):
            self._type_into(dialog.label_edit, str(step["label"]))
        dialog.organize_check.setChecked(bool(step.get("allow_organize", False)))
        ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if not ok.isEnabled():
            raise StepError("the Add and scan button is disabled")
        ok.click()

    def _do_scan(self, _step: dict[str, Any]) -> None:
        self._trigger("act_scan")

    def _do_extract(self, _step: dict[str, Any]) -> None:
        self._trigger("act_extract")

    def _do_resolve(self, _step: dict[str, Any]) -> None:
        self._trigger("act_resolve")

    def _trigger(self, action_name: str) -> None:
        action = getattr(self.window, action_name)
        if not action.isEnabled():
            raise StepError(f"{action.text()} is disabled")
        action.trigger()

    @staticmethod
    def _type_into(edit: QLineEdit, text: str, enter: bool = False) -> None:
        edit.clear()
        QTest.keyClicks(edit, text)  # real key events delivered to the widget, not to the operating system
        if enter:
            QTest.keyClick(edit, Qt.Key.Key_Return)

    def _do_type(self, step: dict[str, Any]) -> None:
        edit = self._find(str(step["widget"]), QLineEdit)
        self._type_into(edit, str(step["text"]), bool(step.get("enter", False)))

    def _do_search(self, step: dict[str, Any]) -> None:
        self._type_into(self.window.search_box, str(step["text"]), enter=True)

    def _do_filter(self, step: dict[str, Any]) -> None:
        self._type_into(self.window.filter_box, str(step.get("text", "")))

    def _do_tab(self, step: dict[str, Any]) -> None:
        from knowledgevista.gui.window import TAB_KEYS

        self.window.tabs.setCurrentIndex(TAB_KEYS.index(str(step["name"])))

    def _do_scope(self, step: dict[str, Any]) -> None:
        wanted = str(step["scope"])

        def walk(item: QTreeWidgetItem) -> QTreeWidgetItem | None:
            if item.data(0, Qt.ItemDataRole.UserRole) == wanted:
                return item
            for i in range(item.childCount()):
                found = walk(item.child(i))
                if found is not None:
                    return found
            return None

        tree = self.window.sidebar
        for i in range(tree.topLevelItemCount()):
            found = walk(tree.topLevelItem(i))
            if found is not None:
                tree.setCurrentItem(found)
                return
        raise StepError(f"the sidebar has no entry for {wanted!r}")

    def _do_select(self, step: dict[str, Any]) -> None:
        self.window.tabs.setCurrentIndex(0)
        self.window.doc_table.selectRow(self._doc_row(step))

    def _do_select_hit(self, step: dict[str, Any]) -> None:
        self.window.tabs.setCurrentIndex(1)
        self.window.hit_table.selectRow(int(step.get("row", 0)))

    def _do_select_review(self, step: dict[str, Any]) -> None:
        self.window.tabs.setCurrentIndex(2)
        proxy = self.window.review_proxy
        for r in range(proxy.rowCount()):
            item = proxy.index(r, 0).data(ITEM_ROLE)
            if (item["title"] or item["name"]) == step["paper"] and item["label"] == step["field"]:
                self.window.review_table.selectRow(r)
                return
        raise StepError(f"no review item for {step['paper']!r} / {step['field']!r}")

    def _do_dialog(self, step: dict[str, Any]) -> None:
        """Fill a dialog that is open through its real widgets (by object name), then press one of its buttons by its label."""
        dialog = self._dialog(str(step["name"]))
        for name, value in (step.get("set") or {}).items():
            widget = dialog.findChild(QObject, name)
            if isinstance(widget, QLineEdit):
                self._type_into(widget, str(value))
            elif isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QComboBox):
                widget.setEditText(str(value)) if widget.isEditable() else widget.setCurrentText(str(value))
            else:
                raise StepError(f"{name!r} in {dialog.objectName()} is not something a script can fill in")
        if step.get("press"):
            self._do_press({"text": step["press"]})

    def _do_close_dialogs(self, _step: dict[str, Any]) -> None:
        for dialog in list(self.window._dialogs):  # noqa: SLF001
            dialog.close()
        for box in list(self.window._boxes):  # noqa: SLF001
            box.close()

    def _do_external(self, step: dict[str, Any]) -> None:
        """Run `kv` in a SEPARATE process against this catalog (the window is open and keeps running meanwhile). The window must notice
        what it did through the catalog's revision alone."""
        args = [sys.executable, "-m", "knowledgevista", "--catalog", str(self.catalog), *[str(a) for a in step["args"]]]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)  # noqa: S603
        expected = int(step.get("expect_exit", 0))

        def poll() -> bool:
            if proc.poll() is None:
                return True
            out, err = proc.communicate()
            if proc.returncode != expected:
                raise StepError(f"kv {' '.join(map(str, step['args']))} exited {proc.returncode}, expected {expected}: {err.strip() or out.strip()}")
            say(f"drive: external kv {' '.join(map(str, step['args']))[:60]} -> exit {proc.returncode}")
            return False

        # An exception raised inside a poll must reach the ledger as THIS step's failure, not escape the timer chain.
        def guarded() -> bool:
            try:
                return poll()
            except StepError as exc:
                self.ledger.step_failed(self.index, "external", str(exc), exc)
                return False

        self._wait(guarded)

    def _do_log(self, step: dict[str, Any]) -> None:
        """Say something through the application's own logger. It exists so a script can PROVE `expect_clean` can fail: a check that
        cannot fail is not a check."""
        import logging

        getattr(logging.getLogger("knowledgevista.gui.drive"), str(step.get("level", "warning")))(str(step.get("message", "a diagnostic from the script")))

    def _do_mark(self, step: dict[str, Any]) -> None:
        self.marks[str(step["name"])] = self.window.revision

    def _do_restart(self, step: dict[str, Any]) -> None:
        """Close the window the way a person does (state saved, jobs stopped) and open a new one from what is on disk."""
        from knowledgevista.gui.app import make_window

        old = self.window
        old.close()
        new = make_window(self.catalog, shell=self.shell, remember=True, poll_ms=old._watch.interval())  # noqa: SLF001
        self.window = new
        if not hidden():
            new.show()
        old.deleteLater()
        say("drive: restarted the window")

    def _do_shot(self, step: dict[str, Any]) -> None:
        """A direct paint of the window (or one named widget), so it can sit behind anything and needs no screen grab."""
        target = self.window if not step.get("widget") else self._find(str(step["widget"]))
        path = Path(str(step["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        if not target.grab().save(str(path)):
            raise StepError(f"could not write {path}")
        self.shots.append(str(path))

    # -- steps: asserting ------------------------------------------------------------------------------------------------

    def _text_of(self, widget: Any) -> str:
        if isinstance(widget, (QLabel, QLineEdit)):
            return widget.text()
        if isinstance(widget, QPlainTextEdit):
            return widget.toPlainText()
        if hasattr(widget, "text"):
            return widget.text()
        raise StepError(f"{widget.objectName()!r} has no text to read")

    @staticmethod
    def _compare(step: dict[str, Any], actual: Any) -> bool:
        ok = True
        if "equals" in step:
            ok &= actual == step["equals"]
        if "contains" in step:
            ok &= step["contains"] in actual
        if "not_contains" in step:
            ok &= step["not_contains"] not in actual
        if "at_least" in step:
            ok &= actual >= step["at_least"]
        if "at_most" in step:
            ok &= actual <= step["at_most"]
        if "includes" in step:
            ok &= all(x in actual for x in step["includes"])
        if "endswith_any" in step:
            ok &= any(str(a).endswith(step["endswith_any"]) for a in actual)
        return bool(ok)

    def _check(self, step: dict[str, Any]) -> tuple[bool, Any]:
        check, w = str(step.get("check", "")), self.window
        if check == "rows":
            actual = self._table(str(step["table"])).model().rowCount()
        elif check == "titles":
            actual = [w.doc_proxy.index(r, 1).data() for r in range(w.doc_proxy.rowCount())]
        elif check == "cell":
            table = self._table(str(step.get("table", "docTable")))
            row = self._doc_row(step) if table is w.doc_table and ("title" in step or "name" in step) else int(step.get("row", 0))
            actual = table.model().index(row, int(step["column"])).data()
        elif check == "detail_title":
            actual = w.detail.title.text()
        elif check == "detail_field":
            rows = {r["field"]: r for r in w.detail.field_rows()}
            if str(step["field"]) not in rows:
                return False, sorted(rows)
            actual = rows[str(step["field"])][str(step.get("column", "value"))]
        elif check == "text":
            actual = self._text_of(self._find(str(step["widget"])))
        elif check == "message":
            kinds = [step["kind"]] if step.get("kind") else None
            actual = "\n".join(text for kind, text in w.messages if kinds is None or kind in kinds)
        elif check == "tab":
            actual = w.current_tab
        elif check == "tab_text":
            actual = w.tabs.tabText(int(step["index"]))
        elif check == "review_items":
            actual = len(w.review_all)
        elif check == "filter_text":
            actual = w.filter_box.text()
        elif check == "search_text":
            actual = w.search_box.text()
        elif check == "scope":
            actual = w.scope
        elif check == "revision":
            actual = w.revision
            if "greater_than_mark" in step:
                return actual is not None and actual > self.marks[str(step["greater_than_mark"])], actual
        elif check == "shell":
            kind = step.get("kind")
            actual = [path for k, path in self.shell.calls if kind is None or k == kind]
        elif check == "shell_count":
            actual = len(self.shell.calls)
        elif check == "text_safe":
            actual = audit(w)
            return actual == [], actual
        elif check == "jobs_idle":
            actual = [j.title for j in w.jobs.active()]
            return actual == [], actual
        elif check == "sidebar_has":
            actual = [w.sidebar.topLevelItem(i).child(j).text(0) for i in range(w.sidebar.topLevelItemCount()) for j in range(w.sidebar.topLevelItem(i).childCount())]
        elif check == "dialog_open":
            actual = [d.objectName() for d in w._dialogs]  # noqa: SLF001
        elif check == "catalog":
            from knowledgevista.db.catalog import open_catalog

            conn = open_catalog(self.catalog, create=False, read_only=True)
            try:
                row = conn.execute(str(step["sql"]), tuple(step.get("args", []))).fetchone()
            finally:
                conn.close()
            actual = row[0] if row is not None else None
        elif check == "state":
            actual = json.loads(paths.gui_state_path().read_text(encoding="utf-8")).get(str(step["key"]))
        elif check == "diagnostic_contains":  # the LIVE ledger, never a file
            return self.ledger.contains(str(step["text"])), self.ledger.summary()
        else:
            raise ValueError(f"unknown check {check!r}")
        return self._compare(step, actual), actual

    def _do_expect(self, step: dict[str, Any]) -> None:
        """State holds within `within_ms` (polled: the screen settles asynchronously). Failure is permanent: a later success never erases it."""
        ident = str(step.get("id") or step.get("check"))
        within = int(step.get("within_ms", 3000))
        began = time.monotonic()

        def poll() -> bool:
            elapsed = int((time.monotonic() - began) * 1000)
            try:
                ok, actual = self._check(step)
            except Exception as exc:  # noqa: BLE001
                self.ledger.expect(ident, False, expected=step, actual=None, elapsed_ms=elapsed, reason=f"check raised: {type(exc).__name__}: {exc}")
                return False
            if ok:
                self.ledger.expect(ident, True, expected=step, actual=actual, elapsed_ms=elapsed)
                return False
            if elapsed >= within:
                self.ledger.expect(ident, False, expected=step, actual=actual, elapsed_ms=elapsed, reason=f"not satisfied within {within} ms (actual: {actual!r})")
                return False
            return True

        self._wait(poll)

    def _do_expect_clean(self, step: dict[str, Any]) -> None:
        """Nothing wrong so far AND quiet for `settle_ms`: a callback that throws 200 ms after this check must still fail the run.
        `allow` lists substrings of diagnostics that are known and excused (a Qt platform plugin's own notice)."""
        settle = int(step.get("settle_ms", 1000))
        allow = [str(a) for a in step.get("allow", [])]
        began = time.monotonic()

        def unexcused() -> list[L.Entry]:
            return [e for e in self.ledger.drive_diagnostics() if not any(a in m for a in allow for m in e.messages)]

        def poll() -> bool:
            elapsed = int((time.monotonic() - began) * 1000)
            bad = unexcused()
            if bad:
                text = "; ".join(f"{e.count}x {e.kind} [{e.channel}] {(e.messages[0] if e.messages else '')[:100]}" for e in bad)
                self.ledger.expect("expect_clean", False, expected="no diagnostics", actual=text, elapsed_ms=elapsed, reason=text)
                return False
            if elapsed >= settle:
                self.ledger.expect("expect_clean", True, expected="no diagnostics", actual="none", elapsed_ms=elapsed)
                return False
            return True

        self._wait(poll)

    def _do_log_report(self, _step: dict[str, Any]) -> None:
        say("drive: startup -- " + self.ledger.summary(L.STARTUP))
        say("drive: drive   -- " + self.ledger.summary(L.DRIVE))

    # -- ending ----------------------------------------------------------------------------------------------------------

    def _finalize(self, reason: str) -> dict[str, Any]:
        """Stop listening, snapshot, write the report ONCE, before any exit."""
        first = not self.ledger.finalized
        report = self.ledger.finalize(lambda: L.build_report(self.ledger, script=self.script, run_id=self.run_id, started=self.started, reason=reason,
                                                            shots=self.shots))
        if first:
            _end_listening()
            if self._watchdog is not None:
                self._watchdog.cancel()
        if first and self.script:
            target = str(Path(self.script).with_suffix(".report.json"))
            try:
                L.write_report_atomically(target, report)
                say(f"drive: report -> {target}")
            except (OSError, ValueError, TypeError) as exc:
                say(f"drive: could not write {target}: {exc}")
        return report

    def _do_quit(self, _step: dict[str, Any]) -> None:
        self.window.shutdown()  # saves the window's state and stops jobs, as closing it does
        report = self._finalize("quit")
        code = 0 if report["passed"] else 1
        say(f"drive: {'PASS' if code == 0 else 'FAIL'} (exit {code})")
        if self.script:  # a hand-built driver must never exit the test runner
            self._exit_now(code)

    def _timed_out(self, seconds: float) -> None:
        self.ledger.step_failed(self.index, "timeout", f"the run did not finish within {seconds:.0f} s (stuck at step {self.index})")
        self._finalize("timeout")
        say(f"drive: FAIL timed out after {seconds:.0f} s")
        if self.script:
            self._exit_now(1)

    @staticmethod
    def _exit_now(code: int) -> None:
        # os._exit skips interpreter shutdown (and flushing), and is the only way to end a run a background thread would otherwise hold open.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        os._exit(code)
