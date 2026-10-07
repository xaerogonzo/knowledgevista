"""The Health tab's text: what the library holds, kept apart from what needs attention."""

from __future__ import annotations

import copy

from knowledgevista.domain import health_view as hv

REPORT = {
    "inventory": {
        "roots": 1, "documents": 1862, "documents_merged_away": 2, "artifacts": 1841, "locations_current": 1862, "locations_by_state": {"active": 1862},
        "locations_ended": 9, "total_bytes": 2_300_000_000, "by_extension_kind": {"pdf": 739, "text": 430}, "by_content_kind": {"pdf": 739},
        "roots_detail": [{"label": "Sci Downloads", "status": "online", "last_scan_at": "2026-10-07T09:00:00Z", "locations": 1862}],
    },
    "health": {
        "locations_missing": 0, "locations_inaccessible": 0, "roots_not_online": 0, "exact_duplicate_groups": 0, "exact_duplicate_files": 0,
        "exact_duplicate_bytes_reclaimable": 0, "extension_content_mismatches": 0, "interrupted_scans": 0, "catalog_revision": 42,
    },
    "search": {"pdf_documents": 739, "searchable": 700, "partially_indexed": 0, "provisional_imported": 0, "extraction_failed": 0, "not_yet_extracted": 30,
               "no_text_layer": 9, "other_files_not_searchable": 1100},
    "metadata": {"pdf_documents": 739, "accepted": {"title": 400, "doi": 393}, "doi_coverage": 0.5318, "title_coverage": 0.5413, "titles_without": {"no_title_found": 4},
                 "title_reasons": {"no_title_found": "nothing in the file or its metadata looked like a title"},
                 "review_queue": {"proposed_items": 120, "safe": 80, "required": 40}},
    "organization": {"collections": 2, "tags": 3, "saved_searches": 1, "relation_proposals_waiting": 5, "document_relations": 0, "artifact_relations": 0,
                     "documents_with_several_artifacts": 0},
}


def changed(**sections):
    report = copy.deepcopy(REPORT)
    for section, values in sections.items():
        report[section].update(values)
    return report


def test_the_inventory_counts_what_is_held_and_never_says_ok():
    text = hv.inventory_text(REPORT)
    assert text.splitlines()[0] == "1,862 documents in 1 folder(s)"
    assert "1,841 distinct files (by content), 1,862 paths, 2.30 GB" in text and "2 document(s) were merged into others" in text
    assert "By type: pdf 739, text 430" in text and "Folder Sci Downloads: 1,862 paths, online, last scanned 2026-10-07T09:00:00Z" in text
    assert "Of 739 PDFs: 400 have an accepted title (54%), 393 an accepted DOI (53%)" in text
    assert "Nothing is" not in text and "missing" not in text, "the inventory counts; judging is the health text's job"


def test_a_healthy_library_says_so_in_one_line():
    text = hv.health_text(REPORT)
    assert text.startswith("Nothing is missing, unreadable, mismatched or interrupted.")
    assert "files" not in text.splitlines()[0], "the health never counts files"


def test_each_problem_is_a_line_with_its_count():
    text = hv.health_text(changed(health={"locations_missing": 3, "locations_inaccessible": 1, "roots_not_online": 1, "extension_content_mismatches": 2, "interrupted_scans": 1}))
    for line in ("3 path(s) whose file is missing", "1 path(s) that cannot be read", "1 folder(s) not reachable right now",
                 "2 file(s) whose name and contents disagree", "1 interrupted scan(s)"):
        assert line in text
    assert "Nothing is missing" not in text


def test_duplicates_say_how_much_space_they_hold():
    text = hv.health_text(changed(health={"exact_duplicate_groups": 17, "exact_duplicate_files": 40, "exact_duplicate_bytes_reclaimable": 56_000_000}))
    assert "17 file(s) exist at more than one path (40 paths, 56.0 MB reclaimable)" in text
    assert "Nothing is missing" in text, "duplicates are a fact about the library, not damage"


def test_search_coverage_lists_only_what_is_wrong_with_it():
    text = hv.health_text(REPORT)
    assert "700 of 739 PDFs are searchable" in text and "30 not extracted yet (Extract text)" in text and "9 scans with no text layer" in text
    assert "only partly indexed" not in text and "failed to extract" not in text
    assert "1,100 file(s) that are not PDFs (not searchable in this version)" in text


def test_waiting_work_splits_what_a_rule_may_take_from_what_needs_a_person():
    text = hv.health_text(REPORT)
    assert "120 proposed value(s): 80 the batch rule may accept, 40 need a person" in text
    assert "5 proposed relation(s) between documents" in text and "4 PDF(s) without a title: nothing in the file or its metadata looked like a title" in text
    assert text.rstrip().endswith("Catalog revision 42")


def test_a_library_with_no_pdfs_has_no_percentages():
    report = changed(metadata={"pdf_documents": 0, "doi_coverage": None, "title_coverage": None, "accepted": {}})
    assert "Of 0 PDFs" not in hv.inventory_text(report)
    assert hv._percent(None) == "not applicable"


def test_sizes_switch_unit_at_a_gigabyte():
    assert hv._megabytes(999_000_000) == "999.0 MB" and hv._megabytes(1_000_000_000) == "1.00 GB"
