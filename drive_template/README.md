# The live driver: a run that leaves evidence

Copied into new projects by TokenSave Manager. It exists because three projects
each built a driver, each found it was throwing information away, and each patched
it separately. Start from this and the lessons are already in.

## What is here

| File | What it is |
|---|---|
| `drive_ledger.py` | **Toolkit-neutral core.** Hooks, phases, expectations, one-shot finalize, atomic report. Byte-identical to TokenSave Manager's `src/helpers/drive_ledger.py` (a test keeps it so). Do not fork it; add channels around it. |
| `debug_drive_tk.py` | A small Tk driver on top of it. Rename the env var, grow the steps. |

There is no Qt skeleton, because none could be tested here. The Qt differences are
under *Porting to Qt* below, taken from a working one (Fortuna Lab's
`src/fortuna/ui/debug_drive.py`).

## Why in the app, and not a harness

A harness that builds its own window is a *parallel construction* of the thing
under test: different theme, fonts and DPI, so every measurement is of a window no
user has. It also must not drive the machine's mouse: the window needs focus for
every step, the screen is unusable, and one stray click landed in a browser and
started its dictation recorder. Steps run **inside** the process on the toolkit's
own timer; capture with a direct paint call, not a screen grab, so the window can
sit behind anything.

## A run is evidence, not a demonstration

A run that reaches its last line proves only that it reached its last line. The
app can log a warning, raise inside a callback, or kill a worker thread and the
script still "finishes". So the driver keeps what the app said about itself and
turns it into a verdict.

**Steps**

| Step | Meaning |
|---|---|
| `expect` | State holds within `within_ms`. Polled: UI state settles asynchronously. **Failure is permanent**; a later success never erases it. |
| `expect_clean` | The app recorded nothing during the drive **and stayed quiet for `settle_ms`**. Without the window a callback that throws 200 ms later passes a check made at 100 ms. |
| `log_report` | Print the ledger. |
| `quit` | Finalize, write the report, exit non-zero on failure. |

**The rules that were each paid for**

1. **Capture from launch.** Call `begin()` as early as a root exists. Startup
   diagnostics are kept in their own phase, so launch noise cannot fail
   `expect_clean` for ever, but a startup *exception* still fails the run.
2. **Every hook is fail-open.** Record inside a `try`, then *always* chain the
   original. A ledger fault must never change what the app does.
3. **Restore conditionally.** On uninstall, put a hook back only if it is still
   ours. If something replaced it meanwhile, leave theirs alone and record drift.
   For `report_callback_exception` restore the *state*: an inherited method is
   restored by deleting our instance attribute, not by pinning a bound copy.
4. **One lock.** Records arrive from the UI thread, workers, `logging` and
   `atexit`. Never assume one thread.
5. **A failed step is a failure of the run**, with the step, reason and exception
   recorded, and it must not strand the `quit` that ends the run.
6. **Finalize once, before you exit.** Stop, uninstall, snapshot, write the report
   atomically, flush, *then* `os._exit(code)`. `os._exit` skips `atexit`, so
   `quit` finalizes itself and `atexit` is only the fallback. Never write the
   report through a captured channel.
7. **`step()` arms no timer; `_run_next()` does.** A test that loops over
   `_run_next` leaves one armed timer per step on a window that is only closed, and
   they fire into a later test: one suite aborted with heap corruption, no
   traceback, the crash site moving between runs.
8. **Print through one encoding-safe function.** A legacy console cannot encode
   `✓`; the `UnicodeEncodeError` is raised inside a step, escapes the timer chain,
   and looks exactly like the app hanging.
9. **A tool that did not run is not a pass.** Commit SHA, for example, is
   `KNOWN` or `UNKNOWN`, never a default.

**The report** is `<script>.report.json` beside the script, gitignored: run id,
pid, script hash, commit, expectations, startup and drive diagnostics, failed
steps. Two simultaneous drives of one script are unsupported; they would share
the file, and the run id / pid say whose it is.

## Prove a check can say no

Before trusting any green: make it fail once. A script whose `expect` names a tab
that does not exist must exit 1 and list the failure in the report; the same script
with a real tab must exit 0. A check that cannot fail is not a check.

Commit the drive scripts. They are source. Gitignore only what they produce.

## Porting to Qt

- Add the Qt message handler channel (`qInstallMessageHandler`), save the previous
  one and chain to it; Qt's default writes to stderr, so keep doing that.
- Arm timers with the **window as the context object**:
  `QTimer.singleShot(ms, window, slot)`, never a lambda capturing the driver. A
  bound method keeps the driver alive while a step is queued, and the context lets
  Qt cancel the chain when the window dies instead of running it against a freed
  window.
- Keep the driver unparented and hold a reference to it: a `QTimer` whose owner is
  collected stops firing and strands the run.
- Everything else, including `drive_ledger.py`, is unchanged.
