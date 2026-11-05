# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The value the Searcher caches for one hybrid result pool (#328).

Written by ``Searcher.search`` (a fresh pool) and ``Searcher.grow_pool`` (a
grown one), read by ``continue_page``, ``grow_pool`` and
``get_pool_metadata``. It used to be a ``dict[str, Any]``, so a writer that
forgot a key failed as a ``KeyError`` in a reader, at continuation time.
Every field here has no default, so the same slip is a ``TypeError`` at the
writer; ``PageCache``'s ``value_type`` refuses anything that is not one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from localmail.search.query import ParsedQuery
from localmail.search.sort_axes import SortMode, SortOrder


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
