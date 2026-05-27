"""Pre-flight estimator for lock-heavy schema migrations.

See:
  * docs/superpowers/specs/2026-05-27-large-archive-upgrade-estimator-design.md
  * docs/operations/upgrade-runbook.md
  * GitHub issue #2

The module exposes:
  * ``EstimateResult`` — frozen dataclass returned by every estimator.
  * ``ESTIMATORS`` — registry mapping revision name to estimator function.
  * ``estimate_0006`` — projects size + duration for migration 0006.

Adding an estimator for another migration:
  * Implement ``estimate_NNNN(conn, cfg, applied) -> EstimateResult``.
  * Register it in ``ESTIMATORS``.
  * Add a test class to ``tests/test_upgrade_estimate.py``.
  * Add a section to the runbook.

The module performs no IO except via the injected ``psycopg.Connection``.
This keeps it reusable from a future HTTP route or MCP tool without
refactor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal

import psycopg

from localmail.config import UpgradeEstimateConfig


@dataclass(frozen=True)
class EstimateResult:
    """One row of `localmail estimate-upgrade` output.

    ``current_bytes`` is populated when ``status == "applied"`` (with
    keys naming the indexes / columns whose sizes were read from
    ``pg_total_relation_size``). ``projected_bytes`` and
    ``projected_duration_s`` are populated when ``status == "pending"``
    or ``status == "not_applicable"``. The two are mutually exclusive
    by convention; consumers should only consult the dict that matches
    the status.
    """

    revision: str
    status: Literal["pending", "applied", "not_applicable"]
    current_bytes: dict[str, int]
    projected_bytes: dict[str, int]
    projected_duration_s: float
    warnings: list[str] = field(default_factory=list)


EstimatorFn = Callable[
    [psycopg.Connection, UpgradeEstimateConfig, bool], EstimateResult
]


# Populated below. Declared early so the type alias above can refer to the
# return type without a forward reference.
ESTIMATORS: dict[str, EstimatorFn] = {}


_MIB = 1024 * 1024  # named constant; no magic-number hidden in arithmetic


def _table_exists(conn: psycopg.Connection, relname: str) -> bool:
    """Cheap catalog lookup. to_regclass() returns NULL (not exception)
    for a missing relation."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (relname,))
        row = cur.fetchone()
        assert row is not None
        return row[0] is not None


def estimate_0006(
    conn: psycopg.Connection,
    cfg: UpgradeEstimateConfig,
    applied: bool,
) -> EstimateResult:
    """Project (or report actual) cost of migration 0006_search_indexes.

    Args:
        conn: open psycopg connection. Read-only; no transaction needed.
        cfg: throughput rates + sizing factors.
        applied: caller-supplied result of looking up the revision in
            ``schema_migrations``. Passed in (not queried internally)
            so unit tests can exercise both branches independently of
            fixture state.

    Returns:
        ``EstimateResult`` with ``status="pending"`` (projections),
        ``status="applied"`` (actual sizes), or ``status="not_applicable"``
        (messages table missing — `init-db` has never been run).
    """
    if not _table_exists(conn, "messages"):
        return EstimateResult(
            revision="0006_search_indexes",
            status="not_applicable",
            current_bytes={},
            projected_bytes={},
            projected_duration_s=0.0,
            warnings=["messages table not present — run `localmail init-db` first"],
        )
    if applied:
        return _estimate_0006_applied(conn, cfg)
    return _estimate_0006_pending(conn, cfg)


def _estimate_0006_pending(
    conn: psycopg.Connection,
    cfg: UpgradeEstimateConfig,
) -> EstimateResult:
    warnings: list[str] = []

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        row = cur.fetchone()
        assert row is not None
        rows = int(row[0])

        if rows == 0:
            return EstimateResult(
                revision="0006_search_indexes",
                status="pending",
                current_bytes={},
                projected_bytes={"fts_v2": 0, "gin_messages": 0, "gin_chunks": 0},
                projected_duration_s=0.0,
                warnings=warnings,
            )

        cur.execute("SELECT pg_total_relation_size('messages')")
        row = cur.fetchone()
        assert row is not None
        current_table_bytes = int(row[0])

        cur.execute(
            """
            SELECT avg(
                length(coalesce(subject, ''))
                + length(coalesce(body_text, ''))
                + length(coalesce(body_html, ''))
            )
            FROM messages
            """
        )
        row = cur.fetchone()
        assert row is not None
        avg_text_len = float(row[0] or 0.0)

    projected_fts_v2 = int(rows * avg_text_len * cfg.fts_v2_blowup_factor)
    projected_gin_messages = int(projected_fts_v2 * cfg.gin_size_factor)
    # chunks GIN is independent of the messages fts column — we have no
    # signal here for sizing it accurately because message_chunks is
    # populated by the embed worker, not by the migration itself. Project
    # as zero with a warning so the output stays honest.
    projected_gin_chunks = 0
    warnings.append(
        "message_chunks GIN size cannot be projected before chunks exist; "
        "rerun after the embed worker has populated chunks for an accurate "
        "estimate."
    )

    rewrite_duration = (
        current_table_bytes + projected_fts_v2
    ) / (cfg.table_rewrite_mb_per_sec * _MIB)
    gin_duration = projected_gin_messages / (cfg.gin_build_mb_per_sec * _MIB)
    projected_duration_s = rewrite_duration + gin_duration

    return EstimateResult(
        revision="0006_search_indexes",
        status="pending",
        current_bytes={},
        projected_bytes={
            "fts_v2": projected_fts_v2,
            "gin_messages": projected_gin_messages,
            "gin_chunks": projected_gin_chunks,
        },
        projected_duration_s=projected_duration_s,
        warnings=warnings,
    )


def _estimate_0006_applied(
    conn: psycopg.Connection,
    cfg: UpgradeEstimateConfig,
) -> EstimateResult:
    """Stub — implemented in Task 4."""
    raise NotImplementedError("_estimate_0006_applied lands in Task 4")


ESTIMATORS["0006_search_indexes"] = estimate_0006
