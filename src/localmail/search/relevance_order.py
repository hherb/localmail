# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The one ordering rule for the relevance path: ties break by date.

``sort="date"`` has always been strict — ``date_keyset.DATE_ORDER_BY_SQL``
emits ``COALESCE(internal_date, date_sent) DESC NULLS LAST, m.id DESC``, so
two messages can never swap places between runs. The rank path had no such
rule. ``Searcher._build_results`` sorted on the score alone and Python's
sort is stable, so equal scores kept the order fusion produced — which is
the sequence the four retrieval arms happened to be walked in, and carries
no date information whatever.

Ties are ordinary rather than exotic. An RRF score is a sum of
``1 / (k + rank)``, so any two messages hit at the same rank in different
arms score identically, and with ``reranker_enabled`` false (the default)
that RRF score *is* the final score.

Three stages have to agree or the rule holds on one page and not the next:

* ``rrf_fuse`` orders the fused hits. It is pure and has no database, so it
  breaks ties on ``message_id`` alone — enough to stop arm order deciding,
  not enough to be date-correct.
* ``Searcher._retrieve_pool`` cuts to ``rerank_pool_size``, which decides
  *which* equally relevant rows survive at all. It has a connection, so it
  re-orders on real dates before cutting. Without that the page-level rule
  below would be exact about rows the cut had already thrown away.
* ``Searcher._build_results`` assembles the page the caller sees.

The last two share :func:`relevance_key`, so a change to the rule cannot
reach one and miss the other. That is the same one-authority call
``date_keyset`` makes for the date walk's SQL.
"""
from __future__ import annotations

from datetime import datetime, timezone

#: Sorts below every real timestamp, so an undated row lands **last** under
#: ``reverse=True``. That matches the date walk's ``DESC NULLS LAST`` rather
#: than merely being a convenient filler: both date columns are nullable and
#: archive imports really do produce rows with neither, so the two orderings
#: would otherwise disagree about where those rows go.
NULL_DATE_SENTINEL = datetime.min.replace(tzinfo=timezone.utc)

#: ``(has_date, when)`` — the flag first, so the sentinel can never be
#: compared against a real timestamp and no naive/aware mismatch can arise.
DateKey = tuple[int, datetime]


def date_key(when: datetime | None) -> DateKey:
    """Sort key for ``COALESCE(internal_date, date_sent) DESC NULLS LAST``.

    Built for ``sorted(..., reverse=True)``: ascending it puts undated rows
    first, so reversed they land last.
    """
    return (1, when) if when is not None else (0, NULL_DATE_SENTINEL)


def relevance_key(
    *, score: float, when: datetime | None, message_id: int,
) -> tuple[float, DateKey, int]:
    """Sort key for the rank path, for ``sorted(..., reverse=True)``.

    Score first: the date is a **tiebreak**, not the sort. A key that led
    with the date would quietly turn every relevance search into a date
    search, which is the opposite failure and much harder to notice.

    ``message_id`` last, because the date tiebreak has ties of its own — a
    bulk send shares ``date_sent`` to the second, and an archive import
    derives ``internal_date`` from an mbox ``From_`` line — and without a
    total order the remainder is decided by input order, i.e. the arbitrary
    thing this module exists to remove. It is a tiebreak of a tiebreak and
    carries no meaning; it is here to make the result reproducible.
    """
    return (score, date_key(when), message_id)
