"""Service layer for admin-UI account management (Sub-plan 2A).

Pure functions over a psycopg connection. No FastAPI imports; no IO beyond
the connection passed in. Field validation is shared with the daemon via
the AccountConfig model in localmail.config.
"""

from __future__ import annotations

import imaplib
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

import imapclient.exceptions
import psycopg
from imapclient import IMAPClient
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from localmail import imap_client as _imap
from localmail import secrets as _secrets
from localmail.api.errors import NotFound
from localmail.config import AccountConfig as _AccountConfig


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


# Selected column NAMES must match the Account dataclass field names — both
# reads below use psycopg `class_row`, which maps result columns to
# constructor kwargs by name. Column ORDER is therefore irrelevant, and a
# rename/extra column fails loudly at fetch time rather than silently
# shifting positions (#119).
_SELECT_FULL = """
    SELECT id, name, email_address, auth_method, oauth_provider,
           imap_host, imap_port,
           folder_allow, folder_deny, folder_deny_flags,
           sync_enabled, created_at, updated_at
      FROM accounts
"""


def list_accounts(conn: psycopg.Connection) -> list[AccountSummary]:
    """Return every configured account, oldest first."""
    with conn.cursor(row_factory=class_row(AccountSummary)) as cur:
        cur.execute(
            "SELECT id, name, email_address, auth_method, sync_enabled "
            "FROM accounts ORDER BY id"
        )
        return cur.fetchall()


def get_account(conn: psycopg.Connection, account_id: int) -> Account:
    """Return one account by id. Raises NotFound if absent."""
    with conn.cursor(row_factory=class_row(Account)) as cur:
        cur.execute(_SELECT_FULL + " WHERE id = %s", (account_id,))
        row = cur.fetchone()
    if row is None:
        raise NotFound(f"account {account_id} not found")
    return row


def get_account_by_name(conn: psycopg.Connection, name: str) -> Account | None:
    """Return one account by name, or None if absent.

    Unlike `get_account`, absence returns None (not NotFound): CLI callers
    treat a missing name as a normal branch (seed from TOML, or fail with a
    domain-specific message). Reuses `_SELECT_FULL` so the column shape cannot
    drift from the other readers.
    """
    with conn.cursor(row_factory=class_row(Account)) as cur:
        cur.execute(_SELECT_FULL + " WHERE name = %s", (name,))
        return cur.fetchone()


def list_accounts_full(conn: psycopg.Connection) -> list[Account]:
    """Return every account as a full Account row, oldest first.

    Shares the `_SELECT_FULL` column shape with `get_account` so the two
    cannot drift. Used by the init-db TOML->DB seed to detect config drift.
    """
    with conn.cursor(row_factory=class_row(Account)) as cur:
        cur.execute(_SELECT_FULL + " ORDER BY id")
        return cur.fetchall()


def list_syncable_accounts(conn: psycopg.Connection) -> list[Account]:
    """Return live (non-archive), sync-enabled accounts, oldest first.

    This is the daemon's account source: archive accounts have no IMAP host
    and `sync_enabled = FALSE` accounts are paused, so neither spawns workers.
    Shares `_SELECT_FULL` with `get_account` so the column shape can't drift.
    """
    with conn.cursor(row_factory=class_row(Account)) as cur:
        cur.execute(
            _SELECT_FULL
            + " WHERE auth_method IN ('password', 'oauth2') AND sync_enabled"
            + " ORDER BY id"
        )
        return cur.fetchall()


class AccountFieldError(ValueError):
    """Raised when field validation rejects a create/update."""


_NAME_MAX = 128
_HOSTNAME_MAX = 255
_PORT_MIN = 1
_PORT_MAX = 65535


def _validate_email(email_address: str) -> None:
    if not email_address or not email_address.strip():
        raise AccountFieldError("email_address must not be blank")
    if '@' not in email_address:
        raise AccountFieldError("email_address must contain '@'")


def _validate_combo(*, auth_method: str, imap_host: str | None,
                    imap_port: int | None,
                    oauth_provider: str | None) -> None:
    """Shape rule for (auth_method, host, port, oauth_provider).

    Used by both create and update so the two paths cannot diverge.
    """
    if auth_method not in ('password', 'oauth2', 'archive'):
        raise AccountFieldError(f"unknown auth_method {auth_method!r}")
    if auth_method == 'archive':
        if imap_host is not None or imap_port is not None:
            raise AccountFieldError(
                "archive accounts must not have imap_host/imap_port"
            )
    else:
        if not imap_host:
            raise AccountFieldError("live accounts require imap_host")
        if len(imap_host) > _HOSTNAME_MAX:
            raise AccountFieldError("imap_host too long")
        if imap_port is None or not (_PORT_MIN <= imap_port <= _PORT_MAX):
            raise AccountFieldError(
                "live accounts require imap_port in 1..65535"
            )
    if auth_method == 'oauth2' and oauth_provider not in ('gmail',):
        raise AccountFieldError(
            "oauth2 accounts require oauth_provider='gmail'"
        )


def _validate_create_fields(*, name: str, email_address: str,
                             auth_method: str, imap_host: str | None,
                             imap_port: int | None,
                             oauth_provider: str | None) -> None:
    if not name or not name.strip():
        raise AccountFieldError("name must not be blank")
    if len(name) > _NAME_MAX:
        raise AccountFieldError(f"name longer than {_NAME_MAX} chars")
    _validate_email(email_address)
    _validate_combo(
        auth_method=auth_method, imap_host=imap_host,
        imap_port=imap_port, oauth_provider=oauth_provider,
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
    if 'email_address' in fields:
        _validate_email(cast('str', fields['email_address']))
    _validate_combo(
        auth_method=cast('str', fields.get('auth_method', current.auth_method)),
        imap_host=cast('str | None', fields.get('imap_host', current.imap_host)),
        imap_port=cast('int | None', fields.get('imap_port', current.imap_port)),
        oauth_provider=cast('str | None', fields.get(
            'oauth_provider', current.oauth_provider)),
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
        except psycopg.errors.UniqueViolation as e:
            raise AccountFieldError(str(e).split('\n', 1)[0]) from e
        except psycopg.errors.CheckViolation as e:
            raise AccountFieldError(
                "update violates a CHECK constraint on accounts"
            ) from e
        if cur.rowcount == 0:
            raise NotFound(f"account {account_id} not found")
    return get_account(conn, account_id)


def touch_account_updated_at(conn: psycopg.Connection, account_id: int) -> None:
    """Bump accounts.updated_at so the daemon's hot-reload notices a change
    that did not otherwise edit the row (e.g. a credential rotation)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE accounts SET updated_at = now() WHERE id = %s", (account_id,)
        )
        if cur.rowcount == 0:
            raise NotFound(f"account {account_id} not found")


class AccountInUse(ValueError):
    """Raised when delete is refused because messages reference the account.

    Subclasses ValueError to match the sibling AccountFieldError parent —
    both signal caller-supplied state that's wrong (#123).
    """


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


def store_password(account: Account, password: str) -> None:
    """Store an IMAP password for the given account in the keyring."""
    if account.auth_method != 'password':
        raise AccountFieldError(
            f"store_password requires auth_method='password', got "
            f"{account.auth_method!r}"
        )
    _secrets.set_password(account.name, password)


def clear_secret(account: Account) -> None:
    """Best-effort clear both the password and any refresh-token entries.

    Missing entries are tolerated — the operator-facing intent is
    "leave nothing behind", not "fail if nothing is there".
    """
    _secrets.delete_password(account.name)
    _secrets.delete_refresh_token(account.name)


@dataclass(frozen=True)
class FolderInfo:
    name: str
    flags: tuple[str, ...]


def _open_imap_connection(
    account: Account, *, gmail_client_secrets: Path | None = None
) -> AbstractContextManager[IMAPClient]:
    """Indirection point so tests can monkeypatch without touching real IMAP."""
    # Account.auth_method's Literal includes 'archive' but AccountConfig's
    # doesn't (the daemon's config has no archive concept). The caller
    # (probe_connection) refuses archive accounts before we get here.
    cfg = _AccountConfig(
        name=account.name,
        email=account.email_address,
        imap_host=account.imap_host or '',
        imap_port=account.imap_port or 993,
        auth_method=account.auth_method,  # type: ignore[arg-type]
        oauth_provider=account.oauth_provider,  # type: ignore[arg-type]
    )
    return _imap.open_connection(cfg, gmail_client_secrets=gmail_client_secrets)


# Exception types a *genuine* IMAP connect failure raises — wrong host/port
# (socket.gaierror / ConnectionRefusedError → OSError), TLS handshake error
# (ssl.SSLError → OSError), low-level protocol error (imaplib.IMAP4.error), and
# bad credentials (imapclient LoginError → IMAPClientError). probe_connection
# deliberately does NOT catch these — its contract is to raise on connect
# failure. Transport routes catch this tuple to render a friendly error
# (#158); the broadening lives at the route, never in the service.
CONNECT_FAILURE_EXC_TYPES: tuple[type[BaseException], ...] = (
    OSError,
    imaplib.IMAP4.error,
    imapclient.exceptions.IMAPClientError,
)


def probe_connection(
    conn: psycopg.Connection,
    account_id: int,
    *,
    gmail_client_secrets: Path | None = None,
) -> list[FolderInfo]:
    """Open IMAP, list folders, return summary. Raises on connect failure.

    Archive accounts raise AccountFieldError (no host to probe). oauth2 accounts
    require ``gmail_client_secrets`` (threaded in by the caller from
    app.state.gmail_client_secrets_file); a missing refresh token surfaces as a
    clean AccountFieldError rather than an opaque 500.
    """
    account = get_account(conn, account_id)
    if account.auth_method == 'archive':
        raise AccountFieldError(
            "probe_connection not applicable to archive accounts"
        )
    if account.auth_method == 'oauth2' and gmail_client_secrets is None:
        raise AccountFieldError(
            "Gmail OAuth is not configured on this server "
            "([gmail_oauth] client_secrets_file)"
        )
    try:
        with _open_imap_connection(
            account, gmail_client_secrets=gmail_client_secrets
        ) as client:
            listing = client.list_folders()
    except RuntimeError as e:
        raise AccountFieldError(str(e)) from e
    return [FolderInfo(name=name, flags=tuple(flags)) for flags, _delim, name in listing]
