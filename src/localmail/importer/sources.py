"""Pure readers for mbox / maildir archive sources (no DB).

Each reader yields `ImportedMessage(mailbox_name, raw_bytes, received_date)`.
`received_date` is the archive's delivery timestamp — the mbox envelope
`From ` line (asctime, treated as UTC) or the maildir message file date —
and becomes `messages.internal_date` on import.
"""
from __future__ import annotations

import mailbox
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


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
    # The asctime is the last 5 whitespace-separated fields, e.g.
    # "Wed Jan  1 12:00:00 2025". str.split() collapses the double space before
    # a single-digit day; strptime's %d accepts the un-padded day either way.
    parts = line.split()
    if len(parts) < 5:
        return None
    candidate = " ".join(parts[-5:])
    try:
        st = time.strptime(candidate, _ASCTIME_FMT)
    except ValueError:
        return None
    return datetime(*st[:6], tzinfo=timezone.utc)


def iter_mbox(path: Path, *, mailbox_name: str) -> Iterator[ImportedMessage]:
    """Yield each message in an mbox file as an ImportedMessage.

    The whole file is one logical folder named `mailbox_name`. The received
    date comes from each message's envelope `From ` line.
    """
    box = mailbox.mbox(str(path), create=False)
    try:
        for key in box.iterkeys():
            msg = box.get_message(key)
            raw = msg.as_bytes()
            received = parse_mbox_from_date(msg.get_from() or "")
            yield ImportedMessage(mailbox_name=mailbox_name, raw=raw, received_date=received)
    finally:
        box.close()


def _maildir_received(msg: mailbox.MaildirMessage) -> datetime | None:
    try:
        return datetime.fromtimestamp(msg.get_date(), tz=timezone.utc)
    except (OSError, ValueError, OverflowError):
        return None


def _iter_one_maildir(
    box: mailbox.Maildir,
    name: str,
) -> Iterator[ImportedMessage]:
    for key in box.iterkeys():
        msg = box.get_message(key)
        yield ImportedMessage(
            mailbox_name=name, raw=msg.as_bytes(), received_date=_maildir_received(msg),
        )


def iter_maildir(path: Path) -> Iterator[ImportedMessage]:
    """Yield every message across a maildir and its subfolders.

    The root maildir maps to a mailbox named after its directory; each
    subfolder (`mailbox.Maildir.list_folders()`) maps to a mailbox preserving
    the folder name. The received date is each message file's delivery time.
    """
    root = mailbox.Maildir(str(path), create=False)
    try:
        yield from _iter_one_maildir(root, path.name)
        for folder_name in root.list_folders():
            yield from _iter_one_maildir(root.get_folder(folder_name), folder_name)
    finally:
        root.close()
