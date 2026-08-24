# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""``sort_order`` on the request *model*: accepted, and null-by-default.

Schema-level only — nothing here issues a request. The behaviour that
needs the wire (the field reaching ``searcher.search``, and the
``sort="rank"`` + ``sort_order="asc"`` 400) is pinned in
``test_serve_search_route.py``, beside the identical pins its sibling
``sort`` already had.
"""
from __future__ import annotations

from localmail.serve.routes.search import SearchRequest


def test_sort_order_is_null_by_default_not_desc() -> None:
    """"Omitted" must stay distinguishable from "asked for" (#308).

    Alongside a cursor the cursor decides the ordering; a model default of
    "desc" would be a statement the caller never made, and would contradict
    every ascending cursor.
    """
    assert SearchRequest(query="x").sort_order is None


def test_sort_order_accepts_both_directions() -> None:
    assert SearchRequest(query="x", sort_order="asc").sort_order == "asc"
    assert SearchRequest(query="x", sort_order="desc").sort_order == "desc"


def test_sort_order_rejects_anything_else() -> None:
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SearchRequest(query="x", sort_order="ascending")


def test_sort_is_still_null_by_default() -> None:
    assert SearchRequest(query="x").sort is None
