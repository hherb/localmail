# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""At-scale regression coverage for the broad-folder browse plan family (#87).

Pins that the DISTINCT-regression signature (``Unique`` node plus
full-projection Sort on top of a Nested Loop) cannot silently come back
through a refactor of ``localmail.api.browse.build_where``.

Sits between two existing layers:

* ``tests/test_api_browse_plan.py`` — unit-scale eligibility tests
  (fixture scale; deliberately permit a Sort because the planner
  inverts the semi-join at fixture scale).
* ``tests/acceptance/run_browse_explain.py`` — operator-run harness at
  200k+ rows (catches this class but is not CI-gated).

The test seeds a calibrated archive (3 accounts × N rows, broad folder
labelling 50% of each account) and asserts that EXPLAIN on the
broad-folder probe shows:

1. ``Index Scan using messages_recent_idx on messages``.
2. No ``Unique`` node — the canonical DISTINCT marker. A clean EXISTS
   semi-join never emits one.
3. No full-projection ``Sort`` on top of a Nested Loop — the legitimate
   inverted-semi-join Sort sits at sub-calibration scale, ruled out by
   the calibration gate.

The calibration gate runs first: if the planner picks a non-date-ordered
walk, the regression class can't surface at that scale and the test
fails fast with a hint to bump ``LOCALMAIL_REGRESSION_ROWS``.

Scale tunable via env var:

* ``LOCALMAIL_REGRESSION_ROWS`` — override the default row count.

Auto-skips when no DB is reachable (via the standard ``db_conn`` fixture).
"""
from __future__ import annotations

import logging
import os

import psycopg

from tests.acceptance.browse_explain_lib import (
    DEFAULT_PAGE_SIZE,
    ProbeSpec,
    SeedConfig,
    run_explain,
    seed_accounts,
    seed_folder_filter_mailboxes,
    seed_messages,
)


# Calibrated scale at which the planner reliably picks the date-ordered
# walk for the broad-folder probe (50% labelled, 3 accounts, balanced
# distribution). Below this scale the planner inverts the semi-join —
# legitimate, but the #87 regression class can't surface, so the
# calibration gate fails. Operators with a slow CI runner can lower
# this via LOCALMAIL_REGRESSION_ROWS at the cost of the calibration
# gate possibly failing on PG planner cost-model drift.
#
# Calibration: smallest stable N (5/5 consecutive PASSes) = 4500;
# applied 1.5× headroom multiplier rounded up to the nearest 1000.
# Measured 2026-05-26 against PostgreSQL 18.1 on macOS aarch64.
DEFAULT_REGRESSION_ROWS = 7_000

# Three accounts: enough that the ACL filter is non-trivial without
# inflating the broad-folder Cartesian product. Changing this without
# re-calibrating DEFAULT_REGRESSION_ROWS will silently shift the
# calibration gate threshold.
_NUM_ACCOUNTS = 3
_MIN_REGRESSION_ROWS = 1000


def _resolved_row_count() -> int:
    """Read ``LOCALMAIL_REGRESSION_ROWS`` or fall back to the default."""
    raw = os.environ.get("LOCALMAIL_REGRESSION_ROWS")
    if raw is None:
        return DEFAULT_REGRESSION_ROWS
    try:
        n = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"LOCALMAIL_REGRESSION_ROWS must be an integer, got {raw!r}"
        ) from exc
    if n < _MIN_REGRESSION_ROWS:
        raise ValueError(
            f"LOCALMAIL_REGRESSION_ROWS must be at least "
            f"{_MIN_REGRESSION_ROWS} to be meaningful, got {n}"
        )
    return n


def test_broad_folder_filter_does_not_regress_to_distinct_plan_family(
    db_conn: psycopg.Connection,
) -> None:
    """The broad-folder probe must not produce a ``Unique`` node or a
    full-projection ``Sort`` over the messages projection at scale.

    Calibration gate runs first: the planner must pick the date-ordered
    walk (plan family ``"index-walk (option 1)"``). If it picks the
    inverted semi-join, the scale is below the regression-detection
    threshold and the test fails with a hint to bump
    LOCALMAIL_REGRESSION_ROWS.

    Once calibrated, the signature assertion catches the #87 regression
    class (DISTINCT re-introduced; EXISTS swapped for IN (SELECT ...);
    any change that forces the planner to dedup on the messages side).
    """
    log = logging.getLogger(__name__)

    n_rows = _resolved_row_count()
    # ``balanced`` gives each account an equal slice of total rows, which
    # in turn gives the broad mailbox (50% per account) the largest
    # uniform messages-side semi-join — the planner's strongest signal
    # to prefer the date-ordered walk, where the DISTINCT regression
    # class would surface. ``skewed`` / ``tail`` distort that signal.
    cfg = SeedConfig(
        total_rows=n_rows,
        num_accounts=_NUM_ACCOUNTS,
        distribution="balanced",
    )

    account_ids = seed_accounts(db_conn, _NUM_ACCOUNTS)
    seed_messages(db_conn, account_ids, cfg, verbose=False)
    folders = seed_folder_filter_mailboxes(db_conn, account_ids, verbose=False)
    first_account_id = account_ids[0]
    broad_mailbox_id = folders.broad[0]

    probe = ProbeSpec(
        name="broad folder initial page",
        account_ids=[first_account_id],
        cursor=None,
        folder_ids=[broad_mailbox_id],
    )
    summary = run_explain(db_conn, probe, page_size=DEFAULT_PAGE_SIZE)

    log.info(
        "at-scale broad-folder probe: rows=%d, plan_family=%r, "
        "exec_ms=%.2f, buf_hit=%d, buf_read=%d",
        n_rows, summary.plan_family,
        summary.execution_ms,
        summary.shared_hit_blocks, summary.shared_read_blocks,
    )

    assert summary.plan_family == "index-walk (option 1)", (
        f"calibration gate failed: planner picked plan family "
        f"{summary.plan_family!r} at {n_rows} rows. The #87 regression "
        f"class (Unique + full Sort) can only surface when the planner "
        f"prefers the date-ordered walk. Bump LOCALMAIL_REGRESSION_ROWS "
        f"or investigate a PG planner cost-model change.\n\n"
        f"Raw EXPLAIN:\n{summary.raw}"
    )

    assert "Index Scan using messages_recent_idx" in summary.raw, (
        f"messages_recent_idx no longer used for the broad-folder probe.\n\n"
        f"Raw EXPLAIN:\n{summary.raw}"
    )
    assert not summary.has_unique_node, (
        f"Unique node detected — DISTINCT semantics have come back through "
        f"a refactor of build_where (#87 regression class).\n\n"
        f"Raw EXPLAIN:\n{summary.raw}"
    )
    assert not summary.has_full_sort, (
        f"Full Sort node detected at calibrated scale — the planner has "
        f"abandoned the date-ordered walk despite the calibration gate "
        f"passing. Likely a new plan family; inspect raw EXPLAIN.\n\n"
        f"Raw EXPLAIN:\n{summary.raw}"
    )
