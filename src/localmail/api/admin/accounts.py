"""Service layer for admin-UI account management (Sub-plan 2A).

Pure functions over a psycopg connection. No FastAPI imports; no IO beyond
the connection passed in. Field validation is shared with the daemon via
the AccountConfig model in localmail.config.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg

from localmail.api.errors import NotFound


AuthMethod = Literal['password', 'oauth2', 'archive']


@dataclass(frozen=True)
class AccountSummary:
    id: int
    name: str
    email_address: str
    auth_method: AuthMethod
    sync_enabled: bool


@dataclass(frozen=True)
class Account:
    id: int
    name: str
    email_address: str
    auth_method: AuthMethod
    oauth_provider: str | None
    imap_host: str | None
    imap_port: int | None
    folder_allow: list[str] | None
    folder_deny: list[str] | None
    folder_deny_flags: list[str] | None
    sync_enabled: bool
    created_at: datetime
    updated_at: datetime


# Column order below MUST stay in sync with Account field order — get_account
# uses Account(*row) which is a positional unpack. mypy cannot catch a mismatch.
_SELECT_FULL = """
    SELECT id, name, email_address, auth_method, oauth_provider,
           imap_host, imap_port,
           folder_allow, folder_deny, folder_deny_flags,
           sync_enabled, created_at, updated_at
      FROM accounts
"""


def list_accounts(conn: psycopg.Connection) -> list[AccountSummary]:
    """Return every configured account, oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, email_address, auth_method, sync_enabled "
            "FROM accounts ORDER BY id"
        )
        return [AccountSummary(*row) for row in cur.fetchall()]


def get_account(conn: psycopg.Connection, account_id: int) -> Account:
    """Return one account by id. Raises NotFound if absent."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_FULL + " WHERE id = %s", (account_id,))
        row = cur.fetchone()
    if row is None:
        raise NotFound(f"account {account_id} not found")
    return Account(*row)
