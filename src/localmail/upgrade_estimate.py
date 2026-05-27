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

_INDEX_NAMES_0006 = (
    "messages_fts_v2_idx",
    "message_chunks_fts_idx",
)


def _table_exists(conn: psycopg.Connection, relname: str) -> bool:
    """Cheap catalog lookup. to_regclass() returns NULL (not exception)
    for a missing relation."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (relname,))
        row = cur.fetchone()
        assert row is not None
        return row[0] is not None


def _safe_relation_size(conn: psycopg.Connection, relname: str) -> int | None:
    """Return pg_total_relation_size(relname) in bytes, or None if missing.

    Uses to_regclass() so a missing relation surfaces as NULL rather
    than throwing UndefinedTable. Reads only — no lock beyond
    AccessShareLock on pg_class.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_total_relation_size(to_regclass(%s))",
            (relname,),
        )
        row = cur.fetchone()
        assert row is not None
        if row[0] is None:
            return None
        return int(row[0])


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
        return _estimate_0006_applied(conn)
    return _estimate_0006_pending(conn, cfg)


_CHUNKS_GIN_EMPTY_WARNING = (
    "message_chunks GIN size cannot be projected before chunks exist; "
    "rerun after the embed worker has populated chunks for an accurate "
    "estimate."
)


def _project_chunks_gin_bytes(
    conn: psycopg.Connection,
    cfg: UpgradeEstimateConfig,
    warnings: list[str],
) -> int:
    """Project bytes-on-disk for ``message_chunks_fts_idx`` post-migration.

    Returns 0 and appends ``_CHUNKS_GIN_EMPTY_WARNING`` to ``warnings`` if
    ``message_chunks`` is empty or missing — same honest-zero contract as
    the original implementation, just scoped to one helper so the
    pending-branch math stays readable.

    Mirrors the messages-GIN formula:
        rows × avg(octet_length(text)) × fts_v2_blowup × gin_size

    ``octet_length`` is intentional (matches the messages-side projection
    so non-ASCII chunks don't underestimate the disk footprint).
    """
    if not _table_exists(conn, "message_chunks"):
        warnings.append(_CHUNKS_GIN_EMPTY_WARNING)
        return 0

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), avg(octet_length(text)) FROM message_chunks"
        )
        row = cur.fetchone()
        assert row is not None
        chunks_count = int(row[0])
        avg_chunk_text_bytes = float(row[1] or 0.0)

    if chunks_count == 0:
        warnings.append(_CHUNKS_GIN_EMPTY_WARNING)
        return 0

    return int(
        chunks_count
        * avg_chunk_text_bytes
        * cfg.fts_v2_blowup_factor
        * cfg.gin_size_factor
    )


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

        # octet_length() returns UTF-8 byte count, not character count —
        # the projection is in bytes, so this matters for CJK / emoji /
        # any non-ASCII content where the byte count is 2-4x the
        # character count.
        cur.execute(
            """
            SELECT avg(
                octet_length(coalesce(subject, ''))
                + octet_length(coalesce(body_text, ''))
                + octet_length(coalesce(body_html, ''))
            )
            FROM messages
            """
        )
        row = cur.fetchone()
        assert row is not None
        avg_text_bytes = float(row[0] or 0.0)

    projected_fts_v2 = int(rows * avg_text_bytes * cfg.fts_v2_blowup_factor)
    projected_gin_messages = int(projected_fts_v2 * cfg.gin_size_factor)

    # chunks GIN: project from message_chunks when populated (the embed
    # worker may have run between 0004 and 0006), else fall back to 0
    # with a warning so the output stays honest. Mirrors the messages
    # GIN formula: text bytes × blowup × gin_size.
    projected_gin_chunks = _project_chunks_gin_bytes(conn, cfg, warnings)

    rewrite_duration = (
        current_table_bytes + projected_fts_v2
    ) / (cfg.table_rewrite_mb_per_sec * _MIB)
    # Both GIN builds contribute to the lock window; sum them so an
    # operator with a populated message_chunks table doesn't undersize
    # their maintenance window.
    gin_duration = (
        projected_gin_messages + projected_gin_chunks
    ) / (cfg.gin_build_mb_per_sec * _MIB)
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


def _estimate_0006_applied(conn: psycopg.Connection) -> EstimateResult:
    """Report actual sizes for migration 0006's two GIN indexes.

    Takes no ``cfg`` — the applied branch reports observed sizes only,
    no projection math.

    If a named index is missing (operator manually dropped it), a
    warning is appended and that index's size key is omitted from
    ``current_bytes``. The call never raises for missing-index.
    """
    current_bytes: dict[str, int] = {}
    warnings: list[str] = []

    for idx_name in _INDEX_NAMES_0006:
        size = _safe_relation_size(conn, idx_name)
        if size is None:
            warnings.append(
                f"index {idx_name} missing despite migration applied"
            )
            continue
        current_bytes[idx_name] = size

    return EstimateResult(
        revision="0006_search_indexes",
        status="applied",
        current_bytes=current_bytes,
        projected_bytes={},
        projected_duration_s=0.0,
        warnings=warnings,
    )


ESTIMATORS["0006_search_indexes"] = estimate_0006
