"""Pure readers for mbox / maildir archive sources (no DB).

Each reader yields `ImportedMessage(mailbox_name, raw_bytes, received_date)`.
`received_date` is the archive's delivery timestamp — the mbox envelope
`From ` line (asctime, treated as UTC) or the maildir message file date —
and becomes `messages.internal_date` on import.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class ImportedMessage:
    mailbox_name: str
    raw: bytes
    received_date: datetime | None


_ASCTIME_FMT = "%a %b %d %H:%M:%S %Y"


def parse_mbox_from_date(from_line: str) -> datetime | None:
    """Parse the date from an mbox envelope `From ` line → UTC datetime.

    The line is `<envelope-sender> <asctime>` (asctime carries no timezone, so
    we treat it as UTC — a small, documented imprecision). Returns None when
    the trailing asctime is absent or unparseable.
    """
    line = from_line.strip()
    if not line:
        return None
    # The asctime is the last 5 whitespace-separated fields:
    # "Wed Jan  1 12:00:00 2025" (note the double space before a 1-digit day).
    parts = line.split()
    if len(parts) < 5:
        return None
    candidate = " ".join(parts[-5:])
    try:
        st = time.strptime(candidate, _ASCTIME_FMT)
    except ValueError:
        return None
    return datetime(*st[:6], tzinfo=timezone.utc)
