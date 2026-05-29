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
