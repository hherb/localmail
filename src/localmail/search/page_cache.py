# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Bounded LRU + TTL cache for paginated search results.

Keys are opaque search_token strings (the Searcher generates them). Values
are of one type, named at construction and checked at ``put`` (#328): the
Searcher stores ``PoolEntry`` objects, and a raw dict is refused where it is
written rather than failing later where it is read.
"""

from __future__ import annotations

import time
from collections import OrderedDict


class CacheMissError(KeyError):
    """Token not present or expired."""


class PageOutOfPoolError(IndexError):
    """Requested page beyond the cached pool's size."""


class PageCache[V]:
    """Thread-unsafe LRU+TTL store; wrap in a lock if shared across threads.

    For this project the cache is only touched from inside Searcher methods
    that are themselves called from one request at a time per process; if
    that changes (e.g. concurrent MCP tool calls), add a threading.Lock.

    ``value_type`` is keyword-only with no default: ``object`` would accept
    anything, which is the unchecked bag this parameter exists to end.
    """

    def __init__(self, maxsize: int, ttl_s: float, *, value_type: type[V]) -> None:
        self._maxsize = maxsize
        self._ttl = ttl_s
        self._value_type = value_type
        self._data: OrderedDict[str, tuple[float, V]] = OrderedDict()

    def put(self, key: str, value: V) -> None:
        if not isinstance(value, self._value_type):
            raise TypeError(
                f"PageCache holds {self._value_type.__name__} values, "
                f"not {type(value).__name__}"
            )
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = (time.monotonic(), value)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def get(self, key: str) -> V:
        if key not in self._data:
            raise CacheMissError(key)
        stamp, value = self._data[key]
        if time.monotonic() - stamp > self._ttl:
            del self._data[key]
            raise CacheMissError(key)
        self._data.move_to_end(key)
        return value

    def invalidate(self, key: str) -> None:
        self._data.pop(key, None)
