"""IMAP IDLE loop for an account's INBOX.

One connection per account is kept open and in IDLE state. New-mail
notifications from the server cause an immediate incremental sync; otherwise
IDLE is re-issued every `idle_renew_seconds` (RFC 2177 says a server MAY drop
an IDLE connection after 29 minutes).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .imap_client import open_connection
from .sync import sync_mailbox, upsert_account, upsert_mailbox
from .worker import WorkerContext

log = logging.getLogger(__name__)

INBOX = "INBOX"
HEARTBEAT_SECONDS = 30
RENEW_GUARD_SECONDS = 5


def run_inbox_idle_loop(ctx: WorkerContext) -> None:
    """Long-running loop: open IMAP, run an IDLE session, reconnect on failure."""
    backoff = 1.0
    while not ctx.stop.is_set():
        try:
            _one_inbox_session(ctx)
            backoff = 1.0
        except Exception:
            log.exception("inbox-idle session crashed for %s", ctx.account.name)
            if ctx.stop.wait(backoff):
                break
            backoff = min(backoff * 2, 60.0)


def _one_inbox_session(ctx: WorkerContext) -> None:
    """One full lifecycle of an IDLE-on-INBOX session. Returns when stop is set
    or when the IDLE call raises (caller retries with backoff)."""
    with open_connection(
        ctx.account,
        ssl=ctx.ssl,
        gmail_client_secrets=ctx.gmail_client_secrets,
    ) as imap:
        account_id, mailbox = _ensure_inbox_row(ctx)
        imap.select_folder(INBOX)

        # Catch up on anything that arrived while the daemon was down.
        _sync_inbox(ctx, imap, account_id)

        imap.idle()
        try:
            renew_at = time.monotonic() + ctx.idle_renew_seconds
            while not ctx.stop.is_set():
                renew_at = _idle_step(ctx, imap, account_id, renew_at)
        finally:
            try:
                imap.idle_done()
            except Exception:
                pass


def _idle_step(ctx: WorkerContext, imap: Any, account_id: int, renew_at: float) -> float:
    """Wait briefly for IDLE notifications. If any, sync and re-issue IDLE.
    If the renewal deadline is reached, force-cycle IDLE. Return the next
    renewal deadline (monotonic timestamp)."""
    budget = max(1.0, renew_at - time.monotonic())
    timeout = float(min(HEARTBEAT_SECONDS, budget))
    responses = imap.idle_check(timeout=timeout) or []

    if ctx.stop.is_set():
        return renew_at

    if responses:
        imap.idle_done()
        _sync_inbox(ctx, imap, account_id)
        imap.idle()
        return time.monotonic() + ctx.idle_renew_seconds

    if time.monotonic() >= renew_at - RENEW_GUARD_SECONDS:
        imap.idle_done()
        imap.idle()
        return time.monotonic() + ctx.idle_renew_seconds

    return renew_at


def _ensure_inbox_row(ctx: WorkerContext):
    with ctx.pool.connection() as conn:
        account_id = upsert_account(conn, ctx.account)
        mailbox = upsert_mailbox(
            conn, account_id=account_id, name=INBOX, delimiter=None, flags=[]
        )
        conn.commit()
    return account_id, mailbox


def _sync_inbox(ctx: WorkerContext, imap: Any, account_id: int) -> int:
    with ctx.pool.connection() as conn:
        mailbox = upsert_mailbox(
            conn, account_id=account_id, name=INBOX, delimiter=None, flags=[]
        )
        conn.commit()
        n = sync_mailbox(
            conn,
            imap,
            account_id=account_id,
            mailbox=mailbox,
            attachments_root=ctx.attachments_root,
        )
    if n:
        log.info("idle sync of %s/INBOX: +%d", ctx.account.name, n)
    return n
