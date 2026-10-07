"""The metadata fields, and the one canonical form each is stored and compared in.

A value is validated and normalised BEFORE it is stored, whoever supplies it (a provider, the PDF, a person typing
`kv metadata set`): "manual values are still validated and normalised". Unknown is the absence of a value, never an
empty string, so `normalise` refuses an empty value rather than storing one.

`authors` is a JSON list of `{"family", "given", "name"}` objects (a person has family/given, a consortium has only
a name). A local source that knows only a string ("A. Examplar") stores it as `name`, because splitting a name into
family and given without being told which is which is a guess, and guesses are not stored as structure.
"""

from __future__ import annotations

import json
import re

from knowledgevista.domain import titles
from knowledgevista.domain.doi import normalise_doi

FIELDS = ("doi", "title", "authors", "year", "container", "publisher", "type", "volume", "issue", "pages", "isbn", "arxiv")
MIN_YEAR, MAX_YEAR = 1400, 2100
IDENTIFIER_FIELDS = ("doi", "isbn", "arxiv", "year")
_ARXIV = re.compile(r"^(?:arxiv:)?(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?$", re.IGNORECASE)


def isbn_ok(digits: str) -> bool:
    """The ISBN-10 or ISBN-13 check digit, so a number that merely looks like one is not stored as one."""
    if len(digits) == 13 and digits.isdigit():
        return sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits)) % 10 == 0
    if len(digits) == 10 and digits[:9].isdigit() and (digits[9].isdigit() or digits[9] in "Xx"):
        total = sum((10 - i) * int(d) for i, d in enumerate(digits[:9])) + (10 if digits[9] in "Xx" else int(digits[9]))
        return total % 11 == 0
    return False


def normalise(field: str, raw: object) -> str:
    """The canonical stored text for `raw` in `field`; raises ValueError (with a message fit to show) if it is not one."""
    if field not in FIELDS:
        raise ValueError(f"unknown field {field!r}; the fields are: {', '.join(FIELDS)}")
    if field == "authors":
        return _authors(raw)
    # Free text loses markup and entities (a provider's title carries JATS tags); an IDENTIFIER never does, because it can contain
    # `<` and `>` itself: an old Wiley DOI is `10.1002/(sici)1096-987x(199604)17:5/6<490::aid-jcc1>3.0.co;2-p`, and stripping
    # "tags" from it stores a different, wrong DOI (found by `doctor` on a real library).
    text = " ".join(str(raw if raw is not None else "").split()) if field in IDENTIFIER_FIELDS else titles.clean(str(raw if raw is not None else ""))
    if not text:
        raise ValueError(f"{field} cannot be empty; to say a value is unknown, leave it unset")
    if field == "doi":
        doi = normalise_doi(text)
        if not doi:
            raise ValueError(f"{raw!r} is not a DOI (expected something like 10.1234/abc)")
        return doi
    if field == "year":
        if not re.fullmatch(r"\d{4}", text) or not MIN_YEAR <= int(text) <= MAX_YEAR:
            raise ValueError(f"{raw!r} is not a year between {MIN_YEAR} and {MAX_YEAR}")
        return text
    if field == "isbn":
        digits = re.sub(r"[\s-]", "", text).upper()
        if not isbn_ok(digits):
            raise ValueError(f"{raw!r} is not a valid ISBN (the check digit does not match)")
        return digits
    if field == "arxiv":
        match = _ARXIV.match(text)
        if not match:
            raise ValueError(f"{raw!r} is not an arXiv identifier")
        return match.group(1).lower()
    return text


def _authors(raw: object) -> str:
    if isinstance(raw, str):
        raw = [{"name": n.strip()} for n in re.split(r"\s*;\s*", raw) if n.strip()]
    people = []
    for entry in raw or []:  # type: ignore[union-attr]
        if isinstance(entry, str):
            entry = {"name": entry}
        family, given, name = (titles.clean(str(entry.get(k) or "")) or None for k in ("family", "given", "name"))
        if family or name:
            people.append({"family": family, "given": given, "name": name})
    if not people:
        raise ValueError("authors cannot be empty; to say they are unknown, leave them unset")
    return json.dumps(people, ensure_ascii=False, separators=(",", ":"))


#: Fields whose text is compared by its letters and digits when asking whether two proposals AGREE: a title spelled with a
#: non-breaking hyphen by one source and an ASCII hyphen by another (measured: 44 documents of a real library) is one title.
TEXT_FIELDS = ("title", "container", "publisher")


def agreement_key(field: str, value: str) -> str:
    """What two proposals for `field` must share to agree. Exact text for identifiers, numbers and structures; letters and
    digits only for the free-text fields, so typography never makes two copies of one title disagree."""
    return titles.alnum_key(value) if field in TEXT_FIELDS else value


def authors_display(value: str) -> str:
    """`Examplar, A.; Placeholder, B.` for a stored authors value (also accepts an already-plain string)."""
    try:
        people = json.loads(value)
    except ValueError:
        return value
    names = []
    for person in people:
        if person.get("family"):
            names.append(f"{person['family']}, {person['given']}" if person.get("given") else person["family"])
        else:
            names.append(person.get("name") or "?")
    return "; ".join(names)


def display(field: str, value: str) -> str:
    return authors_display(value) if field == "authors" else value
