# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Regressions for the per-account IDLE + poll threads racing on one message.

The daemon runs two threads per account (IDLE on INBOX, poll on every other
folder), and Gmail delivers one Message-Id to INBOX and several labels at once,
so both threads routinely process the same message concurrently. These pin the
three consequences that used to follow.
"""

from __future__ import annotations

import threading
from pathlib import Path

import psycopg
import pytest

from localmail import imap_client as imap_mod
from localmail import sync as sync_mod
from localmail.attachments import write_attachments
from localmail.config import DaemonConfig
from localmail.parser import parse_message
from localmail.sync import process_one_message, upsert_message
from localmail.worker import WorkerContext

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

    This is the cheap single-connection shape; the cross-transaction property it
    cannot reach — that the re-read actually sees a *concurrent* winner — is
    pinned by `test_two_real_connections_racing_...` below.
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


def test_two_real_connections_racing_on_one_message_agree_on_the_winner(
    db_conn: psycopg.Connection, db_dsn: str,
) -> None:
    """The real IDLE-vs-poll shape: two connections, winner still uncommitted.

    The monkeypatched test above cannot reach the property that actually makes
    the fix work, because there the conflicting row is in the *same*
    transaction and so is trivially visible. Here the loser races a genuinely
    concurrent, *uncommitted* writer, which is what the two daemon threads do.

    Two behaviours are load-bearing and pinned here:

    * `ON CONFLICT DO NOTHING` blocks on the speculative-insert lock until the
      winner commits, rather than immediately reporting "no conflict".
    * The re-read afterwards takes a fresh per-statement snapshot and therefore
      sees the winner's now-committed row.

    Both hold under READ COMMITTED (psycopg's default) and *not* under
    REPEATABLE READ, where the INSERT raises `SerializationFailure` instead —
    so this also guards against the isolation level being raised on the sync
    path without revisiting `upsert_message`.
    """
    account = make_account()
    account_id = _ensure_account(db_conn, account)
    db_conn.commit()  # the racing connection must be able to see the account
    parsed = parse_message(_eml.plain())

    # Winner: insert, then hold the transaction open so the loser must wait.
    winner_id, inserted = upsert_message(
        db_conn, account_id=account_id, parsed=parsed, internal_date=None,
    )
    assert inserted is True

    loser: dict[str, object] = {}
    started = threading.Event()

    def race() -> None:
        with psycopg.connect(db_dsn, autocommit=False) as other:
            started.set()
            try:
                loser["result"] = upsert_message(
                    other, account_id=account_id, parsed=parsed, internal_date=None,
                )
            except BaseException as exc:  # noqa: BLE001 - reported, then asserted on
                loser["error"] = exc
            other.rollback()

    t = threading.Thread(target=race, name="racing-writer")
    t.start()
    assert started.wait(10)
    # The loser is now blocked on the speculative-insert lock: it cannot finish
    # until this transaction resolves. If it could, the fix would be relying on
    # a race it does not actually win.
    t.join(timeout=1.0)
    assert t.is_alive(), "loser did not block on the uncommitted winner"

    db_conn.commit()
    t.join(timeout=10)
    assert not t.is_alive()

    assert "error" not in loser, f"loser raised instead of yielding the winner: {loser.get('error')!r}"
    assert loser["result"] == (winner_id, False)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM messages WHERE account_id = %s AND message_id = %s",
            (account_id, parsed.message_id),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == 1


def test_losing_the_race_records_no_failed_message(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch,
) -> None:
    """The end-to-end promise: a raced message never lands in `failed_messages`.

    `list-failed` is an operator's "what did sync choke on" view; polluting it
    with healthy mail is the whole point of the fix. `process_one_message` is
    the layer that used to translate the loser's `UniqueViolation` into a row
    there, so assert at that layer rather than only on `upsert_message`.
    """
    account = make_account()
    account_id = _ensure_account(db_conn, account)
    raw = _eml.plain()

    mailbox = sync_mod.upsert_mailbox(
        db_conn, account_id=account_id, name="INBOX", delimiter="/", flags=[],
    )
    first_id, _ = process_one_message(
        db_conn, account_id=account_id, mailbox_id=mailbox.id, uid=1, raw=raw,
        flags=[], attachments_root=tmp_path,
    )

    real = sync_mod._existing_message_id
    seen = {"n": 0}

    def miss_once(cur, **kw):
        seen["n"] += 1
        return None if seen["n"] == 1 else real(cur, **kw)

    monkeypatch.setattr(sync_mod, "_existing_message_id", miss_once)

    other = sync_mod.upsert_mailbox(
        db_conn, account_id=account_id, name="Archive", delimiter="/", flags=[],
    )
    second_id, did_insert = process_one_message(
        db_conn, account_id=account_id, mailbox_id=other.id, uid=7, raw=raw,
        flags=[], attachments_root=tmp_path,
    )

    assert second_id == first_id
    assert did_insert is False
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM failed_messages")
        row = cur.fetchone()
    assert row is not None
    assert row[0] == 0, "a raced-but-healthy message was recorded as a poison pill"


def test_unexpected_constraint_conflict_raises_a_diagnostic_not_a_bare_assert(
    db_conn: psycopg.Connection, monkeypatch,
) -> None:
    """`ON CONFLICT DO NOTHING` has no target, so it swallows *any* unique
    violation — a desynced `messages_id_seq` after a restore without `setval`,
    say. That must surface as a named error, not an `AssertionError` with no
    context (which would be a *worse* `failed_messages` entry than the
    `UniqueViolation` this PR replaced) and not as a `None` id.
    """
    account = make_account()
    account_id = _ensure_account(db_conn, account)
    parsed = parse_message(_eml.plain())

    upsert_message(db_conn, account_id=account_id, parsed=parsed, internal_date=None)

    # Rewind the sequence so the next INSERT collides on the primary key rather
    # than on either dedup index, and make the existence check miss so we reach
    # the INSERT at all.
    with db_conn.cursor() as cur:
        cur.execute("SELECT setval(pg_get_serial_sequence('messages', 'id'), 1, false)")
    monkeypatch.setattr(sync_mod, "_existing_message_id", lambda cur, **kw: None)

    other = parse_message(_eml.multipart_alt())
    with pytest.raises(RuntimeError, match="unexpected constraint"):
        upsert_message(db_conn, account_id=account_id, parsed=other, internal_date=None)


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


def test_imapclient_accepts_the_timeout_kwarg_we_pass() -> None:
    """Guard the fake in the test above against drifting from the real API.

    `Recorder` swallows `**kw`, so it would keep passing if imapclient ever
    dropped or renamed `timeout` and every worker silently went back to
    blocking forever. Assert against the real signature instead.
    """
    import inspect

    from imapclient import IMAPClient

    assert "timeout" in inspect.signature(IMAPClient.__init__).parameters


def test_workers_pass_the_configured_imap_timeout_not_the_module_default(
    monkeypatch,
) -> None:
    """The bound has to be operator-tunable, not a hardcoded constant.

    A server-side stall with nothing on the wire (a Gmail SEARCH over a very
    large `\\All` folder) is indistinguishable from a black-hole: it raises
    socket.timeout, the IDLE/poll loops treat that as a crashed session, and the
    account livelocks in reconnect-with-backoff. An operator hitting that must
    be able to raise `[daemon] imap_timeout_s` without editing source, so pin
    that both workers actually forward it.
    """
    from localmail import idle as idle_mod
    from localmail import poller as poll_mod

    for module, entry in ((idle_mod, "_one_inbox_session"), (poll_mod, "_one_poll_pass")):
        seen: dict[str, object] = {}

        def fake_open(account, **kw):
            seen.update(kw)
            raise _StopWorker

        monkeypatch.setattr(module, "open_connection", fake_open)
        ctx = _worker_ctx(imap_timeout_s=17.5)
        with pytest.raises(_StopWorker):
            getattr(module, entry)(ctx)
        assert seen["timeout"] == 17.5, f"{module.__name__} dropped the configured bound"


class _StopWorker(Exception):
    """Abort a worker entry point as soon as it has opened its connection."""


def _worker_ctx(*, imap_timeout_s: float) -> WorkerContext:
    return WorkerContext(
        account=make_account(),
        account_id=1,
        pool=None,  # type: ignore[arg-type]  # unreached: fake_open raises first
        attachments_root=Path("/nonexistent"),
        idle_renew_seconds=1740,
        poll_seconds=300,
        gmail_client_secrets=None,
        stop=threading.Event(),
        imap_timeout_s=imap_timeout_s,
    )


def test_daemon_config_exposes_the_imap_bound() -> None:
    """`DaemonConfig.imap_timeout_s` is the knob the workers read."""
    assert DaemonConfig().imap_timeout_s == imap_mod.DEFAULT_IMAP_TIMEOUT_SECONDS
    assert DaemonConfig(imap_timeout_s=5.0).imap_timeout_s == 5.0
