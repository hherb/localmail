# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""#426 item 5: the `Date:` header read once, for `date_sent` and `date_local`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from localmail.date_header import parse_date_header, sender_local_time


def test_the_sender_local_time_keeps_the_header_s_own_offset() -> None:
    # The report's example: stored as 23:58 UTC, written as 09:58 +1000.
    got = sender_local_time("Wed, 12 Jul 2023 09:58:24 +1000")
    assert got == "2023-07-12T09:58:24+10:00"


def test_it_names_the_same_instant_as_date_sent() -> None:
    raw = "Wed, 12 Jul 2023 09:58:24 +1000"
    local = datetime.fromisoformat(sender_local_time(raw))
    assert local == parse_date_header(raw) == datetime(
        2023, 7, 11, 23, 58, 24, tzinfo=UTC)


@pytest.mark.parametrize("raw", [None, "", "not a date", "Wed, 99 Foo 2023"])
def test_an_absent_or_unparseable_header_has_no_local_time(raw) -> None:
    assert sender_local_time(raw) is None


def test_minus_zero_zero_states_no_local_offset() -> None:
    # RFC 5322 3.3: "-0000" says the time is UTC and the sender's local zone
    # is unknown, so there is no sender-local time to give.
    assert sender_local_time("Wed, 12 Jul 2023 09:58:24 -0000") is None
    assert parse_date_header("Wed, 12 Jul 2023 09:58:24 -0000") is not None
