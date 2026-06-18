# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unit tests for pure helpers in ``tests/acceptance/run_browse_explain.py``.

The harness itself is a script that needs a live Postgres; these tests
exercise its decoupled helpers in isolation:

* ``mid_cursor_from_seed`` — derives the mid-keyset cursor from the
  ``SeedConfig`` alone, no DB query (replaces the linear
  ``OFFSET COUNT(*)/2`` scan that issue #79 flagged).
* ``_scan_actual_rows`` — parses the top-node row count from
  ``EXPLAIN ANALYZE`` output. Postgres 17 and earlier emit integer
  ``rows=N`` inside the ``(actual time=... rows=N loops=M)`` group;
  Postgres 18+ emits fractional ``rows=N.NN``. The PR #82 handoff
  noted the previous parser searched for the non-existent literal
  ``"actual rows="`` substring and therefore always returned 0.
"""
from __future__ import annotations

from datetime import timedelta, timezone

from tests.acceptance.browse_explain_lib import (
    DATE_SPAN_DAYS,
    EPOCH_ANCHOR,
    SeedConfig,
    _scan_actual_rows,
    mid_cursor_from_seed,
)


# ---- mid_cursor_from_seed ---------------------------------------------


def test_mid_cursor_uses_anchor_plus_half_span() -> None:
    """The 50th-percentile date is ``anchor + date_span_days/2`` days.

    The seed places dates uniformly in ``[anchor, anchor + span]``, so
    half the rows lie below this midpoint and half above. This is the
    direct replacement for the OFFSET-based scan that #79 retires.
    """
    cfg = SeedConfig(total_rows=100_000, num_accounts=5, distribution="skewed")
    ts, _ = mid_cursor_from_seed(cfg)
    expected_ts = EPOCH_ANCHOR + timedelta(days=cfg.date_span_days / 2)
    assert ts == expected_ts


def test_mid_cursor_id_is_half_of_total_rows() -> None:
    """``id`` is ``total_rows // 2``.

    The synthetic seed uses dense BIGSERIAL ids; the secondary sort
    key only matters as a tie-breaker, so any id inside the seeded
    range is valid. ``total_rows // 2`` keeps the cursor near the
    middle of the relation just like the OFFSET scan did.
    """
    cfg = SeedConfig(total_rows=100_000, num_accounts=5, distribution="skewed")
    _, mid_id = mid_cursor_from_seed(cfg)
    assert mid_id == cfg.total_rows // 2


def test_mid_cursor_respects_custom_date_span_days() -> None:
    """A non-default ``date_span_days`` produces a proportional midpoint.

    Locks the contract so future tuning of the seed date range doesn't
    silently drift the mid-keyset probe to a stale anchor offset.
    """
    cfg = SeedConfig(
        total_rows=10_000, num_accounts=2, distribution="balanced",
        date_span_days=200,
    )
    ts, _ = mid_cursor_from_seed(cfg)
    assert ts == EPOCH_ANCHOR + timedelta(days=100.0)


def test_mid_cursor_returns_timezone_aware_datetime() -> None:
    """The cursor's ``ts`` must be tz-aware so it round-trips through
    the ``timestamptz`` columns the harness queries. Naive datetimes
    would silently become ``UTC+local`` on the wire."""
    cfg = SeedConfig(total_rows=1_000, num_accounts=1, distribution="balanced")
    ts, _ = mid_cursor_from_seed(cfg)
    assert ts.tzinfo is not None
    assert ts.utcoffset() == timezone.utc.utcoffset(ts)


def test_mid_cursor_is_pure_no_database_needed() -> None:
    """Smoke test: derivation does not touch any global state or IO.

    Calling it twice with the same config yields the same cursor, and
    no ``conn`` argument is accepted (a regression would either reject
    the call or hit the network)."""
    cfg = SeedConfig(total_rows=1_000, num_accounts=1, distribution="balanced")
    first = mid_cursor_from_seed(cfg)
    second = mid_cursor_from_seed(cfg)
    assert first == second


def test_mid_cursor_scales_with_total_rows() -> None:
    """``total_rows`` drives ``id`` but never the timestamp.

    Issue #79's complaint is precisely that the OFFSET-based picker
    scaled linearly with ``--total-rows``. The pure derivation must
    not — date is anchor-derived, id is a simple integer halving."""
    a = mid_cursor_from_seed(
        SeedConfig(total_rows=10_000, num_accounts=1, distribution="balanced")
    )
    b = mid_cursor_from_seed(
        SeedConfig(total_rows=5_000_000, num_accounts=1, distribution="balanced")
    )
    assert a[0] == b[0]
    assert a[1] != b[1]
    assert b[1] == 5_000_000 // 2


# ---- _scan_actual_rows --------------------------------------------------


_PG18_RESULT_LINE = (
    "Result  (cost=0.00..0.01 rows=1 width=4) "
    "(actual time=0.000..0.000 rows=1.00 loops=1)"
)
_PG17_RESULT_LINE = (
    "Result  (cost=0.00..0.01 rows=1 width=4) "
    "(actual time=0.000..0.000 rows=1 loops=1)"
)
_PG18_INDEX_SCAN_LINE = (
    "Index Scan using messages_recent_idx on messages m  "
    "(cost=0.42..1234.56 rows=1000 width=128) "
    "(actual time=0.005..1.234 rows=51.00 loops=1)"
)


def test_scan_actual_rows_parses_pg18_fractional_rows() -> None:
    """PG 18 emits ``rows=N.NN`` (loop-averaged). The parser must take
    the integer part and not return 0 like the pre-#79 version did."""
    assert _scan_actual_rows([_PG18_RESULT_LINE]) == 1


def test_scan_actual_rows_parses_pg17_integer_rows() -> None:
    """PG ≤17 emits ``rows=N`` (integer). Same parse, different lexeme."""
    assert _scan_actual_rows([_PG17_RESULT_LINE]) == 1


def test_scan_actual_rows_ignores_planner_estimate() -> None:
    """The cost-group also contains ``rows=N`` (planner estimate). We
    want the *actual*; mixing the two would silently report the wrong
    metric on every probe."""
    assert _scan_actual_rows([_PG18_INDEX_SCAN_LINE]) == 51


def test_scan_actual_rows_returns_zero_when_no_actual_line() -> None:
    """No ``actual time=`` group anywhere → 0. Happens when EXPLAIN
    was run without ANALYZE."""
    lines = [
        "Result  (cost=0.00..0.01 rows=1 width=4)",
        "Planning Time: 0.030 ms",
    ]
    assert _scan_actual_rows(lines) == 0


def test_scan_actual_rows_returns_first_node_count() -> None:
    """Pulls the top-node row count, not a nested child node. The
    first matching line wins so the result reflects what the query
    actually returned (post-LIMIT), not an inner scan count."""
    lines = [
        "Limit  (cost=0..1 rows=51 width=128) "
        "(actual time=0.5..1.0 rows=51.00 loops=1)",
        "  ->  Index Scan using messages_recent_idx on messages m  "
        "(actual time=0.005..1.234 rows=100000.00 loops=1)",
    ]
    assert _scan_actual_rows(lines) == 51


def test_scan_actual_rows_handles_malformed_token() -> None:
    """A truncated line ('actual time=X..Y rows=' with nothing after)
    must not crash — it just falls through to the next line."""
    lines = [
        "Bogus  (actual time=0.0..0.0 rows= loops=1)",
        "Result  (actual time=0.0..0.0 rows=7.00 loops=1)",
    ]
    assert _scan_actual_rows(lines) == 7


def test_scan_actual_rows_handles_empty_input() -> None:
    """Empty list → 0 (no crashing iter exhaustion)."""
    assert _scan_actual_rows([]) == 0


# ---- Cross-check: default SeedConfig still matches module constants ----


def test_seed_config_defaults_match_module_constants() -> None:
    """If the module-level seed constants drift, the mid-cursor docs
    in NEXT_SESSION.md / CLAUDE.md become stale. Pin them here so any
    intentional change forces a test update."""
    cfg = SeedConfig(total_rows=1, num_accounts=1, distribution="balanced")
    assert cfg.date_span_days == DATE_SPAN_DAYS
