"""How a value's evidence reads to a person: source names, evidence as lines, and whether the sources agree.

Pure, so the library window and any later view tell the same story. Nothing here decides anything: it describes what the
catalog already holds. A value's evidence is untrusted text (an excerpt of a PDF, a provider's title), so these functions only
ever produce plain strings; a caller must show them as text, never as markup.
"""

from __future__ import annotations

from typing import Any

from knowledgevista.domain import fields as fieldmod

#: What a source name means, in the words a person would use. Unknown names show as themselves: a new source never hides.
SOURCE_LABELS = {
    "pdf_text_doi": "a DOI printed in the document's text",
    "pdf_metadata_doi": "a DOI in the file's own metadata",
    "layout_title": "the title as laid out on page one",
    "pdf_xmp_title": "the file's XMP title",
    "pdf_info_title": "the file's Info title",
    "pdf_xmp_authors": "the file's XMP authors",
    "pdf_info_authors": "the file's Info authors",
    "isbn_text": "an ISBN printed in the text",
    "arxiv_text": "an arXiv identifier printed on page one",
    "filename_hint": "the file name",
    "crossref": "Crossref (a record for the DOI)",
    "crossref_title_search": "Crossref (a search by title)",
    "manual": "stated by a person",
}

#: How an origin reads, and the one-letter badge that sits beside a value. The badge is a hint; the word is the fact.
ORIGIN_LABELS = {
    "observed": "read from the file",
    "resolved": "returned by a provider",
    "inferred": "deduced from other values",
    "assigned": "stated by a person",
}
ORIGIN_BADGES = {"observed": "O", "resolved": "R", "inferred": "I", "assigned": "A"}

FIELD_LABELS = {
    "doi": "DOI", "title": "Title", "authors": "Authors", "year": "Year", "container": "Journal / book", "publisher": "Publisher",
    "type": "Type", "volume": "Volume", "issue": "Issue", "pages": "Pages", "isbn": "ISBN", "arxiv": "arXiv",
}
#: Shown even when unset, so "no DOI" is visible as a gap rather than an absent row.
CORE_FIELDS = ("title", "authors", "year", "doi", "container")

#: Evidence keys that explain the most, first. Everything else follows alphabetically.
_PREFERRED = ("excerpt", "page", "region", "provider", "state", "verdict", "reasons", "components", "cues", "foreign_cues")

MAX_VALUE_CHARS = 300

AGREEMENT = ("agreement", "discrepancy", "unresolved", "uncorroborated", "unknown")
AGREEMENT_TEXT = {
    "agreement": "Agreement: another, independent source supports this value.",
    "discrepancy": "Discrepancy: a source proposes a different value that has not been decided.",
    "unresolved": "Unresolved: nothing is accepted yet; the proposals are waiting for a person.",
    "uncorroborated": "Uncorroborated: no other source supports this value.",
    "unknown": "Nothing is known for this field.",
}


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source)


def origin_label(origin: str) -> str:
    return ORIGIN_LABELS.get(origin, origin)


def field_label(field: str) -> str:
    return FIELD_LABELS.get(field, field)


def _scalar(value: Any) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= MAX_VALUE_CHARS else text[: MAX_VALUE_CHARS - 1] + "…"


def _flatten(prefix: str, value: Any, out: list[tuple[str, str]]) -> None:
    if isinstance(value, dict):
        for key in sorted(value):
            _flatten(f"{prefix}.{key}" if prefix else str(key), value[key], out)
    elif isinstance(value, (list, tuple)):
        if value and all(isinstance(v, (str, int, float, bool)) for v in value):
            out.append((prefix, _scalar(", ".join(str(v) for v in value))))
        else:
            for number, item in enumerate(value, start=1):
                _flatten(f"{prefix}[{number}]", item, out)
    elif value is None or value == "" or value == [] or value == {}:
        return  # an absent fact is not a line: "excerpt: " would read as an excerpt that is empty
    else:
        out.append((prefix, _scalar(value)))


def evidence_lines(evidence: dict[str, Any] | None) -> list[tuple[str, str]]:
    """(name, value) pairs for an evidence record, the most explanatory first, long values cut, empty facts left out."""
    flat: list[tuple[str, str]] = []
    _flatten("", evidence or {}, flat)

    def rank(pair: tuple[str, str]) -> tuple[int, str]:
        head = pair[0].split(".")[0].split("[")[0]
        return (_PREFERRED.index(head) if head in _PREFERRED else len(_PREFERRED), pair[0])

    return sorted(flat, key=rank)


def agreement(field: str, accepted: dict[str, Any] | None, candidates: list[dict[str, Any]]) -> str:
    """Whether the sources for one field agree.

    `accepted` has `value` and `source`; each candidate has `value`, `source` and `status`. Only proposals that are still live count:
    a rejected, stale or set-aside one is a record of a decision, not a vote."""
    live = [c for c in candidates if c["status"] in ("proposed", "accepted")]
    if accepted is None:
        return "unresolved" if any(c["status"] == "proposed" for c in live) else "unknown"
    key = fieldmod.agreement_key(field, accepted["value"])
    if any(c["status"] == "proposed" and fieldmod.agreement_key(field, c["value"]) != key for c in live):
        return "discrepancy"
    if any(fieldmod.agreement_key(field, c["value"]) == key and c["source"] != accepted["source"] for c in live):
        return "agreement"
    return "uncorroborated"


def provenance_sentence(accepted: dict[str, Any]) -> str:
    """One sentence on how an accepted value came to be: who accepted it, from what, and whether it is locked."""
    by = accepted.get("accepted_by") or "unknown"
    who = "you" if by == "user" else ("a batch rule" if str(by).startswith("rule:") else by)
    origin = origin_label(accepted.get("origin") or "")
    source = source_label(accepted.get("source") or "")
    text = f"Accepted by {who}, from {source} ({origin})."
    if accepted.get("locked"):
        text += " Locked: no resolver may change it."
    return text


STATUS_TEXT = {
    "proposed": "waiting for a person", "accepted": "accepted", "rejected": "rejected by a person",
    "stale": "no longer found in the file", "set_aside": "set aside (judged not to be this document's)",
}


def _indent(lines: list[tuple[str, str]], pad: str) -> list[str]:
    return [f"{pad}{name}: {value}" for name, value in lines]


def render_why(why: dict[str, Any]) -> str:
    """The "why this value?" answer as plain text, from `library_view.field_evidence`. Every external string in it is shown as it is."""
    out = [f"{why['label']}", "", why["agreement_text"], ""]
    accepted = why["accepted"]
    if accepted is None:
        out += ["Accepted value", "  none: nothing has been accepted for this field."]
    else:
        out += ["Accepted value", f"  {accepted['display']}", f"  {accepted['sentence']}"]
        if accepted.get("accepted_at"):
            out.append(f"  Accepted at {accepted['accepted_at']}.")
        if accepted["evidence"]:
            out += ["  Evidence:", *_indent(accepted["evidence"], "    ")]
    out.append("")
    proposals = why["proposals"]
    out.append(f"Proposals ({len(proposals)})" if proposals else "Proposals\n  none")
    for p in proposals:
        relation = {"same": "same as the accepted value", "differs": "DIFFERS from the accepted value"}.get(p["relation_to_accepted"] or "", "")
        facts = [STATUS_TEXT.get(p["status"], p["status"]), f"confidence {p['confidence']}", "a rule may accept it" if p["review"] == "safe" else "needs a person"]
        if p["classification"]:
            facts.append(f"DOI classed {p['classification']}")
        if relation:
            facts.append(relation)
        out += [f"  {p['display']}", f"    from {p['source_label']} ({origin_label(p['origin'])}); " + "; ".join(facts)]
        out += _indent(p["evidence"], "      ")
    history = why["history"]
    if history:
        out += ["", "History (newest first)"]
        for h in history:
            old = h["old"] if h["old"] is not None else "(none)"
            out.append(f"  {h['at']}  {h['actor']}: {old} -> {h['new'] if h['new'] is not None else '(cleared)'}" + (f"  [{h['reason']}]" if h["reason"] else ""))
    return "\n".join(out)
