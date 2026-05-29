"""Tests for the TOML->DB account seed (init-db)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from localmail.api.admin.accounts import (
    Account,
    create_account,
    list_accounts_full,
)
from localmail.config import AccountConfig

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _cfg(name: str, **overrides) -> AccountConfig:
    """An AccountConfig with sensible live-IMAP defaults."""
    base = dict(
        name=name,
        email=f"{name}@example.com",
        imap_host="imap.example.com",
        imap_port=993,
        auth_method="password",
        oauth_provider=None,
        folder_allow=[],
        folder_deny=[],
        folder_deny_flags=[],
    )
    base.update(overrides)
    return AccountConfig(**base)


def _db_account(name: str, **overrides) -> Account:
    """An Account (DB-row dataclass) for pure-planner tests."""
    base = dict(
        id=1,
        name=name,
        email_address=f"{name}@example.com",
        auth_method="password",
        oauth_provider=None,
        imap_host="imap.example.com",
        imap_port=993,
        folder_allow=[],
        folder_deny=[],
        folder_deny_flags=[],
        sync_enabled=True,
        created_at=_T0,
        updated_at=_T0,
    )
    base.update(overrides)
    return Account(**base)


def test_list_accounts_full_returns_full_rows(db_conn) -> None:
    create_account(
        db_conn, name="alice", email_address="alice@example.com",
        auth_method="password", imap_host="imap.example.com", imap_port=993,
        oauth_provider=None, folder_allow=["INBOX"], folder_deny=[],
        folder_deny_flags=["\\Trash"],
    )
    db_conn.commit()

    rows = list_accounts_full(db_conn)

    assert [r.name for r in rows] == ["alice"]
    row = rows[0]
    assert row.email_address == "alice@example.com"
    assert row.imap_host == "imap.example.com"
    assert row.folder_allow == ["INBOX"]
    assert row.folder_deny_flags == ["\\Trash"]
    assert row.sync_enabled is True


from localmail.account_seed import (
    AccountDrift,
    SeedPlan,
    plan_account_seed,
)


def test_plan_empty_config_is_empty_plan() -> None:
    plan = plan_account_seed([], {})
    assert plan == SeedPlan(to_insert=[], drift=[])


def test_plan_all_new_names_all_insert() -> None:
    cfgs = [_cfg("alice"), _cfg("bob")]
    plan = plan_account_seed(cfgs, {})
    assert plan.to_insert == cfgs
    assert plan.drift == []


def test_plan_identical_match_is_skipped_no_drift() -> None:
    cfg = _cfg("alice")
    existing = {"alice": _db_account("alice")}
    plan = plan_account_seed([cfg], existing)
    assert plan.to_insert == []
    assert plan.drift == []


def test_plan_single_field_drift_lists_that_field() -> None:
    cfg = _cfg("alice", imap_port=143)
    existing = {"alice": _db_account("alice", imap_port=993)}
    plan = plan_account_seed([cfg], existing)
    assert plan.to_insert == []
    assert plan.drift == [AccountDrift(name="alice", fields=["imap_port"])]


def test_plan_multi_field_drift_lists_all() -> None:
    cfg = _cfg("alice", imap_port=143, email="new@example.com")
    existing = {"alice": _db_account("alice", imap_port=993,
                                     email_address="old@example.com")}
    plan = plan_account_seed([cfg], existing)
    assert plan.to_insert == []
    assert len(plan.drift) == 1
    assert set(plan.drift[0].fields) == {"imap_port", "email_address"}


def test_plan_folder_none_vs_empty_is_not_drift() -> None:
    cfg = _cfg("alice", folder_allow=[])
    existing = {"alice": _db_account("alice", folder_allow=None)}
    plan = plan_account_seed([cfg], existing)
    assert plan.drift == []


def test_plan_folder_order_matters() -> None:
    cfg = _cfg("alice", folder_allow=["A", "B"])
    existing = {"alice": _db_account("alice", folder_allow=["B", "A"])}
    plan = plan_account_seed([cfg], existing)
    assert plan.drift == [AccountDrift(name="alice", fields=["folder_allow"])]


def test_plan_mixed_batch() -> None:
    cfgs = [
        _cfg("new"),                       # insert
        _cfg("same"),                      # skip, no drift
        _cfg("drift", imap_port=143),      # skip, drift
    ]
    existing = {
        "same": _db_account("same"),
        "drift": _db_account("drift", imap_port=993),
    }
    plan = plan_account_seed(cfgs, existing)
    assert [c.name for c in plan.to_insert] == ["new"]
    assert plan.drift == [AccountDrift(name="drift", fields=["imap_port"])]
