"""The pure half of "why this value?": how evidence reads, and whether the sources agree."""

from __future__ import annotations

import pytest

from knowledgevista.domain import evidence_view as ev


def cand(value, source="layout_title", status="proposed"):
    return {"value": value, "source": source, "status": status}


def accepted(value, source="layout_title"):
    return {"value": value, "source": source}


# ------------------------------------------------------------------------------------------------------- agreement


def test_nothing_known_and_nothing_proposed_is_unknown():
    assert ev.agreement("title", None, []) == "unknown"


def test_proposals_without_an_accepted_value_are_unresolved():
    assert ev.agreement("title", None, [cand("A")]) == "unresolved"


def test_only_decided_proposals_without_an_accepted_value_is_unknown():
    """A rejected or set-aside proposal is a record of a decision, not a vote: it must not make a field look pending."""
    assert ev.agreement("title", None, [cand("A", status="rejected"), cand("B", status="set_aside"), cand("C", status="stale")]) == "unknown"


def test_a_different_value_still_waiting_is_a_discrepancy():
    assert ev.agreement("title", accepted("A"), [cand("B", "crossref")]) == "discrepancy"


def test_a_rejected_different_value_is_not_a_discrepancy():
    assert ev.agreement("title", accepted("A"), [cand("B", "crossref", status="rejected")]) == "uncorroborated"


def test_another_source_saying_the_same_thing_is_agreement():
    assert ev.agreement("title", accepted("A", "layout_title"), [cand("A", "layout_title", "accepted"), cand("A", "crossref", "accepted")]) == "agreement"


def test_the_accepted_values_own_source_is_not_corroboration():
    """One source found twice is still one source."""
    assert ev.agreement("title", accepted("A", "layout_title"), [cand("A", "layout_title", "accepted")]) == "uncorroborated"


def test_a_discrepancy_outranks_agreement():
    got = ev.agreement("title", accepted("A"), [cand("A", "crossref", "accepted"), cand("B", "pdf_info_title")])
    assert got == "discrepancy"


def test_typography_does_not_make_a_discrepancy():
    """Titles are compared by their letters and digits: a non-breaking hyphen is the same title (fields.agreement_key)."""
    assert ev.agreement("title", accepted("Cu‑Zn alloys"), [cand("Cu-Zn alloys", "crossref")]) == "agreement"


def test_identifiers_are_compared_exactly():
    assert ev.agreement("doi", accepted("10.1/a"), [cand("10.1/A", "crossref")]) == "discrepancy"


def test_every_agreement_state_has_a_sentence():
    assert set(ev.AGREEMENT_TEXT) == set(ev.AGREEMENT)


# ------------------------------------------------------------------------------------------------------- evidence lines


def test_empty_facts_are_not_lines():
    """`excerpt: ` with nothing after it reads as an excerpt that is empty."""
    assert ev.evidence_lines({"excerpt": "", "page": None, "cues": [], "notes": {}}) == []
    assert ev.evidence_lines(None) == []


def test_the_most_explanatory_lines_come_first():
    lines = ev.evidence_lines({"zeta": "z", "alpha": "a", "page": 1, "excerpt": "see 10.1/x"})
    assert [name for name, _ in lines] == ["excerpt", "page", "alpha", "zeta"]


def test_nested_facts_are_named_by_their_path():
    lines = dict(ev.evidence_lines({"components": {"on_first_page": 3, "in_pdf_metadata": 2}, "reasons": ["a", "b"]}))
    assert lines["components.on_first_page"] == "3"
    assert lines["reasons"] == "a, b"


def test_a_list_of_records_is_numbered():
    lines = dict(ev.evidence_lines({"matches": [{"page": 1}, {"page": 4}]}))
    assert lines == {"matches[1].page": "1", "matches[2].page": "4"}


def test_a_long_value_is_cut_with_a_mark():
    (name, value), = ev.evidence_lines({"excerpt": "x" * 1000})
    assert len(value) == ev.MAX_VALUE_CHARS and value.endswith("…")


def test_whitespace_in_a_value_is_collapsed():
    (_, value), = ev.evidence_lines({"excerpt": "a\n\n  b\t c"})
    assert value == "a b c"


def test_markup_in_evidence_is_returned_unchanged():
    """Escaping is the window's job at the point of display; changing the text here would make two views disagree about it."""
    (_, value), = ev.evidence_lines({"excerpt": "<b>x</b> &amp;"})
    assert value == "<b>x</b> &amp;"


# ------------------------------------------------------------------------------------------------------- sentences


@pytest.mark.parametrize("by,who", [("user", "you"), ("rule:safe_batch_v1", "a batch rule"), ("import", "import")])
def test_the_provenance_sentence_names_who_accepted(by, who):
    text = ev.provenance_sentence({"accepted_by": by, "origin": "observed", "source": "layout_title", "locked": False})
    assert text.startswith(f"Accepted by {who}, from the title as laid out on page one (read from the file).")
    assert "Locked" not in text


def test_a_locked_value_says_so():
    assert "Locked: no resolver may change it." in ev.provenance_sentence({"accepted_by": "user", "origin": "assigned", "source": "manual", "locked": True})


def test_an_unknown_source_shows_as_itself():
    assert ev.source_label("a_future_source") == "a_future_source"
    assert ev.origin_label("a_future_origin") == "a_future_origin"
    assert ev.field_label("a_future_field") == "a_future_field"


def test_every_core_field_and_origin_has_a_label():
    from knowledgevista.domain.fields import FIELDS

    assert all(f in ev.FIELD_LABELS for f in FIELDS)
    assert set(ev.ORIGIN_BADGES) == set(ev.ORIGIN_LABELS) == {"observed", "resolved", "inferred", "assigned"}
    assert len(set(ev.ORIGIN_BADGES.values())) == 4, "two origins sharing a badge letter would look identical"


# ------------------------------------------------------------------------------------------------------- the whole answer


def _why(**over):
    base = {
        "label": "Title", "agreement_text": ev.AGREEMENT_TEXT["agreement"],
        "accepted": {"display": "Cu(II) Complexes", "sentence": "Accepted by you, from Crossref (a record for the DOI) (returned by a provider).",
                     "accepted_at": "2026-10-07T10:00:00Z", "evidence": [("verdict", "exact")]},
        "proposals": [
            {"display": "Cu(II) Complexes", "source_label": "Crossref (a record for the DOI)", "origin": "resolved", "status": "accepted", "confidence": "exact",
             "review": "safe", "classification": None, "relation_to_accepted": "same", "evidence": [("verdict", "exact")]},
            {"display": "Copper complexes", "source_label": "the file's Info title", "origin": "observed", "status": "rejected", "confidence": "low",
             "review": "required", "classification": None, "relation_to_accepted": "differs", "evidence": []},
        ],
        "history": [{"at": "2026-10-07T10:00:00Z", "actor": "user", "old": None, "new": "Cu(II) Complexes", "reason": "accepted candidate from crossref"}],
    }
    base.update(over)
    return base


def test_the_whole_answer_reads_top_to_bottom():
    assert ev.render_why(_why()) == "\n".join([
        "Title", "", "Agreement: another, independent source supports this value.", "",
        "Accepted value", "  Cu(II) Complexes", "  Accepted by you, from Crossref (a record for the DOI) (returned by a provider).",
        "  Accepted at 2026-10-07T10:00:00Z.", "  Evidence:", "    verdict: exact", "",
        "Proposals (2)",
        "  Cu(II) Complexes", "    from Crossref (a record for the DOI) (returned by a provider); accepted; confidence exact; a rule may accept it; same as the accepted value",
        "      verdict: exact",
        "  Copper complexes", "    from the file's Info title (read from the file); rejected by a person; confidence low; needs a person; DIFFERS from the accepted value",
        "", "History (newest first)", "  2026-10-07T10:00:00Z  user: (none) -> Cu(II) Complexes  [accepted candidate from crossref]",
    ])


def test_the_answer_for_a_field_with_nothing_says_none_twice():
    text = ev.render_why(_why(accepted=None, proposals=[], history=[], agreement_text=ev.AGREEMENT_TEXT["unknown"]))
    assert "  none: nothing has been accepted for this field." in text and "Proposals\n  none" in text and "History" not in text


def test_markup_in_the_answer_is_shown_as_it_is():
    text = ev.render_why(_why(accepted={"display": "<b>x</b> &amp;", "sentence": "Accepted by you.", "accepted_at": None, "evidence": [("excerpt", '<img src="http://a/b.png">')]}))
    assert "<b>x</b> &amp;" in text and '<img src="http://a/b.png">' in text


# ---------------------------------------------------------------------------------------------- found by the mutation sweep


def test_a_rejected_proposal_from_another_source_is_not_corroboration():
    """Rejected means a person said no: it must not make an accepted value look supported."""
    got = ev.agreement("title", accepted("A", "layout_title"), [cand("A", "layout_title", "accepted"), cand("A", "crossref", "rejected")])
    assert got == "uncorroborated"


def test_an_older_accepted_candidate_that_was_replaced_is_not_a_discrepancy():
    """Accepting a different value leaves the earlier candidate marked accepted; it is history, not a source still disagreeing."""
    got = ev.agreement("title", accepted("B", "manual"), [cand("A", "layout_title", "accepted"), cand("B", "manual", "accepted")])
    assert got == "uncorroborated"
