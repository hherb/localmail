# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Mailbox-source reader tests (pure, no DB)."""
from __future__ import annotations

import dataclasses
import mailbox as _mailbox
from datetime import datetime, timezone

import pytest

from localmail.importer.sources import ImportedMessage, iter_maildir, iter_mbox, parse_mbox_from_date
from tests import _eml


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

    with pytest.raises(dataclasses.FrozenInstanceError):
        m.mailbox_name = "other"  # type: ignore[misc]


def test_iter_mbox_yields_messages_with_stem_name(tmp_path):
    box_path = tmp_path / "takeout.mbox"
    box = _mailbox.mbox(str(box_path))
    box.lock()
    m = _mailbox.mboxMessage(_eml.plain())
    m.set_from("alice@example.com Wed Jan  1 12:00:00 2025")
    box.add(m)
    box.flush()
    box.unlock()

    out = list(iter_mbox(box_path, mailbox_name="takeout"))
    assert len(out) == 1
    assert out[0].mailbox_name == "takeout"
    assert b"Hello Bob" in out[0].raw
    assert out[0].received_date == datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_iter_maildir_maps_subfolders_to_mailbox_names(tmp_path):
    md = _mailbox.Maildir(str(tmp_path / "md"))
    md.add(_mailbox.MaildirMessage(_eml.plain()))
    sub = md.add_folder("Archive")
    sub.add(_mailbox.MaildirMessage(_eml.utf8_subject()))

    out = list(iter_maildir(tmp_path / "md"))
    names = {m.mailbox_name for m in out}
    assert "md" in names           # root folder → directory name
    assert "Archive" in names      # subfolder → folder name
    assert len(out) == 2
