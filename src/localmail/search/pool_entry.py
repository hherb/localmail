# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The value the Searcher caches for one hybrid result pool (#328).

Written by ``Searcher.search`` (a fresh pool) and ``Searcher.grow_pool`` (a
grown one), read by ``continue_page``, ``grow_pool`` and
``get_pool_metadata``. It used to be a ``dict[str, Any]``, so a writer that
forgot a key failed as a ``KeyError`` in a reader, at continuation time.
Every field here has no default, so the same slip is a ``TypeError`` at the
writer; ``PageCache``'s ``value_type`` refuses anything that is not one. The
two cross-field rules are checked here too, so a broken writer fails at the
``put`` rather than at the next continuation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from localmail.search.query import ParsedQuery
from localmail.search.sort_axes import (
    SortMode, SortOrder, is_rankable, ordering_pair_error,
)


@dataclass(frozen=True)
class PoolEntry:
    # The query the pool was retrieved for, after account-name resolution.
    parsed: ParsedQuery
    # Hydrated hits, in retrieval order; ``scores`` is parallel to it.
    hydrated: list[dict[str, Any]]
    scores: list[float]
    candidates_per_arm: int
    rerank_pool_size: int
    page_size: int
    # The caller that built the pool; ``None`` means unscoped (CLI, library).
    # A reader given a different user treats the token as unknown.
    user_id: int | None
    # The ordering the pool was built with. ``PoolMetadata`` reports both so
    # a paging request contradicting the pool is refused, not ignored (#312).
    sort: SortMode
    sort_order: SortOrder
    # The page a grow started this pool at, and every message the pages
    # before it served (#426). A fresh pool is ``1`` and empty.
    first_page: int
    excluded_ids: frozenset[int]

    def __post_init__(self) -> None:
        # Readers zip the two with ``strict=True``; this moves that failure
        # from continuation time to the writer.
        if len(self.scores) != len(self.hydrated):
            raise ValueError(
                f"PoolEntry scores ({len(self.scores)}) and hydrated "
                f"({len(self.hydrated)}) must be parallel")
        # The fourth holder of #356's pair, beside SearchPage, PoolMetadata
        # and search_response; rankability is the pool query's own.
        pair_error = ordering_pair_error(
            sort=self.sort, rankable=is_rankable(free_text=self.parsed.free_text))
        if pair_error is not None:
            raise ValueError(pair_error)
