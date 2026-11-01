# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The `Date:` header, read one way for `date_sent` and for `date_local` (#426).

Pure — no IO. `date_sent` is `TIMESTAMPTZ`, so storing it keeps the instant
and drops the sender's offset; `sender_local_time` renders the same instant on
the sender's own clock. Both go through `parse_date_header`, so the two cannot
disagree about which instant a header names.
"""

from __future__ import annotations

import email.utils
from datetime import datetime


def parse_date_header(raw: str | None) -> datetime | None:
    """The instant `raw` names, or None when absent or unparseable.

    Naive for the RFC 5322 `-0000` form ("UTC, local zone unknown"), which is
    what `email.utils.parsedate_to_datetime` returns for it.
    """
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
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
