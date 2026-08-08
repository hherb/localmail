# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""How often a *repeating* batch-level backend failure is reported (#267).

`_embed_table` catches batch-level backend errors, rolls back, and returns 0 so
the chunks are re-claimed next sweep — deliberately, so a network blip never
poisons the queue. Nothing is written to the database on that path, so a
WARNING is the only record an operator ever gets. Since #259 a sweep counts as
progress when *either* queue advanced, so while a language backlog drains, a
persistently broken backend is retried once per base poll interval: a full
traceback for each of the two chunk tables every ~5 s, for hours.

The rule: **report a failure — with its traceback — when it is the first on
record for that table, when the exception type changes, or when the interval
has elapsed since the last report. Otherwise stay silent and count.** The next
report names how many it swallowed, so nothing is lost, only deferred.

Three consequences are deliberate and each closes a way the obvious
"suppress after the first" rule fails:

- **Success does not clear the record.** A backend alternating 200/503 — the
  "network blip" the batch-level handler exists for — makes every failure the
  first of a fresh streak under a reset-on-success rule, so every one carries a
  traceback and the throttle buys nothing. Recovery is expressed by the
  interval instead: break, recover, break again a minute later and it is the
  same incident; an hour later and it is a new one.
- **The exception type is part of the record.** A count alone cannot tell a
  continuing failure from a *different* one arriving mid-streak — the second
  would be suppressed and, worse, reported as a continuation of the first, so
  the one traceback on record names the wrong problem.
- **Every report carries the traceback**, including the periodic ones. A rule
  that logs it once per process leaves a long incident undiagnosable from a
  truncated log or a supervisor ring buffer, with no way back short of
  restarting the daemon — which also destroys the failing state.

Owning the record and the rule that reads it in one module is the same
reasoning as `sweep_pacing` (progress predicate beside the arithmetic),
`blob_temps` (minting beside matching), and `shutdown_budget` (the child's
budget beside the supervisor's kill deadline): split apart, the predicate ends
up trusting a counter whose only guarantee is that its caller remembered to
maintain it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class ReportedFailure:
    """What has already been said about one chunk table's batch failures.

    `since_report` counts the failures swallowed since `reported_at` — the
    number the *next* report names. It is not a streak: a successful sweep in
    between leaves it alone, because the throttle paces log output rather than
    tracking the backend's state.
    """

    exc_name: str
    reported_at: float
    since_report: int


@dataclass(frozen=True, slots=True)
class FailureReport:
    """What to do about one batch failure, and what to remember afterwards."""

    record: ReportedFailure
    log_it: bool
    suppressed: int
    """Failures swallowed since the previous report; 0 unless `log_it`."""


def note_failure(
    prev: ReportedFailure | None,
    *,
    exc_name: str,
    now: float,
    interval_s: float,
) -> FailureReport:
    """Fold one batch failure into `prev` and decide whether it is logged.

    Args:
        prev: What was last reported for this chunk table, or None if nothing
            has been.
        exc_name: `type(exc).__name__` of the failure.
        now: A monotonic reading, in seconds. Must come from the same clock as
            every other call for this table — `reported_at` is only ever
            compared against a later `now`.
        interval_s: Minimum seconds between reports of the same exception type.
            `<= 0` disables the throttle, i.e. every failure is reported (the
            pre-#267 behaviour), and is what `SearchConfig`'s `0` means.

    Returns:
        The `FailureReport` to act on; store `.record` back under the table.
    """
    fresh = ReportedFailure(exc_name=exc_name, reported_at=now, since_report=0)
    if prev is None or prev.exc_name != exc_name:
        # A different exception type starts its own count: attributing the
        # previous type's swallowed failures to this line is the very
        # conflation keying on the type exists to prevent.
        return FailureReport(record=fresh, log_it=True, suppressed=0)
    if interval_s <= 0 or now - prev.reported_at >= interval_s:
        return FailureReport(record=fresh, log_it=True, suppressed=prev.since_report)
    return FailureReport(
        record=replace(prev, since_report=prev.since_report + 1),
        log_it=False,
        suppressed=0,
    )
