"""Unit tests for the pure enable/disable-account decision planner."""

from __future__ import annotations

import pytest

from localmail.cli_sync_toggle import SyncTogglePlan, plan_sync_toggle


@pytest.mark.parametrize("enable", [True, False])
def test_archive_account_is_rejected_either_direction(enable: bool) -> None:
    plan = plan_sync_toggle(
        name="arc", auth_method="archive",
        currently_enabled=False, enable=enable,
    )
    assert plan.action == "reject"
    assert "arc" in plan.message
    assert "archive" in plan.message


def test_enabling_already_enabled_is_noop() -> None:
    plan = plan_sync_toggle(
        name="work", auth_method="password",
        currently_enabled=True, enable=True,
    )
    assert plan.action == "noop"
    assert "already" in plan.message
    assert "enabled" in plan.message


def test_disabling_already_disabled_is_noop() -> None:
    plan = plan_sync_toggle(
        name="work", auth_method="oauth2",
        currently_enabled=False, enable=False,
    )
    assert plan.action == "noop"
    assert "already" in plan.message
    assert "disabled" in plan.message


def test_enabling_disabled_account_applies() -> None:
    plan = plan_sync_toggle(
        name="work", auth_method="password",
        currently_enabled=False, enable=True,
    )
    assert plan.action == "apply"
    assert "enabled" in plan.message
    assert "already" not in plan.message


def test_disabling_enabled_account_applies() -> None:
    plan = plan_sync_toggle(
        name="work", auth_method="oauth2",
        currently_enabled=True, enable=False,
    )
    assert plan.action == "apply"
    assert "disabled" in plan.message
    assert "already" not in plan.message


def test_plan_is_frozen() -> None:
    plan = SyncTogglePlan(action="noop", message="x")
    with pytest.raises(Exception):
        plan.action = "apply"  # type: ignore[misc]
