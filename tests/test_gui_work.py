"""What the window's jobs do, called directly with a job context: each is one service call with the right arguments and the right promises.

The window tests press buttons and read the screen; these read the catalog, because "the Stop button stopped it" and "the plan is for
ONE document" are properties of the work, and a screen that looked right could hide either being wrong.
"""

from __future__ import annotations

import types

import pytest
from guisupport import require_qt
from lab import PAPER_TITLE, Lab

require_qt()

from knowledgevista.errors import ErrorCode, KvError  # noqa: E402
from knowledgevista.gui import jobs as J  # noqa: E402
from knowledgevista.gui import work  # noqa: E402
from knowledgevista.services import library_view as lv  # noqa: E402
from knowledgevista.services import metadata, organize, scan as scan_service  # noqa: E402


class Ctx(J.JobContext):
    """A job context with no thread behind it. `stop_after` makes `check` / `should_stop` ask to stop from that call on."""

    def __init__(self, catalog, *, lane=J.WRITE, stop_after: int | None = None):
        job = J.Job(1, "t", "t", lane)
        bridge = types.SimpleNamespace(progress=types.SimpleNamespace(emit=lambda *_a: None))
        super().__init__(job, catalog, bridge)
        self.stop_after, self.asked = stop_after, 0

    def should_stop(self) -> bool:
        self.asked += 1
        return self.stop_after is not None and self.asked > self.stop_after

    def check(self, *_a) -> None:
        if self.should_stop():
            raise J.JobCancelled()


@pytest.fixture
def lab(qapp, tmp_path):
    made = Lab(tmp_path)
    yield made
    made.close()


def reader(lab):
    return Ctx(lab.catalog, lane=J.READ)


# --------------------------------------------------------------------------------------------------------------- reading


def test_a_snapshot_is_one_revision_throughout(lab):
    snap = work.snapshot(reader(lab), "all")
    assert snap["revision"] == snap["listing"].revision == snap["sidebar"]["revision"] == lab.env.revision()
    assert snap["library_id"] and len(snap["review"]) == 11 and snap["listing"].total_documents == 7


def test_a_snapshot_can_leave_out_the_review_queue(lab):
    assert work.snapshot(reader(lab), "all", with_review=False)["review"] is None


def test_the_revision_is_one_integer(lab):
    assert work.current_revision(reader(lab)) == lab.env.revision()


# --------------------------------------------------------------------------------------------------------------- scanning


def test_scan_without_a_folder_says_to_add_one(qapp, tmp_path):
    from knowledgevista.gui.app import prepare

    catalog, _ = prepare(tmp_path / "empty.sqlite")
    with pytest.raises(KvError) as caught:
        work.scan(Ctx(catalog))
    assert caught.value.code == ErrorCode.NOT_FOUND and "Add one first" in caught.value.message


def test_scan_reports_what_it_found(lab):
    (lab.env.lib / "notes" / "new.txt").write_text("brand new")
    (lab.env.lib / "papers" / "second.pdf").unlink()
    result = work.scan(Ctx(lab.catalog))
    assert (result["files"], result["new_documents"], result["went_missing"], result["skipped"]) == (7, 1, 1, [])
    assert result["roots"][0]["root"] == "lib"


def test_stopping_a_scan_between_files_leaves_a_catalog_the_next_scan_completes(lab):
    for n in range(12):
        (lab.env.lib / "notes" / f"extra{n:02d}.txt").write_text(f"file {n}")
    ctx = Ctx(lab.catalog, stop_after=4)
    with pytest.raises(J.JobCancelled):
        work.scan(ctx)
    assert ctx.asked > 4, "the scan really asked, file by file"
    partway = lab.env.one("SELECT COUNT(*) FROM location WHERE ended_at IS NULL")
    assert 7 <= partway < 19, "some of the new files were recorded and not all"
    assert lab.env.one("SELECT COUNT(*) FROM scan_run WHERE status = 'interrupted'") == 1, "a stopped scan is recorded as interrupted, not completed"
    result = work.scan(Ctx(lab.catalog))
    assert result["files"] == 19 and lab.env.one("SELECT COUNT(*) FROM location WHERE ended_at IS NULL AND state = 'active'") == 19
    assert lab.env.one("SELECT COUNT(*) FROM scan_run WHERE status = 'running'") == 0 and lab.env.one("SELECT COUNT(*) FROM scan_run WHERE status = 'completed'") >= 2


def test_a_scan_does_not_start_another_folder_once_asked_to_stop(lab, tmp_path):
    from knowledgevista.services.roots import add_root

    other = tmp_path / "second-folder"
    other.mkdir()
    (other / "x.txt").write_text("x")
    add_root(lab.conn, str(other), label="Second")
    ctx = Ctx(lab.catalog, stop_after=0)
    with pytest.raises(J.JobCancelled):
        work.scan(ctx)
    assert lab.env.one("SELECT COUNT(*) FROM root WHERE last_scan_at IS NOT NULL") <= 1


# --------------------------------------------------------------------------------------------------------------- extracting and resolving


def test_extraction_stops_between_files_and_says_so(qapp, tmp_path):
    made = Lab(tmp_path, extract=False)
    try:
        ctx = Ctx(made.catalog, stop_after=2)
        result = work.extract(ctx)
        assert result["stopped"] is True and result["extracted"] == 2
        done = work.extract(Ctx(made.catalog))
        assert done["stopped"] is False and done["current"] == 2 and done["extracted"] == result["candidates"] - 2
    finally:
        made.close()


def test_resolution_stops_between_documents_and_says_so(qapp, tmp_path):
    made = Lab(tmp_path, extract=True, resolve=False)
    try:
        result = work.resolve(Ctx(made.catalog, stop_after=2))
        assert result["stopped"] is True and result["documents"] == 2
        assert made.env.one("SELECT status FROM resolve_run ORDER BY started_at DESC LIMIT 1") == "interrupted"
        finished = work.resolve(Ctx(made.catalog))
        assert finished["stopped"] is False
    finally:
        made.close()


def test_resolution_with_no_extraction_store_is_refused_with_advice(qapp, tmp_path):
    from knowledgevista.gui.app import prepare

    catalog, _ = prepare(tmp_path / "c.sqlite")
    with pytest.raises(KvError) as caught:
        work.resolve(Ctx(catalog))
    assert caught.value.code == ErrorCode.NOT_EXTRACTED and "Extract text first" in caught.value.message


def test_resolution_proposes_and_accepts_nothing(qapp, tmp_path):
    made = Lab(tmp_path, extract=True, resolve=False)
    try:
        work.resolve(Ctx(made.catalog))
        assert made.env.one("SELECT COUNT(*) FROM metadata_value") == 0 and made.env.one("SELECT COUNT(*) FROM metadata_candidate WHERE status = 'proposed'") > 0
        assert made.env.one("SELECT online FROM resolve_run") == 0 and made.env.one("SELECT accept_safe FROM resolve_run") == 0
    finally:
        made.close()


# --------------------------------------------------------------------------------------------------------------- folders


def test_a_folder_is_added_with_organizing_off_unless_asked(qapp, tmp_path):
    from knowledgevista.gui.app import prepare

    catalog, _ = prepare(tmp_path / "c.sqlite")
    folder = tmp_path / "docs"
    folder.mkdir()
    off = work.add_root(Ctx(catalog), str(folder), None, False)
    assert off["allow_organize"] is False and off["label"] == "docs"
    other = tmp_path / "other"
    other.mkdir()
    on = work.add_root(Ctx(catalog), str(other), "Mine", True)
    assert on["allow_organize"] is True and on["label"] == "Mine"


def test_a_folder_containing_the_catalog_is_refused(qapp, tmp_path):
    """The window passes the catalog's path so the library can never contain it: scanning would hash the live catalog every time."""
    from knowledgevista.gui.app import prepare

    catalog, _ = prepare(tmp_path / "inside" / "c.sqlite")
    with pytest.raises(KvError) as caught:
        work.add_root(Ctx(catalog), str(tmp_path / "inside"), None, False)
    assert caught.value.code == ErrorCode.ROOT_OVERLAP and "contains the catalog" in caught.value.message


# --------------------------------------------------------------------------------------------------------------- deciding


def test_deciding_an_item_is_the_users_decision(lab):
    item = next(i for i in lv.review_items(lab.reader()) if i["name"] == "papers/second.pdf" and i["field"] == "title")
    assert work.decide(Ctx(lab.catalog), item, True) == {"decision": "accepted", "field": "title", "changed": True}
    assert metadata.get_values(lab.conn, lab.doc("papers/second.pdf"))["title"]["accepted_by"] == "user"


def test_the_batch_rule_is_named_in_what_it_records(lab):
    result = work.accept_safe(Ctx(lab.catalog))
    assert result == {"accepted": 3, "proposals": 3, "left_for_a_person": 0}
    assert {v["accepted_by"] for v in metadata.get_values(lab.conn, lab.doc("papers/aqueous.pdf")).values()} == {"rule:safe_batch_v1"}


# --------------------------------------------------------------------------------------------------------------- organising


def test_a_collection_is_created_once_and_then_added_to_whatever_the_case(lab):
    first = work.add_to_collection(Ctx(lab.catalog), "Esters", lab.doc("papers/aqueous.pdf"))
    again = work.add_to_collection(Ctx(lab.catalog), "ESTERS", lab.doc("papers/second.pdf"))
    assert first["created"] is True and again["created"] is False and again["name"] == "Esters"
    assert [(c["name"], c["members"]) for c in organize.list_collections(lab.conn)] == [("Esters", 2)]
    repeat = work.add_to_collection(Ctx(lab.catalog), "esters", lab.doc("papers/second.pdf"))
    assert repeat["added"] == 0 and repeat["already_members"] == 1


def test_a_tag_is_added_once(lab):
    document = lab.doc("books/book.pdf")
    assert work.add_tag(Ctx(lab.catalog), "Reference", document)["added"] == 1
    assert work.add_tag(Ctx(lab.catalog), "reference", document)["added"] == 0
    assert organize.tags_of(lab.conn, document) == ["Reference"]


# --------------------------------------------------------------------------------------------------------------- proposing a rename


def make_organizable(lab):
    lab.conn.execute("UPDATE root SET allow_organize = 1")
    for path, title in (("papers/aqueous.pdf", PAPER_TITLE), ("papers/second.pdf", "Partition Coefficients of Imaginary Amides")):
        document = lab.accept_title(path, title)
        metadata.set_value(lab.conn, document, "year", "2021", lock=False)
        metadata.set_value(lab.conn, document, "authors", [{"family": "Examplar", "given": "A."}], lock=False)


def test_a_rename_proposal_is_for_the_one_document_asked_about(lab):
    make_organizable(lab)
    plan = work.rename_plan(reader(lab), lab.doc("papers/aqueous.pdf"))
    assert [i.document_id for i in plan.items] == [lab.doc("papers/aqueous.pdf")], "the other documents with titles are not in this proposal"
    assert plan.items[0].status == "planned" and plan.options["documents"] == [lab.doc("papers/aqueous.pdf")]


def test_a_rename_proposal_for_a_folder_that_does_not_allow_organizing_is_refused(lab):
    lab.accept_title("papers/aqueous.pdf", PAPER_TITLE)
    with pytest.raises(KvError) as caught:
        work.rename_plan(reader(lab), lab.doc("papers/aqueous.pdf"))
    assert caught.value.code == ErrorCode.ROOT_NOT_ORGANIZABLE and "kv root allow-organize" in caught.value.message


def test_a_document_with_no_current_file_has_nothing_to_rename(lab):
    document = lab.doc("papers/second.pdf")
    (lab.env.lib / "papers" / "second.pdf").unlink()
    scan_service.scan_root(lab.conn, lab.env.root.root_id)
    lab.conn.execute("UPDATE location SET ended_at = '2026-01-01T00:00:00Z', end_reason = 'replaced' WHERE ended_at IS NULL AND relative_path = 'papers/second.pdf'")
    with pytest.raises(KvError) as caught:
        work.rename_plan(reader(lab), document)
    assert caught.value.code == ErrorCode.FILE_MISSING


def test_saving_a_plan_registers_it_and_refuses_to_overwrite_it(lab):
    make_organizable(lab)
    plan = work.rename_plan(reader(lab), lab.doc("papers/aqueous.pdf"))
    path = work.save_plan(Ctx(lab.catalog), plan)
    assert lab.env.one("SELECT COUNT(*) FROM plan_registry WHERE plan_id = ?", plan.plan_id) == 1
    with pytest.raises(KvError) as caught:
        work.save_plan(Ctx(lab.catalog), plan)
    assert caught.value.code == ErrorCode.DESTINATION_EXISTS and path in caught.value.message


# --------------------------------------------------------------------------------------------------------------- opening


def test_locating_a_file_for_opening_launches_nothing(lab, monkeypatch):
    from knowledgevista.services import opener

    launched = []
    monkeypatch.setattr(opener, "launch", launched.append)
    record = work.locate_for_open(reader(lab), lab.doc("papers/second.pdf"), pdf_page=3)
    assert record["launched"] is False and launched == [] and record["path"].endswith("second.pdf")
    assert record["requested_page"] == {"pdf_page": 3, "printed_label": None} and record["page_targeted"] is False


def test_a_file_that_is_gone_cannot_be_located_for_opening(lab):
    (lab.env.lib / "papers" / "second.pdf").unlink()
    with pytest.raises(KvError) as caught:
        work.locate_for_open(reader(lab), lab.doc("papers/second.pdf"))
    assert caught.value.code == ErrorCode.FILE_MISSING


def test_the_job_context_refuses_a_writer_to_a_read_job(lab):
    with pytest.raises(RuntimeError):
        with Ctx(lab.catalog, lane=J.READ).writer():
            pass
