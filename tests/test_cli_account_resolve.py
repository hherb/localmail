"""Pure resolver: DB row vs seed-from-TOML vs not-found. No IO."""
from __future__ import annotations

from datetime import datetime, timezone

from localmail.api.admin.accounts import Account
from localmail.cli_account_resolve import (
    Found, NotFound, SeedThenUse, plan_account_resolution,
)
from localmail.config import AccountConfig


def _db_account(name: str) -> Account:
    now = datetime(2026, 5, 30, tzinfo=timezone.utc)
    return Account(
        id=1, name=name, email_address=f"{name}@example.com",
        auth_method="password", oauth_provider=None,
        imap_host="imap.example.com", imap_port=993,
        folder_allow=None, folder_deny=None, folder_deny_flags=None,
        sync_enabled=True, created_at=now, updated_at=now,
    )


def _toml_account(name: str) -> AccountConfig:
    return AccountConfig(
        name=name, email=f"{name}@example.com",
        imap_host="imap.example.com", imap_port=993,
        auth_method="password", oauth_provider=None,
    )


def test_found_when_in_db():
    db = {"work": _db_account("work")}
    res = plan_account_resolution("work", [_toml_account("work")], db)
    assert isinstance(res, Found)
    assert res.account.name == "work"


def test_seed_when_only_in_toml():
    res = plan_account_resolution("work", [_toml_account("work")], {})
    assert isinstance(res, SeedThenUse)
    assert res.config.name == "work"


def test_not_found_when_in_neither():
    res = plan_account_resolution("ghost", [_toml_account("work")], {})
    assert isinstance(res, NotFound)
    assert res.name == "ghost"


def test_db_wins_over_toml():
    db = {"work": _db_account("work")}
    res = plan_account_resolution("work", [_toml_account("work")], db)
    assert isinstance(res, Found)  # never SeedThenUse when the row exists
