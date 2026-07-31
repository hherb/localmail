# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Synthetic-UID collision and its recovery path (#215).

`message_labels` carries UNIQUE (mailbox_id, uid). Imports invent the UID, and
two sources whose filename stem matches resolve to the same mailbox_id -- so a
per-run counter restarting at 1 recycles UIDs the previous import committed and
poison-pills perfectly good messages into `failed_messages`.
"""
from __future__ import annotations

import mailbox as _mailbox
from pathlib import Path

import psycopg

from localmail.importer import runner
from localmail.sync import retry_failed_messages
from tests.conftest import TEST_DSN

_IMPORT_KW = dict(checkpoint_every=50, checkpoint_seconds=3600.0)


def _conn_factory():
    return psycopg.connect(TEST_DSN, autocommit=False)


def _msg(n: int) -> bytes:
    """A distinct, well-formed message with its own Message-Id."""
    return (
        f"From: alice@example.com\r\n"
        f"To: bob@example.com\r\n"
        f"Subject: message {n}\r\n"
        f"Date: Wed, 01 Jan 2025 12:00:00 +0000\r\n"
        f"Message-Id: <msg-{n}@example.com>\r\n"
        f"\r\n"
        f"body of message {n}\r\n"
    ).encode()


def _account(conn, *, name: str, auth_method: str) -> int:
    host, port = (None, None) if auth_method == "archive" else ("imap.example.com", 993)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, imap_host, "
            "imap_port, config) VALUES (%s, %s, %s, %s, %s, '{}') RETURNING id",
            (name, f"{name}@b.test", auth_method, host, port),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def _mbox_at(tmp_path: Path, subdir: str, stem: str, *messages: bytes) -> str:
    """Write an mbox at <subdir>/<stem>.mbox — the stem becomes the mailbox name."""
    directory = tmp_path / subdir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}.mbox"
    box = _mailbox.mbox(str(path))
    box.lock()
    for raw in messages:
        m = _mailbox.mboxMessage(raw)
        m.set_from("alice@example.com Wed Jan  1 12:00:00 2025")
        box.add(m)
    box.flush()
    box.unlock()
    return str(path)


def _job(conn, account_id: int, path: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO import_jobs (account_id, source_kind, source_path, status) "
            "VALUES (%s, 'mbox', %s, 'pending') RETURNING id",
            (account_id, path),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def _job_counts(conn, jid: int) -> dict:
    conn.rollback()  # discard this connection's snapshot; re-read fresh
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, inserted, skipped_dup, failed FROM import_jobs WHERE id=%s",
            (jid,),
        )
        row = cur.fetchone()
    assert row is not None
    return {"status": row[0], "inserted": row[1], "skipped_dup": row[2], "failed": row[3]}


def _mailbox_id(conn, account_id: int, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM mailboxes WHERE account_id=%s AND name=%s",
            (account_id, name),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def test_two_sources_sharing_a_stem_import_without_collision(db_conn, tmp_path):
    """The reported failure: 2023/Inbox.mbox then 2024/Inbox.mbox, same account.

    Both resolve to one mailbox_id. With a per-run counter the second import
    re-issues uid=1,2 over the first import's committed rows and every message
    lands in failed_messages purely from the recycled UID.
    """
    aid = _account(db_conn, name="arch", auth_method="archive")

    first = _mbox_at(tmp_path, "2023", "Inbox", _msg(1), _msg(2), _msg(3))
    jid1 = _job(db_conn, aid, first)
    runner.run_import(_conn_factory, jid1, attachments_root=tmp_path / "blobs", **_IMPORT_KW)
    assert _job_counts(db_conn, jid1) == {
        "status": "completed", "inserted": 3, "skipped_dup": 0, "failed": 0}

    second = _mbox_at(tmp_path, "2024", "Inbox", _msg(4), _msg(5))
    jid2 = _job(db_conn, aid, second)
    runner.run_import(_conn_factory, jid2, attachments_root=tmp_path / "blobs", **_IMPORT_KW)

    assert _job_counts(db_conn, jid2) == {
        "status": "completed", "inserted": 2, "skipped_dup": 0, "failed": 0}
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM failed_messages")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM messages WHERE account_id=%s", (aid,))
        assert cur.fetchone()[0] == 5
        # One shared mailbox, five labels, five distinct UIDs.
        cur.execute(
            "SELECT count(*), count(DISTINCT uid) FROM message_labels ml "
            "JOIN mailboxes mb ON mb.id = ml.mailbox_id WHERE mb.account_id=%s",
            (aid,),
        )
        assert cur.fetchone() == (5, 5)


def test_reimport_of_the_same_file_still_dedups(db_conn, tmp_path):
    """Continuing from MAX(uid)+1 must not break message-level idempotence."""
    aid = _account(db_conn, name="arch", auth_method="archive")
    path = _mbox_at(tmp_path, "2023", "Inbox", _msg(1), _msg(2))

    jid1 = _job(db_conn, aid, path)
    runner.run_import(_conn_factory, jid1, attachments_root=tmp_path / "blobs", **_IMPORT_KW)
    jid2 = _job(db_conn, aid, path)
    runner.run_import(_conn_factory, jid2, attachments_root=tmp_path / "blobs", **_IMPORT_KW)

    assert _job_counts(db_conn, jid2) == {
        "status": "completed", "inserted": 0, "skipped_dup": 2, "failed": 0}
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages WHERE account_id=%s", (aid,))
        assert cur.fetchone()[0] == 2


def _seed_colliding_failure(db_conn, tmp_path, *, auth_method: str) -> tuple[int, int]:
    """One imported message holding uid=1, plus a failed row replaying uid=1.

    Models the state a pre-fix import left behind: the failed message is
    perfectly good, it just drew a UID another row already owns.
    """
    aid = _account(db_conn, name="acct", auth_method=auth_method)
    path = _mbox_at(tmp_path, "2023", "Inbox", _msg(1))
    jid = _job(db_conn, aid, path)
    runner.run_import(_conn_factory, jid, attachments_root=tmp_path / "blobs", **_IMPORT_KW)
    db_conn.rollback()

    mb_id = _mailbox_id(db_conn, aid, "Inbox")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO failed_messages (account_id, mailbox_id, uid, raw_bytes, "
            "raw_sha256, error_class, error_message) "
            "VALUES (%s, %s, 1, %s, sha256(%s), 'UniqueViolation', 'recycled uid')",
            (aid, mb_id, _msg(2), _msg(2)),
        )
    db_conn.commit()
    return aid, mb_id


def test_retry_reallocates_a_synthetic_uid_for_archive_accounts(db_conn, tmp_path):
    """Recovery path for rows a pre-fix import already poisoned.

    Replaying the stored uid=1 collides forever; archive UIDs are synthetic, so
    retry re-allocates and the message is finally ingested.
    """
    aid, mb_id = _seed_colliding_failure(db_conn, tmp_path, auth_method="archive")

    succeeded, still_failing = retry_failed_messages(
        db_conn, attachments_root=tmp_path / "blobs")

    assert (succeeded, still_failing) == (1, 0)
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM failed_messages")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM messages WHERE account_id=%s", (aid,))
        assert cur.fetchone()[0] == 2
        cur.execute(
            "SELECT uid FROM message_labels WHERE mailbox_id=%s ORDER BY uid", (mb_id,))
        uids = [r[0] for r in cur.fetchall()]
    assert uids == [1, 2], "the recovered message must take the next free UID"


def test_retry_preserves_a_real_imap_uid_for_live_accounts(db_conn, tmp_path):
    """A live account's UID is the server's truth — never silently re-allocated.

    The collision here is a genuine invariant violation worth surfacing, so the
    row stays in failed_messages rather than being papered over.
    """
    _aid, mb_id = _seed_colliding_failure(db_conn, tmp_path, auth_method="password")

    succeeded, still_failing = retry_failed_messages(
        db_conn, attachments_root=tmp_path / "blobs")

    assert (succeeded, still_failing) == (0, 1)
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute("SELECT uid FROM message_labels WHERE mailbox_id=%s", (mb_id,))
        assert [r[0] for r in cur.fetchall()] == [1]
