"""The naming policy: what a file should be called, from what a person has ACCEPTED about it. Pure, versioned, deterministic.

    <First author>[ et al.][ (<year>)] - <Title><.ext>          e.g.  Examplar et al. (2021) - Aqueous Solubility of Invented Esters.pdf

A name is built ONLY from accepted metadata (a proposal never names a file) and ONLY when there is an accepted title. A document with no
accepted title is left alone: an unknown stays unknown, and a file called `Unknown - Untitled (2021).pdf` is worse than `cm4c01978.pdf`
because it looks like an answer. The same inputs give the same name on every machine and in every directory order: nothing here reads the
filesystem, the clock or a random source.

Rules, each of which a test pins (tests/test_naming.py):

  * UNICODE. NFC, so one visual name has one spelling. Format/bidirectional control characters (category Cf: a right-to-left override can
    make `exe.fdp` display as `pdf.exe`) and control characters are removed.
  * CHARACTERS WINDOWS FORBIDS (`< > : " / \\ | ? *`) are replaced or removed so a name made here is valid everywhere the library may be
    copied to: `:` `/` `\\` `|` become ` - `, `"` becomes `'`, the rest are dropped.
  * NO TRAILING DOT OR SPACE (Windows silently strips them, so two different names become one file), no leading space, whitespace collapsed.
  * RESERVED DEVICE NAMES (CON, PRN, AUX, NUL, COM1-9, LPT1-9, with or without an extension) get a trailing `_`.
  * LENGTH. A stem longer than MAX_STEM is cut and ends `~` plus eight hex characters of the SHA-256 of the UNCUT stem, so two long names
    that differ only past the cut still differ, and the cut is the same every time.
  * A LEAD AUTHOR typed as one string ("Examplar, A.") is the part before the first comma ("Examplar"); one with no comma (a consortium) is
    used whole. An author with a family name uses it.
  * COLLISIONS are not resolved here (they depend on what else is being named): `disambiguate` does it for a whole plan, by artifact id,
    never by directory order.

`NAMING_POLICY_VERSION` changes with any rule above. A plan records it, and a plan made under another version is refused as stale.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

NAMING_POLICY_VERSION = "naming-1"
LAYOUTS = ("in_place", "by_year")
MAX_STEM = 120
#: A path component this long or longer is refused outright by a plan (NTFS allows 255 UTF-16 units; leave room for a suffix).
MAX_COMPONENT = 200
UNKNOWN_YEAR_DIR = "unknown-year"

_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10)),
                       "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"})
_TO_DASH = str.maketrans({":": " - ", "/": " - ", "\\": " - ", "|": " - "})
_DROPPED = frozenset('<>?*')
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class NameDecision:
    """What the policy proposes for one file."""

    stem: str
    extension: str
    subdirectory: str | None  # None = stay in the current folder
    snapshot: dict[str, Any] = field(default_factory=dict)  # the accepted values the name was built from, so staleness can be told later

    @property
    def filename(self) -> str:
        return self.stem + self.extension


def clean(text: str) -> str:
    """One path component's worth of text: valid on every platform, one spelling, no invisible tricks."""
    text = unicodedata.normalize("NFC", text or "").translate(_TO_DASH).replace('"', "'")
    kept = []
    for ch in text:
        category = unicodedata.category(ch)
        if ch.isspace() or category.startswith("Z"):  # tabs and newlines are controls AND whitespace: they separate words, they do not vanish
            kept.append(" ")
        elif ch in _DROPPED or category.startswith("C"):  # controls, format (bidi overrides, zero-width), surrogates, private use, unassigned
            continue
        else:
            kept.append(ch)
    return _SPACE.sub(" ", "".join(kept)).strip(" .")


def avoid_reserved(stem: str) -> str:
    """`CON` and `CON.v2` are device names on Windows even with an extension; a `_` right after the device name makes them ordinary."""
    head, dot, rest = stem.partition(".")
    return head + "_" + dot + rest if head.upper() in _RESERVED else stem


def shorten(stem: str, limit: int = MAX_STEM) -> str:
    """`stem` unchanged if it fits, else its start, `~`, and eight hex characters of the SHA-256 of the whole of it."""
    if len(stem) <= limit:
        return stem
    tag = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:8]
    return stem[: limit - 9].rstrip(" .") + "~" + tag


def _people(authors_json: str | None) -> list[str]:
    if not authors_json:
        return []
    try:
        people = json.loads(authors_json)
    except ValueError:
        return []
    out = []
    for person in people:
        if not isinstance(person, dict):
            continue
        family = person.get("family")
        name = person.get("name")
        if family:
            out.append(family)
        elif name:
            # A name typed as one string: "Family, Given" is the convention for a person, so the part before the comma is the family name;
            # a consortium or an organisation has no comma and is used whole.
            out.append(name.split(",", 1)[0] if "," in name else name)
    return out


def name_for(title: str | None, authors_json: str | None, year: str | None, extension: str, layout: str = "in_place") -> NameDecision | None:
    """The proposed name, or None when there is nothing accepted to build one from (no title)."""
    if layout not in LAYOUTS:
        raise ValueError(f"unknown layout {layout!r}; layouts: {', '.join(LAYOUTS)}")
    title_part = clean(title or "")
    if not title_part:
        return None
    people = _people(authors_json)
    lead = clean(people[0]) if people else ""
    if lead and len(people) > 1:
        lead += " et al."
    year_part = f"({year})" if year and re.fullmatch(r"\d{4}", year) else ""
    head = " ".join(part for part in (lead, year_part) if part)
    stem = f"{head} - {title_part}" if head else title_part
    stem = avoid_reserved(shorten(stem.strip(" .")))
    ext = extension.lower() if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", extension or "") else ""
    subdirectory = (year if year_part else UNKNOWN_YEAR_DIR) if layout == "by_year" else None
    return NameDecision(stem, ext, subdirectory, {"title": title, "authors": authors_json, "year": year, "layout": layout})


def collision_key(relative_path: str) -> str:
    """What decides whether two destinations are one file on any filesystem we may be copied to: NFC and case-insensitive."""
    return unicodedata.normalize("NFC", relative_path).lower()


def disambiguate(entries: list[tuple[str, str, str]]) -> dict[str, str]:
    """Names for a set of items that would otherwise share destinations.

    `entries` is (item key, artifact id, wanted relative path). Items whose wanted paths are unique (by `collision_key`) keep them. Every
    member of a colliding group gets ` [<first eight of its artifact id>]` before the extension, so no member is favoured by the order it
    was listed in, and where one artifact is wanted at the same path twice (two copies, a layout that merges folders) a counter ` -2`,
    ` -3` follows in item-key order. Returns item key -> final relative path. Pure and order-independent.
    """
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for entry in entries:
        groups.setdefault(collision_key(entry[2]), []).append(entry)
    final: dict[str, str] = {}
    for members in groups.values():
        if len(members) == 1:
            final[members[0][0]] = members[0][2]
            continue
        seen: dict[str, int] = {}
        for key, artifact_id, wanted in sorted(members, key=lambda m: m[0]):  # each artifact's counter depends only on the order of ITS OWN items' keys
            directory, _, name = wanted.rpartition("/")
            stem, dot, ext = name.rpartition(".")
            stem, ext = (stem, "." + ext) if dot and stem else (name, "")
            tag = f" [{artifact_id[:8]}]"
            seen[artifact_id] = seen.get(artifact_id, 0) + 1
            if seen[artifact_id] > 1:
                tag += f" -{seen[artifact_id]}"
            final[key] = (directory + "/" if directory else "") + shorten(stem + tag) + ext
    return final


def component_problems(relative_path: str) -> list[str]:
    """Why a relative path must not be used as a destination: empty if it is fine. Used by the plan validator and the executor, so the
    rules a plan is held to and the rules a file is held to cannot differ."""
    problems = []
    if not relative_path or relative_path.startswith("/") or "\\" in relative_path:
        problems.append("a destination is a '/'-separated path inside the root")
    for part in relative_path.split("/"):
        if part in ("", ".", ".."):
            problems.append(f"path component {part!r} is not allowed")
        elif ":" in part:
            problems.append(f"{part!r} contains ':' (a drive or an alternate data stream)")
        elif part.startswith(" ") or part.endswith((" ", ".")):
            problems.append(f"{part!r} ends in a dot or a space, which Windows strips")
        elif any(unicodedata.category(c).startswith("C") for c in part):
            problems.append(f"{part!r} contains a control or format character")
        elif any(c in '<>"|?*' for c in part):
            problems.append(f"{part!r} contains a character Windows forbids")
        elif part.split(".")[0].upper() in _RESERVED:
            problems.append(f"{part!r} is a reserved device name")
        elif len(part) > MAX_COMPONENT:
            problems.append(f"{part[:20]!r}... is {len(part)} characters, over {MAX_COMPONENT}")
    return problems
