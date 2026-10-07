"""A long job can be asked to stop between items, and what it did stays whole while the rest is left for the next run.

The library window's Cancel button depends on this: it cannot kill a worker mid-write, so a job has to be able to stop at a point
where everything already done is complete and everything not done is untouched.
"""

from __future__ import annotations

from lab import Lab

from knowledgevista.services.extract import extract_library
from knowledgevista.services.resolve_metadata import resolve_library


def stop_after(n):
    """A `should_stop` that says no `n` times and yes from then on, counting how often it was asked."""
    state = {"asked": 0}

    def should_stop():
        state["asked"] += 1
        return state["asked"] > n

    should_stop.state = state
    return should_stop


def test_extraction_stops_between_files_and_the_next_run_finishes(tmp_path):
    lab = Lab(tmp_path, extract=False)
    try:
        stopper = stop_after(2)
        first = extract_library(lab.conn, lab.index, session=lab.session, should_stop=stopper)
        assert first.stopped is True and first.extracted == 2, "two files were stored, then it stopped"
        stored = lab.index.execute("SELECT COUNT(*) FROM extraction").fetchone()[0]
        assert stored == 2, "a stopped run leaves only whole extractions"
        assert lab.index.execute("SELECT COUNT(*) FROM extraction WHERE status = 'failed'").fetchone()[0] == 0

        second = extract_library(lab.conn, lab.index, session=lab.session)
        assert second.stopped is False and second.current == 2 and second.extracted == first.candidates - 2
        assert lab.index.execute("SELECT COUNT(*) FROM extraction").fetchone()[0] == first.candidates
    finally:
        lab.close()


def test_a_stop_asked_before_the_first_file_does_nothing(tmp_path):
    lab = Lab(tmp_path, extract=False)
    try:
        report = extract_library(lab.conn, lab.index, session=lab.session, should_stop=lambda: True)
        assert (report.stopped, report.extracted) == (True, 0)
        assert lab.index.execute("SELECT COUNT(*) FROM extraction").fetchone()[0] == 0
    finally:
        lab.close()


def test_a_job_that_is_never_asked_to_stop_does_not_report_stopped(tmp_path):
    lab = Lab(tmp_path, extract=False)
    try:
        report = extract_library(lab.conn, lab.index, session=lab.session, should_stop=lambda: False)
        assert report.stopped is False and report.extracted == report.candidates
        assert report.as_dict()["stopped"] is False
    finally:
        lab.close()


def test_resolution_stops_between_documents_and_resuming_reaches_the_same_state(tmp_path):
    whole = Lab(tmp_path / "whole", extract=True, resolve=False)
    cut = Lab(tmp_path / "cut", extract=True, resolve=False)
    try:
        resolve_library(whole.conn, whole.index, whole.cache, session=whole.session)

        partial = resolve_library(cut.conn, cut.index, cut.cache, session=cut.session, should_stop=stop_after(2))
        assert partial["stopped"] is True and partial["documents"] == 2
        status = cut.conn.execute("SELECT status FROM resolve_run WHERE run_id = ?", (partial["run_id"],)).fetchone()[0]
        assert status == "interrupted", "a run cut short says so; it is not 'completed'"

        resumed = resolve_library(cut.conn, cut.index, cut.cache, session=cut.session)
        assert resumed["stopped"] is False

        def proposals(lab):
            return sorted(tuple(r) for r in lab.conn.execute(
                "SELECT l.relative_path, c.field, c.value, c.source, c.status FROM metadata_candidate c "
                "JOIN document_artifact da ON da.document_id = c.document_id JOIN location l ON l.artifact_id = da.artifact_id AND l.ended_at IS NULL"))

        assert proposals(cut) == proposals(whole) and proposals(whole), "stopping and resuming changes nothing about the outcome"
    finally:
        whole.close()
        cut.close()
