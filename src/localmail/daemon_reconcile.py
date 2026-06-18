# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Pure account-reconcile diff for the daemon's hot-reload (2B.1).

No IO, no threads. The daemon reads the desired syncable account set from the
DB and compares it against the threads it currently runs; this module turns the
two ``{account_id: updated_at}`` fingerprint maps into a spawn/teardown/respawn
plan. Keyed on ``updated_at`` so any change to an account row (config edit or a
credential touch) forces a respawn; only inequality matters, so writer clock
skew is harmless.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True)
class ReconcilePlan:
    to_spawn: tuple[int, ...]      # in desired, not running
    to_teardown: tuple[int, ...]   # running, not in desired
    to_respawn: tuple[int, ...]    # in both, updated_at differs

    @property
    def is_empty(self) -> bool:
        return not (self.to_spawn or self.to_teardown or self.to_respawn)


def plan_reconcile(
    running: Mapping[int, datetime],
    desired: Mapping[int, datetime],
) -> ReconcilePlan:
    """Diff the running fingerprints against the desired ones.

    ``running`` / ``desired`` map ``account_id`` to the ``updated_at`` the
    bundle was spawned with / the current DB value. Returns sorted, disjoint
    id tuples so the caller's apply order is deterministic.
    """
    running_ids = set(running)
    desired_ids = set(desired)
    to_spawn = tuple(sorted(desired_ids - running_ids))
    to_teardown = tuple(sorted(running_ids - desired_ids))
    to_respawn = tuple(
        sorted(
            aid
            for aid in running_ids & desired_ids
            if running[aid] != desired[aid]
        )
    )
    return ReconcilePlan(
        to_spawn=to_spawn, to_teardown=to_teardown, to_respawn=to_respawn
    )
