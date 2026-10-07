"""The organizer's plan: a frozen, hashed, self-describing document, and the rules a plan must satisfy before anything is moved.

A plan is a JSON FILE a person can read, keep and review. It is the whole of the proposal: every item names the file by its SHA-256, its
current and wanted path, the accepted metadata the name was built from, and a risk; nothing is looked up again at apply time except to check
that the world still matches what the plan says. It carries a hash over everything else, so an edited plan is refused rather than
trusted, and a format version, so a plan from a newer program is refused rather than half understood.

This module is pure: it builds, serialises and validates plans and touches no catalog or filesystem. What the world looks like NOW
(is the file still there, does the metadata still say that) is the executor's question (services/organizer.py).

    plan_format      the layout of this document; only PLAN_FORMAT is understood
    naming_policy    the naming rules the names came from (domain/naming.py); another version makes the plan stale
    catalog_revision informational: the catalog revision the plan was made at. Staleness is decided per item, not by this number, so an
                     unrelated tag does not invalidate a plan about file names
    roots            each root the plan touches, with the identity (key, volume) it had when planned
    items            one per location considered: operation rename | move | rename+move | unchanged, status planned | unchanged | blocked
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from knowledgevista.domain.naming import component_problems

PLAN_FORMAT = 1
OPERATIONS = ("rename", "move", "rename+move", "unchanged")
STATUSES = ("planned", "unchanged", "blocked")
RISKS = ("low", "medium", "high")
CONFIDENCES = ("high", "medium")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


class PlanInvalid(ValueError):
    """The plan cannot be used: the message says which rule it broke. Carries `reasons` when there are several."""

    def __init__(self, message: str, reasons: list[str] | None = None):
        super().__init__(message)
        self.reasons = reasons or [message]


@dataclass
class PlanItem:
    item_id: str
    operation: str
    status: str
    root_id: str
    location_id: str
    artifact_id: str
    document_id: str
    old_path: str
    new_path: str
    expected_sha256: str
    expected_size: int
    reason: str
    snapshot: dict[str, Any]
    source: str
    confidence: str
    risk: str
    blocked_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Plan:
    plan_id: str
    created_at: str
    catalog_revision: int
    naming_policy: str
    app_version: str
    options: dict[str, Any]
    roots: list[dict[str, Any]]
    items: list[PlanItem] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        counts: dict[str, dict[str, int]] = {"operation": {}, "status": {}, "risk": {}}
        for item in self.items:
            for key, value in (("operation", item.operation), ("status", item.status), ("risk", item.risk)):
                counts[key][value] = counts[key].get(value, 0) + 1
        return {"items": len(self.items), **{k: dict(sorted(v.items())) for k, v in counts.items()}}

    def body(self) -> dict[str, Any]:
        return {
            "plan_format": PLAN_FORMAT, "plan_id": self.plan_id, "created_at": self.created_at, "catalog_revision": self.catalog_revision,
            "naming_policy": self.naming_policy, "app_version": self.app_version, "options": self.options, "roots": self.roots,
            "summary": self.summary(), "items": [i.as_dict() for i in self.items],
        }


def canonical(body: dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def hash_of(body: dict[str, Any]) -> str:
    return hashlib.sha256(canonical({k: v for k, v in body.items() if k != "hash"})).hexdigest()


def dumps(plan: Plan) -> str:
    """The plan as the text of its file: readable, stable key order, with the hash of everything else."""
    body = plan.body()
    body["hash"] = hash_of(body)
    return json.dumps(body, sort_keys=True, indent=1, ensure_ascii=True) + "\n"


def _same_directory(a: str, b: str) -> bool:
    return a.rpartition("/")[0] == b.rpartition("/")[0]


def _same_name(a: str, b: str) -> bool:
    return a.rpartition("/")[2] == b.rpartition("/")[2]


def _structural_problems(path: str) -> list[str]:
    """The minimum a recorded current path must be: a relative, '/'-separated path with no escapes. Nothing about its spelling."""
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        return ["must be a '/'-separated path inside the root"]
    return [f"component {part!r} is not allowed" for part in path.split("/") if part in ("", ".", "..")]


def _item_problems(item: dict[str, Any], root_ids: set[str]) -> list[str]:
    where = f"item {item.get('item_id', '?')}"
    problems: list[str] = []
    for key in PlanItem.__dataclass_fields__:
        if key not in item and key != "blocked_reason":
            problems.append(f"{where}: missing {key}")
    if problems:
        return problems
    if item["operation"] not in OPERATIONS:
        problems.append(f"{where}: unknown operation {item['operation']!r}")
    if item["status"] not in STATUSES:
        problems.append(f"{where}: unknown status {item['status']!r}")
    if item["risk"] not in RISKS or item["confidence"] not in CONFIDENCES:
        problems.append(f"{where}: unknown risk or confidence")
    if item["root_id"] not in root_ids:
        problems.append(f"{where}: names a root the plan does not list")
    if not _SHA256.match(str(item["expected_sha256"])) or item["artifact_id"] != item["expected_sha256"]:
        problems.append(f"{where}: expected_sha256 must be the artifact's SHA-256")
    if not _HEX32.match(str(item["document_id"])) or not _HEX32.match(str(item["location_id"])):
        problems.append(f"{where}: document_id and location_id must be 32 hex characters")
    if not isinstance(item["expected_size"], int) or item["expected_size"] < 0:
        problems.append(f"{where}: expected_size must be a non-negative integer")
    # A file may already be called anything (names made by other programs); only the DESTINATION is held to the naming rules.
    problems += [f"{where}: old_path: {why}" for why in _structural_problems(item["old_path"])]
    if item["status"] != "unchanged":
        problems += [f"{where}: new_path: {why}" for why in component_problems(item["new_path"])]
    if item["status"] == "unchanged":
        if item["operation"] != "unchanged" or item["old_path"] != item["new_path"]:
            problems.append(f"{where}: an unchanged item must have the same old and new path")
    elif item["old_path"] == item["new_path"]:
        problems.append(f"{where}: {item['status']} item with the same old and new path")
    elif item["operation"] in OPERATIONS and item["operation"] != "unchanged":
        same_dir, same_name = _same_directory(item["old_path"], item["new_path"]), _same_name(item["old_path"], item["new_path"])
        expected = "rename" if same_dir else ("move" if same_name else "rename+move")
        if item["operation"] != expected:
            problems.append(f"{where}: operation {item['operation']!r} does not match the paths (that is a {expected})")
    if item["status"] == "blocked" and not item.get("blocked_reason"):
        problems.append(f"{where}: a blocked item must say why")
    return problems


def validate(data: Any) -> Plan:
    """The plan in `data`, or `PlanInvalid` naming every rule it breaks. Checks the format, the hash and each item."""
    if not isinstance(data, dict):
        raise PlanInvalid("A plan is a JSON object.")
    version = data.get("plan_format")
    if version != PLAN_FORMAT:
        raise PlanInvalid(f"This plan is format {version!r}; this program understands format {PLAN_FORMAT}. A plan from a newer program is refused, never half understood.")
    needed = ("plan_id", "created_at", "catalog_revision", "naming_policy", "app_version", "options", "roots", "items", "hash")
    missing = [k for k in needed if k not in data]
    if missing:
        raise PlanInvalid(f"The plan is missing: {', '.join(missing)}.")
    if hash_of(data) != data["hash"]:
        raise PlanInvalid("The plan's hash does not match its contents: it was edited after it was made, or damaged. Make a new plan.")
    roots, items = data["roots"], data["items"]
    if not isinstance(roots, list) or not isinstance(items, list) or not all(isinstance(r, dict) and "root_id" in r for r in roots):
        raise PlanInvalid("The plan's roots and items must be lists.")
    root_ids = {r["root_id"] for r in roots}
    reasons: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            reasons.append("an item is not an object")
            continue
        if item.get("item_id") in seen:
            reasons.append(f"item id {item.get('item_id')} appears twice")
        seen.add(item.get("item_id"))
        reasons += _item_problems(item, root_ids)
    if reasons:
        raise PlanInvalid(f"The plan breaks {len(reasons)} rule(s): {reasons[0]}" + (" (and more)" if len(reasons) > 1 else ""), reasons)
    return Plan(
        data["plan_id"], data["created_at"], data["catalog_revision"], data["naming_policy"], data["app_version"], data["options"], roots,
        [PlanItem(**{k: item.get(k) for k in PlanItem.__dataclass_fields__}) for item in items],
    )


def loads(text: str) -> Plan:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise PlanInvalid(f"That is not a plan file (not valid JSON: {exc}).") from exc
    return validate(data)
