"""The read model behind the library window: the document list and its order, one document's detail, "why this value?", the review
queue and a page search. Real library, real extraction, real catalog; the window itself is tested separately."""

from __future__ import annotations

import pytest
from lab import PAPER_DOI, PAPER_TITLE, Lab

from knowledgevista.domain.candidate import CandidateSpec
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.services import library_view as lv
from knowledgevista.services import metadata, organize, relations
from knowledgevista.services.scan import scan_root


@pytest.fixture
def lab(tmp_path):
    made = Lab(tmp_path)
    yield made
    made.close()


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    """A lab nothing in the module changes, for the tests that only read."""
    made = Lab(tmp_path_factory.mktemp("shared"))
    yield made
    made.close()


def reasons(listing):
    return [row.reason for row in listing.rows]


# ---------------------------------------------------------------------------------------------------- the document list


def test_every_live_document_is_listed_once(shared):
    listing = lv.list_documents(shared.reader(), shared.index)
    assert sorted(row.name for row in listing.rows) == sorted([
        "books/book.pdf", "notes/readme.txt", "papers/a&amp;b.pdf", "papers/aqueous-copy.pdf", "papers/aqueous.pdf", "papers/second.pdf", "scans/scan.pdf"])
    assert listing.total_documents == 7 and len(set(listing.ids())) == 7


def test_the_default_order_is_inbox_first_then_alphabetical(lab):
    """unresolved, ambiguous, missing, new, THEN everything else alphabetically (docs/ARCHITECTURE.md, milestone 7)."""
    lab.accept_title("papers/aqueous.pdf", "Alpha Paper")
    lab.accept_title("papers/second.pdf", "Zeta Paper")
    copy = lab.accept_title("papers/aqueous-copy.pdf", "Middle Paper")
    second = lab.doc("papers/second.pdf")
    artifact = lab.env.one("SELECT artifact_id FROM document_artifact WHERE document_id = ?", second)
    metadata.upsert_candidate(lab.conn, second, artifact, CandidateSpec(
        "doi", "10.5555/kv.ambiguous", "observed", "pdf_text_doi", "ambiguous-1", classification="ambiguous", confidence="ambiguous"), None, "test")
    (lab.env.lib / "papers" / "aqueous-copy.pdf").unlink()
    scan_root(lab.conn, lab.env.root.root_id)

    listing = lv.list_documents(lab.reader(), lab.index)
    assert [(row.name, row.reason) for row in listing.rows] == [
        ("books/book.pdf", "unresolved"), ("papers/a&amp;b.pdf", "unresolved"), ("scans/scan.pdf", "unresolved"),
        ("papers/second.pdf", "ambiguous"), ("papers/aqueous-copy.pdf", "missing"), ("notes/readme.txt", "new"),
        ("papers/aqueous.pdf", None),
    ]
    assert [row.rank for row in listing.rows] == list(range(7))
    assert next(r for r in listing.rows if r.document_id == copy).available is False


def test_the_rest_sorts_alphabetically_by_title_ignoring_case(lab):
    """The leftovers (no inbox reason) order by what a person reads, which is the title, not the path."""
    for path, title in (("papers/aqueous.pdf", "banana"), ("papers/second.pdf", "Apple"), ("papers/aqueous-copy.pdf", "cherry"),
                        ("papers/a&amp;b.pdf", "Date"), ("books/book.pdf", "Elder"), ("scans/scan.pdf", "fig")):
        lab.accept_title(path, title)
    listing = lv.list_documents(lab.reader(), lab.index)
    tail = [row.display_title for row in listing.rows if row.reason is None]
    assert tail == ["Apple", "banana", "cherry", "Date", "Elder", "fig"]


def test_a_row_describes_its_document(lab):
    document = lab.accept_title("papers/aqueous.pdf", "Alpha Paper")
    metadata.set_value(lab.conn, document, "year", "2020", lock=False)
    row = next(r for r in lv.list_documents(lab.reader(), lab.index).rows if r.document_id == document)
    assert (row.title, row.title_origin, row.title_locked, row.year, row.kind, row.root, row.available, row.copies) == (
        "Alpha Paper", "assigned", False, "2020", "pdf", "lib", True, 1)
    assert row.size and row.size > 0 and row.waiting >= 1, "the DOI and authors proposals are still waiting"


def test_a_locked_title_says_so(lab):
    document = lab.doc("papers/aqueous.pdf")
    metadata.set_value(lab.conn, document, "title", "Locked One", lock=True)
    assert next(r for r in lv.list_documents(lab.reader(), lab.index).rows if r.document_id == document).title_locked is True


def test_an_untitled_document_is_called_by_its_path(shared):
    row = next(r for r in lv.list_documents(shared.reader(), shared.index).rows if r.name == "papers/second.pdf")
    assert row.title is None and row.display_title == "papers/second.pdf"


def test_the_non_pdf_is_listed_with_its_own_kind(shared):
    row = next(r for r in lv.list_documents(shared.reader(), shared.index).rows if r.name == "notes/readme.txt")
    assert row.kind == "text" and row.reason == "new"


def test_a_merged_away_document_is_not_listed(lab):
    keep, absorb = lab.doc("papers/aqueous.pdf"), lab.doc("papers/aqueous-copy.pdf")
    relations.merge(lab.conn, keep, absorb)
    listing = lv.list_documents(lab.reader(), lab.index)
    assert absorb not in listing.ids() and keep in listing.ids()
    assert listing.total_documents == 6
    assert len(next(r for r in listing.rows if r.document_id == keep).name) > 0
    assert next(r for r in listing.rows if r.document_id == keep).copies == 2, "both files now belong to the surviving document"


def test_the_listing_carries_the_revision_it_was_read_at(lab):
    before = lv.list_documents(lab.reader(), lab.index).revision
    lab.accept_title("papers/aqueous.pdf", "Alpha Paper")
    assert lv.list_documents(lab.reader(), lab.index).revision > before


# ---------------------------------------------------------------------------------------------------------------- scopes


def test_a_view_scope_lists_only_that_view_with_its_own_reason(lab):
    lab.accept_title("papers/second.pdf", "Zeta Paper")
    second = lab.doc("papers/second.pdf")
    artifact = lab.env.one("SELECT artifact_id FROM document_artifact WHERE document_id = ?", second)
    metadata.upsert_candidate(lab.conn, second, artifact, CandidateSpec(
        "doi", "10.5555/kv.ambiguous", "observed", "pdf_text_doi", "ambiguous-1", classification="ambiguous", confidence="ambiguous"), None, "test")
    listing = lv.list_documents(lab.reader(), lab.index, "view:ambiguous")
    assert listing.ids() == [second] and listing.rows[0].reason == "ambiguous"
    assert listing.total_documents == 7, "the total is the library's, not the scope's"


def test_inside_a_view_the_reason_is_that_views_even_if_the_inbox_files_it_earlier(lab):
    """An untitled PDF is `unresolved` in the inbox; opened from the New view it must say `new` if it is there."""
    listing = lv.list_documents(lab.reader(), lab.index, "view:new")
    assert {row.reason for row in listing.rows} == {"new"}


def test_a_collection_scope(lab):
    first, second = lab.doc("papers/aqueous.pdf"), lab.doc("papers/second.pdf")
    made = organize.new_collection(lab.conn, "Esters", [first, second])
    assert set(lv.list_documents(lab.reader(), lab.index, f"collection:{made['collection_id']}").ids()) == {first, second}
    assert set(lv.list_documents(lab.reader(), lab.index, "collection:esters").ids()) == {first, second}, "by name, any case"


def test_a_tag_scope(lab):
    document = lab.doc("books/book.pdf")
    organize.add_tags(lab.conn, [document], ["Reference"])
    assert lv.list_documents(lab.reader(), lab.index, "tag:reference").ids() == [document]


def test_an_unknown_collection_is_an_error_not_an_empty_list(lab):
    with pytest.raises(KvError) as caught:
        lv.list_documents(lab.reader(), lab.index, "collection:nothing-by-this-name")
    assert caught.value.code == ErrorCode.NOT_FOUND


@pytest.mark.parametrize("scope", ["", "views:inbox", "view:", "collection:", "everything"])
def test_a_malformed_scope_is_refused(shared, scope):
    if scope == "":
        assert lv.parse_scope(scope) == ("all", None)
        return
    with pytest.raises(KvError) as caught:
        lv.parse_scope(scope)
    assert caught.value.code == ErrorCode.INVALID_ARGUMENTS


# --------------------------------------------------------------------------------------------------------------- sidebar


def test_the_sidebar_counts_agree_with_the_views(lab):
    document = lab.doc("books/book.pdf")
    organize.new_collection(lab.conn, "Shelf", [document])
    organize.add_tags(lab.conn, [document], ["Reference"])
    side = lv.sidebar(lab.reader(), lab.index)
    assert side["documents"] == 7
    counts = {v["scope"]: v["count"] for v in side["views"]}
    for scope, count in counts.items():
        assert count == len(lv.list_documents(lab.reader(), lab.index, scope).rows), scope
    assert [v["label"] for v in side["views"]][:2] == ["Inbox", "Unresolved"]
    assert [(c["label"], c["count"]) for c in side["collections"]] == [("Shelf", 1)]
    assert [(t["label"], t["count"]) for t in side["tags"]] == [("Reference", 1)]
    assert [r["label"] for r in side["roots"]] == ["lib"] and side["roots"][0]["status"] == "online"


# ---------------------------------------------------------------------------------------------------------------- detail


def test_detail_says_what_is_known_and_what_is_waiting(shared):
    document = shared.doc("papers/aqueous.pdf")
    detail = lv.document_detail(shared.reader(), shared.index, document)
    fields = {f["field"]: f for f in detail["fields"]}
    assert fields["title"]["set"] is False and fields["title"]["waiting"] == 1 and fields["title"]["agreement"] == "unresolved"
    assert fields["year"]["set"] is False and fields["year"]["waiting"] == 0 and fields["year"]["agreement"] == "unknown"
    assert [f["field"] for f in detail["fields"]][:5] == ["doi", "title", "authors", "year", "container"], "the core fields always show, even unset"
    assert detail["waiting"] == 3 and detail["available"] is True and detail["kind"] == "pdf"
    assert detail["files"] == [{"root": "lib", "root_status": "online", "path": "papers/aqueous.pdf", "state": "active"}]


def test_an_accepted_value_carries_its_badge_and_provenance(lab):
    document = lab.doc("papers/aqueous.pdf")
    metadata.set_value(lab.conn, document, "title", "Stated By A Person", lock=True)
    title = {f["field"]: f for f in lv.document_detail(lab.reader(), lab.index, document)["fields"]}["title"]
    assert (title["set"], title["display"], title["origin"], title["badge"], title["source"], title["locked"], title["accepted_by"]) == (
        True, "Stated By A Person", "assigned", "A", "manual", True, "user")
    assert title["origin_label"] == "stated by a person" and title["source_label"] == "stated by a person"


def test_an_optional_field_appears_only_when_it_has_something(lab):
    document = lab.doc("papers/aqueous.pdf")
    assert "volume" not in {f["field"] for f in lv.document_detail(lab.reader(), lab.index, document)["fields"]}
    metadata.set_value(lab.conn, document, "volume", "12", lock=False)
    assert "volume" in {f["field"] for f in lv.document_detail(lab.reader(), lab.index, document)["fields"]}


def test_a_merged_away_document_still_has_a_detail_that_says_where_it_went(lab):
    keep, absorb = lab.doc("papers/aqueous.pdf"), lab.doc("papers/aqueous-copy.pdf")
    relations.merge(lab.conn, keep, absorb)
    detail = lv.document_detail(lab.reader(), lab.index, absorb)
    assert detail["retired"] is True and detail["merged_into"] == keep


def test_an_unknown_document_is_not_found(shared):
    with pytest.raises(KvError) as caught:
        lv.document_detail(shared.reader(), shared.index, "0" * 32)
    assert caught.value.code == ErrorCode.NOT_FOUND


# ----------------------------------------------------------------------------------------------------- why this value?


def test_evidence_for_a_value_nobody_has_accepted(shared):
    why = lv.field_evidence(shared.reader(), shared.doc("papers/aqueous.pdf"), "title")
    assert why["accepted"] is None and why["agreement"] == "unresolved" and why["agreement_text"].startswith("Unresolved")
    assert {p["source"] for p in why["proposals"]} == {"layout_title", "pdf_info_title"}
    assert all(p["status"] == "proposed" and p["relation_to_accepted"] is None for p in why["proposals"])
    layout = next(p for p in why["proposals"] if p["source"] == "layout_title")
    assert layout["source_label"] == "the title as laid out on page one" and layout["evidence"], "a proposal shows the evidence it rests on"


def test_evidence_after_accepting_a_proposal_shows_where_it_came_from(lab):
    document = lab.doc("papers/aqueous.pdf")
    pending = next(i for i in lv.review_items(lab.reader()) if i["document_id"] == document and i["field"] == "title")
    lv.decide_item(lab.conn, pending, accept=True)
    why = lv.field_evidence(lab.reader(), document, "title")
    assert why["accepted"]["display"] == PAPER_TITLE and why["accepted"]["origin"] == "observed"
    assert why["accepted"]["sentence"].startswith("Accepted by you, from ")
    assert why["accepted"]["evidence"], "the evidence the accepted value rests on is kept with it"
    assert {p["status"] for p in why["proposals"]} == {"accepted"} and all(p["relation_to_accepted"] == "same" for p in why["proposals"])
    assert why["agreement"] == "uncorroborated" or why["agreement"] == "agreement"
    assert why["history"] and why["history"][0]["actor"] == "user" and why["history"][0]["new"] == PAPER_TITLE


def test_a_different_proposal_beside_an_accepted_value_is_a_discrepancy(lab):
    document = lab.doc("papers/aqueous.pdf")
    metadata.set_value(lab.conn, document, "title", "A Title Typed By A Person", lock=False)
    why = lv.field_evidence(lab.reader(), document, "title")
    assert why["agreement"] == "discrepancy"
    assert {p["relation_to_accepted"] for p in why["proposals"]} == {"differs"}
    assert why["accepted"]["origin"] == "assigned" and why["accepted"]["evidence"] == []


def test_rejected_proposals_stay_on_the_page(lab):
    """"Why not that one?" must be answerable: a rejected proposal is shown, marked rejected, and does not count as a vote."""
    document = lab.doc("papers/aqueous.pdf")
    metadata.set_value(lab.conn, document, "title", "A Title Typed By A Person", lock=False)
    item = next(i for i in lv.review_items(lab.reader()) if i["document_id"] == document and i["field"] == "title")
    lv.decide_item(lab.conn, item, accept=False)
    why = lv.field_evidence(lab.reader(), document, "title")
    assert {p["status"] for p in why["proposals"]} == {"rejected"}
    assert why["agreement"] == "uncorroborated"


def test_a_field_nobody_has_touched_is_unknown(shared):
    why = lv.field_evidence(shared.reader(), shared.doc("papers/aqueous.pdf"), "year")
    assert (why["accepted"], why["proposals"], why["agreement"]) == (None, [], "unknown")


def test_evidence_refuses_a_field_that_does_not_exist_and_a_document_that_does_not_exist(shared):
    with pytest.raises(KvError) as bad_field:
        lv.field_evidence(shared.reader(), shared.doc("papers/aqueous.pdf"), "colour")
    assert bad_field.value.code == ErrorCode.INVALID_ARGUMENTS
    with pytest.raises(KvError) as missing:
        lv.field_evidence(shared.reader(), "0" * 32, "title")
    assert missing.value.code == ErrorCode.NOT_FOUND


# ------------------------------------------------------------------------------------------------------------- review


def test_review_items_name_the_paper_they_are_about(shared):
    items = lv.review_items(shared.reader())
    assert items and all(i["name"] and i["label"] and i["sources_text"] for i in items)
    item = next(i for i in items if i["name"] == "papers/aqueous.pdf" and i["field"] == "doi")
    assert item["display"] == PAPER_DOI and item["label"] == "DOI" and item["sources_text"] == "a DOI printed in the document's text"


def test_review_can_be_narrowed_to_what_the_batch_rule_would_take(shared):
    safe, required = lv.review_items(shared.reader(), only="safe"), lv.review_items(shared.reader(), only="required")
    assert safe and required and {i["review"] for i in safe} == {"safe"} and {i["review"] for i in required} == {"required"}
    assert len(safe) + len(required) == len(lv.review_items(shared.reader()))


def test_accepting_an_item_sets_the_value_and_clears_it_from_the_queue(lab):
    document = lab.doc("papers/second.pdf")
    item = next(i for i in lv.review_items(lab.reader()) if i["document_id"] == document and i["field"] == "title")
    before = lab.env.revision()
    result = lv.decide_item(lab.conn, item, accept=True)
    assert result == {"decision": "accepted", "field": "title", "changed": True} and lab.env.revision() > before
    assert metadata.get_values(lab.conn, document)["title"]["value"] == item["value"]
    assert not [i for i in lv.review_items(lab.reader()) if i["document_id"] == document and i["field"] == "title"]


def test_rejecting_an_item_rejects_every_candidate_that_said_it(lab):
    document = lab.doc("papers/second.pdf")
    item = next(i for i in lv.review_items(lab.reader()) if i["document_id"] == document and i["field"] == "title")
    assert len(item["candidate_ids"]) == 2, "layout and file-metadata titles agree: one item, two candidates"
    assert lv.decide_item(lab.conn, item, accept=False) == {"decision": "rejected", "field": "title", "changed": True}
    assert {c["status"] for c in metadata.get_candidates(lab.conn, document) if c["candidate_id"] in item["candidate_ids"]} == {"rejected"}
    assert metadata.get_values(lab.conn, document).get("title") is None, "rejecting a proposal accepts nothing"


def test_a_locked_value_cannot_be_replaced_by_accepting_a_proposal(lab):
    document = lab.doc("papers/second.pdf")
    metadata.set_value(lab.conn, document, "title", "Fixed By A Person", lock=True)
    item = next(i for i in lv.review_items(lab.reader()) if i["document_id"] == document and i["field"] == "title")
    with pytest.raises(KvError) as caught:
        lv.decide_item(lab.conn, item, accept=True)
    assert caught.value.code == ErrorCode.METADATA_LOCKED
    assert metadata.get_values(lab.conn, document)["title"]["value"] == "Fixed By A Person"


# ---------------------------------------------------------------------------------------------------------------- search


def test_a_search_names_the_document_and_the_page(shared):
    found = lv.search_pages(shared.reader(), shared.index, "aqueous")
    assert found["hits"] and found["searchable"] == 6, "six PDFs have an extraction (one of them a scan with no text); the text file has none"
    hit = next(h for h in found["hits"] if h["name"] == "papers/aqueous.pdf")
    assert hit["pdf_page"] >= 1 and hit["snippet"] and hit["available"] is True and hit["document_id"] == shared.doc("papers/aqueous.pdf")
    assert hit["provisional"] is False


def test_a_search_says_what_it_could_not_see(shared):
    found = lv.search_pages(shared.reader(), shared.index, "aqueous")
    assert found["caveats"] == ["1 scan(s) with no text layer"]
    assert "A missing hit is not proof of absence." in found["note"]


def test_a_malformed_query_is_an_error_not_no_hits(shared):
    for text in ("", "   ", "year:", "year:soon", "doi:nope", "*"):
        with pytest.raises(KvError) as caught:
            lv.search_pages(shared.reader(), shared.index, text)
        assert caught.value.code == ErrorCode.QUERY_INVALID, text


def test_a_filter_narrows_the_search_and_says_how_many_documents_it_selected(lab):
    lab.accept_title("papers/aqueous.pdf", "Alpha Paper")
    metadata.set_value(lab.conn, lab.doc("papers/aqueous.pdf"), "year", "2020", lock=False)
    found = lv.search_pages(lab.reader(), lab.index, "aqueous year:2020")
    assert {h["name"] for h in found["hits"]} == {"papers/aqueous.pdf"}
    assert found["scope"] == {"filters": [["year", "2020"]], "documents_matching": 1}


def test_a_search_pages_with_a_cursor(shared):
    first = lv.search_pages(shared.reader(), shared.index, "aqueous", limit=3)
    assert len(first["hits"]) == 3 and first["truncated"] and first["next_cursor"]
    second = lv.search_pages(shared.reader(), shared.index, "aqueous", limit=3, token=first["next_cursor"])
    seen = {(h["artifact_id"], h["pdf_page"]) for h in first["hits"]}
    assert second["hits"] and not seen & {(h["artifact_id"], h["pdf_page"]) for h in second["hits"]}


def test_searching_before_anything_is_extracted_says_there_is_nothing_to_search(tmp_path):
    made = Lab(tmp_path, extract=False)
    try:
        from knowledgevista import paths
        from knowledgevista.index.store import open_index

        index = open_index(paths.index_path(made.catalog), create=True)
        found = lv.search_pages(made.reader(), index, "aqueous")
        assert found["hits"] == [] and found["searchable"] == 0
        assert any("not extracted yet" in c for c in found["caveats"])
        index.close()
    finally:
        made.close()


# ---------------------------------------------------------------------------------------------------------------- health


def test_health_keeps_inventory_and_health_apart(shared):
    report = lv.health_report(shared.reader(), shared.index)
    assert {"inventory", "health"} <= set(report)
    assert report["inventory"]["documents"] == 7 and "locations_missing" in report["health"]
    assert "locations_missing" not in report["inventory"]


# --------------------------------------------------------------------------------- reading never writes; text is never altered


def test_nothing_here_writes(lab):
    """Every read function runs on a connection that cannot write: a stray UPDATE would raise instead of passing."""
    reader = lab.reader()
    document = lab.doc("papers/aqueous.pdf")
    lv.list_documents(reader, lab.index)
    lv.list_documents(reader, lab.index, "view:missing")
    lv.sidebar(reader, lab.index)
    lv.document_detail(reader, lab.index, document)
    lv.field_evidence(reader, document, "title")
    lv.review_items(reader)
    lv.search_pages(reader, lab.index, "aqueous")
    lv.health_report(reader, lab.index)
    with pytest.raises(Exception, match="readonly"):  # the connection really cannot write, so the calls above could not have either
        reader.execute("UPDATE library SET catalog_revision = catalog_revision + 1")


def test_text_from_outside_passes_through_unchanged(lab):
    """The view layer must not escape or strip: the window shows it as text, and changing it here would hide what a person typed."""
    markup = '<b>x</b> &amp; <img src="http://example.invalid/a.png">'
    document = lab.doc("papers/aqueous.pdf")
    organize.new_collection(lab.conn, markup, [document])
    organize.add_tags(lab.conn, [document], [markup])
    side = lv.sidebar(lab.reader(), lab.index)
    assert [c["label"] for c in side["collections"]] == [markup] and [t["label"] for t in side["tags"]] == [markup]
    detail = lv.document_detail(lab.reader(), lab.index, document)
    assert detail["collections"] == [markup] and detail["tags"] == [markup]
    assert any(row.name == "papers/a&amp;b.pdf" for row in lv.list_documents(lab.reader(), lab.index).rows)


def test_decided_proposals_are_not_counted_as_waiting(lab):
    document = lab.doc("papers/aqueous.pdf")
    before = lv.document_detail(lab.reader(), lab.index, document)
    assert next(f for f in before["fields"] if f["field"] == "title")["waiting"] == 1
    item = next(i for i in lv.review_items(lab.reader()) if i["document_id"] == document and i["field"] == "title")
    lv.decide_item(lab.conn, item, accept=True)
    after = lv.document_detail(lab.reader(), lab.index, document)
    assert next(f for f in after["fields"] if f["field"] == "title")["waiting"] == 0
    assert after["waiting"] == before["waiting"] - 1
    doi = next(i for i in lv.review_items(lab.reader()) if i["document_id"] == document and i["field"] == "doi")
    lv.decide_item(lab.conn, doi, accept=False)
    assert next(f for f in lv.document_detail(lab.reader(), lab.index, document)["fields"] if f["field"] == "doi")["waiting"] == 0


def test_replacing_an_accepted_value_is_not_reported_as_a_discrepancy(lab):
    document = lab.doc("papers/aqueous.pdf")
    item = next(i for i in lv.review_items(lab.reader()) if i["document_id"] == document and i["field"] == "title")
    lv.decide_item(lab.conn, item, accept=True)
    metadata.set_value(lab.conn, document, "title", "A Person Replaced It", lock=False)
    why = lv.field_evidence(lab.reader(), document, "title")
    assert why["agreement"] == "uncorroborated", "the earlier accepted candidate is history, not a source that still disagrees"
    assert {p["status"] for p in why["proposals"]} == {"accepted"} and {p["relation_to_accepted"] for p in why["proposals"]} == {"differs"}
