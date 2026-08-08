# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The rule for how often a repeating batch failure is reported (#267)."""

from __future__ import annotations

from localmail.search.failure_pacing import ReportedFailure, note_failure

INTERVAL = 300.0


def _note(prev, *, exc_name="ConnectionError", now=0.0, interval_s=INTERVAL):
    return note_failure(prev, exc_name=exc_name, now=now, interval_s=interval_s)


def test_the_first_failure_on_record_is_reported() -> None:
    report = _note(None, now=10.0)
    assert report.log_it is True
    assert report.suppressed == 0
    assert report.record == ReportedFailure("ConnectionError", 10.0, 0)


def test_a_repeat_inside_the_interval_is_swallowed_and_counted() -> None:
    first = _note(None, now=0.0)
    second = _note(first.record, now=5.0)
    third = _note(second.record, now=10.0)

    assert second.log_it is False
    assert third.log_it is False
    assert third.record.since_report == 2
    # The record still points at the report that *was* made, so the interval
    # is measured from it rather than from the last failure.
    assert third.record.reported_at == 0.0


def test_the_report_resumes_once_the_interval_elapses_and_names_the_backlog() -> None:
    """The traceback re-arms: a long incident stays diagnosable from a
    truncated log without restarting the daemon."""
    state = _note(None, now=0.0).record
    for tick in (5.0, 10.0, 15.0):
        state = _note(state, now=tick).record

    report = _note(state, now=INTERVAL)
    assert report.log_it is True
    assert report.suppressed == 3
    assert report.record.since_report == 0


def test_a_different_exception_type_reports_immediately() -> None:
    """A second failure mode arriving mid-incident must not be swallowed and
    reported as a continuation of the first — the one traceback on record
    would name the wrong problem."""
    state = _note(None, exc_name="ConnectionError", now=0.0).record
    state = _note(state, exc_name="ConnectionError", now=5.0).record

    report = _note(state, exc_name="ImportError", now=10.0)
    assert report.log_it is True
    assert report.record.exc_name == "ImportError"


def test_a_type_change_does_not_attribute_the_old_backlog_to_the_new_line() -> None:
    state = _note(None, exc_name="ConnectionError", now=0.0).record
    state = _note(state, exc_name="ConnectionError", now=5.0).record
    assert state.since_report == 1

    report = _note(state, exc_name="TimeoutError", now=6.0)
    assert report.suppressed == 0
    assert report.record.since_report == 0


def test_a_flapping_backend_is_still_throttled() -> None:
    """Recovery is expressed by the interval, not by clearing the record —
    nothing here is told about the successes between the failures, so a
    backend alternating 200/503 cannot make every failure a fresh incident."""
    state = _note(None, now=0.0).record
    reports = [_note(state, now=t) for t in (5.0, 10.0, 15.0)]
    assert [r.log_it for r in reports] == [False, False, False]


def test_a_zero_interval_disables_the_throttle() -> None:
    """0 is the escape hatch back to pre-#267 behaviour: every failure logs."""
    state = _note(None, now=0.0, interval_s=0).record
    for tick in (0.1, 0.2, 0.3):
        report = _note(state, now=tick, interval_s=0)
        assert report.log_it is True
        assert report.record.since_report == 0
        state = report.record


def test_the_interval_boundary_reports() -> None:
    state = _note(None, now=0.0).record
    assert _note(state, now=INTERVAL - 0.001).log_it is False
    assert _note(state, now=INTERVAL).log_it is True
