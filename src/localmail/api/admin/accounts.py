"""Service layer for admin-UI account management (Sub-plan 2A).

Pure functions over a psycopg connection. No FastAPI imports; no IO beyond
the connection passed in. Field validation is shared with the daemon via
the AccountConfig model in localmail.config.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

import psycopg
from psycopg.types.json import Jsonb

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


class AccountFieldError(ValueError):
    """Raised when field validation rejects a create/update."""


_NAME_MAX = 128
_HOSTNAME_MAX = 255
_PORT_MIN = 1
_PORT_MAX = 65535


def _validate_create_fields(*, name: str, email_address: str,
                             auth_method: str, imap_host: str | None,
                             imap_port: int | None,
                             oauth_provider: str | None) -> None:
    if not name or not name.strip():
        raise AccountFieldError("name must not be blank")
    if len(name) > _NAME_MAX:
        raise AccountFieldError(f"name longer than {_NAME_MAX} chars")
    if auth_method not in ('password', 'oauth2', 'archive'):
        raise AccountFieldError(f"unknown auth_method {auth_method!r}")
    if auth_method == 'archive':
        if imap_host is not None or imap_port is not None:
            raise AccountFieldError(
                "archive accounts must not have imap_host/imap_port"
            )
    else:
        if not imap_host:
            raise AccountFieldError("imap_host required for live accounts")
        if len(imap_host) > _HOSTNAME_MAX:
            raise AccountFieldError("imap_host too long")
        if imap_port is None or not (_PORT_MIN <= imap_port <= _PORT_MAX):
            raise AccountFieldError("imap_port required and in 1..65535")
    if auth_method == 'oauth2' and oauth_provider not in ('gmail',):
        raise AccountFieldError(
            "oauth2 accounts require oauth_provider='gmail'"
        )


def _validate_update_field_combo(*, current_method: str,
                                  new_method: str | None,
                                  imap_host: str | None,
                                  imap_port: int | None) -> None:
    method = new_method or current_method
    if method == 'archive' and (imap_host is not None or imap_port is not None):
        raise AccountFieldError(
            "archive accounts must not have imap_host/imap_port"
        )


def create_account(
    conn: psycopg.Connection,
    *,
    name: str,
    email_address: str,  # required: column is NOT NULL in 0001_init.sql
    auth_method: AuthMethod,
    imap_host: str | None,
    imap_port: int | None,
    oauth_provider: str | None,
    folder_allow: list[str] | None,
    folder_deny: list[str] | None,
    folder_deny_flags: list[str] | None,
) -> Account:
    """Insert a new account row and return it."""
    _validate_create_fields(
        name=name, email_address=email_address, auth_method=auth_method,
        imap_host=imap_host, imap_port=imap_port,
        oauth_provider=oauth_provider,
    )
    with conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO accounts (name, email_address, auth_method, "
                "  oauth_provider, imap_host, imap_port, "
                "  folder_allow, folder_deny, folder_deny_flags, config) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb) "
                "RETURNING id",
                (
                    name, email_address, auth_method, oauth_provider,
                    imap_host, imap_port,
                    Jsonb(folder_allow) if folder_allow is not None else None,
                    Jsonb(folder_deny) if folder_deny is not None else None,
                    Jsonb(folder_deny_flags) if folder_deny_flags is not None else None,
                ),
            )
        except psycopg.errors.UniqueViolation as e:
            raise AccountFieldError(f"account name {name!r} already exists") from e
        row = cur.fetchone()
        assert row is not None
        new_id = row[0]
    return get_account(conn, new_id)


_UPDATABLE = {
    'email_address', 'auth_method', 'oauth_provider',
    'imap_host', 'imap_port',
    'folder_allow', 'folder_deny', 'folder_deny_flags',
    'sync_enabled',
}


def update_account(conn: psycopg.Connection, account_id: int,
                   **fields: object) -> Account:
    """Partial-update an account. Bumps updated_at = now()."""
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise AccountFieldError(f"unknown fields: {sorted(unknown)}")
    if not fields:
        return get_account(conn, account_id)

    current = get_account(conn, account_id)
    _validate_update_field_combo(
        current_method=current.auth_method,
        new_method=cast('str | None', fields.get('auth_method')),
        imap_host=cast('str | None', fields.get('imap_host', current.imap_host)),
        imap_port=cast('int | None', fields.get('imap_port', current.imap_port)),
    )

    set_sql_parts: list[str] = []
    values: list[object] = []
    for key, val in fields.items():
        if key in ('folder_allow', 'folder_deny', 'folder_deny_flags'):
            set_sql_parts.append(f"{key} = %s")
            values.append(Jsonb(val) if val is not None else None)
        else:
            set_sql_parts.append(f"{key} = %s")
            values.append(val)
    set_sql_parts.append("updated_at = now()")
    values.append(account_id)

    with conn.cursor() as cur:
        try:
            cur.execute(
                f"UPDATE accounts SET {', '.join(set_sql_parts)} WHERE id = %s",
                values,
            )
        except (psycopg.errors.UniqueViolation, psycopg.errors.CheckViolation) as e:
            raise AccountFieldError(str(e).split('\n', 1)[0]) from e
        if cur.rowcount == 0:
            raise NotFound(f"account {account_id} not found")
    return get_account(conn, account_id)


class AccountInUse(RuntimeError):
    """Raised when delete is refused because messages reference the account."""


def delete_account(conn: psycopg.Connection, account_id: int,
                   *, force: bool = False) -> None:
    """Delete an account. Refuses if messages reference it unless force=True."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM accounts WHERE id = %s", (account_id,))
        if cur.fetchone() is None:
            raise NotFound(f"account {account_id} not found")
        if not force:
            cur.execute(
                "SELECT 1 FROM messages WHERE account_id = %s LIMIT 1",
                (account_id,))
            if cur.fetchone() is not None:
                raise AccountInUse(
                    f"account {account_id} still has messages; pass force=True"
                )
        cur.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
