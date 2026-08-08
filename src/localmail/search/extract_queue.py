# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The one authority on what the attachment-extraction queue still holds.

`extract_worker._claim_batch` decides which blobs the worker will process;
`localmail search-status` reports how many are outstanding. Those two used to
be written apart, and drifted (#277): the report derived pending work as
``eligible - extracted``, where *extracted* meant "has an ``attachment_text``
row with non-empty text". Every blob the worker had already **disposed of by
writing an empty-text sentinel** — ``type-skipped`` (#216),
``lightweight-empty``, ``size-skipped``, a #266-healed row — therefore counted
as outstanding forever, because the claim excludes a blob the moment *any*
``attachment_text`` row exists. Blobs parked at a retry cap (#153) were the
same defect wearing a different row.

That is exactly the drift #251 found on the language half of the same command,
and the fix has the same shape: the claim predicate lives here, and both the
worker and the report compose it rather than restating it.

The eligible population divides into four buckets that are **disjoint and
jointly exhaustive** — `test_extract_queue.py` pins the partition:

======================  =========================================
``extracted``           an ``attachment_text`` row carrying text
``no_text``             an ``attachment_text`` row carrying ``''``
``gave_up``             no row, and a retry cap is exhausted
``pending``             no row, and the worker will still claim it
======================  =========================================

Only ``pending`` is work; ``gave_up`` clears via
``localmail retry-failed-extractions``, and ``no_text`` is terminal by design
(``_claim_batch`` never re-opens a rowed blob — see the one-way-door note on
``SKIPPED_EXTRACTOR``).

Every fragment below assumes ``attachment_blobs`` is aliased ``b`` and that
`QUEUE_FROM_SQL` supplied the joins, so the aliases the predicates read are
defined by the same string that introduces them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import psycopg
from psycopg.rows import class_row

from localmail.config import SearchConfig

QUEUE_FROM_SQL = """
    FROM attachment_blobs b
    LEFT JOIN attachment_text       t  USING (sha256)
    LEFT JOIN failed_extractions    f  USING (sha256)
    LEFT JOIN transient_extractions tr USING (sha256)
"""
"""FROM clause defining the aliases every predicate here reads."""

UNDER_RETRY_CAPS_SQL = (
    "(f.sha256 IS NULL OR f.retry_count < %(max_retries)s)"
    " AND (tr.sha256 IS NULL OR tr.transient_count < %(max_transient_retries)s)"
)
"""Both retry budgets still have room: the poison-pill counter
(``failed_extractions.retry_count``) and the independent consecutive-transient
counter (``transient_extractions.transient_count``, #153)."""

CLAIMABLE_WHERE_SQL = f"t.sha256 IS NULL AND ({UNDER_RETRY_CAPS_SQL})"
"""What `extract_worker._claim_batch` will hand the worker.

Deliberately carries **no allowlist half** — the MIME/extension allowlists live
in `SearchConfig` and are applied in Python after the claim (#216), so a blob
outside them is claimed, then disposed of with a ``type-skipped`` row.
"""

GAVE_UP_WHERE_SQL = f"t.sha256 IS NULL AND NOT ({UNDER_RETRY_CAPS_SQL})"
"""Unprocessed, but parked: a retry cap is exhausted so the claim skips it.
Recoverable — `localmail retry-failed-extractions` clears both counters."""

EXTRACTED_WHERE_SQL = "t.sha256 IS NOT NULL AND t.extracted_text <> ''"
"""Processed, with text to show for it."""

NO_TEXT_WHERE_SQL = "t.sha256 IS NOT NULL AND t.extracted_text = ''"
"""Processed into a sentinel: skipped by size or type, extracted to nothing,
or healed by #266. ``attachment_text.extracted_text`` is ``NOT NULL``, so this
and `EXTRACTED_WHERE_SQL` between them cover every rowed blob."""

ALLOWLISTED_WHERE_SQL = """
    b.mime_type = ANY(%(mime_allowlist)s)
    OR EXISTS (
        SELECT 1
          FROM messages m, jsonb_array_elements(m.attachments) AS a
         WHERE m.attachments @> jsonb_build_array(
                 jsonb_build_object('sha256', encode(b.sha256,'hex')))
           AND a->>'sha256' = encode(b.sha256,'hex')
           AND lower(substring(a->>'filename' FROM '\\.[^.]+$'))
               = ANY(%(extension_allowlist)s))
"""
"""SQL mirror of `attachment_kind.is_allowlisted`.

The extension half reads the **original filename** out of
``messages.attachments``, never ``attachment_blobs.path`` — that path is
content-addressable and extensionless, so a ``suffix`` comparison against it is
always ``''`` (#216). Unparenthesised; combine it as ``WHERE (…)``.
"""

QUEUE_COUNTS_SQL = f"""
    SELECT count(*)                                          AS eligible,
           count(*) FILTER (WHERE {EXTRACTED_WHERE_SQL})     AS extracted,
           count(*) FILTER (WHERE {NO_TEXT_WHERE_SQL})       AS no_text,
           count(*) FILTER (WHERE {GAVE_UP_WHERE_SQL})       AS gave_up,
           count(*) FILTER (WHERE {CLAIMABLE_WHERE_SQL})     AS pending
    {QUEUE_FROM_SQL}
    WHERE ({ALLOWLISTED_WHERE_SQL})
"""
"""All five counters in one pass.

Aggregated rather than run as five statements because the allowlist half is an
``EXISTS`` over ``messages.attachments`` — the expensive part of
`search-status` on a real archive, and there is no reason to pay for it more
than once.
"""


def cap_params(*, max_retries: int, max_transient_retries: int) -> dict[str, int]:
    """Bind the two retry budgets `CLAIMABLE_WHERE_SQL` reads.

    Keyword-only: the two are both small ints with the same type, so a
    positional call site could swap them and still run.
    """
    return {
        "max_retries": max_retries,
        "max_transient_retries": max_transient_retries,
    }


def allowlist_params(
    *, mime_allowlist: Sequence[str], extension_allowlist: Sequence[str]
) -> dict[str, list[str]]:
    """Bind the two allowlists `ALLOWLISTED_WHERE_SQL` reads."""
    return {
        "mime_allowlist": list(mime_allowlist),
        "extension_allowlist": list(extension_allowlist),
    }


@dataclass(frozen=True)
class QueueCounts:
    """How the eligible blob population divides, at one moment.

    Constructed by name (psycopg ``class_row``), so the SELECT's column
    aliases — not their order — are what bind to these fields.
    """

    eligible: int
    extracted: int
    no_text: int
    gave_up: int
    pending: int

    def __post_init__(self) -> None:
        total = self.extracted + self.no_text + self.gave_up + self.pending
        if total != self.eligible:
            raise ValueError(
                f"extraction queue buckets do not sum to the eligible "
                f"population: extracted={self.extracted} no_text={self.no_text} "
                f"gave_up={self.gave_up} pending={self.pending} "
                f"(total {total}) vs eligible={self.eligible}"
            )


def fetch_queue_counts(
    conn: psycopg.Connection, cfg: SearchConfig
) -> QueueCounts:
    """Count the eligible blob population by disposition (one query).

    The only IO in this module — kept beside the predicates it runs so a
    reader sees the rule and its single caller together, the same shape as
    `uids.max_label_uid`.
    """
    params: dict[str, object] = {
        **cap_params(
            max_retries=cfg.extract_worker_max_retries,
            max_transient_retries=cfg.extract_worker_max_transient_retries,
        ),
        **allowlist_params(
            mime_allowlist=cfg.extractor_mime_allowlist,
            extension_allowlist=cfg.extractor_extension_allowlist,
        ),
    }
    with conn.cursor(row_factory=class_row(QueueCounts)) as cur:
        cur.execute(QUEUE_COUNTS_SQL, params)  # noqa: S608
        row = cur.fetchone()
        assert row is not None
        return row
