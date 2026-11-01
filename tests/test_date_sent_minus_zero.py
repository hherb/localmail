# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A `-0000` `Date:` is stored as the UTC instant it names (#431).

RFC 5322 3.3: `-0000` means "the time is UTC, the sender's local zone is
unknown". `parsedate_to_datetime` returns it naive, psycopg sends naive as
`timestamp`, and Postgres reads that in the *session* zone — #415's shape,
for `date_sent`. An unknown zone (`XYZ`, which the default policy renders as
`-0000`) and a header with no zone at all parse naive too, so they take the
same path. `date_local` must stay None for all three.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

from localmail import sync as sync_mod
from localmail.date_header import date_sent_instant, sender_local_time
from localmail.parser import parse_message
from localmail.sync import process_one_message

from .test_sync import _ensure_account, make_account

_UTC_INSTANT = datetime(2023, 7, 12, 9, 58, 24, tzinfo=timezone.utc)
# Any zone other than UTC exposes the defect; Perth is the Mac cluster's.
_NON_UTC_SESSION_ZONE = "Australia/Perth"

_ZONELESS_HEADERS = [
    "Wed, 12 Jul 2023 09:58:24 -0000",
    "Wed, 12 Jul 2023 09:58:24 XYZ",
    "Wed, 12 Jul 2023 09:58:24",
]


def _raw(date: str) -> bytes:
    return (
        f"Message-Id: <m0@x>\r\nFrom: a@x\r\nDate: {date}\r\n"
        "Subject: s\r\n\r\nbody\r\n"
    ).encode()


@pytest.mark.parametrize("date", _ZONELESS_HEADERS)
def test_a_zoneless_date_is_read_as_utc(date: str) -> None:
    assert date_sent_instant(date) == _UTC_INSTANT
    assert date_sent_instant(date).tzinfo is not None  # type: ignore[union-attr]


def test_an_offset_bearing_date_keeps_its_own_offset() -> None:
    dt = date_sent_instant("Wed, 12 Jul 2023 19:58:24 +1000")
    assert dt == _UTC_INSTANT
    assert dt is not None and dt.utcoffset() is not None
    assert dt.utcoffset().total_seconds() == 10 * 3600  # type: ignore[union-attr]


@pytest.mark.parametrize("raw", [None, "", "not a date"])
def test_absent_or_unparseable_is_still_none(raw: str | None) -> None:
    assert date_sent_instant(raw) is None


@pytest.mark.parametrize("date", _ZONELESS_HEADERS)
def test_date_local_still_states_no_offset(date: str) -> None:
    assert sender_local_time(date) is None


@pytest.mark.parametrize("date", _ZONELESS_HEADERS)
def test_the_parser_yields_an_aware_date_sent(date: str) -> None:
    assert parse_message(_raw(date)).date_sent == _UTC_INSTANT
    assert parse_message(_raw(date)).date_sent.tzinfo is not None  # type: ignore[union-attr]


@pytest.mark.parametrize("date", _ZONELESS_HEADERS)
def test_the_stored_instant_ignores_the_session_zone(
    db_conn: psycopg.Connection, tmp_path: Path, date: str,
) -> None:
    # Load-bearing: on a UTC-default cluster (DGX, likely CI) the pre-fix
    # code stores the right value and this test would pass without it.
    with db_conn.cursor() as cur:
        cur.execute(f"SET TimeZone = '{_NON_UTC_SESSION_ZONE}'")
    account_id = _ensure_account(db_conn, make_account())
    mailbox = sync_mod.upsert_mailbox(
        db_conn, account_id=account_id, name="INBOX", delimiter="/", flags=[],
    )

    msg_id, inserted = process_one_message(
        db_conn, account_id=account_id, mailbox_id=mailbox.id, uid=1,
        raw=_raw(date), flags=[], attachments_root=tmp_path,
    )

    assert inserted
    with db_conn.cursor() as cur:
        cur.execute("SELECT date_sent FROM messages WHERE id = %s", (msg_id,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] == _UTC_INSTANT
