# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Tests for the in-memory page cache used for pagination."""

from __future__ import annotations

import time

from localmail.search.page_cache import PageCache, PageOutOfPoolError, CacheMissError


def test_put_get_roundtrip():
    c = PageCache(maxsize=4, ttl_s=60)
    c.put("tok", {"results": list(range(50)), "pool_size": 50})
    e = c.get("tok")
    assert e["pool_size"] == 50


def test_missing_token_raises():
    c = PageCache(maxsize=4, ttl_s=60)
    import pytest
    with pytest.raises(CacheMissError):
        c.get("nope")


def test_ttl_eviction():
    c = PageCache(maxsize=4, ttl_s=0.05)
    c.put("tok", {"results": [1]})
    time.sleep(0.1)
    import pytest
    with pytest.raises(CacheMissError):
        c.get("tok")


def test_lru_eviction_when_full():
    c = PageCache(maxsize=2, ttl_s=60)
    c.put("a", {"results": []})
    c.put("b", {"results": []})
    c.get("a")  # touches a; b becomes LRU
    c.put("c", {"results": []})  # evicts b
    import pytest
    with pytest.raises(CacheMissError):
        c.get("b")
    assert c.get("a") is not None
    assert c.get("c") is not None
