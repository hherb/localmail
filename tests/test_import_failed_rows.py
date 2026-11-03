# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Re-running an import keeps one `failed_messages` row per poison pill (#440).

A poison pill gets no `message_labels` row, so `max_label_uid` does not reserve
its uid. A re-run therefore gave the same bytes a fresh uid, and
`record_failed_message`'s upsert on `(account_id, mailbox_id, uid)` inserted a
second row instead of bumping `retry_count` on the first. The same gap let a
*different* pill land on an old pill's uid and overwrite its stored bytes.
"""
from __future__ import annotations

from localmail.importer import runner
from tests.test_import_uid_collision import (
    _IMPORT_KW,
    _account,
    _conn_factory,
    _job,
    _mbox_at,
    _msg,
)

_POISON_MARK = b"POISON"


def _poisoned(n: int) -> bytes:
    return _msg(n).replace(b"body of message", _POISON_MARK + b" body of message")


def _poison_marked_messages(monkeypatch) -> None:
    real = runner.process_one_message

    def scripted(conn, **kw):
        if _POISON_MARK in kw["raw"]:
            raise ValueError("synthetic poison")
        return real(conn, **kw)

    monkeypatch.setattr(runner, "process_one_message", scripted)


def _subject(raw: bytes) -> bytes:
    # The mbox source rewrites CRLF to LF, so compare the subject, not the bytes.
    return raw.split(b"Subject: ", 1)[1].splitlines()[0]


def _import(db_conn, account_id: int, path: str, tmp_path) -> None:
    runner.run_import(
        _conn_factory, _job(db_conn, account_id, path),
        attachments_root=tmp_path / "att", **_IMPORT_KW)


def _failed_rows(conn) -> list[tuple[int, bytes, int]]:
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SELECT uid, raw_bytes, retry_count FROM failed_messages ORDER BY uid")
        return [(int(u), bytes(r), int(c)) for u, r, c in cur.fetchall()]


def test_reimporting_a_poison_pill_bumps_its_row_instead_of_adding_one(
        db_conn, tmp_path, monkeypatch):
    _poison_marked_messages(monkeypatch)
    acct = _account(db_conn, name="arch", auth_method="archive")
    path = _mbox_at(tmp_path, "a", "Inbox", _msg(1), _poisoned(2), _msg(3))

    _import(db_conn, acct, path, tmp_path)
    _import(db_conn, acct, path, tmp_path)

    rows = _failed_rows(db_conn)
    assert len(rows) == 1
    assert _subject(rows[0][1]) == b"message 2"
    assert rows[0][2] == 1  # the second run bumped the first run's row


def test_a_new_pill_never_overwrites_an_older_pills_row(db_conn, tmp_path, monkeypatch):
    """Run 1 ends on a pill, so its uid sits above every label uid. Run 2's
    first message is a different pill sharing the mailbox: the counter used to
    hand it that same uid, and the upsert replaced run 1's bytes."""
    _poison_marked_messages(monkeypatch)
    acct = _account(db_conn, name="arch", auth_method="archive")
    first = _mbox_at(tmp_path, "2023", "Inbox", _msg(1), _poisoned(2))
    second = _mbox_at(tmp_path, "2024", "Inbox", _poisoned(3), _msg(4))

    _import(db_conn, acct, first, tmp_path)
    _import(db_conn, acct, second, tmp_path)

    rows = _failed_rows(db_conn)
    assert sorted(_subject(r[1]) for r in rows) == [b"message 2", b"message 3"]
    assert all(r[2] == 0 for r in rows)
