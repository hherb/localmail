"""Mailbox-source reader tests (pure, no DB)."""
from __future__ import annotations

from datetime import datetime, timezone

from localmail.importer.sources import ImportedMessage, parse_mbox_from_date


def test_parse_mbox_from_date_asctime_utc():
    line = "alice@example.com Wed Jan  1 12:00:00 2025"
    dt = parse_mbox_from_date(line)
    assert dt == datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_mbox_from_date_no_sender_prefix():
    # mbox From_ lines sometimes carry only the date.
    dt = parse_mbox_from_date("MAILER-DAEMON Fri Jul  8 09:08:34 2011")
    assert dt == datetime(2011, 7, 8, 9, 8, 34, tzinfo=timezone.utc)


def test_parse_mbox_from_date_malformed_returns_none():
    assert parse_mbox_from_date("") is None
    assert parse_mbox_from_date("not a date") is None


def test_imported_message_is_frozen():
    m = ImportedMessage(mailbox_name="INBOX", raw=b"x", received_date=None)
    assert m.mailbox_name == "INBOX"
    assert m.raw == b"x"
    assert m.received_date is None
