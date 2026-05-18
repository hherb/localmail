"""Account and folder listing for the GUI navigation tree.

`is_archive_only` is currently derived as "account exists but no mailbox has
been synced in the last 30 days". Promoted to a column in a future migration
if the derivation becomes expensive.

`is_shared` is set to True when the caller has access to more than one
account — the GUI uses it to switch on account grouping/chips.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

_ARCHIVE_STALENESS_DAYS = 30


def list_accounts(
    conn: psycopg.Connection, *, allowed_account_ids: list[int],
) -> list[dict[str, Any]]:
    """Return one dict per account the caller may read.

    `allowed_account_ids = []` returns an empty list — a user with no grants
    sees no accounts. `capabilities.is_shared` reflects whether the caller
    has access to >1 account (presentation hint, not authorisation).
    """
    if not allowed_account_ids:
        return []
    is_shared = len(allowed_account_ids) > 1
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.name, a.email_address,
                   (SELECT max(mb.last_sync_at) FROM mailboxes mb WHERE mb.account_id = a.id) AS last_sync_at,
                   (SELECT count(*) FROM messages m WHERE m.account_id = a.id) AS message_count
              FROM accounts a
             WHERE a.id = ANY(%s)
             ORDER BY a.name
            """,
            (allowed_account_ids,),
        )
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for aid, name, email_address, last_sync_at, message_count in rows:
        is_archive_only = last_sync_at is None or _is_stale(last_sync_at)
        out.append({
            "id": str(aid),
            "name": name,
            "address": email_address,
            "last_sync_at": last_sync_at.isoformat() if last_sync_at else None,
            "message_count": int(message_count),
            "capabilities": {
                "can_sync": not is_archive_only,
                "is_archive_only": is_archive_only,
                "is_shared": is_shared,
            },
        })
    return out


def list_folders(
    conn: psycopg.Connection, account_id: int, *, allowed_account_ids: list[int],
) -> list[dict[str, Any]]:
    """Return folders for `account_id`, or an empty list if the caller cannot read it.

    Returning empty rather than raising mirrors `/v1/accounts` semantics: a
    user without access sees "no folders" for an account they cannot see,
    not a 403 that reveals the account exists.
    """
    if account_id not in allowed_account_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT mb.id, mb.name, mb.flags, mb.uidnext,
                   (SELECT count(*) FROM message_labels ml WHERE ml.mailbox_id = mb.id) AS message_count
              FROM mailboxes mb
             WHERE mb.account_id = %s
             ORDER BY mb.name
            """,
            (account_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": str(mb_id),
            "name": name,
            "full_path": name,
            # flags is a text[] in the DB; join to a space-separated string for the API
            "flags": " ".join(flags) if flags else None,
            "last_uid": int(uidnext) if uidnext is not None else None,
            "message_count": int(count),
        }
        for mb_id, name, flags, uidnext, count in rows
    ]


def _is_stale(last_sync_at: datetime) -> bool:
    now = datetime.now(timezone.utc)
    return (now - last_sync_at) > timedelta(days=_ARCHIVE_STALENESS_DAYS)
