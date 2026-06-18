# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Bridge DB account rows to the daemon's AccountConfig worker boundary.

Pure: no IO. The daemon enumerates syncable accounts from the DB
(`api.admin.accounts.list_syncable_accounts`) and maps each row through
`account_config_from_row`, so the existing AccountConfig-based worker code
(imap_client, idle, poller, sync) is unchanged.
"""

from __future__ import annotations

from typing import Literal, cast

from localmail.api.admin.accounts import Account
from localmail.config import AccountConfig


def account_config_from_row(account: Account) -> AccountConfig:
    """Map a DB ``Account`` row to the daemon's ``AccountConfig``.

    Raises ``ValueError`` for archive accounts (no IMAP host) — callers
    filter these out via ``list_syncable_accounts`` before mapping; the
    guard is defensive. Per-account ``poll_seconds`` has no DB column, so it
    is always ``None`` (the daemon falls back to the daemon-wide default).
    """
    if account.auth_method == "archive":
        raise ValueError(
            f"account {account.name!r} is an archive account and has no IMAP source"
        )
    if account.imap_host is None or account.imap_port is None:
        raise ValueError(
            f"live account {account.name!r} is missing imap_host/imap_port"
        )
    return AccountConfig(
        name=account.name,
        email=account.email_address,
        imap_host=account.imap_host,
        imap_port=account.imap_port,
        auth_method=account.auth_method,
        oauth_provider=cast("Literal['gmail'] | None", account.oauth_provider),
        folder_allow=account.folder_allow or [],
        folder_deny=account.folder_deny or [],
        folder_deny_flags=account.folder_deny_flags or [],
    )
