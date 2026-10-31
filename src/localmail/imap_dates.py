# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The one rule for what an IMAP INTERNALDATE may become in the database (#415).

`messages.internal_date` is `TIMESTAMPTZ`, and psycopg sends a *naive*
datetime as `timestamp`, which Postgres then reads in the connection's session
time zone. That is the defect #415 was: imapclient's default
`normalise_times=True` produced naive host-local times, so every row was off by
(host offset - session offset). `imap_client.open_connection` now turns
normalising off, and this guard is what keeps a naive value out of the column
if one ever arrives anyway (a server omitting the zone, which RFC 3501 does not
allow, or a client built some other way). Absent is honest; a guessed zone is
not, and search falls back to `date_sent` for a NULL.
"""

from __future__ import annotations

from datetime import datetime


def aware_internal_date(value: object) -> datetime | None:
    """Return ``value`` if it is a timezone-aware datetime, else ``None``."""
    if isinstance(value, datetime) and value.utcoffset() is not None:
        return value
    return None
