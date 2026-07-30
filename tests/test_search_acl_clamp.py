# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Regression: the per-user ACL is a hard bound no search DSL token can widen.

The API/MCP layers express the ACL by injecting `account_id:` tokens into the
query string. `parse_query` unions *every* `account_id:` token — including any
smuggled through the untrusted free-text query — into one list, which would
OR-widen the `m.account_id = ANY(...)` predicate past the caller's grant. The
Searcher clamps that list to the allowed set before any retrieval, so an
injected id can never reach another account's mail.
"""

from __future__ import annotations

from localmail.api.search import _scope_filters_by_acl, build_query_string
from localmail.search.arms import _filter_sql
from localmail.search.query import parse_query
from localmail.search.searcher import _NO_ACCOUNT_SENTINEL, _clamp_account_ids_to_acl


def test_free_text_account_id_injection_is_clamped_to_acl() -> None:
    # Caller granted ONLY account 5; query smuggles account_id:7 via free text.
    allowed = [5]
    scoped = _scope_filters_by_acl({}, allowed)
    assert scoped is not None
    query = build_query_string(free_text="invoice account_id:7", filters=scoped)
    parsed = parse_query(query)
    # Both ids land in the same list pre-clamp — that is the vulnerability.
    assert set(parsed.filters.account_ids or []) == {5, 7}

    clamped = _clamp_account_ids_to_acl(parsed, allowed)
    assert clamped.filters.account_ids == [5]
    where, params = _filter_sql(clamped.filters)
    assert params == [[5]]


def test_no_account_filter_is_restricted_to_full_acl() -> None:
    parsed = parse_query("invoice")
    clamped = _clamp_account_ids_to_acl(parsed, [5, 6])
    assert set(clamped.filters.account_ids or []) == {5, 6}


def test_caller_may_narrow_within_acl() -> None:
    parsed = parse_query("invoice account_id:6")
    clamped = _clamp_account_ids_to_acl(parsed, [5, 6])
    assert clamped.filters.account_ids == [6]


def test_all_injected_ids_outside_acl_match_nothing() -> None:
    # Every requested id is outside the grant -> a sentinel that matches no
    # account, never the empty-list-drops-the-clause "all accounts" failure.
    parsed = parse_query("secret account_id:98 account_id:99")
    clamped = _clamp_account_ids_to_acl(parsed, [5])
    assert clamped.filters.account_ids == [_NO_ACCOUNT_SENTINEL]
    where, _ = _filter_sql(clamped.filters)
    assert "account_id = ANY" in where


def test_clamp_skipped_when_no_acl_supplied() -> None:
    # CLI / local callers pass allowed_account_ids=None and keep full DSL power.
    parsed = parse_query("invoice account_id:7")
    # The helper is only invoked when allowed is not None; assert the DSL is
    # otherwise untouched so the local-search contract is preserved.
    assert parsed.filters.account_ids == [7]
