"""Planning: PROPOSE the renames and moves the naming policy would make, as a frozen plan. Reads the catalog and, to see what is already
there, the filesystem. WRITES NEITHER: a plan is a proposal a person reviews, and nothing happens to a file until `kv apply` is given it.

What is planned, and what is not:

  * Only roots that say `allow_organize` (a flag a person sets per root; the default is no), that are enabled and online.
  * Only documents with an ACCEPTED title. A proposal never names a file, and a document whose title is unknown is left as it is.
  * Only active locations of live documents. A retired (merged-away) document has no files of its own.
  * Every location is considered, so a file that is already called what it should be appears as `unchanged`: the plan is the whole
    answer, not just the changes.
  * A destination that is already occupied is `blocked` with the reason, never overwritten and never silently renamed around: the person
    decides. A destination that is occupied by a file that THIS plan moves away is fine (the executor orders chains).

The names come from domain/naming.py and collisions are resolved there (by artifact id, never by directory order), so the same catalog and
the same files give the same plan on any machine.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from knowledgevista import __version__, paths
from knowledgevista.db.catalog import revision
from knowledgevista.domain import naming, plan as planmod
from knowledgevista.domain.ids import new_id, utc_now
from knowledgevista.domain.pathkeys import is_within, normalise_root, path_key
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import organizer_fs as fs
from knowledgevista.services.roots import Root, find_roots, list_roots

_RISK = {("rename", "high"): "low", ("rename", "medium"): "medium", ("move", "high"): "medium", ("move", "medium"): "high",
         ("rename+move", "high"): "medium", ("rename+move", "medium"): "high"}


@dataclass
class _Candidate:
    root: Root
    location_id: str
    artifact_id: str
    document_id: str
    size: int
    old_path: str
    accepted: dict[str, object]
    decision: naming.NameDecision | None
    wanted: str | None


def organizable_roots(conn: sqlite3.Connection, selectors: list[str] | None) -> list[Root]:
    """The roots a plan may cover. An explicitly named root that does not allow organizing is an error that says how to allow it; with no
    selector, every enabled, online root that allows it (and an error if there is none)."""
    if selectors:
        chosen = [r for selector in selectors for r in find_roots(conn, selector)]
        for root in chosen:
            if not root.allow_organize:
                raise KvError(ErrorCode.ROOT_NOT_ORGANIZABLE, f"Root {root.label!r} does not allow organizing. A person says so, per root: kv root allow-organize {root.label}",
                              {"root_id": root.root_id, "label": root.label})
            if not root.enabled or root.status != "online":
                raise KvError(ErrorCode.ROOT_UNAVAILABLE, f"Root {root.label!r} is {'disabled' if not root.enabled else root.status}; nothing was planned for it.", {"root_id": root.root_id})
        return sorted({r.root_id: r for r in chosen}.values(), key=lambda r: r.root_id)
    chosen = [r for r in list_roots(conn) if r.enabled and r.allow_organize and r.status == "online"]
    if not chosen:
        raise KvError(ErrorCode.ROOT_NOT_ORGANIZABLE, "No root allows organizing (or none is online). A person says so, per root: kv root allow-organize <root>")
    return chosen


def _accepted(conn: sqlite3.Connection, document_id: str) -> dict[str, object]:
    out: dict[str, object] = {}
    for r in conn.execute("SELECT field, value, origin, source, accepted_by, locked FROM metadata_value WHERE document_id = ? AND field IN ('title', 'authors', 'year')", (document_id,)):
        out[r["field"]] = r["value"]
        if r["field"] == "title":
            out["title_source"], out["title_accepted_by"], out["title_origin"] = r["source"], r["accepted_by"], r["origin"]
    return out


def _candidates(conn: sqlite3.Connection, root: Root, layout: str, documents: set[str] | None) -> list[_Candidate]:
    rows = conn.execute(
        "SELECT l.location_id, l.relative_path, l.artifact_id, a.size, da.document_id FROM location l "
        "JOIN artifact a ON a.artifact_id = l.artifact_id JOIN document_artifact da ON da.artifact_id = l.artifact_id "
        "JOIN document d ON d.document_id = da.document_id AND d.retired_at IS NULL "
        "WHERE l.root_id = ? AND l.ended_at IS NULL AND l.state = 'active' AND l.artifact_id IS NOT NULL ORDER BY l.path_key, l.location_id", (root.root_id,)).fetchall()
    found, cache = [], {}
    for r in rows:
        if documents is not None and r["document_id"] not in documents:
            continue
        accepted = cache.setdefault(r["document_id"], _accepted(conn, r["document_id"]))
        directory, _, name = r["relative_path"].rpartition("/")
        extension = os.path.splitext(name)[1]
        decision = naming.name_for(accepted.get("title"), accepted.get("authors"), accepted.get("year"), extension, layout)  # type: ignore[arg-type]
        wanted = None
        if decision is not None:
            folder = decision.subdirectory if decision.subdirectory is not None else directory
            wanted = (folder + "/" if folder else "") + decision.filename
        found.append(_Candidate(root, r["location_id"], r["artifact_id"], r["document_id"], r["size"], r["relative_path"], accepted, decision, wanted))
    return found


def _operation(old: str, new: str) -> str:
    if old == new:
        return "unchanged"
    same_dir = old.rpartition("/")[0] == new.rpartition("/")[0]
    same_name = old.rpartition("/")[2] == new.rpartition("/")[2]
    return "rename" if same_dir else ("move" if same_name else "rename+move")


def build_plan(conn: sqlite3.Connection, *, roots: list[Root], layout: str = "in_place", documents: set[str] | None = None, check_disk: bool = True) -> planmod.Plan:
    """The plan for `roots`. Pure over the catalog and the filesystem; changes neither."""
    if layout not in naming.LAYOUTS:
        raise KvError(ErrorCode.INVALID_ARGUMENTS, f"Unknown layout {layout!r}; layouts: {', '.join(naming.LAYOUTS)}.")
    candidates = [c for root in roots for c in _candidates(conn, root, layout, documents)]
    named = [c for c in candidates if c.wanted is not None]
    finals = naming.disambiguate([(f"{c.root.root_id}:{c.location_id}", c.artifact_id, f"{c.root.root_id}/{c.wanted}") for c in named])
    destination = {c.location_id: finals[f"{c.root.root_id}:{c.location_id}"].split("/", 1)[1] for c in named}
    vacated = {(c.root.root_id, path_key(c.old_path)) for c in named if destination[c.location_id] != c.old_path}
    occupied = {(r["root_id"], r["path_key"]) for r in conn.execute("SELECT root_id, path_key FROM location WHERE ended_at IS NULL")}
    items: list[planmod.PlanItem] = []
    for index, c in enumerate(candidates, start=1):
        item_id = f"i{index:05d}"
        accepted = c.accepted
        snapshot = c.decision.snapshot if c.decision else {"title": accepted.get("title"), "authors": accepted.get("authors"), "year": accepted.get("year"), "layout": layout}
        confidence = "high" if accepted.get("title_accepted_by") == "user" else "medium"
        source = str(accepted.get("title_source") or "none")
        new_path = destination.get(c.location_id, c.old_path)
        operation = _operation(c.old_path, new_path)
        if c.wanted is None:
            items.append(planmod.PlanItem(item_id, "unchanged", "unchanged", c.root.root_id, c.location_id, c.artifact_id, c.document_id, c.old_path, c.old_path,
                                          c.artifact_id, c.size, "no accepted title: left as it is", snapshot, source, confidence, "low"))
            continue
        if operation == "unchanged":
            items.append(planmod.PlanItem(item_id, "unchanged", "unchanged", c.root.root_id, c.location_id, c.artifact_id, c.document_id, c.old_path, c.old_path,
                                          c.artifact_id, c.size, "already named as the policy would name it", snapshot, source, confidence, "low"))
            continue
        key = (c.root.root_id, path_key(new_path))
        blocked = None
        problems = naming.component_problems(new_path)
        if problems:
            blocked = f"the proposed name is not usable: {problems[0]}"
        elif key in occupied and key not in vacated and path_key(new_path) != path_key(c.old_path):
            blocked = "a file the catalog knows is already at that path"
        elif check_disk and path_key(new_path) != path_key(c.old_path) and key not in vacated and fs.lexists(fs.os_path(c.root.configured_path, new_path)):
            blocked = "a file is already at that path on disk"
        risk = _RISK[(operation, confidence)] if operation in ("rename", "move", "rename+move") else "low"
        reason = (f"accepted title ({source}); authors and year {'accepted' if accepted.get('authors') or accepted.get('year') else 'unknown'}"
                  + ("; case or Unicode form only" if path_key(new_path) == path_key(c.old_path) else ""))
        items.append(planmod.PlanItem(item_id, operation, "blocked" if blocked else "planned", c.root.root_id, c.location_id, c.artifact_id, c.document_id, c.old_path, new_path,
                                      c.artifact_id, c.size, reason, snapshot, source, confidence, risk, blocked))
    return planmod.Plan(
        new_id(), utc_now(), revision(conn), naming.NAMING_POLICY_VERSION, __version__,
        {"layout": layout, "roots": [r.root_id for r in roots], "documents": sorted(documents) if documents is not None else None},
        [{"root_id": r.root_id, "root_key": r.root_key, "path": r.configured_path, "volume_id": r.volume_id, "label": r.label} for r in roots], items,
    )


def default_plan_directory() -> Path:
    return paths.data_dir() / "plans"


def register_plan(conn: sqlite3.Connection, plan: planmod.Plan, path: Path) -> None:
    """Record that this catalog made this plan, with the hash of its contents, so `apply` can tell it from a stranger's or an edited one."""
    from knowledgevista.db.catalog import bump_revision, transaction

    with transaction(conn):
        conn.execute(
            "INSERT INTO plan_registry (plan_id, path, plan_hash, created_at, catalog_revision, naming_policy, item_count, planned_count, options_json, app_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plan.plan_id, str(path), planmod.hash_of(plan.body()), plan.created_at, plan.catalog_revision, plan.naming_policy, len(plan.items),
             sum(1 for i in plan.items if i.status == "planned"), planmod.canonical(plan.options).decode("ascii"), plan.app_version))
        bump_revision(conn)


def write_plan(conn: sqlite3.Connection, plan: planmod.Plan, out: Path | None = None) -> Path:
    """Write the plan's file and register it. REFUSES a destination inside any root (writing into the library is a mutation of it) and an
    existing file (a plan is frozen: a new plan is a new file)."""
    target = (out or default_plan_directory() / f"plan-{plan.plan_id[:12]}.json").resolve()
    _, key = normalise_root(str(target))
    for root in list_roots(conn):
        if is_within(root.root_key, key):
            raise KvError(ErrorCode.INVALID_ARGUMENTS, f"The plan file would be written inside the root {root.label!r}; a plan lives outside the library it describes.", {"path": str(target)})
    if target.exists():
        raise KvError(ErrorCode.DESTINATION_EXISTS, f"{target} already exists; a plan is frozen, so a new plan is a new file.", {"path": str(target)})
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(planmod.dumps(plan), encoding="utf-8", newline="\n")
    register_plan(conn, plan, target)
    return target
