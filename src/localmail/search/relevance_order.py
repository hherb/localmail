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

**Two** stages carry the rule, and both need it:

* ``Searcher._cut_pool`` cuts to ``rerank_pool_size``, which decides
  *which* equally relevant rows survive at all. It has a connection, so it
  re-orders on real dates before cutting — when it cuts; a pool that
  already fits is returned untouched, since the page-level sort below
  decides the order anyway. Without this stage that page-level rule would
  be exact about rows the cut had already thrown away.
* ``Searcher._build_results`` assembles the page the caller sees, and
  ``continue_page`` re-runs it over the whole cached pool for every later
  page.

They share :func:`relevance_key`, so a change to the rule cannot reach one
and miss the other. That is the same one-authority call ``date_keyset``
makes for the date walk's SQL.

``rrf_fuse`` breaks its own ties on ``message_id`` as well, but that is
**defence in depth, not a third stage**, and the distinction is worth
keeping straight. :func:`relevance_key` is a *total* order — ``rrf_fuse``
emits one hit per ``message_id``, so no two entries can tie on all three
components — and both stages above re-sort with it, so fusion's output
order cannot reach a caller of ``Searcher.search``. What the tiebreak buys
is that ``rrf_fuse`` is a well-defined pure function for its direct callers
and its own tests, and a backstop should anything later consume fused order
directly. Do not describe it as load-bearing for the page: an unobservable
belt presented as a stage is what makes a reader distrust the two that are.
"""
from __future__ import annotations

from datetime import datetime, timezone

#: Padding, and **only** padding: it exists so the key's second slot is
#: always a ``datetime`` and the type stays ``tuple[int, datetime]``. Its
#: *value* is never compared — the ``has_date`` flag below decides the
#: NULLS-LAST placement, and two undated rows both yield this same object.
#: Verified rather than argued: ``datetime.min``, ``datetime.max`` and a
#: naive value all produce identical orderings, and a subclass raising on
#: every comparison operator sorts a mixed pool without ever firing.
#:
#: Private, deliberately. A caller reaching for it would write
#: ``when == _NULL_DATE_SENTINEL`` to mean "undated" and get the wrong
#: answer for a real message dated year 1 — which the ``(has_date, when)``
#: shape exists to make impossible. ``searcher.py``'s own comment on the
#: retired ``_DATE_EXPR_SQL`` alias makes the same call.
_NULL_DATE_SENTINEL = datetime.min.replace(tzinfo=timezone.utc)

#: ``(has_date, when)`` — the flag first, so the *sentinel* can never be
#: compared against a real timestamp. That is the whole of what the flag
#: buys: two **real** timestamps both carry ``1`` and still meet in slot 1,
#: so a naive/aware pair still raises ``TypeError``. What actually rules
#: that out is the schema — ``messages.date_sent`` and
#: ``messages.internal_date`` are both ``TIMESTAMPTZ``, so psycopg only
#: ever yields aware values — not this construction.
DateKey = tuple[int, datetime]


def date_key(when: datetime | None) -> DateKey:
    """Sort key for ``COALESCE(internal_date, date_sent) DESC NULLS LAST``.

    Built for ``sorted(..., reverse=True)``: ascending it puts undated rows
    first, so reversed they land last. The placement comes from the
    ``0``/``1`` flag, not from the sentinel's value.

    This is the *Python* statement of the NULLS-LAST rule. The live date
    walk states it independently in SQL (``date_keyset.DATE_ORDER_BY_SQL``,
    ``DESC NULLS LAST``); the two are bound by a differential test in
    ``tests/test_relevance_order.py``, not by sharing an implementation.
    """
    return (1, when) if when is not None else (0, _NULL_DATE_SENTINEL)


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
