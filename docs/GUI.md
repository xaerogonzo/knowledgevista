# The library window

`kv gui` opens a window over the same catalog the command line uses. It needs the optional `gui` extra
(`pip install "knowledgevista[gui]"`, PySide6); without it the command says so and nothing else changes. The core never imports Qt.

The window is an **adapter**, like the CLI and the MCP server (ARCHITECTURE.md): it carries no SQL and no rules of its own. What it
shows is decided in `services/library_view.py`, which is tested without a display; what a person changes goes through the services
the commands call (`metadata`, `review`, `organize`, `roots`, `scan`, `extract`, `resolve`). Anything the window can do, `kv` can do,
and the two see each other's changes.

## A person's path through it

| | |
|---|---|
| **Add folder…** | A dialog for a folder and an optional name. *Allow the organizer to rename and move files here* is a separate box and starts **unticked**: adding a folder to read is not permission to change it. Adding scans it. |
| **Scan / Extract text / Resolve** | The three long jobs, in the toolbar and the Library menu. Each runs on a worker, shows in the Jobs panel with a **Stop** button, and can be stopped at its next safe point (between files, between documents). A stopped job keeps what it finished; run it again to continue. *Resolve* is offline: it reads DOIs and titles from the extracted text and **proposes** them. Nothing is accepted and nothing leaves the computer. |
| **Documents** | Every live document, **what needs a person first** (unresolved, ambiguous, missing, new) then everything else alphabetically by title. The *Why* column says why a row is where it is; a click on a header re-sorts, *Reset order* returns to the default. The filter box narrows the list by title, author, DOI or path. The sidebar's views (Inbox, Unresolved, …), collections and tags narrow it by scope. |
| **Details** | One document: its files and whether they are reachable, and each metadata field with its **origin badge** (`O` read from the file, `R` returned by a provider, `I` deduced, `A` stated by a person), whether it is locked, and whether the sources agree (*agrees*, *DIFFERS*, *waiting*, *one source*). |
| **Why?** | Beside every field that has anything to say: how the accepted value came to be (who accepted it, from what, the evidence it rests on), every other proposal with its evidence **including rejected ones** ("why not that one?"), and the history. See *Agreement* below. |
| **Review** | Every proposal waiting for a person, most urgent first. **Accept** and **Reject** act on the selected one; **Accept all safe…** asks first and then runs the batch rule `safe_batch_v1` (it never replaces a value a person set or locked and leaves disagreements alone). Nothing is accepted until it is accepted. |
| **Search inside documents** | The toolbar box takes the same query language as `kv search` (`aqueous solubility`, `a \| b`, `year:2020 author:smith`, quoted phrases). A malformed query says so; it never shows "no results". Every result list says what the search **could not** see (scans with no text layer, PDFs not extracted yet), and a document that has no reachable copy is greyed. A page result is a navigation aid, not a quotation. |
| **Open / Show in folder** | Hands the file to the operating system's viewer (`locate`: disk beats catalog, so a file renamed outside is found if the catalog already knows it). A PDF viewer cannot be told which page, so the window says which page to go to. The built-in reader that can is a later milestone. |
| **Propose rename…** | Builds the same plan `kv plan create --document` builds and **shows** it: old name, new name, what, why, risk, and the reason an item is blocked. A folder that does not allow organizing is refused with the sentence that says how to allow it. *Save plan file* writes the frozen, hashed plan outside the library and registers it. **Nothing is renamed.** Applying a plan is `kv apply` (after `--dry-run`) and is deliberately not a button here. |
| **Add to collection… / Tag…** | The virtual organisation: nothing is moved. |
| **Health** | What the library *holds* and what *needs attention*, as two separate texts: "1,862 files" and "7 need attention" are different questions and are never blended. |

## What the window will not do

It never deletes, never moves or renames a file, never applies a plan, never writes to a source PDF, never sends a DOI or a title
anywhere (online lookup is `kv resolve --online`, after `kv config set online_lookup true`), and never accepts a proposal unless a
person pressed the button (or confirmed the batch). It adds no network code.

## Agreement

*Agreement* is shown for each field and read off the proposals, never guessed (`domain/evidence_view.py`):

| State | Meaning |
|---|---|
| `agreement` | an accepted value, and another, **independent** source (a different source name) proposed or accepted the same thing |
| `discrepancy` | an accepted value, and a source still **waiting** proposes a different one |
| `unresolved` | nothing accepted, and at least one proposal is waiting |
| `uncorroborated` | an accepted value that only its own source (or a person) supports |
| `unknown` | nothing is known for this field |

A rejected, stale or set-aside proposal is a record of a decision, not a vote: it never makes a field look pending or contested. Titles
are compared by their letters and digits (a non-breaking hyphen is not a disagreement); identifiers are compared exactly.

## How it stays honest and responsive

**Jobs (`gui/jobs.py`).** The interface thread never reads the catalog, hashes a file or waits on the disk. Everything is a `Job`
on one of two lanes. The **write lane has one thread**, because the catalog has one logical writer: writes run one at a time, in the
order they were asked. The **read lane** runs a few at once on read-only connections (which coexist with the writer under WAL), and
every read job opens **one snapshot** of the catalog, so the table, the sidebar counts and the review queue always agree. A **channel**
is "the latest request wins": of three searches typed in a row only the newest may change the screen, and an older answer that arrives
late is dropped. Every state change happens on the interface thread, so a job counts as unfinished until its result is on screen.
Widget values are read on the interface thread, before a job is queued, never inside one.

**Following the catalog.** The window polls the catalog's `catalog_revision` (a one-integer read on a worker). When it moves, the
lists reload, so `kv tag add …` in a terminal while the window is open appears without a click. Polling pauses while the window itself
is writing, so it never shows a half-finished state.

**Text from outside is text (`gui/text.py`).** A title read from a PDF, a file name, a tag a person typed or a provider's record may
contain markup, and Qt guesses: a label, a tooltip or a message box shows HTML if the text looks like HTML, and an `<img src="http://…">`
is a request to a stranger's server. So every label is created plain, tooltips built from outside text are escaped, messages are plain,
and `text.audit()` walks a live window and reports anything that is not (a test runs it over every tab, and the driven tour ends with
it). Item views draw their text as plain text; only their tooltips need care.

**What it remembers (`gui/state.py`).** Size, layout, the scope, the selected document, the filter and search text and the tab, in
`gui-state.json` in the config folder. It is a convenience: a missing, damaged or newer file gives the defaults (a damaged one is
logged), and a selection saved from another library is not applied to this one. The library's own state is the catalog, so deleting
the file loses only where the window was.

## The in-app driver

`kv gui` with `KNOWLEDGEVISTA_DRIVE=<script.json>` runs a script **inside the process, through the real widgets**, and ends in a
verdict (`gui/drive.py`; the machinery is `drive_template/`'s ledger, byte for byte). It types into the real search box, selects rows
in the real table, presses real buttons and fills in real dialogs. It never moves the mouse or sends an operating-system key, so it
needs no focus and cannot click into another program.

```bash
KNOWLEDGEVISTA_HOME=/tmp/kv-scratch KV_DRIVE_LIBRARY=/tmp/papers KV_DRIVE_SHOTS=/tmp/shots \
KNOWLEDGEVISTA_DRIVE=drive/library_tour.json kv gui
```

* A run is **evidence, not a demonstration**: a logged warning, an exception inside a slot, a dying worker or a Qt warning is kept; a
  failed `expect` is permanent (a later success never erases it); the exit status is non-zero when anything failed, and
  `<script>.report.json` records the run (script hash, commit, every expectation with expected and actual, diagnostics, shots).
* It **refuses to drive the real library**: a script adds folders, scans and changes things. Set `KNOWLEDGEVISTA_HOME` to a scratch
  folder (or pass `--catalog`), or say so on purpose with `KNOWLEDGEVISTA_DRIVE_ALLOW_REAL=1`. The refusal happens before anything is
  created.
* The shell is a recording one: `Open` is recorded and **no viewer starts**.
* `${NAME}` in any string of a script is replaced by that environment variable, so one committed script runs against any scratch library.
* A run that does not finish is stopped by a watchdog (`KNOWLEDGEVISTA_DRIVE_TIMEOUT_S`, default 300) and fails; a script that ends
  without `quit` fails too. `KNOWLEDGEVISTA_DRIVE_HIDDEN=1` never shows the window (shots are a direct paint, so they still work).

**Steps.** `add_root {path,label,allow_organize}` (through the real dialog), `scan`, `extract`, `resolve`, `wait_jobs {within_ms}`,
`search {text}`, `type {widget,text,enter}`, `filter {text}`, `press {widget | text}`, `select {title | name | row}`, `select_hit {row}`,
`select_review {paper,field}`, `scope {scope}`, `tab {name}`, `dialog {name,set,press}`, `close_dialogs`, `external {args,expect_exit}`
(a **separate `kv` process** against the same catalog), `restart` (close as a person does, open a new window from disk), `mark {name}`,
`shot {path,widget}`, `sleep {ms}`, `log {level,message}` (to prove `expect_clean` can fail), `expect`, `expect_clean {settle_ms,allow}`,
`log_report`, `quit`.

**Checks** (`expect {check,…,within_ms}`; failures are polled until `within_ms`): `rows {table}`, `titles`, `cell`, `detail_title`,
`detail_field {field,column}`, `text {widget}`, `message {kind}`, `tab`, `tab_text {index}`, `scope`, `revision {greater_than_mark}`,
`review_items`, `filter_text`, `search_text`, `shell {kind,endswith_any}`, `shell_count`, `text_safe`, `jobs_idle`, `sidebar_has`,
`dialog_open`, `catalog {sql}` (ground truth, read from the catalog with a read-only connection, never from the screen),
`state {key}`, `diagnostic_contains`. Comparators: `equals`, `contains`, `not_contains`, `at_least`, `at_most`, `includes`, `endswith_any`.

`drive/library_tour.json` is the milestone-7 acceptance run: add a root, scan, extract, resolve, search, show the evidence for a title,
accept it, propose a rename (and see that nothing moved), open the file, change the library from a terminal and watch the window follow,
restart, and find the state intact. `tests/test_gui_drive.py` runs it in a subprocess **and** runs the scripts that must fail, because a
check that cannot fail is not a check.

## Testing it

`tests/guisupport.py` sets `QT_QPA_PLATFORM=offscreen` before Qt is imported, so no window ever appears and no desktop session is
needed. The offscreen platform has **no fonts**, so a screenshot from it is boxes; to look at the window, run with
`QT_QPA_PLATFORM=windows` (or the platform's own) and take a `grab()` of a window that is never shown. In CI, `KV_REQUIRE_GUI=1`
turns "PySide6 is not installed" from a skip into a failure, so a suite that skipped the whole window because the extra was missing
cannot be green.
