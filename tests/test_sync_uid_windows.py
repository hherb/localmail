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
    with pytest.raises(ValueError, match="window must be positive"):
        uid_search_specs(1, 100, window=0)


def test_the_fake_refuses_a_reply_over_its_cap():
    """Both sync tests below mean something only because this raises."""
    imap = _mailbox_with(5)
    imap.select_folder("INBOX")
    with pytest.raises(imaplib.IMAP4.error, match="got more than 1000000 bytes"):
        imap.search(["UID", "1:*"])
    assert imap.search(["UID", "1:4"]) == [1, 2, 3, 4]


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


def _candidate_lines(conn, imap, tmp_path: Path) -> list[str]:
    lines: list[str] = []
    _sync(conn, imap, account=make_account(), attachments_root=tmp_path,
          progress=lines.append)
    return [line for line in lines if "candidate UID" in line]


def test_the_open_tail_s_echo_of_an_earlier_uid_is_counted_once(
    db_conn, tmp_path: Path, monkeypatch,
):
    """An expunged tail leaves UIDNEXT high, so ``17:*`` echoes UID 6.

    Window ``5:8`` already returned it; without the dedup it is a second
    candidate, processed twice and spending a ``max_messages`` slot.
    """
    monkeypatch.setattr(sync, "UID_SEARCH_WINDOW", 4)
    imap = _mailbox_with(6)
    imap.reported_uidnext = 20

    assert _candidate_lines(db_conn, imap, tmp_path) == [
        "  INBOX: 6 of 6 candidate UID(s) this run",
    ]


def test_uids_past_a_stale_uidnext_are_still_found(
    db_conn, tmp_path: Path, monkeypatch,
):
    """The last window stays open, so a UIDNEXT reported too low loses nothing."""
    monkeypatch.setattr(sync, "UID_SEARCH_WINDOW", 4)
    imap = _mailbox_with(12)
    imap.reported_uidnext = 11

    results = _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    assert results == {"INBOX": 12}
    assert _labels(db_conn) == (12, 12)


def test_an_incremental_pass_with_no_new_mail_has_no_candidates(
    db_conn, tmp_path: Path,
):
    """``3:*`` echoes UID 2 (the ``*`` quirk); the filter must drop it."""
    imap = _mailbox_with(2)
    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    assert _candidate_lines(db_conn, imap, tmp_path) == [
        "  INBOX: 0 of 0 candidate UID(s) this run",
    ]


def test_a_phantom_below_the_last_window_is_still_swept(
    db_conn, tmp_path: Path, monkeypatch,
):
    """A closed sweep window sees the same snapshot as the open tail does."""
    monkeypatch.setattr(sync, "UID_SEARCH_WINDOW", 4)
    imap = _mailbox_with(10)
    del imap.folders["INBOX"].messages[2]
    imap.phantom_uids = {2}                 # swept in 1:4, already gone

    assert _candidate_lines(db_conn, imap, tmp_path) == [
        "  INBOX: 10 of 10 candidate UID(s) this run",
    ]
    assert _labels(db_conn) == (9, 10)
    assert imap.search(["UID", "2:2"]) == [], "the single-UID probe stays exact"
