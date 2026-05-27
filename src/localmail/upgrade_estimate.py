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


def estimate_0006(
    conn: psycopg.Connection,
    cfg: UpgradeEstimateConfig,
    applied: bool,
) -> EstimateResult:
    """Stub — fully implemented in Task 3 and Task 4."""
    raise NotImplementedError("estimate_0006 lands in Task 3")


ESTIMATORS["0006_search_indexes"] = estimate_0006
