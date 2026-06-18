# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Minimal in-memory IMAP server suitable for sync.py's Protocol surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class FakeFolder:
    name: str
    delimiter: str = "/"
    flags: tuple[str, ...] = ()
    uidvalidity: int = 1
    # Maps UID -> (raw_bytes, flags_tuple, internal_date | None).
    messages: dict[int, tuple[bytes, tuple[str, ...], datetime | None]] = field(default_factory=dict)

    @property
    def uidnext(self) -> int:
        return (max(self.messages) + 1) if self.messages else 1


class FakeIMAPClient:
    """Implements just enough of the imapclient.IMAPClient surface for sync.py."""

    def __init__(self) -> None:
        self.folders: dict[str, FakeFolder] = {}
        self._selected: FakeFolder | None = None
        self._idle_active: bool = False
        self._idle_pending: list = []
        # Test counters so assertions can verify protocol transitions.
        self.idle_call_count = 0
        self.idle_done_call_count = 0

    # --- test setup helpers (not part of the IMAP protocol) ------------------

    @classmethod
    def with_folders(cls, names: list[str]) -> 'FakeIMAPClient':
        """Construct a FakeIMAPClient with the named folders already created.

        All folders get empty flags and the standard '/' delimiter.
        """
        client = cls()
        for name in names:
            client.add_folder(name)
        return client

    def add_folder(self, name: str, *, flags: tuple[str, ...] = (), uidvalidity: int = 1) -> FakeFolder:
        f = FakeFolder(name=name, flags=flags, uidvalidity=uidvalidity)
        self.folders[name] = f
        return f

    def append(
        self,
        folder: str,
        raw: bytes,
        flags: tuple[str, ...] = (),
        internal_date: datetime | None = None,
    ) -> int:
        f = self.folders[folder]
        uid = f.uidnext
        f.messages[uid] = (raw, flags, internal_date)
        return uid

    def bump_uidvalidity(self, folder: str) -> None:
        f = self.folders[folder]
        f.uidvalidity += 1

    # --- IMAP protocol surface -----------------------------------------------

    def list_folders(self) -> list[tuple]:
        return [
            (tuple(f.flags), f.delimiter, f.name) for f in self.folders.values()
        ]

    def select_folder(self, folder: str) -> dict:
        self._selected = self.folders[folder]
        return {
            b"UIDVALIDITY": self._selected.uidvalidity,
            b"UIDNEXT": self._selected.uidnext,
            b"EXISTS": len(self._selected.messages),
        }

    def search(self, criteria) -> list[int]:
        assert self._selected is not None
        if criteria == "ALL" or criteria == ["ALL"]:
            return list(self._selected.messages.keys())
        if isinstance(criteria, list) and len(criteria) == 2 and criteria[0] == "UID":
            spec = criteria[1]
            lo_s, _, hi_s = spec.partition(":")
            lo = int(lo_s)
            uids = list(self._selected.messages.keys())
            if hi_s == "*":
                hits = [u for u in uids if u >= lo]
                if not hits and uids:
                    # Emulate the IMAP quirk that "N:*" always returns at least one UID.
                    return [max(uids)]
                return hits
            hi = int(hi_s)
            return [u for u in uids if lo <= u <= hi]
        raise NotImplementedError(f"unsupported search criteria: {criteria!r}")

    # IDLE protocol surface (test-only, deterministic).

    def idle(self) -> None:
        self._idle_active = True
        self.idle_call_count += 1

    def idle_done(self) -> tuple:
        self._idle_active = False
        self.idle_done_call_count += 1
        return ((), ())

    def idle_check(self, timeout: float = 0.0) -> list:
        # Don't actually sleep — tests need to run instantly.
        responses = list(self._idle_pending)
        self._idle_pending.clear()
        return responses

    def simulate_new_mail(self, count: int = 1) -> None:
        """Queue an EXISTS notification for the next idle_check()."""
        self._idle_pending.append((b"EXISTS", count))

    def fetch(self, uids: list[int], data: list) -> dict:
        assert self._selected is not None
        out: dict[int, dict] = {}
        for uid in uids:
            entry = self._selected.messages.get(int(uid))
            if entry is None:
                continue
            raw, flags, internal_date = entry
            out[int(uid)] = {
                b"BODY[]": raw, b"FLAGS": flags, b"UID": int(uid),
                b"INTERNALDATE": internal_date,
            }
        return out
