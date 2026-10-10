"""What the window's jobs DO: one function per thing, each taking the `JobContext` the job manager gives it.

Kept apart from the window so each is a plain function over the services (no widgets), run on a worker, and tested through the real
job manager. Reads open one snapshot (`ctx.reader()`); writes open the catalog for writing (`ctx.writer()`), which only the write lane
may do. Every write here calls the same service the command line calls, so a person gets the same result from either.
"""

from __future__ import annotations

from typing import Any

from knowledgevista import paths
from knowledgevista.db.catalog import revision as catalog_revision
from knowledgevista.domain import plan as planmod
from knowledgevista.errors import ErrorCode, KvError
from knowledgevista.extract.client import ExtractionSession
from knowledgevista.gui.jobs import JobContext
from knowledgevista.index.metacache import open_metacache
from knowledgevista.index.store import open_index
from knowledgevista.services import library_view as lv
from knowledgevista.services import opener, organize, organizer_plan, review, roots
from knowledgevista.services.extract import extract_library
from knowledgevista.services.resolve_metadata import resolve_library
from knowledgevista.services.scan import scan_root

# ------------------------------------------------------------------------------------------------------------------ reading


def snapshot(ctx: JobContext, scope: str, with_review: bool = True) -> dict[str, Any]:
    """Everything the lists show, from ONE snapshot of the catalog, so the table, the sidebar counts and the review queue agree."""
    with ctx.reader() as (conn, index):
        listing = lv.list_documents(conn, index, scope)
        return {
            "listing": listing, "sidebar": lv.sidebar(conn, index), "revision": listing.revision,
            "review": lv.review_items(conn) if with_review else None,
            "library_id": conn.execute("SELECT library_id FROM library").fetchone()[0],
        }


def current_revision(ctx: JobContext) -> int:
    with ctx.reader() as (conn, _):
        return catalog_revision(conn)


def detail(ctx: JobContext, document_id: str) -> dict[str, Any]:
    with ctx.reader() as (conn, index):
        return lv.document_detail(conn, index, document_id)


def evidence(ctx: JobContext, document_id: str, field: str) -> dict[str, Any]:
    with ctx.reader() as (conn, _):
        return lv.field_evidence(conn, document_id, field)


def search(ctx: JobContext, text: str, token: str | None = None) -> dict[str, Any]:
    with ctx.reader() as (conn, index):
        return lv.search_pages(conn, index, text, token=token)


def health(ctx: JobContext) -> dict[str, Any]:
    with ctx.reader() as (conn, index):
        return lv.health_report(conn, index)


def rename_plan(ctx: JobContext, document_id: str) -> planmod.Plan:
    """The rename proposal for ONE document, built the way `kv plan create --document` builds it. Reads the catalog and the disk; writes neither.
    A folder that does not allow organizing is refused with the sentence that says how to allow it."""
    with ctx.reader() as (conn, _):
        folders = [r[0] for r in conn.execute(
            "SELECT DISTINCT l.root_id FROM location l JOIN document_artifact da ON da.artifact_id = l.artifact_id "
            "WHERE da.document_id = ? AND l.ended_at IS NULL", (document_id,))]
        if not folders:
            raise KvError(ErrorCode.FILE_MISSING, "This document has no current file to rename.", {"document_id": document_id})
        selected = organizer_plan.organizable_roots(conn, folders)
        return organizer_plan.build_plan(conn, roots=selected, documents={document_id})


def locate_for_open(ctx: JobContext, document_id: str, pdf_page: int | None = None) -> dict[str, Any]:
    """Where the document's file is NOW (disk beats catalog), without opening it: the window opens it, on its own thread."""
    with ctx.reader() as (conn, _):
        return opener.open_document(conn, document_id, pdf_page=pdf_page, do_launch=False)


# ------------------------------------------------------------------------------------------------------------------ writing


def scan(ctx: JobContext) -> dict[str, Any]:
    """Look at every enabled folder. Cancelling stops between files and leaves an interrupted scan, which the next scan closes."""
    reports = []
    with ctx.writer() as (conn, _):
        selected = roots.find_roots(conn, None)
        if not selected:
            raise KvError(ErrorCode.NOT_FOUND, "There is no folder to scan yet. Add one first.")
        for root in selected:
            ctx.check()
            report = scan_root(conn, root.root_id, progress=ctx.progress, after_file=ctx.check)
            reports.append({"root": root.label, **report.as_dict()})
    skipped = [r for r in reports if r["status"] != "completed"]
    return {"roots": reports, "files": sum(r["files_seen"] for r in reports), "new_documents": sum(r["new_documents"] for r in reports),
            "moved": sum(r["moved"] for r in reports), "went_missing": sum(r["went_missing"] for r in reports), "skipped": [r["root"] for r in skipped]}


def extract(ctx: JobContext) -> dict[str, Any]:
    with ctx.writer(index=True) as (conn, index):
        with ExtractionSession() as session:
            report = extract_library(conn, index, session=session, progress=ctx.progress, should_stop=ctx.should_stop)
    return report.as_dict()


def resolve(ctx: JobContext) -> dict[str, Any]:
    """Offline: read DOIs and titles from the extracted text and PROPOSE them. Nothing is accepted and nothing leaves the machine."""
    with ctx.writer() as (conn, _):
        index = open_index(paths.index_path(ctx.catalog), create=False, read_only=True)
        if index is None:
            raise KvError(ErrorCode.NOT_EXTRACTED, "Nothing has been extracted yet, so there is no text to read DOIs and titles from. Extract text first.")
        cache = open_metacache(paths.metacache_path(ctx.catalog), create=True)
        try:
            with ExtractionSession() as session:
                return resolve_library(conn, index, cache, session=session, progress=ctx.progress, should_stop=ctx.should_stop)
        finally:
            index.close()
            cache.close()


def add_root(ctx: JobContext, path: str, label: str | None, allow_organize: bool) -> dict[str, Any]:
    with ctx.writer() as (conn, _):
        return roots.add_root(conn, path, label=label, allow_organize=allow_organize, catalog_path=str(ctx.catalog)).as_dict()


def copy_catalog(ctx: JobContext, source: str, folder: str) -> dict[str, Any]:
    """Copy a library's catalog to `folder` (the window's Move catalog). Read-only on the original, touches no document, overwrites and
    deletes nothing (services/catalog_copy.py). On the write lane so it never overlaps this window's own writes to the catalog."""
    ctx.progress("Copying the catalog…")
    from knowledgevista.services import catalog_copy

    result = catalog_copy.copy_catalog(source, folder)
    return {"source": str(result.source), "destination": str(result.destination), "library_id": result.library_id,
            "cache_copied": result.cache_copied, "notes": list(result.notes)}


def decide(ctx: JobContext, item: dict[str, Any], accept: bool) -> dict[str, Any]:
    with ctx.writer() as (conn, _):
        return lv.decide_item(conn, item, accept)


def accept_safe(ctx: JobContext) -> dict[str, Any]:
    """The batch rule, `safe_batch_v1`: accepts only proposals marked safe, never replaces a person's value, leaves disagreements alone."""
    with ctx.writer() as (conn, _):
        batch = review.accept_safe(conn)
    return {"accepted": sum(1 for a in batch.accepted if a.changed), "proposals": len(batch.accepted), "left_for_a_person": len(batch.skipped)}


def add_to_collection(ctx: JobContext, name: str, document_id: str) -> dict[str, Any]:
    with ctx.writer() as (conn, _):
        try:
            organize.find_collection(conn, name)
        except KvError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
            return {"created": True, **organize.new_collection(conn, name, [document_id])}
        return {"created": False, **organize.add_to_collection(conn, name, [document_id])}


def add_tag(ctx: JobContext, tag: str, document_id: str) -> dict[str, Any]:
    with ctx.writer() as (conn, _):
        return organize.add_tags(conn, [document_id], [tag])


def save_plan(ctx: JobContext, plan: planmod.Plan) -> str:
    """Write the plan file (outside every folder) and register it. Applying it is `kv apply`, deliberately not here."""
    with ctx.writer() as (conn, _):
        return str(organizer_plan.write_plan(conn, plan))
