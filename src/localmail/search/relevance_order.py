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

The module also owns what may *enter* that key, because a total order is
only as total as its slots. :func:`date_key` maps an absent date into one;
:func:`finite_scores` keeps a non-finite rerank score out of one. Both are
the same job at different slots, which is why they live together rather
than beside the code that happens to call them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
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


@dataclass(frozen=True)
class FiniteScores:
    """The sanitised score list, and how many entries were substituted.

    A **dataclass, not a NamedTuple**, and the difference is the quality
    of the failure rather than its presence. A caller who forgets
    ``.scores`` and binds the container is caught either way, but not in
    the same place: a two-field ``NamedTuple`` is an iterable of length 2,
    so it gets *past* ``_build_results``' ``zip(hydrated, scores,
    strict=True)`` on a two-row pool and dies one expression later inside
    the sort, as ``'<' not supported between instances of 'list' and
    'int'`` — naming neither the field nor the function. The dataclass
    fails at the zip itself, naming the type. (mypy also rejects the slip
    statically, ``_safe_rerank`` being annotated ``-> list[float]``.)

    The one branch where a ``NamedTuple`` really would be **silent** is
    ``_build_results``' ``sort="date"``, whose key reads ``x[0]`` and
    never touches the score — so it would build a page whose
    ``SearchResult.score`` is a list for one row and an int for another.
    That branch is documented unreachable and pinned by
    ``tests/test_searcher_pool_sort_unreachable.py``, which is what keeps
    this a readability argument rather than a correctness one.
    """

    scores: list[float]
    replaced: int


def finite_scores(
    scores: list[float], *, fallback: list[float],
) -> FiniteScores:
    """Rank every row the reranker could not score below every row it could.

    :func:`relevance_key`'s first slot is a ``float`` handed over by the
    cross-encoder, and a NaN there makes the comparator **inconsistent**:
    every comparison against NaN is ``False``, so neither of a pair is
    "less than" the other and Timsort's output depends on input order.
    The damage is not confined to the NaN row — measured over the shipped
    key, 9 of the 24 permutations of one four-row pool mis-order the three
    rows whose scores were perfectly good. This is the slot-0 counterpart
    of what :func:`date_key`'s flag does for slot 1: keep a value that has
    no place in a total order out of one.

    ``math.isfinite`` is the predicate, so ±inf is substituted alongside
    NaN. An infinity does order consistently, so it corrupts nothing —
    but it pins its row to one end of every page it appears on, which no
    model can have meant, and the remedy is the same. Wording it as "NaN
    is bad, inf is tolerable" would be two predicates for one question.

    **The substitute is derived from the batch's own scores, never from
    the fused RRF score of that row, because the two are not on one
    scale.** RRF is a sum of ``1 / (k + rank)`` terms — with the default
    ``rrf_k = 60`` no term exceeds ``1/61``, and every term is positive
    whenever ``k >= 0``. A cross-encoder returns raw logits, routinely
    negative (fastembed's own example is ``[-1.24, -10.6]``). Splicing an
    RRF value into a list of logits therefore places the row at the sign
    boundary of a distribution it was never measured against: on a query
    the model rates poorly *the one row it failed to score wins the
    page*, which inverts the ranking this module exists to get right.
    Ranking it last is the honest reading of "relevance unknown" — the
    row is still in the pool, still reachable, and no longer claiming a
    relevance nothing established.

    ``nextafter`` rather than ``min(usable) - 1.0`` so the guarantee is
    "strictly below" for every input: at a score near ``-MAX_FLOAT`` the
    subtraction is absorbed and the unscored row would *tie* the worst
    scored one, which the date tiebreak could then resolve upward.

    **The substitute is then re-checked for finiteness, and that is not
    ceremony.** ``nextafter(-MAX_FLOAT, -inf)`` is ``-inf``, so at that one
    input the demotion would put a non-finite value straight back into the
    sort key — this function's own defect, produced by its own fix, and on
    HTTP a 500 from the response renderer. There is no finite value below
    ``-MAX_FLOAT``, so the demotion is simply not representable there.

    ``fallback`` is therefore read on two paths, which the single
    ``isfinite`` test covers together because they mean the same thing —
    *this batch cannot be ordered relative to itself*: when **no** score is
    usable (no batch-relative position to demote to), and when the
    demotion cannot be expressed. Both degrade to the fused order, which is
    self-consistent and is what ``_safe_rerank`` gives when the reranker
    raises — the same event, seen from one layer down.

    Two raises, both of which were already hard failures downstream, so
    neither is newly fatal: a length mismatch (checked here, and again at
    ``_build_results``' strict ``zip``), and a non-numeric score
    (``math.isfinite`` rejects it; ``sorted`` could not order it against
    a float either). ``FastEmbedReranker.rerank`` validates length itself
    and ``_safe_rerank`` degrades on that raise, so only a third-party
    ``Reranker`` reaches these.

    Returns a fresh list on **every** path — the all-finite one included,
    which is the tempting short-circuit — so the signature carries one
    aliasing contract rather than three, the trap ``Searcher._cut_pool``
    records for its own short-circuit.
    """
    if len(scores) != len(fallback):
        raise ValueError(
            f"{len(scores)} scores against {len(fallback)} fallback scores"
        )
    usable = [score for score in scores if math.isfinite(score)]
    unscored = math.nextafter(min(usable), -math.inf) if usable else -math.inf
    if not math.isfinite(unscored):
        return FiniteScores(scores=list(fallback), replaced=len(scores))
    return FiniteScores(
        scores=[s if math.isfinite(s) else unscored for s in scores],
        replaced=len(scores) - len(usable),
    )
