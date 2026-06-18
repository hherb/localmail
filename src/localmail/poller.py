# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Periodic poll loop for every folder *except* INBOX (which the IDLE loop owns)."""

from __future__ import annotations

import logging
import time
from typing import Any

from .heartbeat import safe_heartbeat
from .imap_client import open_connection
from .sync import folders_to_sync, sync_mailbox, upsert_mailbox
from .worker import WorkerContext

log = logging.getLogger(__name__)

INBOX = "INBOX"
# Re-beat liveness at least this often while idling between poll passes, so a
# healthy poll thread never reads stale even when `poll_seconds` exceeds
# `heartbeat_stale_seconds`. Matches idle.HEARTBEAT_SECONDS.
HEARTBEAT_SECONDS = 30


def run_poll_loop(ctx: WorkerContext) -> None:
    """Long-running loop: every `poll_seconds`, sync every non-INBOX folder."""
    backoff = 1.0
    while not ctx.stop.is_set():
        try:
            _one_poll_pass(ctx)
            backoff = 1.0
        except Exception as exc:
            log.exception("poll pass crashed for %s", ctx.account.name)
            safe_heartbeat(ctx.pool, worker_kind="poll",
                           account_id=ctx.account_id, state="reconnecting",
                           last_error_msg=str(exc))
            if ctx.stop.wait(backoff):
                break
            backoff = min(backoff * 2, 60.0)
            continue
        if _wait_between_passes(ctx):
            break


def _wait_between_passes(ctx: WorkerContext) -> bool:
    """Sleep up to `poll_seconds`, re-beating an `idle` liveness heartbeat every
    `HEARTBEAT_SECONDS` so the daemon-status `stale` flag stays accurate for the
    poll thread regardless of how large `poll_seconds` is. Returns True if stop
    was signalled (caller should break)."""
    deadline = time.monotonic() + ctx.poll_seconds
    while True:
        safe_heartbeat(ctx.pool, worker_kind="poll",
                       account_id=ctx.account_id, state="idle")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if ctx.stop.wait(min(HEARTBEAT_SECONDS, remaining)):
            return True


def _one_poll_pass(ctx: WorkerContext) -> dict[str, int]:
    """Open a connection, sync every non-INBOX folder, close. Returns
    `{folder_name: new_messages}`."""
    results: dict[str, int] = {}
    with open_connection(
        ctx.account,
        ssl=ctx.ssl,
        gmail_client_secrets=ctx.gmail_client_secrets,
    ) as imap:
        account_id = ctx.account_id
        safe_heartbeat(ctx.pool, worker_kind="poll",
                       account_id=account_id, state="polling")

        folders = imap.list_folders()
        selectable = folders_to_sync(
            folders,
            allow=ctx.account.folder_allow,
            deny=ctx.account.folder_deny,
            deny_flags=ctx.account.folder_deny_flags,
        )

        for name, delim, flags in selectable:
            if ctx.stop.is_set():
                break
            if name == INBOX:
                continue  # owned by the IDLE loop
            safe_heartbeat(ctx.pool, worker_kind="poll", account_id=account_id,
                           state="syncing", current_folder=name)
            results[name] = _sync_folder(ctx, imap, account_id, name, delim, flags)

    return results


def _sync_folder(
    ctx: WorkerContext,
    imap: Any,
    account_id: int,
    name: str,
    delimiter: str | None,
    flags: list[str],
) -> int:
    with ctx.pool.connection() as conn:
        mailbox = upsert_mailbox(
            conn,
            account_id=account_id,
            name=name,
            delimiter=delimiter,
            flags=flags,
        )
        conn.commit()
        return sync_mailbox(
            conn,
            imap,
            account_id=account_id,
            mailbox=mailbox,
            attachments_root=ctx.attachments_root,
        )
