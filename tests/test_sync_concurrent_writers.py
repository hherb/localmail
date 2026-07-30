# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Regressions for the per-account IDLE + poll threads racing on one message.

The daemon runs two threads per account (IDLE on INBOX, poll on every other
folder), and Gmail delivers one Message-Id to INBOX and several labels at once,
so both threads routinely process the same message concurrently. These pin the
three consequences that used to follow.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

from localmail import imap_client as imap_mod
from localmail import sync as sync_mod
from localmail.attachments import write_attachments
from localmail.parser import parse_message
from localmail.sync import upsert_message

from . import _eml
from .test_sync import _ensure_account, make_account


def test_upsert_message_returns_the_winner_id_instead_of_raising(
    db_conn: psycopg.Connection, monkeypatch,
) -> None:
    """Losing the insert race must not look like a poison-pill message.

    `upsert_message` checks for an existing row and then INSERTs. The loser of
    that window used to raise `UniqueViolation`, which `process_one_message`
    recorded in `failed_messages` as if the message were malformed. The
    existence check is monkeypatched to miss once, which is exactly what the
    concurrent writer causes.
    """
    account = make_account()
    account_id = _ensure_account(db_conn, account)
    parsed = parse_message(_eml.plain())

    first_id, inserted = upsert_message(
        db_conn, account_id=account_id, parsed=parsed, internal_date=None,
    )
    assert inserted is True

    # Simulate the race: the pre-INSERT existence check misses, so the INSERT
    # runs even though the row is already there.
    real = sync_mod._existing_message_id
    calls = {"n": 0}

    def miss_once(cur, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real(cur, **kw)

    monkeypatch.setattr(sync_mod, "_existing_message_id", miss_once)

    second_id, inserted_again = upsert_message(
        db_conn, account_id=account_id, parsed=parsed, internal_date=None,
    )
    assert second_id == first_id
    assert inserted_again is False
    assert calls["n"] == 2  # missed, then re-read the winner

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM messages WHERE account_id = %s AND message_id = %s",
            (account_id, parsed.message_id),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == 1


def test_concurrent_blob_writers_do_not_collide_on_a_shared_temp(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch,
) -> None:
    """Two writers of the same blob must both succeed, leaving it intact.

    Both used to derive the same `<sha>.tmp` path, which breaks two ways: one
    writer can truncate (open-for-write) the temp the other is about to
    `replace()`, installing a short blob at the content-addressed path; or the
    faster writer's `replace()` moves the shared temp away and the slower one
    dies on `FileNotFoundError`.

    The interleaving forced here -- the second writer completes entirely from
    inside the first's `write_bytes` -- deterministically produces the second
    form (the unfixed code raises `FileNotFoundError` on the outer `replace`).
    The private-temp fix covers both, since neither writer can now observe the
    other's temp at all.
    """
    account = make_account()
    account_id = _ensure_account(db_conn, account)
    parsed = parse_message(_eml.with_attachment())
    payload = parsed.attachments[0].payload
    assert payload

    upsert_message(
        db_conn, account_id=account_id, parsed=parsed, internal_date=None,
    )

    real_write_bytes = Path.write_bytes
    reentered = {"done": False}

    def racing_write_bytes(self: Path, data: bytes):
        result = real_write_bytes(self, data)
        if not reentered["done"]:
            reentered["done"] = True
            # A second writer completes the whole write+replace while the
            # first is still inside its own write.
            write_attachments(db_conn, parsed, root=tmp_path)
        return result

    monkeypatch.setattr(Path, "write_bytes", racing_write_bytes)
    rows = write_attachments(db_conn, parsed, root=tmp_path)
    monkeypatch.undo()

    assert reentered["done"], "the interleaving under test did not happen"
    sha = rows[0]["sha256"]
    blob = tmp_path / "blobs" / sha[:2] / sha[2:4] / sha
    assert blob.exists()
    assert blob.read_bytes() == payload, "canonical blob is truncated/corrupted"
    # No temp files left behind by either writer.
    assert list(blob.parent.glob("*.tmp")) == []


def test_open_connection_bounds_every_blocking_imap_call(monkeypatch) -> None:
    """A network black-hole must not hang a worker forever.

    Without a socket timeout, `imapclient` blocks indefinitely on dropped
    packets: the worker holds its pool connection, never observes the daemon
    stop event, and gets respawned as a duplicate. This is the IMAP analogue of
    the Postgres connect bounds in `test_daemon_connect_timeout.py`.
    """
    seen: dict[str, object] = {}

    class Recorder:
        def __init__(self, **kw):
            seen.update(kw)

        def login(self, *a, **k):
            pass

        def logout(self):
            pass

    monkeypatch.setattr(imap_mod, "IMAPClient", Recorder)
    monkeypatch.setattr(imap_mod.secrets, "get_password", lambda name: "pw")

    with imap_mod.open_connection(make_account()):
        pass
    assert seen["timeout"] == imap_mod.DEFAULT_IMAP_TIMEOUT_SECONDS

    seen.clear()
    with imap_mod.open_connection(make_account(), timeout=5.0):
        pass
    assert seen["timeout"] == 5.0
