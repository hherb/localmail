# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The ``date_local`` hit field: each hit's ``Date:`` on the sender's clock (#426).

``messages.date_sent`` is ``TIMESTAMPTZ``, so the header's own offset is gone
from the row; it survives only in ``raw_bytes``. The header block is read the
way ``GET /v1/messages/{id}?headers=list`` reads it: a bounded prefix, parsed
under the policy sync uses, re-read in full with a WARNING when the block runs
past the ceiling (0 of 129,590 live messages do). The first ``Date:`` counts,
as it does for ``date_sent``.
"""

from __future__ import annotations

import logging

import psycopg

from localmail.date_header import sender_local_time
from localmail.header_block import (
    PREFIX_READ_BYTES, header_block, header_block_of_whole, parse_header_block,
    prefix_is_truncated,
)

logger = logging.getLogger(__name__)

_PREFIXES_SQL = ("SELECT id, substring(raw_bytes from 1 for %s)"
                 " FROM messages WHERE id = ANY(%s)")
_WHOLE_SQL = "SELECT raw_bytes FROM messages WHERE id = %s"


def fetch_local_dates(
    conn: psycopg.Connection, message_ids: list[int],
) -> dict[int, str | None]:
    """``date_local`` per id; None where the header is absent or states no
    offset. Applies no ACL: pass only ids the caller may read (a page of
    already-scoped hits). An id with no row is absent from the result."""
    out: dict[int, str | None] = {}
    if not message_ids:
        return out
    with conn.cursor() as cur:
        cur.execute(_PREFIXES_SQL, (PREFIX_READ_BYTES, message_ids))
        rows = cur.fetchall()
        for mid, prefix in rows:
            block = header_block(bytes(prefix),
                                 truncated=prefix_is_truncated(bytes(prefix)))
            if block is None:
                logger.warning(
                    "no header/body separator found in the first %d bytes of "
                    "message %s; re-reading in full", PREFIX_READ_BYTES, mid)
                cur.execute(_WHOLE_SQL, (mid,))
                whole = cur.fetchone()
                assert whole is not None
                block = header_block_of_whole(bytes(whole[0]))
            date = next((e.value for e in parse_header_block(block)
                         if e.name.lower() == "date"), None)
            out[mid] = sender_local_time(date)
    return out
