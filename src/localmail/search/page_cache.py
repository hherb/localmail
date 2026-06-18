# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Bounded LRU + TTL cache for paginated search results.

Keys are opaque search_token strings (the Searcher generates them). Values
are dicts holding the reranked pool plus parsed query metadata.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any


class CacheMissError(KeyError):
    """Token not present or expired."""


class PageOutOfPoolError(IndexError):
    """Requested page beyond the cached pool's size."""


class PageCache:
    """Thread-unsafe LRU+TTL store; wrap in a lock if shared across threads.

    For this project the cache is only touched from inside Searcher methods
    that are themselves called from one request at a time per process; if
    that changes (e.g. concurrent MCP tool calls), add a threading.Lock.
    """

    def __init__(self, maxsize: int, ttl_s: float) -> None:
        self._maxsize = maxsize
        self._ttl = ttl_s
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def put(self, key: str, value: Any) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = (time.monotonic(), value)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def get(self, key: str) -> Any:
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
