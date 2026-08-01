# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unit tests for the pure DB-row -> AccountConfig adapter."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from localmail.api.admin.accounts import Account
from localmail.config import AccountConfig
from localmail.daemon_accounts import account_config_from_row


def _row(**over) -> Account:
    base = dict(
        id=1,
        name="acct",
        email_address="me@example.com",
        auth_method="password",
        oauth_provider=None,
        imap_host="imap.example.com",
        imap_port=993,
        folder_allow=None,
        folder_deny=None,
        folder_deny_flags=None,
        sync_enabled=True,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    base.update(over)
    return Account(**base)


def test_maps_password_row():
    assert account_config_from_row(_row()) == AccountConfig(
        name="acct",
        email="me@example.com",
        imap_host="imap.example.com",
        imap_port=993,
        auth_method="password",
    )


def test_legacy_name_from_the_db_maps_without_raising():
    """#217's rule is a *create* boundary, not a read one.

    A pre-#217 release could seed a colon-carrying name into `accounts` —
    `create_account` only checked blank/length. Enforcing the rule on the
    `AccountConfig` field instead of on `Config` would make this adapter raise,
    and `Daemon._spawn_account` calls it unguarded: one legacy row would stop
    every account's sync thread, with no remedy (`name` is not updatable).
    """
    cfg = account_config_from_row(_row(name="gmail:refresh"))
    assert cfg.name == "gmail:refresh"


def test_none_folder_lists_become_empty():
    cfg = account_config_from_row(
        _row(folder_allow=None, folder_deny=None, folder_deny_flags=None)
    )
    assert cfg.folder_allow == []
    assert cfg.folder_deny == []
    assert cfg.folder_deny_flags == []


def test_populated_folder_lists_pass_through():
    cfg = account_config_from_row(
        _row(folder_allow=["INBOX"], folder_deny=["Spam"], folder_deny_flags=["\\Trash"])
    )
    assert cfg.folder_allow == ["INBOX"]
    assert cfg.folder_deny == ["Spam"]
    assert cfg.folder_deny_flags == ["\\Trash"]


def test_oauth2_row_maps_provider():
    cfg = account_config_from_row(_row(auth_method="oauth2", oauth_provider="gmail"))
    assert cfg.auth_method == "oauth2"
    assert cfg.oauth_provider == "gmail"


def test_poll_seconds_is_none():
    assert account_config_from_row(_row()).poll_seconds is None


def test_archive_row_raises():
    with pytest.raises(ValueError, match="archive"):
        account_config_from_row(
            _row(auth_method="archive", imap_host=None, imap_port=None)
        )


def test_live_row_missing_host_raises():
    with pytest.raises(ValueError, match="imap_host"):
        account_config_from_row(_row(imap_host=None))
