"""The library window, driven through its real widgets: select a row, press a button, fill in a dialog, wait for the job, read the screen.

Offscreen (no display needed), against a real library with real extraction. What matters here is what a person would see and what ends
up in the catalog, so most tests press a button and then read BOTH the widgets and the catalog.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from guisupport import pump, wait_for
from lab import PAPER_DOI, PAPER_TITLE, Lab
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialogButtonBox, QMessageBox
from support import tree_hashes
from winhelp import (cell, close_dialogs, dialogs, error_texts, find_item, open_window, press, review_row, row_of, select_document, select_review, settle,
                     the_dialog, titles)

from knowledgevista.domain.candidate import CandidateSpec
from knowledgevista.gui import jobs as J
from knowledgevista.gui import state as S
from knowledgevista.gui import text as T
from knowledgevista.gui.models import ITEM_ROLE
from knowledgevista.services import metadata, organize


@pytest.fixture
def lab(qapp, tmp_path):
    made = Lab(tmp_path)
    yield made
    made.close()


@pytest.fixture
def window(lab):
    made = open_window(lab.catalog)
    yield made
    close_dialogs(made)
    made.shutdown()
    made.close()
    made.deleteLater()


# ------------------------------------------------------------------------------------------------------------ the first screen


def test_the_first_screen_puts_what_needs_a_person_first(window):
    assert titles(window) == ["books/book.pdf", "papers/a&amp;b.pdf", "papers/aqueous-copy.pdf", "papers/aqueous.pdf", "papers/second.pdf",
                              "scans/scan.pdf", "notes/readme.txt"]
    assert [cell(window.doc_table, r, 0) for r in range(7)] == ["Unresolved"] * 6 + ["New"]
    assert window.list_note.text() == "7 of 7 document(s) — All documents"
    assert window.revision_label.text().strip() == f"revision {window.revision}" and window.count_label.text().strip() == "7 documents"


def test_the_sidebar_counts_match_what_each_view_holds(window):
    def labels(parent):
        return [parent.child(i).text(0) for i in range(parent.childCount())]

    views = next(window.sidebar.topLevelItem(i) for i in range(window.sidebar.topLevelItemCount()) if window.sidebar.topLevelItem(i).text(0) == "Views")
    assert labels(views) == ["Inbox  (7)", "Unresolved  (6)", "Ambiguous  (0)", "Missing  (0)", "New  (3)", "Duplicates  (0)", "Untagged  (7)", "Uncollected  (7)"]
    assert window.sidebar.currentItem().text(0) == "All documents"
    folders = window.sidebar.topLevelItem(window.sidebar.topLevelItemCount() - 1)
    assert labels(folders) == ["lib  (online)"]


def test_the_review_tab_says_how_many_are_waiting(window):
    assert window.tabs.tabText(2) == "Review (11)"
    assert window.accept_safe_button.isEnabled() and not window.accept_button.isEnabled(), "nothing is selected, so nothing can be accepted yet"
    assert "11 shown of 11 waiting; 3 are safe for the batch rule." in window.review_note.text()


def test_nothing_in_the_window_is_a_job_a_person_asked_for_until_they_ask(window):
    assert window.jobs_table.rowCount() == 0, "the window's own background reads must not clutter the Jobs panel"


def test_an_empty_library_says_how_to_begin(qapp, tmp_path):
    from knowledgevista.gui.app import prepare

    catalog, _ = prepare(tmp_path / "empty.sqlite")
    made = open_window(catalog)
    try:
        assert made.doc_proxy.rowCount() == 0 and "No folders yet" in made.list_note.text() and "Add folder" in made.list_note.text()
        assert made.detail.empty.isVisibleTo(made.detail) and made.detail.document_id is None
    finally:
        made.shutdown()
        made.close()


# ------------------------------------------------------------------------------------------------------------ detail and "why?"


def test_selecting_a_document_shows_what_is_known_and_what_is_waiting(window):
    select_document(window, "papers/aqueous.pdf")
    rows = {r["field"]: r for r in window.detail.field_rows()}
    assert rows["Title"]["value"].startswith("—") and "1 proposed" in rows["Title"]["value"] and rows["Title"]["check"] == "waiting"
    assert window.detail.title.text() == "papers/aqueous.pdf" and "a reachable copy exists" in window.detail.sub.text()
    assert "lib/papers/aqueous.pdf  (active)" in window.detail.where.text()
    assert window.detail.open_button.isEnabled()


def test_why_opens_the_evidence_for_that_field(window):
    select_document(window, "papers/aqueous.pdf")
    press(window.detail.fields.cellWidget(0, 4))  # the DOI row's Why?
    wait_for(lambda: dialogs(window, "evidenceDialog"), what="the evidence window")
    text = the_dialog(window, "evidenceDialog").text()
    assert text.startswith("DOI") and PAPER_DOI in text and "a DOI printed in the document's text" in text
    assert "Unresolved: nothing is accepted yet" in text and "needs a person" in text and "excerpt:" in text


def test_why_is_disabled_for_a_field_nobody_has_touched(window):
    select_document(window, "papers/aqueous.pdf")
    rows = {window.detail.fields.item(r, 0).text(): window.detail.fields.cellWidget(r, 4) for r in range(window.detail.fields.rowCount())}
    assert rows["Title"].isEnabled() and not rows["Year"].isEnabled() and not rows["Journal / book"].isEnabled()


def test_an_accepted_value_shows_its_origin_badge_and_its_provenance(window, lab):
    document = lab.doc("papers/aqueous.pdf")
    metadata.set_value(lab.conn, document, "title", "Stated By A Person", lock=True)
    window.refresh()
    settle(window)
    select_document(window, "Stated By A Person")
    rows = {r["field"]: r for r in window.detail.field_rows()}
    assert rows["Title"]["value"] == "Stated By A Person" and rows["Title"]["source"] == "A locked"
    press(window.detail.fields.cellWidget(1, 4))
    wait_for(lambda: dialogs(window, "evidenceDialog"), what="evidence")
    assert "Accepted by you, from stated by a person (stated by a person). Locked" in the_dialog(window, "evidenceDialog").text()


# ------------------------------------------------------------------------------------------------------------ the review queue


def test_accepting_a_proposal_changes_the_catalog_and_the_screen(window, lab):
    document = lab.doc("papers/second.pdf")
    select_review(window, "papers/second.pdf", "Title")
    press(window.accept_button)
    settle(window)
    assert metadata.get_values(lab.conn, document)["title"]["value"] == "Partition Coefficients of Imaginary Amides"
    assert metadata.get_values(lab.conn, document)["title"]["accepted_by"] == "user"
    assert "Partition Coefficients of Imaginary Amides" in titles(window)
    assert window.tabs.tabText(2) == "Review (10)"
    assert any("Accepted Title" in m for _kind, m in window.messages)
    row = row_of(window, "Partition Coefficients of Imaginary Amides")
    assert cell(window.doc_table, row, 2) == "O", "the title was read from the file, and the badge says so"


def test_rejecting_a_proposal_keeps_the_record_and_accepts_nothing(window, lab):
    document = lab.doc("papers/second.pdf")
    select_review(window, "papers/second.pdf", "Title")
    press(window.reject_button)
    settle(window)
    assert "title" not in metadata.get_values(lab.conn, document)
    assert {c["status"] for c in metadata.get_candidates(lab.conn, document) if c["field"] == "title"} == {"rejected"}
    assert window.tabs.tabText(2) == "Review (10)"


def test_the_buttons_need_a_selection(window):
    window.tabs.setCurrentIndex(2)
    assert not (window.accept_button.isEnabled() or window.reject_button.isEnabled() or window.review_why_button.isEnabled())
    select_review(window, "papers/second.pdf", "Title")
    assert window.accept_button.isEnabled() and window.reject_button.isEnabled() and window.review_why_button.isEnabled()


def test_the_review_filter_narrows_to_what_the_batch_rule_would_take(window):
    window.review_filter.setCurrentIndex(1)
    assert window.review_proxy.rowCount() == 3 and {window.review_proxy.index(r, 6).data() for r in range(3)} == {"Safe for the batch rule"}
    window.review_filter.setCurrentIndex(2)
    assert window.review_proxy.rowCount() == 8
    window.review_filter.setCurrentIndex(0)
    assert window.review_proxy.rowCount() == 11


def test_a_locked_value_cannot_be_overwritten_from_the_window(window, lab):
    document = lab.doc("papers/second.pdf")
    metadata.set_value(lab.conn, document, "title", "Fixed By A Person", lock=True)
    window.refresh()
    settle(window)
    select_review(window, "Fixed By A Person", "Title")
    press(window.accept_button)
    settle(window)
    assert metadata.get_values(lab.conn, document)["title"]["value"] == "Fixed By A Person"
    assert any("locked" in m.lower() for m in error_texts(window)), "the person is told why nothing happened"
    assert window._boxes, "and a message stays until it is dismissed"


def test_accept_all_safe_asks_first_and_then_applies_the_batch_rule(window, lab):
    before = {d: metadata.get_values(lab.conn, lab.doc(d)).get("title") for d in ("papers/aqueous.pdf", "papers/second.pdf")}
    assert before == {"papers/aqueous.pdf": None, "papers/second.pdf": None}
    press(window.accept_safe_button)
    confirm = the_dialog(window, "confirmAcceptSafe")
    assert "Accept the 3 proposal(s)" in confirm.findChildren(__import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel)[0].text()
    assert metadata.get_values(lab.conn, lab.doc("papers/second.pdf")).get("title") is None, "asking is not doing"
    confirm.buttons.button(QDialogButtonBox.StandardButton.Yes).click()
    settle(window)
    assert metadata.get_values(lab.conn, lab.doc("papers/second.pdf"))["title"]["accepted_by"] == "rule:safe_batch_v1"
    assert metadata.get_values(lab.conn, lab.doc("papers/aqueous.pdf"))["title"]["value"] == PAPER_TITLE
    assert window.tabs.tabText(2) == "Review (8)" and any("batch rule" in m for _k, m in window.messages)


def test_cancelling_the_confirmation_changes_nothing(window, lab):
    before = lab.env.revision()
    press(window.accept_safe_button)
    the_dialog(window, "confirmAcceptSafe").buttons.button(QDialogButtonBox.StandardButton.Cancel).click()
    settle(window)
    assert lab.env.revision() == before and window.tabs.tabText(2) == "Review (11)"


# ------------------------------------------------------------------------------------------------------------ searching


def test_a_search_lists_pages_and_says_what_it_could_not_see(window):
    window.search_box.setText("aqueous")
    window.search_box.returnPressed.emit()
    settle(window)
    assert window.current_tab == "search" and window.hit_model.rowCount() > 0
    assert window.search_summary.text().startswith("19 page(s) in 4 document(s) for “aqueous”")
    assert "1 scan(s) with no text layer" in window.search_summary.text() and "A missing hit is not proof of absence." in window.search_summary.text()
    assert {cell(window.hit_table, r, 0) for r in range(window.hit_model.rowCount())} == {"papers/aqueous.pdf", "papers/aqueous-copy.pdf", "papers/second.pdf", "papers/a&amp;b.pdf"}


def test_a_malformed_search_says_so_instead_of_finding_nothing(window):
    for text, wanted in (("year:", "The filter year: has no value."), ("year:soon", "year: wants a year")):
        window.search_box.setText(text)
        window.search_box.returnPressed.emit()
        settle(window)
        assert window.hit_model.rowCount() == 0 and wanted in window.search_summary.text(), text
        assert window.messages[-1] == ("error", window.search_summary.text())
        assert window.current_tab == "search", "the person is taken to where the message is"


def test_a_search_with_a_filter_names_how_many_documents_it_searched(window, lab):
    metadata.set_value(lab.conn, lab.doc("papers/aqueous.pdf"), "year", "2020", lock=False)
    window.search_box.setText("aqueous year:2020")
    window.search_box.returnPressed.emit()
    settle(window)
    assert {cell(window.hit_table, r, 0) for r in range(window.hit_model.rowCount())} == {"papers/aqueous.pdf"}
    assert "Filters selected 1 document(s); only those were searched." in window.search_summary.text()


def test_activating_a_hit_opens_that_documents_file_and_says_the_page_is_on_you(window, lab):
    window.search_box.setText("aqueous")
    window.search_box.returnPressed.emit()
    settle(window)
    index = window.hit_table.model().index(0, 0)
    window.hit_table.doubleClicked.emit(index)
    settle(window)
    hit = index.data(ITEM_ROLE)
    assert window.shell.calls == [("open", str(lab.env.lib / hit["name"]))]
    assert f"Go to page {hit['pdf_page']} yourself" in window.messages[-1][1]


def test_selecting_a_hit_shows_its_document_in_the_detail_pane(window):
    window.search_box.setText("aqueous")
    window.search_box.returnPressed.emit()
    settle(window)
    window.hit_table.selectRow(0)
    settle(window)
    hit = window.hit_model.item_at(0)
    assert window.detail.document_id == hit["document_id"] and window.detail.title.text() == (hit["title"] or hit["name"])


def test_more_results_extends_the_list(window, monkeypatch):
    monkeypatch.setattr("knowledgevista.services.library_view.SEARCH_PAGE", 5)
    window.search_box.setText("aqueous")
    window.search_box.returnPressed.emit()
    settle(window)
    assert window.hit_model.rowCount() == 5 and not window.more_hits.isHidden()
    assert "more are available" in window.search_summary.text()
    press(window.more_hits)
    settle(window)
    assert window.hit_model.rowCount() == 10
    assert len({(h["artifact_id"], h["pdf_page"]) for h in window.search_state["hits"]}) == 10, "no page is listed twice"
    while not window.more_hits.isHidden():
        press(window.more_hits)
        settle(window)
    assert window.hit_model.rowCount() == 19 and "more are available" not in window.search_summary.text()


# ------------------------------------------------------------------------------------------------------------ the list


def test_the_filter_box_narrows_the_list_and_says_what_matched(window):
    window.filter_box.setText("aqueous")
    assert titles(window) == ["papers/aqueous-copy.pdf", "papers/aqueous.pdf"]
    assert window.list_note.text() == "2 of 7 document(s) — All documents"
    window.filter_box.setText("zzz-nothing")
    assert window.doc_proxy.rowCount() == 0 and "Nothing in All documents matches “zzz-nothing”." == window.list_note.text()
    window.filter_box.setText("")
    assert window.doc_proxy.rowCount() == 7


def test_the_filter_matches_every_word_anywhere_in_the_row(window, lab):
    metadata.set_value(lab.conn, lab.doc("papers/second.pdf"), "authors", [{"family": "Examplar", "given": "A."}], lock=False)
    window.refresh()
    settle(window)
    window.filter_box.setText("examplar second")
    assert titles(window) == ["papers/second.pdf"], "words may match different columns, all must match"
    window.filter_box.setText("examplar aqueous")
    assert titles(window) == [], "one word matching one document and another word matching a different one is not a match"


def test_sorting_by_a_column_and_then_resetting_returns_to_the_default_order(window):
    default = titles(window)
    window.doc_table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    assert titles(window) == sorted(default, key=str.casefold, reverse=True)
    press(window.findChild(type(window.accept_button), "resetOrderButton"))
    assert titles(window) == default
    window.doc_table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
    assert titles(window) == list(reversed(default)), "the Why column sorts by the default order, so descending is its reverse"


def test_a_sidebar_view_changes_what_the_list_shows(window, lab):
    missing = find_item(window.sidebar, "Missing  (0)")
    window.sidebar.setCurrentItem(missing)
    settle(window)
    assert window.scope == "view:missing" and window.doc_proxy.rowCount() == 0 and window.list_note.text() == "Missing is empty. That is good news."
    assert window.current_tab == "documents"


def test_a_collection_appears_in_the_sidebar_and_lists_its_members(window, lab):
    first, second = lab.doc("papers/aqueous.pdf"), lab.doc("papers/second.pdf")
    organize.new_collection(lab.conn, "Esters", [first, second])
    window.refresh()
    settle(window)
    item = find_item(window.sidebar, "Esters  (2)")
    window.sidebar.setCurrentItem(item)
    settle(window)
    assert titles(window) == ["papers/aqueous.pdf", "papers/second.pdf"] and window.list_note.text() == "2 of 7 document(s) — Esters"


def test_a_collection_deleted_elsewhere_falls_back_to_all_documents(window, lab):
    made = organize.new_collection(lab.conn, "Temporary", [lab.doc("papers/aqueous.pdf")])
    window.refresh()
    settle(window)
    window.sidebar.setCurrentItem(find_item(window.sidebar, "Temporary  (1)"))
    settle(window)
    organize.retire_collection(lab.conn, made["collection_id"])
    window.refresh()
    settle(window)
    assert window.scope == "all" and window.doc_proxy.rowCount() == 7
    assert any("no longer exists" in m for _k, m in window.messages)


# ------------------------------------------------------------------------------------------------------------ adding and scanning


def test_add_folder_adds_it_scans_it_and_leaves_organizing_off(qapp, tmp_path):
    from knowledgevista.gui.app import prepare

    library = tmp_path / "papers"
    (library / "sub").mkdir(parents=True)
    (library / "a.txt").write_text("alpha")
    (library / "sub" / "b.txt").write_text("beta")
    catalog, _ = prepare(tmp_path / "fresh.sqlite")
    made = open_window(catalog)
    try:
        made.act_add.trigger()
        dialog = the_dialog(made, "addRootDialog")
        assert not dialog.organize_check.isChecked(), "adding a folder to read is not permission to change it"
        assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled(), "no folder chosen yet"
        dialog.path_edit.setText(str(library))
        dialog.label_edit.setText("My papers")
        dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
        wait_for(lambda: made.doc_proxy.rowCount() == 2, what="the two documents to appear")
        settle(made)
        assert sorted(titles(made)) == ["a.txt", "sub/b.txt"]
        from knowledgevista.db.catalog import open_catalog

        conn = open_catalog(catalog, create=False, read_only=True)
        root = conn.execute("SELECT label, allow_organize, last_scan_at FROM root").fetchone()
        conn.close()
        assert (root["label"], root["allow_organize"]) == ("My papers", 0) and root["last_scan_at"]
        assert any(m.startswith("Scanned 2 file(s)") for _k, m in made.messages)
        assert made.state.last_folder == str(library)
    finally:
        made.shutdown()
        made.close()


def test_adding_the_same_folder_twice_says_why_not(window, lab):
    window.add_root(str(lab.env.lib))
    settle(window)
    assert any("already a root" in m for m in error_texts(window)) and window._boxes
    box = window._boxes[-1]
    assert isinstance(box, QMessageBox) and box.textFormat() == Qt.TextFormat.PlainText


def test_a_folder_that_does_not_exist_is_refused_with_a_message(window, tmp_path):
    window.add_root(str(tmp_path / "no-such-folder"))
    settle(window)
    assert any("is not a folder that exists right now" in m for m in error_texts(window))


def test_scan_notices_a_new_file_and_a_missing_one(window, lab):
    (lab.env.lib / "notes" / "new.txt").write_text("brand new")
    (lab.env.lib / "papers" / "second.pdf").unlink()
    press(window.findChild(type(window.accept_button), "resetOrderButton"))
    window.act_scan.trigger()
    wait_for(lambda: any(m.startswith("Scanned") for _k, m in window.messages), what="the scan to finish")
    settle(window)
    assert "notes/new.txt" in titles(window)
    missing = find_item(window.sidebar, "Missing  (1)")
    window.sidebar.setCurrentItem(missing)
    settle(window)
    assert titles(window) == ["papers/second.pdf"] and cell(window.doc_table, 0, 0) == "Missing"
    select_document(window, "papers/second.pdf")
    assert "NO reachable copy right now" in window.detail.sub.text() and not window.detail.open_button.isEnabled()


def test_pressing_scan_twice_scans_once(window):
    window.act_scan.trigger()
    window.act_scan.trigger()
    assert len([j for j in window.jobs.jobs() if j.kind == "scan"]) == 1
    settle(window)


def test_extract_then_resolve_make_a_library_searchable_and_propose_titles(qapp, tmp_path):
    made_lab = Lab(tmp_path, extract=False)
    made = open_window(made_lab.catalog)
    try:
        assert made.tabs.tabText(2) == "Review"
        made.act_extract.trigger()
        wait_for(lambda: any(m.startswith("Extracted text from 6 PDF(s)") for _k, m in made.messages), what="extraction")
        settle(made)
        made.act_resolve.trigger()
        wait_for(lambda: any(m.startswith("Read DOIs and titles from") for _k, m in made.messages), what="resolution")
        settle(made)
        assert made.tabs.tabText(2) == "Review (11)"
        made.search_box.setText("aqueous")
        made.search_box.returnPressed.emit()
        settle(made)
        assert made.hit_model.rowCount() > 0
    finally:
        made.shutdown()
        made.close()
        made_lab.close()


def test_resolve_with_nothing_extracted_says_what_to_do_first(qapp, tmp_path):
    made_lab = Lab(tmp_path, extract=False)
    made = open_window(made_lab.catalog)
    try:
        made.act_resolve.trigger()
        settle(made)
        assert made.messages[-1] == ("info", "Nothing could be read: none of the documents has extracted text yet. Use “Extract text” first, then Resolve.")
        assert made.tabs.tabText(2) == "Review", "and no proposals appeared"
    finally:
        made.shutdown()
        made.close()
        made_lab.close()


def test_resolve_on_a_library_that_was_never_extracted_is_refused_with_the_same_advice(qapp, tmp_path):
    from knowledgevista.gui.app import prepare

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.txt").write_text("alpha")
    catalog, _ = prepare(tmp_path / "x.sqlite")
    made = open_window(catalog)
    try:
        made.add_root(str(tmp_path / "docs"))
        settle(made)
        made.act_resolve.trigger()
        settle(made)
        assert any("Extract text first" in m for m in error_texts(made))
    finally:
        close_dialogs(made)
        made.shutdown()
        made.close()


def test_a_running_job_shows_in_the_jobs_panel_and_can_be_stopped(window):
    release = __import__("threading").Event()
    started = __import__("threading").Event()

    def slow(ctx):
        started.set()
        while not ctx.should_stop():
            release.wait(0.005)
        return {"extracted": 0, "complete": 0, "partial": 0, "failed": 0, "current": 0, "stopped": True}

    window.jobs.submit("extract", "Extract text", slow, lane=J.WRITE, once=True, on_done=lambda job: window._finished(job, "Extract text", lambda r: "stopped"))
    assert started.wait(10)
    pump(50)
    assert window.jobs_table.rowCount() == 1 and window.jobs_table.item(0, 0).text() == "Extract text" and window.jobs_table.item(0, 1).text() == "running"
    press(window.jobs_table.cellWidget(0, 3))
    assert window.jobs_table.item(0, 1).text() == "stopping", "pressing Stop answers at once, before the job has noticed"
    settle(window)
    assert window.jobs_table.item(0, 1).text() == "succeeded" and not window.jobs_table.cellWidget(0, 3).isEnabled()


# ------------------------------------------------------------------------------------------------------------ following the catalog


def test_a_change_made_elsewhere_appears_without_any_action(window, lab):
    select_document(window, "papers/second.pdf")
    before = window.revision
    metadata.set_value(lab.conn, lab.doc("papers/second.pdf"), "title", "Changed In Another Program", lock=False)
    wait_for(lambda: "Changed In Another Program" in titles(window), timeout_ms=8000, what="the window to notice the new revision")
    assert window.revision > before
    wait_for(lambda: window.detail.title.text() == "Changed In Another Program", what="the detail pane to follow")
    assert any("changed outside this window" in m for _k, m in window.messages)


def test_a_command_line_change_while_the_window_is_open_appears(window, lab):
    """The real thing: a separate `kv` process writes, and the open window follows through the catalog revision alone."""
    result = subprocess.run([sys.executable, "-m", "knowledgevista", "--catalog", str(lab.catalog), "tag", "add", "Reference", lab.doc("books/book.pdf")],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    wait_for(lambda: window.sidebar.findItems("Reference  (1)", Qt.MatchFlag.MatchExactly | Qt.MatchFlag.MatchRecursive), timeout_ms=8000,
             what="the new tag to appear in the sidebar")
    assert [t["label"] for t in window.sidebar_data["tags"]] == ["Reference"]


def test_polling_is_quiet_when_nothing_changed(window):
    before = len(window.messages)
    pump(700)
    settle(window)
    assert len(window.messages) == before and window.revision == window.listing.revision


def test_the_window_does_not_poll_while_it_is_writing_and_resumes_after(window):
    def polls():
        return sum(1 for j in window.jobs.jobs() if j.kind == "revision")

    release, started = __import__("threading").Event(), __import__("threading").Event()
    window.jobs.submit("scan", "Scan", lambda ctx: (started.set(), release.wait(10)), lane=J.WRITE)
    assert started.wait(10)
    before = polls()
    pump(600)
    assert polls() == before, "a poll during a long write would only show a half-finished state"
    release.set()
    settle(window)
    wait_for(lambda: polls() > before, what="polling to resume once the write finished")


# ------------------------------------------------------------------------------------------------------------ collections and tags


def test_adding_to_a_new_collection_then_an_existing_one(window, lab):
    select_document(window, "papers/aqueous.pdf")
    press(window.detail.collection_button)
    dialog = the_dialog(window, "collectionDialog")
    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    dialog.combo.setEditText("Esters")
    dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
    settle(window)
    assert [c["name"] for c in organize.list_collections(lab.conn)] == ["Esters"] and "Collections: Esters" in window.detail.organization.text()
    assert any("Started the collection “Esters”" in m for _k, m in window.messages)
    select_document(window, "papers/second.pdf")
    press(window.detail.collection_button)
    dialog = the_dialog(window, "collectionDialog") if len(dialogs(window, "collectionDialog")) == 1 else dialogs(window, "collectionDialog")[-1]
    assert [dialog.combo.itemText(i) for i in range(dialog.combo.count())] == ["Esters"]
    dialog.combo.setEditText("esters")
    dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
    settle(window)
    assert organize.list_collections(lab.conn)[0]["members"] == 2 and any("Added to “Esters”" in m for _k, m in window.messages)


def test_tagging_a_document(window, lab):
    select_document(window, "books/book.pdf")
    press(window.detail.tag_button)
    dialog = the_dialog(window, "tagDialog")
    dialog.combo.setEditText("Reference")
    dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
    settle(window)
    assert organize.tags_of(lab.conn, lab.doc("books/book.pdf")) == ["Reference"] and "Tags: Reference" in window.detail.organization.text()


# ------------------------------------------------------------------------------------------------------------ proposing a rename


def organizable(lab):
    lab.conn.execute("UPDATE root SET allow_organize = 1")
    lab.accept_title("papers/aqueous.pdf", PAPER_TITLE)
    metadata.set_value(lab.conn, lab.doc("papers/aqueous.pdf"), "year", "2021", lock=False)
    metadata.set_value(lab.conn, lab.doc("papers/aqueous.pdf"), "authors", [{"family": "Examplar", "given": "A."}], lock=False)


def test_a_rename_proposal_shows_old_and_new_and_changes_nothing(window, lab):
    organizable(lab)
    window.refresh()
    settle(window)
    before = tree_hashes(lab.env.lib)
    select_document(window, PAPER_TITLE)
    press(window.detail.rename_button)
    wait_for(lambda: dialogs(window, "renameDialog"), what="the proposal")
    dialog = the_dialog(window, "renameDialog")
    rows = dialog.rows()
    assert len(rows) == 1 and rows[0][0] == "papers/aqueous.pdf" and rows[0][1].endswith(".pdf") and rows[0][1] != "papers/aqueous.pdf"
    assert "Examplar" in rows[0][1] and "2021" in rows[0][1] and rows[0][2] in ("rename", "rename+move")
    assert dialog.summary.text().startswith("1 file(s) would be renamed or moved.") and "Nothing has been renamed." in dialog.summary.text()
    assert tree_hashes(lab.env.lib) == before and (lab.env.lib / "papers" / "aqueous.pdf").exists(), "a proposal moves nothing"


def test_saving_the_plan_writes_a_file_outside_the_library_and_still_moves_nothing(window, lab, tmp_path):
    organizable(lab)
    window.refresh()
    settle(window)
    before = tree_hashes(lab.env.lib)
    select_document(window, PAPER_TITLE)
    press(window.detail.rename_button)
    wait_for(lambda: dialogs(window, "renameDialog"), what="the proposal")
    dialog = the_dialog(window, "renameDialog")
    press(dialog.save_button)
    wait_for(lambda: "Plan saved to" in dialog.footer.text(), what="the plan to be saved")
    path = dialog.footer.text().split("Plan saved to ")[1].split(". Nothing")[0]
    plan = json.loads(open(path, encoding="utf-8").read())
    assert plan["plan_format"] == 1 and plan["summary"]["status"] == {"planned": 1} and "hash" in plan
    assert lab.env.lib not in __import__("pathlib").Path(path).parents, "a plan lives outside the library it describes"
    assert lab.conn.execute("SELECT COUNT(*) FROM plan_registry").fetchone()[0] == 1
    assert tree_hashes(lab.env.lib) == before and not dialog.save_button.isEnabled()


def test_a_folder_that_does_not_allow_organizing_says_how_to_allow_it(window, lab):
    lab.accept_title("papers/aqueous.pdf", PAPER_TITLE)
    window.refresh()
    settle(window)
    select_document(window, PAPER_TITLE)
    press(window.detail.rename_button)
    settle(window)
    assert any("does not allow organizing" in m and "kv root allow-organize" in m for m in error_texts(window))
    assert not dialogs(window, "renameDialog")


def test_a_document_with_no_accepted_title_proposes_nothing(window, lab):
    lab.conn.execute("UPDATE root SET allow_organize = 1")
    select_document(window, "papers/second.pdf")
    press(window.detail.rename_button)
    wait_for(lambda: dialogs(window, "renameDialog"), what="the proposal")
    dialog = the_dialog(window, "renameDialog")
    assert dialog.summary.text().startswith("Nothing would change.") and not dialog.save_button.isEnabled()
    assert dialog.rows()[0][3] == "no accepted title: left as it is"


# ------------------------------------------------------------------------------------------------------------ opening a file


def test_open_hands_the_current_path_to_the_shell(window, lab):
    select_document(window, "papers/second.pdf")
    press(window.detail.open_button)
    settle(window)
    assert window.shell.calls == [("open", str(lab.env.lib / "papers" / "second.pdf"))]
    press(window.detail.reveal_button)
    settle(window)
    assert window.shell.calls[-1] == ("reveal", str(lab.env.lib / "papers" / "second.pdf"))


def test_open_after_an_outside_rename_says_so_until_the_next_scan_finds_it(window, lab):
    select_document(window, "papers/second.pdf")
    (lab.env.lib / "papers" / "second.pdf").rename(lab.env.lib / "papers" / "renamed.pdf")
    press(window.detail.open_button)
    settle(window)
    assert window.shell.calls == [] and any("no reachable copy" in m.lower() for m in error_texts(window)), "the catalog does not know the new name yet"
    close_dialogs(window)
    window.act_scan.trigger()
    wait_for(lambda: any(m.startswith("Scanned") for _k, m in window.messages), what="the scan")
    settle(window)
    select_document(window, "papers/renamed.pdf")
    press(window.detail.open_button)
    settle(window)
    assert window.shell.calls == [("open", str(lab.env.lib / "papers" / "renamed.pdf"))]


def test_open_with_a_missing_file_says_so_and_launches_nothing(window, lab):
    select_document(window, "papers/second.pdf")
    (lab.env.lib / "papers" / "second.pdf").unlink()
    press(window.detail.open_button)
    settle(window)
    assert window.shell.calls == [] and error_texts(window)


# ------------------------------------------------------------------------------------------------------------ text from outside is text


MARKUP = '<b>bold</b> &amp; <img src="http://example.invalid/leak.png"> <script>alert(1)</script>'


def test_markup_from_outside_is_shown_as_the_letters_it_is(window, lab):
    document = lab.doc("papers/aqueous.pdf")
    organize.new_collection(lab.conn, MARKUP, [document])
    organize.add_tags(lab.conn, [document], [MARKUP])
    artifact = lab.env.one("SELECT artifact_id FROM document_artifact WHERE document_id = ?", document)
    metadata.upsert_candidate(lab.conn, document, artifact, CandidateSpec("year", "2019", "observed", "pdf_text_doi", "hostile-1", confidence="low",
                                                                          evidence={"excerpt": MARKUP, "page": 1}), None, "test")
    window.refresh()
    settle(window)
    select_document(window, "papers/aqueous.pdf")
    assert window.detail.organization.text() == f"Collections: {MARKUP}\nTags: {MARKUP}"
    assert window.detail.organization.textFormat() == Qt.TextFormat.PlainText
    assert find_item(window.sidebar, f"{MARKUP}  (1)").text(0) == f"{MARKUP}  (1)"
    year_why = next(window.detail.fields.cellWidget(r, 4) for r in range(window.detail.fields.rowCount()) if window.detail.fields.item(r, 0).text() == "Year")
    press(year_why)
    wait_for(lambda: dialogs(window, "evidenceDialog"), what="evidence")
    assert f"excerpt: {MARKUP}" in the_dialog(window, "evidenceDialog").text()
    assert not T.audit(window), T.audit(window)


def test_a_file_name_that_looks_like_markup_is_the_letters_it_is(window):
    """`a&amp;b.pdf` is a legal name: shown as typed (an HTML view would print `a&b`), and escaped once, correctly, in its tooltip."""
    row = row_of(window, "papers/a&amp;b.pdf")
    assert window.doc_proxy.index(row, 1).data(Qt.ItemDataRole.DisplayRole) == "papers/a&amp;b.pdf"
    assert window.doc_proxy.index(row, 8).data(Qt.ItemDataRole.DisplayRole) == "papers/a&amp;b.pdf"
    tip = window.doc_proxy.index(row, 1).data(Qt.ItemDataRole.ToolTipRole)
    assert tip.startswith(T.TOOLTIP_PREFIX) and "papers/a&amp;amp;b.pdf" in tip


def test_a_proposal_whose_evidence_is_markup_has_an_escaped_tooltip(window, lab):
    document = lab.doc("papers/aqueous.pdf")
    artifact = lab.env.one("SELECT artifact_id FROM document_artifact WHERE document_id = ?", document)
    metadata.upsert_candidate(lab.conn, document, artifact, CandidateSpec("year", "2019", "observed", "pdf_text_doi", "hostile-2", confidence="low",
                                                                          evidence={"excerpt": MARKUP}), None, "test")
    window.refresh()
    settle(window)
    row = review_row(window, "papers/aqueous.pdf", "Year")
    tip = window.review_proxy.index(row, 2).data(Qt.ItemDataRole.ToolTipRole)
    assert "<b>" not in tip and "<img" not in tip and "<script" not in tip and "&lt;b&gt;bold&lt;/b&gt;" in tip and tip.startswith(T.TOOLTIP_PREFIX)


def test_the_audit_finds_a_label_that_could_show_markup(window):
    from PySide6.QtWidgets import QLabel

    rogue = QLabel("<b>x</b>", window)
    rogue.setObjectName("rogue")
    rogue.setTextFormat(Qt.TextFormat.RichText)
    rogue.setToolTip("<i>tip</i>")
    from PySide6.QtWidgets import QTextEdit

    rich = QTextEdit(window)
    rich.setObjectName("richRogue")
    problems = T.audit(window)
    assert any("rogue" in p and "RichText" in p for p in problems) and any("tooltip Qt would render as HTML" in p for p in problems)
    assert any("richRogue" in p and "rich text widget" in p for p in problems)
    rogue.deleteLater()
    rich.deleteLater()


def test_the_whole_window_passes_the_audit_in_every_state(window, lab):
    assert T.audit(window) == []
    for tab in range(4):
        window.tabs.setCurrentIndex(tab)
        settle(window)
        assert T.audit(window) == []
    select_document(window, "papers/aqueous.pdf")
    window.search_box.setText("aqueous")
    window.search_box.returnPressed.emit()
    settle(window)
    assert T.audit(window) == []


# ------------------------------------------------------------------------------------------------------------ remembering


def test_the_window_remembers_where_a_person_was(qapp, lab, tmp_path):
    state_path = tmp_path / "state" / "gui.json"
    first = open_window(lab.catalog, remember=True, state_path=state_path)
    select_document(first, "papers/aqueous.pdf")
    first.filter_box.setText("papers")
    first.search_box.setText("aqueous")
    first.search_box.returnPressed.emit()
    settle(first)
    first.tabs.setCurrentIndex(2)
    first.doc_table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    first.close()
    saved = S.load(state_path)
    assert (saved.scope, saved.filter_text, saved.search_text, saved.tab, saved.sort_column, saved.sort_descending) == ("all", "papers", "aqueous", "review", 1, True)
    assert saved.document_id == lab.doc("papers/aqueous.pdf") and saved.geometry and saved.layout

    second = open_window(lab.catalog, remember=True, state_path=state_path)
    try:
        assert second.current_tab == "review" and second.filter_box.text() == "papers" and second.search_box.text() == "aqueous"
        wait_for(lambda: second.detail.document_id == lab.doc("papers/aqueous.pdf"), what="the selected document to be shown again")
        assert titles(second) == sorted(titles(second), key=str.casefold, reverse=True)
        wait_for(lambda: second.hit_model.rowCount() > 0, what="the remembered search")
    finally:
        second.shutdown()
        second.close()


def test_a_selection_from_another_library_is_not_applied(qapp, lab, tmp_path):
    state_path = tmp_path / "gui.json"
    S.save(S.GuiState(library_id="some-other-library", scope="collection:abc", document_id="f" * 32, filter_text="zzz", tab="review", geometry=None), state_path)
    made = open_window(lab.catalog, remember=True, state_path=state_path)
    try:
        assert made.scope == "all" and made.filter_box.text() == "" and made.current_tab == "documents" and made.current_document is None
        assert made.doc_proxy.rowCount() == 7
    finally:
        made.shutdown()
        made.close()


def test_closing_stops_a_running_job_and_does_not_hang(qapp, lab):
    made = open_window(lab.catalog)
    started = __import__("threading").Event()

    def forever(ctx):
        started.set()
        while True:
            ctx.check()
            pump(0)
            __import__("time").sleep(0.002)

    job = made.jobs.submit("scan", "Scan", forever, lane=J.WRITE)
    assert started.wait(10)
    made.close()
    assert job.state == J.CANCELLED


def test_a_damaged_state_file_does_not_stop_the_window_opening(qapp, lab, tmp_path, caplog):
    state_path = tmp_path / "gui.json"
    state_path.write_text("not json at all", encoding="utf-8")
    made = open_window(lab.catalog, remember=True, state_path=state_path)
    try:
        assert made.doc_proxy.rowCount() == 7
    finally:
        made.shutdown()
        made.close()
    assert any("damaged" in r.getMessage() for r in caplog.records)


def test_a_locked_title_is_marked_in_the_table(window, lab):
    metadata.set_value(lab.conn, lab.doc("papers/aqueous.pdf"), "title", "Locked Title", lock=True)
    metadata.set_value(lab.conn, lab.doc("papers/second.pdf"), "title", "Open Title", lock=False)
    window.refresh()
    settle(window)
    assert cell(window.doc_table, row_of(window, "Locked Title"), 2) == "A locked"
    assert cell(window.doc_table, row_of(window, "Open Title"), 2) == "A"


def test_accept_all_safe_is_only_offered_when_something_is_safe(window):
    assert window.accept_safe_button.isEnabled()
    press(window.accept_safe_button)
    the_dialog(window, "confirmAcceptSafe").buttons.button(QDialogButtonBox.StandardButton.Yes).click()
    settle(window)
    assert not window.accept_safe_button.isEnabled(), "nothing safe is left, so there is nothing to accept"


def test_a_detail_for_a_document_no_longer_selected_is_not_shown(window, lab):
    from knowledgevista.services import library_view as lv

    select_document(window, "papers/second.pdf")
    shown = window.detail.title.text()
    reader = lab.reader()
    stale = J.Job(99, "detail", "Load details", J.READ)
    stale.state = J.SUCCEEDED
    stale.result = {**lv.document_detail(reader, lab.index, window.current_document), "document_id": "0" * 32, "title": "SOMEONE ELSE"}
    window._on_detail(stale)
    assert window.detail.title.text() == shown and window.detail.document_id == window.current_document


def test_shutting_down_twice_does_nothing_the_second_time(qapp, lab, tmp_path):
    state_path = tmp_path / "state.json"
    made = open_window(lab.catalog, remember=True, state_path=state_path)
    made.filter_box.setText("first")
    assert made.shutdown() is True
    made.filter_box.setText("second")
    assert made.shutdown() is True
    assert S.load(state_path).filter_text == "first", "the second shutdown must not save again"
    made.close()
