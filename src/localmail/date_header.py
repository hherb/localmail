# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The `Date:` header, read one way for `date_sent` and for `date_local` (#426).

Pure — no IO. `date_sent` is `TIMESTAMPTZ`, so storing an offset-bearing
header keeps the instant and drops the sender's offset; `sender_local_time`
renders the same instant on the sender's own clock. Both go through
`parse_date_header`, so the two cannot disagree about which instant a header
names; `date_sent_instant` only attaches UTC to the naive `-0000` reading
(#431).
"""

from __future__ import annotations

import email.utils
from datetime import datetime, timezone


def parse_date_header(raw: str | None) -> datetime | None:
    """The instant `raw` names, or None when absent or unparseable.

    Naive for the RFC 5322 `-0000` form ("UTC, local zone unknown"), which is
    what `email.utils.parsedate_to_datetime` returns for it. `OverflowError`
    is caught too: a year or offset too large for a C long raises it.
    """
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None


def sender_local_time(raw: str | None) -> str | None:
    """ISO 8601 in the header's own offset, or None when there is none to give.

    None for an absent or unparseable header and for `-0000`, which states
    that the sender's local zone is unknown.
    """
    dt = parse_date_header(raw)
    if dt is None or dt.tzinfo is None:
        return None
    return dt.isoformat()


def date_sent_instant(raw: str | None) -> datetime | None:
    """`parse_date_header`, made aware for storage in `date_sent` (#431).

    A naive result means no usable offset: `-0000`, which RFC 5322 defines
    as UTC, and likewise an unknown zone (`XYZ`) or none at all, which
    `parsedate_to_datetime` reads the same way. Left naive, psycopg
    sends it as `timestamp` and Postgres reads it in the session zone — the
    #415 mechanism. `sender_local_time` keeps the naive reading, since there
    the point is that the sender's own offset is unknown.
    """
    dt = parse_date_header(raw)
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
