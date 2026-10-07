"""The plan document: frozen, hashed, versioned, and refused when it breaks a rule."""

from __future__ import annotations

import copy
import json

import pytest

from knowledgevista.domain import plan as p

SHA_A, SHA_B = "a" * 64, "b" * 64
DOC, LOC, ROOT = "1" * 32, "2" * 32, "3" * 32


def item(n=1, **kw) -> p.PlanItem:
    base = dict(item_id=f"i{n}", operation="rename", status="planned", root_id=ROOT, location_id=f"{n:032x}", artifact_id=SHA_A, document_id=DOC,
                old_path="papers/cm4c01978.pdf", new_path="papers/Examplar (2021) - T.pdf", expected_sha256=SHA_A, expected_size=10,
                reason="accepted title", snapshot={"title": "T"}, source="assigned", confidence="high", risk="low")
    base.update(kw)
    return p.PlanItem(**base)


def make(*items: p.PlanItem) -> p.Plan:
    return p.Plan("4" * 32, "2026-10-08T00:00:00Z", 7, "naming-1", "0.0.1", {"layout": "in_place"},
                  [{"root_id": ROOT, "root_key": "c:\\lib", "path": "C:\\lib", "volume_id": None}], list(items or [item()]))


def text(*items) -> str:
    return p.dumps(make(*items))


def refused(data, fragment):
    with pytest.raises(p.PlanInvalid) as caught:
        p.validate(data)
    assert fragment in str(caught.value) or any(fragment in r for r in caught.value.reasons), caught.value.reasons
    return caught.value


def mutated(*items, **changes):
    data = json.loads(text(*items))
    for key, value in changes.items():
        data[key] = value
    data["hash"] = p.hash_of(data)  # re-sealed: the point is the RULE, not the hash
    return data


def test_a_plan_round_trips_through_its_file_text():
    plan = make(item(1), item(2, old_path="x/old.pdf", new_path="x/New.pdf"))
    again = p.loads(p.dumps(plan))
    assert again.body() == plan.body() and again.items[1].new_path == "x/New.pdf"


def test_the_text_is_stable_and_carries_the_hash_of_everything_else():
    assert text() == text()
    data = json.loads(text())
    assert data["hash"] == p.hash_of(data) and data["plan_format"] == p.PLAN_FORMAT and data["summary"]["items"] == 1
    reordered = dict(reversed(list(data.items())))
    assert p.hash_of(reordered) == data["hash"]  # the hash does not depend on key order


def test_an_edited_plan_is_refused_whatever_was_edited():
    data = json.loads(text())
    for edit in (lambda d: d["items"][0].update(new_path="papers/other.pdf"), lambda d: d.update(naming_policy="naming-2"),
                 lambda d: d["roots"][0].update(path="C:\\elsewhere"), lambda d: d.update(catalog_revision=8), lambda d: d["items"].pop()):
        changed = copy.deepcopy(data)
        edit(changed)
        refused(changed, "hash does not match")


def test_a_plan_of_another_format_or_a_non_plan_is_refused_not_half_understood():
    refused(mutated(plan_format=2), "format 2")
    refused(mutated(plan_format=None), "format None")
    refused([], "JSON object")
    with pytest.raises(p.PlanInvalid, match="not valid JSON"):
        p.loads("{nope")
    data = json.loads(text())
    del data["roots"]
    refused(data, "missing: roots")


def test_every_rule_an_item_can_break_is_named():
    refused(mutated(item(1, operation="teleport")), "unknown operation")
    refused(mutated(item(1, status="maybe")), "unknown status")
    refused(mutated(item(1, risk="tiny")), "unknown risk")
    refused(mutated(item(1, root_id="9" * 32)), "root the plan does not list")
    refused(mutated(item(1, expected_sha256="x" * 64)), "must be the artifact's SHA-256")
    refused(mutated(item(1, expected_sha256=SHA_B)), "must be the artifact's SHA-256")  # not the artifact's own hash
    refused(mutated(item(1, document_id="short")), "32 hex")
    refused(mutated(item(1, expected_size=-1)), "non-negative")
    refused(mutated(item(1, new_path="papers/con.pdf")), "reserved device name")
    refused(mutated(item(1, new_path="../escape.pdf", operation="move")), "not allowed")
    refused(mutated(item(1, new_path="papers/trailing.")), "ends in a dot")
    refused(mutated(item(1, old_path="/abs/x.pdf")), "old_path")


def test_the_operation_must_match_what_the_paths_do():
    refused(mutated(item(1, operation="move")), "that is a rename")
    refused(mutated(item(1, old_path="a/x.pdf", new_path="b/x.pdf", operation="rename")), "that is a move")
    refused(mutated(item(1, old_path="a/x.pdf", new_path="b/y.pdf", operation="rename")), "that is a rename+move")
    p.validate(mutated(item(1, old_path="a/x.pdf", new_path="b/x.pdf", operation="move")))
    p.validate(mutated(item(1, old_path="a/x.pdf", new_path="b/y.pdf", operation="rename+move")))
    p.validate(mutated(item(1, old_path="a/x.pdf", new_path="a/X.pdf", operation="rename")))  # case-only is a rename


def test_unchanged_items_must_not_change_and_changed_ones_must():
    p.validate(mutated(item(1, operation="unchanged", status="unchanged", new_path="papers/cm4c01978.pdf")))
    refused(mutated(item(1, operation="unchanged", status="unchanged")), "same old and new path")
    refused(mutated(item(1, new_path="papers/cm4c01978.pdf")), "same old and new path")


def test_a_blocked_item_says_why_and_is_otherwise_an_ordinary_item():
    refused(mutated(item(1, status="blocked")), "must say why")
    p.validate(mutated(item(1, status="blocked", blocked_reason="a file is already there")))


def test_a_file_that_already_has_a_strange_name_does_not_make_the_plan_invalid():
    p.validate(mutated(item(1, old_path="from/other program/CON .pdf", new_path="from/other program/Fine.pdf")))  # only the destination is held to the rules


def test_item_ids_are_unique():
    refused(mutated(item(1), item(1, location_id=f"{9:032x}")), "appears twice")


def test_several_broken_rules_are_all_reported_and_the_message_counts_them():
    error = refused(mutated(item(1, operation="teleport"), item(2, risk="tiny")), "breaks 2 rule")
    assert len(error.reasons) == 2


def test_the_summary_counts_what_the_plan_holds():
    plan = make(item(1), item(2, operation="unchanged", status="unchanged", new_path="papers/cm4c01978.pdf"), item(3, status="blocked", blocked_reason="x", risk="high"))
    assert plan.summary() == {"items": 3, "operation": {"rename": 2, "unchanged": 1}, "status": {"blocked": 1, "planned": 1, "unchanged": 1}, "risk": {"high": 1, "low": 2}}
