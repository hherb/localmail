# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The UID SEARCH runs in bounded windows, so no reply outgrows imaplib's cap.

imaplib refuses any response line over ``imaplib._MAXLINE`` (1,000,000) bytes,
and a SEARCH reply is a single line of UIDs. A full sync of a large
``[Gmail]/All Mail`` asked ``SEARCH ALL`` and crashed every poll pass forever:
the mailbox never checkpointed, so the next pass asked the same question.
"""

import imaplib
from pathlib import Path

import pytest

from localmail import sync
from localmail.uids import UID_SEARCH_WINDOW, uid_search_specs

from . import _eml
from ._fake_imap import FakeIMAPClient
from .test_sync import _sync, make_account

#: The widest UID IMAP allows (RFC 3501: a non-zero 32-bit unsigned integer).
_MAX_UID = 2**32 - 1


def test_a_window_s_reply_fits_imaplib_s_line_cap():
    """Every UID in a full window at the maximum width, plus its separator."""
    widest_reply = UID_SEARCH_WINDOW * (len(str(_MAX_UID)) + 1) + len("* SEARCH")
    assert widest_reply < imaplib._MAXLINE


@pytest.mark.parametrize(
    ("first", "uidnext", "expected"),
    [
        (1, 0, ["1:*"]),            # no UIDNEXT: the old single open sweep
        (1, 5, ["1:*"]),            # fits one window
        (1, 11, ["1:*"]),           # exactly one window's worth below uidnext
        (1, 12, ["1:10", "11:*"]),
        (1, 25, ["1:10", "11:20", "21:*"]),
        (7, 25, ["7:16", "17:*"]),  # incremental resume point
        (30, 25, ["30:*"]),         # watermark past uidnext: still asks once
    ],
)
def test_uid_search_specs(first, uidnext, expected):
    assert uid_search_specs(first, uidnext, window=10) == expected


def test_only_the_last_window_is_open():
    """The tail stays ``lo:*`` so a UID at or past a stale UIDNEXT is not missed."""
    specs = uid_search_specs(1, 1000, window=10)
    assert all("*" not in s for s in specs[:-1])
    assert specs[-1].endswith(":*")


def test_a_non_positive_window_is_refused():
    with pytest.raises(ValueError):
        uid_search_specs(1, 100, window=0)


def _distinct(i: int) -> bytes:
    return _eml.plain().replace(b"plain-123@", f"plain-{i}@".encode())


def _mailbox_with(n: int) -> FakeIMAPClient:
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    for i in range(n):
        imap.append("INBOX", _distinct(i))
    imap.max_search_results = 4
    return imap


def _labels(conn) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), max(uid) FROM message_labels")
        row = cur.fetchone()
        assert row is not None
        return row


def test_a_full_sync_larger_than_one_reply_completes(db_conn, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sync, "UID_SEARCH_WINDOW", 4)
    imap = _mailbox_with(10)

    results = _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    assert results == {"INBOX": 10}
    assert _labels(db_conn) == (10, 10)


def test_an_incremental_sync_larger_than_one_reply_completes(
    db_conn, tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(sync, "UID_SEARCH_WINDOW", 4)
    imap = _mailbox_with(2)
    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)
    for i in range(2, 11):
        imap.append("INBOX", _distinct(i))

    second = _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    assert second == {"INBOX": 9}
    assert _labels(db_conn) == (11, 11)
