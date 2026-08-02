# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Make a Python ``str`` safe for a PostgreSQL ``TEXT`` column.

Postgres ``TEXT`` rejects the NUL byte (``\\x00``) outright — an INSERT carrying
one raises ``psycopg.DataError``. Three independent producers feed ``TEXT``
columns in this codebase and every one of them can see a NUL:

- ``parser.py`` — real mail carries ``\\x00`` in a subject or body when a sender
  mangles an encoding or attaches binary garbage to a text part.
- ``search/extractor.py`` — a document's *extracted* text inherits whatever the
  source contained; PDFs and mis-typed ``text/plain`` blobs are the usual
  offenders.
- ``search/extract_worker.py`` — a third-party exception message can carry one.

Each of those grew its own private ``_no_nul`` copy, and the extractor path
never got one (#249), so a NUL-carrying extraction failed its INSERT and was recorded
as a poison pill — permanently, since re-extracting the same bytes reproduces
the same NUL. This module is the single implementation they now share.

Pure: no IO, no imports beyond ``typing``.
"""

from __future__ import annotations

from typing import overload

_NUL = "\x00"


@overload
def strip_nuls(s: str) -> str: ...


@overload
def strip_nuls(s: None) -> None: ...


def strip_nuls(s: str | None) -> str | None:
    """Return ``s`` with every NUL byte removed; ``None`` passes through.

    ``None`` is accepted because most callers thread optional header values
    straight through (``strip_nuls(subject)``), and the overloads keep the
    return type exact so a ``str`` in stays a ``str`` out under mypy.

    An all-NUL string becomes ``""``, not ``None``: normalising empty to SQL
    NULL is the parser's decision (``strip_nuls(x) or None``), not this
    function's.

    The membership test before ``replace`` is deliberate — it returns the
    original object on the overwhelmingly common clean path rather than
    allocating a copy of every message body in the archive.
    """
    if s is None or _NUL not in s:
        return s
    return s.replace(_NUL, "")


def strip_nuls_all(xs: list[str]) -> list[str]:
    """Apply :func:`strip_nuls` to each element of ``xs``.

    Order and length are preserved: these are multi-valued header lists, where
    dropping an emptied entry would silently renumber the rest.
    """
    return [strip_nuls(x) for x in xs]
