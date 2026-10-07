"""Shared setup for the organizer tests: a scratch library whose documents have accepted metadata, a root that allows organizing, and an
oracle for "the tree is exactly as it was" that sees files, folders and bytes."""

from __future__ import annotations

import os
from pathlib import Path

from support import make_env, tree_hashes

from knowledgevista.services import metadata, organizer, organizer_plan, roots

TITLE = "Aqueous Solubility of Invented Esters"
AUTHORS = [{"family": "Examplar", "given": "A."}]


def library(tmp_path: Path, files: dict[str, bytes | str], *, allow=True):
    env = make_env(tmp_path, files)
    if allow:
        env.conn.execute("UPDATE root SET allow_organize = 1")
    env.scan()
    return env


def document(env, path: str) -> str:
    return env.one("SELECT da.document_id FROM location l JOIN document_artifact da ON da.artifact_id = l.artifact_id WHERE l.relative_path = ? AND l.ended_at IS NULL", path)


def describe(env, path: str, title: str | None = TITLE, authors=AUTHORS, year: str | None = "2021", lock: bool = False) -> str:
    """Accept metadata for the document at `path`, stated by a person (so `high` confidence)."""
    doc = document(env, path)
    for field, value in (("title", title), ("authors", authors), ("year", year)):
        if value is not None:
            metadata.set_value(env.conn, doc, field, value, lock=lock)
    return doc


def plan_for(env, **kw):
    selected = roots.list_roots(env.conn)
    return organizer_plan.build_plan(env.conn, roots=selected, **kw)


def registered(env, tmp_path: Path, plan, name="plan.json") -> Path:
    return organizer_plan.write_plan(env.conn, plan, tmp_path / "plans" / name)


def make_and_apply(env, tmp_path, **kw):
    path = registered(env, tmp_path, plan_for(env))
    return path, organizer.apply_plan(env.conn, str(path), **kw)


def tree(root: Path) -> dict:
    """Every file with its bytes' hash AND every folder: a rename that leaves an empty folder behind is a difference."""
    files = tree_hashes(root)
    folders = sorted(os.path.relpath(os.path.join(base, d), root).replace(os.sep, "/") for base, dirs, _ in os.walk(root) for d in dirs)
    return {"files": files, "folders": folders}


def paths_now(env) -> list[str]:
    return sorted(r["relative_path"] for r in env.rows("SELECT relative_path FROM location WHERE ended_at IS NULL AND state = 'active'"))
