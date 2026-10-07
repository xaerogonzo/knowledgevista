"""Inventory the licences of the dependencies a given install would pull in, and fail on a blocked or unknown one.

WHY IT EXISTS. This project is AGPL-3.0-or-later and public. One dependency can quietly make that untenable: in
2026-10 `pymupdf4llm` (AGPL) was found to require `pymupdf_layout`, which is licensed Polyform Noncommercial. A
direct-dependency scan misses that, because the problem is two levels down, so this walks the LOCKFILE's closure
(the packages that would really be installed), not just `pyproject.toml`.

    python tools/license_inventory.py                 # the default (core) install
    python tools/license_inventory.py --extras extract,gui
    python tools/license_inventory.py --all-extras --report   # informational: what each extra would add

Licences are read from installed package metadata, so run it in an environment that has the packages (CI syncs all
extras first). A package in the closure that is not installed is reported as UNKNOWN, never skipped: a license check
that silently skipped what it could not read would pass exactly when it is most needed.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

ROOT_PACKAGE = "knowledgevista"

#: A licence that forbids commercial use or is not distributable is incompatible with shipping inside AGPL software.
_BLOCKED = re.compile(r"non-?\s?commercial|polyform|proprietary|no\s+commercial|research\s+use\s+only|all rights reserved", re.I)
#: Recognised licence families. Anything else is UNKNOWN and needs a human decision, never a silent pass.
_KNOWN = re.compile(
    r"\b(?:MIT|BSD|Apache|ISC|MPL|Mozilla|LGPL|GPL|AGPL|GNU|PSF|Python Software Foundation|Zlib|Unlicense|CC0|"
    r"HPND|Public Domain|OFL|Expat|Qt)\b",
    re.I,
)


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def classify(license_text: str) -> str:
    """`blocked`, `ok` or `unknown`. Blocked wins: a dual licence that offers a noncommercial option is still
    flagged, so the author must decide, not a regex (an AGPL-or-commercial dual licence is fine; see the tests)."""
    text = license_text or ""
    if _BLOCKED.search(text):
        return "blocked"
    return "ok" if _KNOWN.search(text) else "unknown"


def closure(lock: dict, extras: set[str]) -> set[str]:
    """Names reachable from the root package's dependencies plus the chosen extras, following each package's
    required (not optional) dependencies. Environment markers are ignored, which over-includes: the safe direction."""
    packages = {normalise(package["name"]): package for package in lock.get("package", [])}
    root = packages.get(ROOT_PACKAGE)
    if root is None:
        raise ValueError(f"{ROOT_PACKAGE!r} is not in the lockfile")
    start = [dep["name"] for dep in root.get("dependencies", [])]
    optional = root.get("optional-dependencies", {})
    unknown_extras = {extra for extra in extras if extra not in optional}
    if unknown_extras:
        raise ValueError(f"extras not defined in the lockfile: {sorted(unknown_extras)}")
    for extra in extras:
        start += [dep["name"] for dep in optional[extra]]

    seen: set[str] = set()
    pending = [normalise(name) for name in start]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        package = packages.get(name)
        if package:
            pending += [normalise(dep["name"]) for dep in package.get("dependencies", [])]
    return seen


def installed_license(name: str) -> str | None:
    """The licence text an installed distribution declares, or None if it is not installed."""
    try:
        meta = metadata.metadata(name)
    except metadata.PackageNotFoundError:
        return None
    parts = [meta.get("License-Expression") or "", meta.get("License") or ""]
    parts += [c.split("::", 1)[1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    return " | ".join(part for part in parts if part).strip() or ""


@dataclass(frozen=True)
class Row:
    name: str
    license: str | None
    verdict: str  # ok | blocked | unknown | not-installed


def inventory(lock: dict, extras: set[str], lookup=installed_license) -> list[Row]:
    rows = []
    for name in sorted(closure(lock, extras)):
        text = lookup(name)
        if text is None:
            rows.append(Row(name, None, "not-installed"))
        else:
            rows.append(Row(name, text, classify(text)))
    return rows


def failures(rows: list[Row]) -> list[Row]:
    return [row for row in rows if row.verdict != "ok"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--lock", default=str(Path(__file__).resolve().parent.parent / "uv.lock"))
    parser.add_argument("--extras", default="", help="comma-separated extras to include (default: none, the core install)")
    parser.add_argument("--all-extras", action="store_true", help="report each extra separately (informational, exit 0)")
    args = parser.parse_args(argv)

    lock = tomllib.loads(Path(args.lock).read_text(encoding="utf-8"))
    if args.all_extras:
        defined = sorted(next(p for p in lock["package"] if normalise(p["name"]) == ROOT_PACKAGE).get("optional-dependencies", {}))
        for extra in defined:
            rows = inventory(lock, {extra})
            print(f"[{extra}] {len(rows)} package(s), {len(failures(rows))} needing attention")
            for row in rows:
                print(f"    {row.verdict:13} {row.name}: {row.license}")
        return 0

    extras = {item.strip() for item in args.extras.split(",") if item.strip()}
    rows = inventory(lock, extras)
    for row in rows:
        print(f"{row.verdict:13} {row.name}: {row.license}")
    bad = failures(rows)
    print(f"license_inventory: {len(rows)} package(s) in the closure for extras {sorted(extras) or 'none'}, {len(bad)} needing attention")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
