"""Apply, undo and recover: the only code in Knowledge Vista that moves a user's file, and the reason it is written the way it is.

THE LIFECYCLE OF A MOVE (docs/ORGANIZER.md): planned -> prechecked -> executing -> succeeded | failed | uncertain, journaled in the catalog.

  1. PRECHECK reads the world and refuses on the first broken precondition: the root allows organizing and is online and is the root the
     plan was made for; nothing on the path is a link; the destination is inside the root and a legal name; the source is a file and its
     SHA-256 is what the plan says (else the move is abandoned: it is not the file that was reviewed); the destination is absent (or is the
     same file under another case/Unicode form); the catalog has nothing else at the destination. A locked file is SKIPPED and reported,
     never a batch failure.
  2. The INTENT IS COMMITTED (`executing`, with any temporary name) before the filesystem is touched.
  3. The move runs through `move_no_overwrite` / `safe_rename`: it cannot replace anything.
  4. The OUTCOME and the catalog's location update commit in ONE transaction, so the journal and the catalog never disagree about a move
     that finished.

A crash can only fall in three places, and each is recoverable without guessing: before step 2 (nothing journaled, nothing moved), between
2 and 3 (journal says executing, file not moved: `recover` sees the source in place and marks it not-done), between 3 and 4 (file moved,
journal says executing: `recover` sees the destination with the expected hash and finishes the bookkeeping). Anything else is `uncertain`,
and an uncertain item is REPORTED with what was observed and is never retried.

UNDO is the same machinery with the paths swapped, under the same rules, and one more: the file at the new path must still be exactly the
bytes that were moved. If it was edited or replaced since, undo refuses that item and says so; it never overwrites and never moves user
work. Chains (A->B while B->C) and swaps are ordered by dependency and, for a cycle, staged through a journaled temporary name.

Nothing is ever deleted except an empty folder this program created itself and recorded.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from knowledgevista import __version__
from knowledgevista.db.catalog import bump_revision, transaction
from knowledgevista.domain import naming, plan as planmod
from knowledgevista.domain.ids import new_id, utc_now
from knowledgevista.domain.pathkeys import fs_path, path_key
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import organizer_fs as fs

OPERATION_SCHEMA = 1
MAX_TOTAL_PATH = 1024


@dataclass
class Hooks:
    """Seams a test uses to stop the world at the three instants a crash matters. Production passes none."""

    before_move: Callable[[sqlite3.Row], None] | None = None  # journal says `executing`, the filesystem is untouched
    after_move: Callable[[sqlite3.Row], None] | None = None  # the file has moved, the catalog and journal do not know yet
    sleep: Callable[[float], None] = time.sleep


@dataclass
class ItemResult:
    item_id: str
    operation: str
    old_path: str
    new_path: str
    state: str  # succeeded | failed | skipped | uncertain | would_move | already_applied | undone
    code: str | None = None
    message: str | None = None
    risk: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Report:
    kind: str
    plan_id: str | None
    operation_id: str | None
    dry_run: bool = False
    already_done: bool = False
    items: list[ItemResult] = field(default_factory=list)
    skipped_by_plan: dict[str, int] = field(default_factory=dict)  # unchanged / blocked, from the plan itself

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.items:
            out[item.state] = out.get(item.state, 0) + 1
        return dict(sorted(out.items()))

    @property
    def problems(self) -> int:
        return sum(1 for i in self.items if i.state in ("failed", "uncertain"))


@dataclass
class _Spec:
    """One move, in either direction: what apply plans and what undo reverses."""

    item_id: str
    operation: str
    root_id: str
    artifact_id: str
    document_id: str
    src: str
    dst: str
    expected_sha256: str
    expected_size: int
    location_before: str | None
    reverses_row_id: int | None = None
    created_dirs: list[str] = field(default_factory=list)
    risk: str | None = None
    #: True for an apply (the name was chosen by the policy and must be a legal one); False for an undo, which puts a file back under whatever
    #: name it had, however odd another program made it.
    enforce_name: bool = False


# ------------------------------------------------------------------------------------------------ loading and checking a plan


def load_registered_plan(conn: sqlite3.Connection, reference: str) -> tuple[planmod.Plan, sqlite3.Row]:
    """The plan named by a file path or a plan id (or unique prefix), checked against what THIS catalog registered when it made it."""
    path = Path(reference)
    if not path.is_file():
        rows = conn.execute("SELECT * FROM plan_registry WHERE plan_id LIKE ?", (reference.lower() + "%",)).fetchall()
        if len(rows) != 1:
            raise KvError(ErrorCode.NOT_FOUND if not rows else ErrorCode.AMBIGUOUS, f"No plan file or registered plan matches {reference!r}.", {"reference": reference})
        path = Path(rows[0]["path"])
        if not path.is_file():
            raise KvError(ErrorCode.FILE_MISSING, f"The plan file {path} is gone; a plan is a file, and this catalog keeps only its hash.", {"path": str(path)})
    try:
        plan = planmod.loads(path.read_text(encoding="utf-8"))
    except planmod.PlanInvalid as exc:
        raise KvError(ErrorCode.PLAN_INVALID, str(exc), {"reasons": exc.reasons, "path": str(path)}) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise KvError(ErrorCode.PLAN_INVALID, f"The plan file could not be read: {exc}", {"path": str(path)}) from exc
    row = conn.execute("SELECT * FROM plan_registry WHERE plan_id = ?", (plan.plan_id,)).fetchone()
    if row is None:
        raise KvError(ErrorCode.PLAN_INVALID, "This catalog did not make that plan (its id is not registered here). A plan is applied only by the catalog that made it.", {"plan_id": plan.plan_id})
    if row["plan_hash"] != planmod.hash_of(plan.body()):
        raise KvError(ErrorCode.PLAN_INVALID, "The plan's contents are not what this catalog registered when it made it.", {"plan_id": plan.plan_id})
    if plan.naming_policy != naming.NAMING_POLICY_VERSION:
        raise KvError(ErrorCode.PLAN_STALE, f"The plan was made under naming policy {plan.naming_policy}; this program uses {naming.NAMING_POLICY_VERSION}. Make a new plan.",
                      {"plan_policy": plan.naming_policy, "current_policy": naming.NAMING_POLICY_VERSION})
    return plan, row


def _check_roots(conn: sqlite3.Connection, plan: planmod.Plan, needed: set[str]) -> dict[str, sqlite3.Row]:
    """The roots an apply or undo touches, each required to be the root the plan was made for, online, and allowed."""
    found: dict[str, sqlite3.Row] = {}
    for entry in plan.roots:
        if entry["root_id"] not in needed:
            continue
        row = conn.execute("SELECT * FROM root WHERE root_id = ?", (entry["root_id"],)).fetchone()
        if row is None:
            raise KvError(ErrorCode.PLAN_STALE, f"Root {entry.get('label', entry['root_id'])} no longer exists.", {"root_id": entry["root_id"]})
        if row["root_key"] != entry["root_key"] or (entry.get("volume_id") and row["volume_id"] and entry["volume_id"] != row["volume_id"]):
            raise KvError(ErrorCode.PLAN_STALE, f"Root {row['label']!r} is not the folder the plan was made for (its location or volume changed).", {"root_id": row["root_id"]})
        if not row["allow_organize"]:
            raise KvError(ErrorCode.ROOT_NOT_ORGANIZABLE, f"Root {row['label']!r} no longer allows organizing.", {"root_id": row["root_id"]})
        if not row["enabled"] or row["status"] != "online":
            raise KvError(ErrorCode.ROOT_UNAVAILABLE, f"Root {row['label']!r} is not reachable right now; nothing was moved.", {"root_id": row["root_id"]})
        found[row["root_id"]] = row
    return found


def _snapshot(conn: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    values = {r["field"]: r["value"] for r in conn.execute("SELECT field, value FROM metadata_value WHERE document_id = ? AND field IN ('title', 'authors', 'year')", (document_id,))}
    return {"title": values.get("title"), "authors": values.get("authors"), "year": values.get("year")}


def _applied(conn: sqlite3.Connection, plan_id: str) -> dict[str, sqlite3.Row]:
    """item id -> the journal row of its still-standing successful move (not undone), across every apply of this plan."""
    out: dict[str, sqlite3.Row] = {}
    for r in conn.execute(
        "SELECT i.* FROM operation_item i JOIN operation o ON o.operation_id = i.operation_id WHERE o.plan_id = ? AND o.kind = 'apply' AND i.state = 'succeeded' ORDER BY i.row_id", (plan_id,)):
        out[r["item_id"]] = r
    return out


def _assess(conn: sqlite3.Connection, plan: planmod.Plan, item: planmod.PlanItem, applied: dict[str, sqlite3.Row]) -> tuple[str, str | None, str | None]:
    """(`pending` | `already_applied` | `stale`, the reason, the location id the move starts from). Staleness is judged against what the item
    says, not against a revision number, so an unrelated tag does not invalidate a plan about file names. The location is found by WHERE THE
    FILE IS (root, path, bytes), not by the id the plan remembered: an undo gives a file a new location row, and the same plan must still apply."""
    location = conn.execute("SELECT * FROM location WHERE root_id = ? AND path_key = ? AND ended_at IS NULL", (item.root_id, path_key(item.old_path))).fetchone()
    if location is not None and location["state"] == "active" and location["relative_path"] == item.old_path and location["artifact_id"] == item.artifact_id:
        current = _snapshot(conn, item.document_id)
        wanted = {k: item.snapshot.get(k) for k in ("title", "authors", "year")}
        if current != wanted:
            changed = [k for k in current if current[k] != wanted[k]]
            return "stale", f"the accepted {', '.join(changed)} changed since the plan was made", None
        if conn.execute("SELECT retired_at FROM document WHERE document_id = ?", (item.document_id,)).fetchone()["retired_at"] is not None:
            return "stale", "the document was merged into another", None
        return "pending", None, location["location_id"]
    done = applied.get(item.item_id)
    if done is not None:
        now = conn.execute("SELECT 1 FROM location WHERE location_id = ? AND ended_at IS NULL AND state = 'active'", (done["location_id_after"],)).fetchone()
        if now is not None:
            return "already_applied", None, None
        return "stale", "it was applied, but its file has since moved again", None
    return "stale", "the file is no longer at the path the plan names, or is no longer that file", None


# ------------------------------------------------------------------------------------------------ preconditions


@dataclass
class _Verdict:
    ok: bool
    state: str = "failed"  # failed | skipped
    code: str | None = None
    message: str | None = None


def _refuse(code: str, message: str, state: str = "failed") -> _Verdict:
    return _Verdict(False, state, code, message)


def _destination_verdict(conn: sqlite3.Connection, spec: _Spec, root: sqlite3.Row, src: str | None) -> _Verdict:
    """The destination must be absent (or be the same file under another case/Unicode form) in BOTH the filesystem and the catalog."""
    base = root["configured_path"]
    dst = fs.os_path(base, spec.dst)
    if fs.lexists(dst) and not (src is not None and fs.same_object(src, dst) and fs.same_name_ignoring_case_and_form(os.path.basename(src), os.path.basename(dst))):
        return _refuse(ErrorCode.DESTINATION_EXISTS, f"Something is already at {spec.dst}. It was not touched.")
    other = conn.execute("SELECT location_id FROM location WHERE root_id = ? AND path_key = ? AND ended_at IS NULL AND location_id IS NOT ?", (spec.root_id, path_key(spec.dst), spec.location_before)).fetchone()
    if other is not None:
        return _refuse(ErrorCode.DESTINATION_EXISTS, f"The catalog records another file at {spec.dst}. Run kv scan, then plan again.")
    return _Verdict(True)


def _precheck(conn: sqlite3.Connection, spec: _Spec, root: sqlite3.Row, *, destination: bool = True) -> _Verdict:
    """Every hard precondition for one move, in the order that makes the cheapest refusals first. Reads; never writes."""
    base = root["configured_path"]
    problems = naming.component_problems(spec.dst) if spec.enforce_name else []
    if problems:
        return _refuse(ErrorCode.PATH_UNSAFE, f"The destination is not usable: {problems[0]}")
    if len(os.path.join(base, *spec.dst.split("/"))) > MAX_TOTAL_PATH:
        return _refuse(ErrorCode.PATH_UNSAFE, f"The destination path would be over {MAX_TOTAL_PATH} characters.")
    link = fs.reparse_problem(base, spec.src)
    if link:
        return _refuse(ErrorCode.PATH_UNSAFE, f"{link} is a link (a symlink or junction); the organizer will not move through one.")
    folder = spec.dst.rpartition("/")[0]
    link = fs.reparse_problem(base, folder) if folder else None  # components that do not exist yet are not links
    if link:
        return _refuse(ErrorCode.PATH_UNSAFE, f"{link} is a link; the organizer will not create or use folders through one.")
    if not fs.contained(base, spec.dst) or not fs.contained(base, spec.src):
        return _refuse(ErrorCode.PATH_UNSAFE, "A path resolves to somewhere outside the root.")
    src, dst = fs.os_path(base, spec.src), fs.os_path(base, spec.dst)
    if not os.path.isfile(src):
        return _refuse(ErrorCode.FILE_MISSING, f"There is no file at {spec.src}.")
    outcome = fs.hash_path(src)
    if outcome.kind == "changing":
        return _refuse(ErrorCode.FILE_CHANGED, "The file is being written to; it was left alone.", "skipped")
    if outcome.kind == "unreadable":
        locked = "PermissionError" in (outcome.error or "") or "WinError 32" in (outcome.error or "")
        return _refuse(ErrorCode.PERMISSION_DENIED if locked else ErrorCode.FILE_MISSING, f"The file could not be read ({outcome.error}); it is in use or protected, so it was left alone.", "skipped" if locked else "failed")
    if outcome.sha256 != spec.expected_sha256:
        return _refuse(ErrorCode.FILE_CHANGED, "The file's bytes are not the ones the plan was made for (it was edited or replaced). It was left alone.")
    if destination:
        verdict = _destination_verdict(conn, spec, root, src)
        if not verdict.ok:
            return verdict
    return _Verdict(True)


# ------------------------------------------------------------------------------------------------ the catalog side of a move


def _relocate(conn: sqlite3.Connection, spec: _Spec, root: sqlite3.Row, operation_id: str, now: str) -> str:
    """End the location a file moved FROM and record the one it moved TO, with history. Same semantics as the scanner's move pairing
    (the old location is ended `moved` with its successor), so a later scan agrees with it. Caller owns the transaction."""
    new_id_ = new_id()
    info = os.stat(fs.os_path(root["configured_path"], spec.dst))
    # Ended FIRST (a case-only move has the same path key: the unique index allows one CURRENT location per key), linked to its successor
    # LAST (the foreign key needs the successor to exist). A staged move was ended when it was staged out, with no successor yet.
    before = conn.execute("SELECT ended_at, end_reason, successor_location_id FROM location WHERE location_id = ?", (spec.location_before,)).fetchone()
    staged = before is not None and before["ended_at"] is not None and before["end_reason"] == "moved" and before["successor_location_id"] is None
    if not staged:
        changed = conn.execute("UPDATE location SET ended_at = ?, end_reason = 'moved' WHERE location_id = ? AND ended_at IS NULL", (now, spec.location_before)).rowcount
        if changed != 1:
            raise sqlite3.IntegrityError("the catalog's location for this file is no longer current")
    conn.execute(
        "INSERT INTO location (location_id, root_id, relative_path, path_key, artifact_id, size, mtime_ns, state, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
        (new_id_, spec.root_id, spec.dst, path_key(spec.dst), spec.artifact_id, info.st_size, info.st_mtime_ns, now, now))
    conn.execute("UPDATE location SET successor_location_id = ? WHERE location_id = ?", (new_id_, spec.location_before))
    conn.execute("INSERT INTO location_event (location_id, run_id, event, at, actor, detail) VALUES (?, NULL, 'organizer_moved_to', ?, 'organizer', ?)",
                 (spec.location_before, now, json.dumps({"to": spec.dst, "operation": operation_id}, sort_keys=True)))
    conn.execute("INSERT INTO location_event (location_id, run_id, event, at, actor, detail) VALUES (?, NULL, 'organizer_moved_from', ?, 'organizer', ?)",
                 (new_id_, now, json.dumps({"from": spec.src, "operation": operation_id}, sort_keys=True)))
    bump_revision(conn)
    return new_id_


def _end_for_staging(conn: sqlite3.Connection, location_id: str, now: str) -> None:
    """The file left its path for a temporary name: its location is ended NOW (the path is free for the next move in the ring) and gets its
    successor when the file lands. Without this the catalog would claim a file at a path that another file is about to take."""
    with transaction(conn):
        conn.execute("UPDATE location SET ended_at = ?, end_reason = 'moved' WHERE location_id = ? AND ended_at IS NULL", (now, location_id))


def _unend(conn: sqlite3.Connection, location_id: str | None, operation_id: str, now: str) -> None:
    """A staged file was put back where it started: its location is current again, with a note saying why."""
    if location_id is None:
        return
    with transaction(conn):
        changed = conn.execute("UPDATE location SET ended_at = NULL, end_reason = NULL WHERE location_id = ? AND end_reason = 'moved' AND successor_location_id IS NULL", (location_id,)).rowcount
        if changed:
            conn.execute("INSERT INTO location_event (location_id, run_id, event, at, actor, detail) VALUES (?, NULL, 'organizer_restored', ?, 'organizer', ?)",
                         (location_id, now, json.dumps({"operation": operation_id}, sort_keys=True)))


# ------------------------------------------------------------------------------------------------ the journal


def _journal_row(conn: sqlite3.Connection, row_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM operation_item WHERE row_id = ?", (row_id,)).fetchone()


def _set_state(conn: sqlite3.Connection, row_id: int, state: str, *, code: str | None = None, message: str | None = None, temp: str | None = None,
               created: list[str] | None = None, after: str | None = None, finished: bool = False) -> None:
    now = utc_now()
    with transaction(conn):
        conn.execute(
            "UPDATE operation_item SET state = ?, error_code = COALESCE(?, error_code), message = COALESCE(?, message), temp_path = COALESCE(?, temp_path), "
            "created_dirs_json = COALESCE(?, created_dirs_json), location_id_after = COALESCE(?, location_id_after), "
            "started_at = COALESCE(started_at, ?), finished_at = CASE WHEN ? THEN ? ELSE finished_at END WHERE row_id = ?",
            (state, code, message, temp, json.dumps(created) if created is not None else None, after, now, 1 if finished else 0, now, row_id))


def _open_operation(conn: sqlite3.Connection, kind: str, plan_id: str | None, specs: list[_Spec], actor: str, undoes: str | None = None) -> tuple[str, list[int]]:
    operation_id, now = new_id(), utc_now()
    ids: list[int] = []
    with transaction(conn):
        conn.execute("INSERT INTO operation (operation_id, kind, plan_id, undoes_operation_id, started_at, status, actor, app_version, operation_schema) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)",
                     (operation_id, kind, plan_id, undoes, now, actor, __version__, OPERATION_SCHEMA))
        for seq, spec in enumerate(specs, start=1):
            cursor = conn.execute(
                "INSERT INTO operation_item (operation_id, seq, item_id, reverses_row_id, operation_kind, root_id, artifact_id, document_id, location_id_before, old_path, new_path, expected_sha256, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned')",
                (operation_id, seq, spec.item_id, spec.reverses_row_id, spec.operation, spec.root_id, spec.artifact_id, spec.document_id, spec.location_before, spec.src, spec.dst, spec.expected_sha256))
            ids.append(cursor.lastrowid)
    return operation_id, ids


def _close_operation(conn: sqlite3.Connection, operation_id: str, report: Report) -> None:
    status = "completed_with_problems" if report.problems else "completed"
    with transaction(conn):
        conn.execute("UPDATE operation SET status = ?, finished_at = ?, summary_json = ? WHERE operation_id = ?",
                     (status, utc_now(), json.dumps({"counts": report.counts(), "problems": report.problems}, sort_keys=True), operation_id))
        bump_revision(conn)


# ------------------------------------------------------------------------------------------------ ordering


def _order(specs: list[_Spec]) -> list[tuple[str, _Spec]]:
    """Steps that move a batch without ever needing a destination that is still occupied by a file that will move.

    A chain (A to B while B goes to C) runs the vacating move first. A cycle (a swap, or a longer ring) has no such first move, so one
    member, the smallest item id, is STAGED: moved to a journaled temporary name, the rest of the ring runs, then it lands. Returns
    ("full" | "stage_out" | "stage_in", spec) steps."""
    remaining = {s.item_id: s for s in specs}
    steps: list[tuple[str, _Spec]] = []
    staged: list[_Spec] = []
    while remaining:
        sources = {s.item_id: path_key(s.src) for s in remaining.values()}
        ready = [i for i in sorted(remaining) if not any(j != i and sources[j] == path_key(remaining[i].dst) and remaining[j].root_id == remaining[i].root_id for j in remaining)]
        if ready:
            steps += [("full", remaining.pop(i)) for i in ready]
            continue
        first = remaining.pop(sorted(remaining)[0])
        steps.append(("stage_out", first))
        staged.append(first)
    steps += [("stage_in", s) for s in staged]
    return steps


# ------------------------------------------------------------------------------------------------ doing one move


def _finish_failed(conn: sqlite3.Connection, row_id: int, spec: _Spec, verdict: _Verdict, results: list[ItemResult]) -> None:
    _set_state(conn, row_id, verdict.state, code=verdict.code, message=verdict.message, finished=True)
    results.append(ItemResult(spec.item_id, spec.operation, spec.src, spec.dst, verdict.state, verdict.code, verdict.message, spec.risk))


def _spelled(path: str) -> bool:
    """Whether a file exists under EXACTLY this spelling. On a case-insensitive filesystem `isfile` answers yes to either spelling of a
    case-only rename, which says nothing about which one it now has; the directory listing does."""
    directory, name = os.path.split(path)
    try:
        return name in os.listdir(directory or ".") and os.path.isfile(path)
    except OSError:
        return False


def _reconcile_after_error(spec: _Spec, root: sqlite3.Row) -> str:
    """After an unexpected failure mid-move: where is the file? `moved`, `not_moved`, or `unknown`. Looks at the disk, believes nothing else."""
    base = root["configured_path"]
    src, dst = fs.os_path(base, spec.src), fs.os_path(base, spec.dst)
    src_there, dst_there = _spelled(src), _spelled(dst)
    if dst_there and not src_there and fs.hash_path(dst).sha256 == spec.expected_sha256:
        return "moved"
    if src_there and not dst_there:
        return "not_moved"
    return "unknown"


def _execute(conn: sqlite3.Connection, spec: _Spec, row_id: int, root: sqlite3.Row, step: str, operation_id: str, hooks: Hooks, results: list[ItemResult],
             verify_hashes: bool) -> bool:
    """Run one step of one move. Returns True when the item finished (succeeded) at this step."""
    base = root["configured_path"]
    src, dst = fs.os_path(base, spec.src), fs.os_path(base, spec.dst)
    directory = os.path.dirname(src)
    temp = None
    if step == "stage_in":
        temp = _journal_row(conn, row_id)["temp_path"]
        src = temp
    elif step == "stage_out" or (os.path.dirname(src) == os.path.dirname(dst) and os.path.basename(src) != os.path.basename(dst)
                                 and fs.same_name_ignoring_case_and_form(os.path.basename(src), os.path.basename(dst))):
        temp = fs.temp_name(directory, str(row_id))
    _set_state(conn, row_id, "executing", temp=temp)  # THE INTENT, committed before the filesystem is touched
    row = _journal_row(conn, row_id)
    if hooks.before_move is not None:
        hooks.before_move(row)
    created: list[str] = []
    try:
        if step != "stage_out":
            parent = os.path.dirname(dst)
            if not os.path.isdir(parent):
                created = fs.make_directories(parent, fs_path(base))
                _set_state(conn, row_id, "executing", created=[os.path.relpath(d, fs_path(base)).replace(os.sep, "/") for d in created])
        if step == "stage_out":
            fs.move_no_overwrite(src, temp, sleep=hooks.sleep)
            if hooks.after_move is not None:
                hooks.after_move(row)
            _end_for_staging(conn, spec.location_before, utc_now())
            return False  # staged, not finished: `stage_in` completes it
        if step == "stage_in":
            fs.move_no_overwrite(temp, dst, sleep=hooks.sleep)
        else:
            fs.safe_rename(src, dst, token=str(row_id), sleep=hooks.sleep)
        if hooks.after_move is not None:
            hooks.after_move(row)
    except fs.Refused as exc:
        where = _reconcile_after_error(spec, root)
        if where == "moved":
            pass  # it happened despite the error report; fall through to bookkeeping
        elif exc.kind == "locked":
            _finish_failed(conn, row_id, spec, _Verdict(False, "skipped", ErrorCode.PERMISSION_DENIED, f"The file is in use ({exc}); it was left alone."), results)
            return False
        elif where == "not_moved":
            code = {"exists": ErrorCode.DESTINATION_EXISTS, "missing": ErrorCode.FILE_MISSING, "permission": ErrorCode.PERMISSION_DENIED}.get(exc.kind, ErrorCode.INTERNAL)
            _finish_failed(conn, row_id, spec, _Verdict(False, "failed", code, f"The move was refused ({exc}); nothing changed."), results)
            return False
        else:
            _finish_failed(conn, row_id, spec, _Verdict(False, "uncertain", ErrorCode.UNCERTAIN, f"The move failed ({exc}) and the files are not where either side expects. Run kv recover."), results)
            return False
    # VERIFY: cheap by default (it exists, the old name is gone, it is the same size); --verify-hashes reads it again.
    landed = fs.os_path(base, spec.dst)
    if not os.path.isfile(landed) or os.path.getsize(landed) != spec.expected_size or (verify_hashes and fs.hash_path(landed).sha256 != spec.expected_sha256):
        _finish_failed(conn, row_id, spec, _Verdict(False, "uncertain", ErrorCode.UNCERTAIN, "The file is at the destination but did not verify (size or bytes differ). It was not moved again; run kv recover."), results)
        return False
    now = utc_now()
    try:
        with transaction(conn):
            after = _relocate(conn, spec, root, operation_id, now)
            conn.execute("UPDATE operation_item SET state = 'succeeded', location_id_after = ?, finished_at = ?, started_at = COALESCE(started_at, ?) WHERE row_id = ?", (after, now, now, row_id))
    except sqlite3.IntegrityError as exc:
        _finish_failed(conn, row_id, spec, _Verdict(False, "uncertain", ErrorCode.UNCERTAIN, f"The file moved but the catalog could not record it ({exc}). Run kv recover, or kv scan."), results)
        return False
    results.append(ItemResult(spec.item_id, spec.operation, spec.src, spec.dst, "succeeded", risk=spec.risk))
    return True


def _back_to_start(conn: sqlite3.Connection, spec: _Spec, row_id: int, root: sqlite3.Row, verdict: _Verdict, results: list[ItemResult]) -> None:
    """A staged file whose destination turned out to be taken: put it back where it started (never over anything) and say why."""
    temp = _journal_row(conn, row_id)["temp_path"]
    try:
        fs.move_no_overwrite(temp, fs.os_path(root["configured_path"], spec.src))
        _unend(conn, spec.location_before, "", utc_now())
        _finish_failed(conn, row_id, spec, _Verdict(False, "failed", verdict.code, f"{verdict.message} The file was put back where it started."), results)
    except fs.Refused as exc:
        _finish_failed(conn, row_id, spec, _Verdict(False, "uncertain", ErrorCode.UNCERTAIN, f"{verdict.message} The file is at {temp} and could not be put back ({exc})."), results)


def _run_specs(conn: sqlite3.Connection, specs: list[_Spec], rows: dict[str, int], roots: dict[str, sqlite3.Row], operation_id: str, hooks: Hooks,
               verify_hashes: bool, on_success: Callable[[_Spec, int], None] | None = None) -> list[ItemResult]:
    results: list[ItemResult] = []
    for step, spec in _order(specs):
        row_id, root = rows[spec.item_id], roots[spec.root_id]
        if step == "stage_in":
            if _journal_row(conn, row_id)["state"] != "executing":
                continue  # its first half did not happen
            verdict = _destination_verdict(conn, spec, root, None)
            if not verdict.ok:
                _back_to_start(conn, spec, row_id, root, verdict, results)
                continue
        else:
            verdict = _precheck(conn, spec, root, destination=step != "stage_out")
            if not verdict.ok:
                _finish_failed(conn, row_id, spec, verdict, results)
                continue
        if _execute(conn, spec, row_id, root, step, operation_id, hooks, results, verify_hashes) and on_success is not None:
            on_success(spec, row_id)
    return results


# ------------------------------------------------------------------------------------------------ apply


def _unfinished(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM operation WHERE status = 'running' ORDER BY started_at LIMIT 1").fetchone()


def apply_plan(conn: sqlite3.Connection, reference: str, *, dry_run: bool = False, verify_hashes: bool = False, actor: str = "user", hooks: Hooks | None = None) -> Report:
    hooks = hooks or Hooks()
    plan, _ = load_registered_plan(conn, reference)
    actionable = [i for i in plan.items if i.status == "planned"]
    report = Report("apply", plan.plan_id, None, dry_run, skipped_by_plan={k: v for k, v in plan.summary()["status"].items() if k != "planned"})
    roots = _check_roots(conn, plan, {i.root_id for i in actionable})
    applied = _applied(conn, plan.plan_id)
    stale: list[dict[str, str]] = []
    pending: list[planmod.PlanItem] = []
    starts: dict[str, str | None] = {}
    for item in actionable:
        status, reason, start = _assess(conn, plan, item, applied)
        starts[item.item_id] = start
        if status == "stale":
            stale.append({"item_id": item.item_id, "old_path": item.old_path, "reason": reason or ""})
        elif status == "already_applied":
            report.items.append(ItemResult(item.item_id, item.operation, item.old_path, item.new_path, "already_applied", risk=item.risk))
        else:
            pending.append(item)
    if stale:
        raise KvError(ErrorCode.PLAN_STALE, f"The plan no longer matches the library: {len(stale)} item(s) are stale (first: {stale[0]['old_path']}: {stale[0]['reason']}). Nothing was moved. Make a new plan.", {"items": stale[:50], "stale": len(stale)})
    if not pending:
        report.already_done = bool(actionable)
        return report
    specs = [_Spec(i.item_id, i.operation, i.root_id, i.artifact_id, i.document_id, i.old_path, i.new_path, i.expected_sha256, i.expected_size, starts[i.item_id], risk=i.risk, enforce_name=True) for i in pending]
    if dry_run:
        for spec in specs:
            verdict = _precheck(conn, spec, roots[spec.root_id])
            report.items.append(ItemResult(spec.item_id, spec.operation, spec.src, spec.dst, "would_move" if verdict.ok else verdict.state, verdict.code, verdict.message, spec.risk))
        return report
    stuck = _unfinished(conn)
    if stuck is not None:
        raise KvError(ErrorCode.OPERATION_UNFINISHED, f"An earlier {stuck['kind']} ({stuck['operation_id'][:8]}, started {stuck['started_at']}) never finished. Run kv recover before moving anything else.",
                      {"operation_id": stuck["operation_id"]})
    operation_id, ids = _open_operation(conn, "apply", plan.plan_id, specs, actor)
    report.operation_id = operation_id
    rows = {spec.item_id: row_id for spec, row_id in zip(specs, ids)}
    report.items += _run_specs(conn, specs, rows, roots, operation_id, hooks, verify_hashes)
    _close_operation(conn, operation_id, report)
    return report


# ------------------------------------------------------------------------------------------------ undo


def _last_undoable(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT o.* FROM operation o WHERE o.kind = 'apply' AND EXISTS (SELECT 1 FROM operation_item i WHERE i.operation_id = o.operation_id AND i.state = 'succeeded') "
        "ORDER BY o.started_at DESC LIMIT 1").fetchone()


def undo_operation(conn: sqlite3.Connection, operation_ref: str | None = None, *, dry_run: bool = False, verify_hashes: bool = False, actor: str = "user", hooks: Hooks | None = None) -> Report:
    hooks = hooks or Hooks()
    if operation_ref:
        rows = conn.execute("SELECT * FROM operation WHERE operation_id LIKE ?", (operation_ref.lower() + "%",)).fetchall()
        if len(rows) != 1:
            raise KvError(ErrorCode.NOT_FOUND if not rows else ErrorCode.AMBIGUOUS, f"No single operation matches {operation_ref!r}.", {"reference": operation_ref})
        operation = rows[0]
    else:
        operation = _last_undoable(conn)
        if operation is None:
            raise KvError(ErrorCode.NOT_FOUND, "There is nothing to undo: no apply has a move that is still in place.")
    if operation["kind"] != "apply":
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"That is a {operation['kind']}, not an apply. To redo a move, apply its plan again.", {"operation_id": operation["operation_id"]})
    items = conn.execute("SELECT * FROM operation_item WHERE operation_id = ? AND state = 'succeeded' ORDER BY seq", (operation["operation_id"],)).fetchall()
    report = Report("undo", operation["plan_id"], None, dry_run)
    if not items:
        report.already_done = True
        return report
    roots: dict[str, sqlite3.Row] = {}
    for root_id in {i["root_id"] for i in items}:
        row = conn.execute("SELECT * FROM root WHERE root_id = ?", (root_id,)).fetchone()
        if not row["allow_organize"]:
            raise KvError(ErrorCode.ROOT_NOT_ORGANIZABLE, f"Root {row['label']!r} no longer allows organizing.", {"root_id": root_id})
        if not row["enabled"] or row["status"] != "online":
            raise KvError(ErrorCode.ROOT_UNAVAILABLE, f"Root {row['label']!r} is not reachable right now; nothing was moved.", {"root_id": root_id})
        roots[root_id] = row
    specs = [_Spec(i["item_id"], i["operation_kind"], i["root_id"], i["artifact_id"], i["document_id"], i["new_path"], i["old_path"], i["expected_sha256"], 0, i["location_id_after"],
                  reverses_row_id=i["row_id"], created_dirs=json.loads(i["created_dirs_json"] or "[]")) for i in items]
    for spec in specs:  # the size the file must still have: what the artifact says
        spec.expected_size = conn.execute("SELECT size FROM artifact WHERE artifact_id = ?", (spec.artifact_id,)).fetchone()[0]
    if dry_run:
        for spec in specs:
            verdict = _precheck(conn, spec, roots[spec.root_id])
            report.items.append(ItemResult(spec.item_id, spec.operation, spec.src, spec.dst, "would_move" if verdict.ok else verdict.state, verdict.code, verdict.message))
        return report
    stuck = _unfinished(conn)
    if stuck is not None:
        raise KvError(ErrorCode.OPERATION_UNFINISHED, f"An earlier {stuck['kind']} ({stuck['operation_id'][:8]}) never finished. Run kv recover before undoing anything.", {"operation_id": stuck["operation_id"]})
    operation_id, ids = _open_operation(conn, "undo", operation["plan_id"], specs, actor, undoes=operation["operation_id"])
    report.operation_id = operation_id
    rows = {spec.item_id: row_id for spec, row_id in zip(specs, ids)}
    by_item = {spec.item_id: spec for spec in specs}

    def mark_undone(spec: _Spec, row_id: int) -> None:
        with transaction(conn):
            conn.execute("UPDATE operation_item SET state = 'undone', undone_by_row_id = ? WHERE row_id = ?", (row_id, spec.reverses_row_id))
        for relative in sorted(spec.created_dirs, key=len, reverse=True):  # only folders THIS program created and recorded, and only if empty
            fs.remove_if_empty(fs.os_path(roots[spec.root_id]["configured_path"], relative))

    report.items += [ItemResult(r.item_id, r.operation, r.old_path, r.new_path, "undone" if r.state == "succeeded" else r.state, r.code, r.message)
                     for r in _run_specs(conn, specs, rows, roots, operation_id, hooks, verify_hashes, mark_undone)]
    del by_item
    _close_operation(conn, operation_id, report)
    return report


# ------------------------------------------------------------------------------------------------ recover


def recover(conn: sqlite3.Connection, *, actor: str = "user", dry_run: bool = False, hooks: Hooks | None = None) -> Report:
    """Reconcile every interrupted operation with the disk. Finishes bookkeeping for a move that happened, marks one that did not, puts a file
    left at a journaled temporary name back where it started, and leaves anything else `uncertain` with what was seen. Never moves a file
    forward and never retries."""
    report = Report("recover", None, None, dry_run)
    unfinished = conn.execute(
        "SELECT i.*, o.kind AS op_kind FROM operation_item i JOIN operation o ON o.operation_id = i.operation_id "
        "WHERE i.state IN ('planned', 'prechecked', 'executing', 'uncertain') ORDER BY i.operation_id, i.row_id").fetchall()
    running = conn.execute("SELECT operation_id FROM operation WHERE status = 'running'").fetchall()
    if not unfinished and not running:
        report.already_done = True
        return report
    for r in unfinished:
        root = conn.execute("SELECT * FROM root WHERE root_id = ?", (r["root_id"],)).fetchone()
        base = root["configured_path"]
        src, dst = fs.os_path(base, r["old_path"]), fs.os_path(base, r["new_path"])
        temp = r["temp_path"]
        spec = _Spec(r["item_id"], r["operation_kind"], r["root_id"], r["artifact_id"], r["document_id"], r["old_path"], r["new_path"], r["expected_sha256"], 0, r["location_id_before"])
        spec.expected_size = conn.execute("SELECT size FROM artifact WHERE artifact_id = ?", (spec.artifact_id,)).fetchone()[0]
        src_there, dst_there, temp_there = _spelled(src), _spelled(dst), bool(temp) and os.path.isfile(temp)
        seen = (f"seen: source {'present' if src_there else 'absent'}, destination {'present' if dst_there else 'absent'}"
                + (f", temporary {temp if temp_there else 'absent'}" if temp else ""))
        dst_is_ours = dst_there and fs.hash_path(dst).sha256 == spec.expected_sha256  # the destination holds THIS file, not somebody else's
        src_is_ours = src_there and fs.hash_path(src).sha256 == spec.expected_sha256
        if r["state"] == "planned":
            outcome = ("failed", ErrorCode.INTERRUPTED, "The operation was interrupted before this move started; nothing changed.")
        elif dst_is_ours and not src_there and not temp_there:
            outcome = ("succeeded", None, "The file had moved; the journal and catalog were brought up to date.")
        elif src_is_ours and not temp_there and not dst_is_ours:
            outcome = ("failed", ErrorCode.INTERRUPTED, "The move never happened; the file is where it started.")
        elif temp_there and not src_there and not dst_is_ours:
            outcome = ("restore", None, "The file was left at its temporary name; it is being put back where it started.")
        else:
            outcome = ("uncertain", ErrorCode.UNCERTAIN, f"The files are not where the journal expects ({seen}). Nothing was changed; look at them yourself.")
        state, code, message = outcome
        if dry_run:
            report.items.append(ItemResult(r["item_id"], r["operation_kind"], r["old_path"], r["new_path"], "would_move" if state in ("succeeded", "restore") else state, code, message))
            continue
        if state == "succeeded":
            try:
                with transaction(conn):
                    after = _relocate(conn, spec, root, r["operation_id"], utc_now())
                    conn.execute("UPDATE operation_item SET state = 'succeeded', location_id_after = ?, finished_at = ?, message = ? WHERE row_id = ?", (after, utc_now(), message, r["row_id"]))
            except sqlite3.IntegrityError as exc:
                _set_state(conn, r["row_id"], "uncertain", code=ErrorCode.UNCERTAIN, message=f"The file is at its destination but the catalog could not record it ({exc}). Run kv scan.", finished=True)
                state, code = "uncertain", ErrorCode.UNCERTAIN
        elif state == "restore":
            try:
                fs.move_no_overwrite(temp, src)
                _unend(conn, r["location_id_before"], r["operation_id"], utc_now())
                _set_state(conn, r["row_id"], "failed", code=ErrorCode.INTERRUPTED, message="Interrupted mid-move; the file was put back where it started.", finished=True)
                state, code, message = "failed", ErrorCode.INTERRUPTED, "Interrupted mid-move; the file was put back where it started."
            except fs.Refused as exc:
                _set_state(conn, r["row_id"], "uncertain", code=ErrorCode.UNCERTAIN, message=f"The file is at {temp} and could not be put back ({exc}).", finished=True)
                state, code, message = "uncertain", ErrorCode.UNCERTAIN, f"The file is at {temp} and could not be put back ({exc})."
        else:
            _set_state(conn, r["row_id"], state, code=code, message=message, finished=True)
        report.items.append(ItemResult(r["item_id"], r["operation_kind"], r["old_path"], r["new_path"], state, code, message))
    if not dry_run:
        with transaction(conn):
            for op in conn.execute("SELECT operation_id FROM operation WHERE status = 'running'").fetchall():
                still = conn.execute("SELECT COUNT(*) FROM operation_item WHERE operation_id = ? AND state IN ('failed', 'uncertain')", (op["operation_id"],)).fetchone()[0]
                conn.execute("UPDATE operation SET status = ?, finished_at = ? WHERE operation_id = ?", ("completed_with_problems" if still else "completed", utc_now(), op["operation_id"]))
            conn.execute("INSERT INTO operation (operation_id, kind, started_at, finished_at, status, actor, app_version, operation_schema, summary_json) VALUES (?, 'recover', ?, ?, ?, ?, ?, ?, ?)",
                         (new_id(), utc_now(), utc_now(), "completed_with_problems" if report.problems else "completed", actor, __version__, OPERATION_SCHEMA, json.dumps({"counts": report.counts()}, sort_keys=True)))
            bump_revision(conn)
    return report


# ------------------------------------------------------------------------------------------------ history


def history(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    out = []
    for op in conn.execute("SELECT * FROM operation ORDER BY started_at DESC, operation_id LIMIT ?", (limit,)):
        counts = {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) AS n FROM operation_item WHERE operation_id = ? GROUP BY state ORDER BY state", (op["operation_id"],))}
        out.append({"operation_id": op["operation_id"], "kind": op["kind"], "plan_id": op["plan_id"], "undoes": op["undoes_operation_id"], "started_at": op["started_at"],
                    "finished_at": op["finished_at"], "status": op["status"], "actor": op["actor"], "app_version": op["app_version"], "operation_schema": op["operation_schema"], "items": counts})
    return out


def operation_items(conn: sqlite3.Connection, operation_ref: str) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    rows = conn.execute("SELECT * FROM operation WHERE operation_id LIKE ?", (operation_ref.lower() + "%",)).fetchall()
    if len(rows) != 1:
        raise KvError(ErrorCode.NOT_FOUND if not rows else ErrorCode.AMBIGUOUS, f"No single operation matches {operation_ref!r}.", {"reference": operation_ref})
    return rows[0], conn.execute("SELECT * FROM operation_item WHERE operation_id = ? ORDER BY seq", (rows[0]["operation_id"],)).fetchall()
