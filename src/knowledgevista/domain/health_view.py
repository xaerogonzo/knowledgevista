"""The Health tab's text: what the library HOLDS (inventory) and what NEEDS ATTENTION (health), as two separate pieces.

"1,862 files" and "7 need attention" are different questions, and a screen that blends them answers neither. The inventory never
says "ok" and the health never counts files; each is complete without the other. Pure: it formats a `stats.library_stats` report.
"""

from __future__ import annotations

from typing import Any


def _percent(value: float | None) -> str:
    return "not applicable" if value is None else f"{value:.0%}"


def _megabytes(size: int) -> str:
    return f"{size / 1e6:,.1f} MB" if size < 1e9 else f"{size / 1e9:,.2f} GB"


def inventory_text(report: dict[str, Any]) -> str:
    inv, meta = report["inventory"], report["metadata"]
    lines = [
        f"{inv['documents']:,} documents in {inv['roots']} folder(s)",
        f"{inv['artifacts']:,} distinct files (by content), {inv['locations_current']:,} paths, {_megabytes(inv['total_bytes'])}",
    ]
    if inv["documents_merged_away"]:
        lines.append(f"{inv['documents_merged_away']:,} document(s) were merged into others (kept in history)")
    if inv["by_extension_kind"]:
        lines.append("By type: " + ", ".join(f"{kind} {count:,}" for kind, count in inv["by_extension_kind"].items()))
    for root in inv["roots_detail"]:
        lines.append(f"Folder {root['label']}: {root['locations']:,} paths, {root['status']}" + (f", last scanned {root['last_scan_at']}" if root["last_scan_at"] else ", never scanned"))
    if meta["pdf_documents"]:
        lines.append(f"Of {meta['pdf_documents']:,} PDFs: {meta['accepted'].get('title', 0):,} have an accepted title ({_percent(meta['title_coverage'])}), "
                     f"{meta['accepted'].get('doi', 0):,} an accepted DOI ({_percent(meta['doi_coverage'])})")
    org = report["organization"]
    lines.append(f"{org['collections']} collection(s), {org['tags']} tag(s), {org['saved_searches']} saved search(es)")
    return "\n".join(lines)


def health_text(report: dict[str, Any]) -> str:
    health, search, meta, org = report["health"], report["search"], report["metadata"], report["organization"]
    lines = []
    problems = [
        (health["locations_missing"], "path(s) whose file is missing"), (health["locations_inaccessible"], "path(s) that cannot be read"),
        (health["roots_not_online"], "folder(s) not reachable right now"), (health["extension_content_mismatches"], "file(s) whose name and contents disagree"),
        (health["interrupted_scans"], "interrupted scan(s)"),
    ]
    lines += [f"{count:,} {label}" for count, label in problems if count]
    if not lines:
        lines.append("Nothing is missing, unreadable, mismatched or interrupted.")
    if health["exact_duplicate_groups"]:  # a fact about the library, not damage: it never makes the line above untrue
        lines.append(f"{health['exact_duplicate_groups']:,} file(s) exist at more than one path ({health['exact_duplicate_files']:,} paths, {_megabytes(health['exact_duplicate_bytes_reclaimable'])} reclaimable)")
    lines.append("")
    lines.append("Search coverage")
    lines.append(f"  {search['searchable']:,} of {search['pdf_documents']:,} PDFs are searchable")
    for key, label in (("not_yet_extracted", "not extracted yet (Extract text)"), ("no_text_layer", "scans with no text layer"),
                       ("partially_indexed", "only partly indexed"), ("extraction_failed", "failed to extract")):
        if search[key]:
            lines.append(f"  {search[key]:,} {label}")
    if search["other_files_not_searchable"]:
        lines.append(f"  {search['other_files_not_searchable']:,} file(s) that are not PDFs (not searchable in this version)")
    waiting = meta["review_queue"]
    lines.append("")
    lines.append("Waiting for a person")
    lines.append(f"  {waiting['proposed_items']:,} proposed value(s): {waiting['safe']:,} the batch rule may accept, {waiting['required']:,} need a person")
    if org["relation_proposals_waiting"]:
        lines.append(f"  {org['relation_proposals_waiting']:,} proposed relation(s) between documents")
    for reason, count in sorted(meta["titles_without"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {count:,} PDF(s) without a title: {meta['title_reasons'].get(reason, reason)}")
    lines.append("")
    lines.append(f"Catalog revision {health['catalog_revision']}")
    return "\n".join(lines)
