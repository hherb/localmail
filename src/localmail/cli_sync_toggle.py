# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Pure decision planner for the enable-account / disable-account CLI commands.

IO-free: given an account's auth_method, its current ``sync_enabled`` value, and
the desired target, decide whether the command should reject (archive rows have
no sync), do nothing (already in the target state), or apply the change. The CLI
maps the resulting action to side effects — reject -> ClickException,
noop -> echo only, apply -> update_account + echo. Mirrors the
``cli_account_resolve`` planner idiom so the branching stays unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ToggleAction = Literal["reject", "noop", "apply"]


@dataclass(frozen=True)
class SyncTogglePlan:
    """What enable/disable-account should do for one account row."""

    action: ToggleAction
    message: str


def plan_sync_toggle(*, name: str, auth_method: str,
                     currently_enabled: bool, enable: bool) -> SyncTogglePlan:
    """Decide the outcome of enable/disable-account for one account.

    - archive accounts never sync, so toggling is rejected either direction;
    - a no-op (already in the target state) succeeds without a DB write;
    - otherwise the change is applied.
    """
    state_word = "enabled" if enable else "disabled"
    if auth_method == "archive":
        return SyncTogglePlan(
            action="reject",
            message=f"account {name!r} is an archive account; "
                    f"sync cannot be {state_word}",
        )
    if currently_enabled == enable:
        return SyncTogglePlan(
            action="noop",
            message=f"account {name!r} sync already {state_word}",
        )
    return SyncTogglePlan(
        action="apply",
        message=f"account {name!r} sync {state_word}",
    )
