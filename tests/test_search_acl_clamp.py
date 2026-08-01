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

from dataclasses import replace

import psycopg

from localmail.api.search import _scope_filters_by_acl, build_query_string
from localmail.search.arms import _filter_sql
from localmail.search.query import parse_query
from localmail.search.searcher import (
    Searcher,
    _NO_ACCOUNT_SENTINEL,
    _clamp_account_ids_to_acl,
)


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
    _, params = _filter_sql(clamped.filters)
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


def test_empty_acl_matches_nothing_rather_than_everything() -> None:
    # An empty list is a real (degenerate) grant, distinct from None. It must
    # NOT fall through to "no clause" — that would hand a caller with zero
    # grants the whole archive.
    parsed = parse_query("secret")
    clamped = _clamp_account_ids_to_acl(parsed, [])
    assert clamped.filters.account_ids == [_NO_ACCOUNT_SENTINEL]
    where, params = _filter_sql(clamped.filters)
    assert "account_id = ANY" in where
    assert params == [[_NO_ACCOUNT_SENTINEL]]


def test_clamp_is_a_noop_when_no_acl_supplied() -> None:
    # CLI / local callers pass allowed_account_ids=None and keep full DSL power.
    # The None branch lives inside the helper, so this drives the production
    # path rather than merely restating what parse_query returned.
    parsed = parse_query("invoice account_id:7")
    clamped = _clamp_account_ids_to_acl(parsed, None)
    assert clamped is parsed
    assert clamped.filters.account_ids == [7]


def test_account_name_filter_and_account_ids_intersect_never_widen() -> None:
    # `account:NAME` resolves into the separate `filters.accounts` field, which
    # `_filter_sql` emits as its own AND clause — so a smuggled account name can
    # only narrow the clamped id set, never union into it. That is why the clamp
    # deliberately covers `account_ids` alone.
    parsed = parse_query("invoice account:other account_id:5")
    clamped = _clamp_account_ids_to_acl(parsed, [5])
    # Stand in for `_resolve_account_names` resolving "other" to a foreign id.
    filters = replace(clamped.filters, accounts=[7])
    where, params = _filter_sql(filters)
    assert where.count("m.account_id = ANY(%s)") == 2
    assert params[:2] == [[7], [5]]


def _bare_searcher() -> Searcher:
    """A Searcher with no backends: `_resolve_account_names` touches only the
    connection it is handed, so the pool/embedder/reranker are never read."""
    return Searcher.__new__(Searcher)


def test_unknown_account_name_matches_nothing_not_everything(
    db_conn: psycopg.Connection,
) -> None:
    """Regression, same empty-list trap one field over: when every `account:NAME`
    is unknown, `_resolve_account_names` used to set `accounts=[]`, which is
    falsy in `_filter_sql` and dropped the clause — matching *every* account,
    the exact opposite of the "matching no rows" warning it logs."""
    parsed = parse_query("invoice account:does-not-exist")
    resolved = _bare_searcher()._resolve_account_names(db_conn, parsed)
    assert resolved.filters.accounts == [_NO_ACCOUNT_SENTINEL]
    where, params = _filter_sql(resolved.filters)
    assert "m.account_id = ANY(%s)" in where
    assert params == [[_NO_ACCOUNT_SENTINEL]]


def test_known_account_name_resolves_to_its_id(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('acl-clamp-known', 'a@example.com', 'imap.example.com', "
            "        'password') RETURNING id",
        )
        row = cur.fetchone()
        assert row is not None
        account_id = row[0]
    parsed = parse_query("invoice account:acl-clamp-known")
    resolved = _bare_searcher()._resolve_account_names(db_conn, parsed)
    assert resolved.filters.accounts == [account_id]


def test_search_requires_an_explicit_allowed_account_ids() -> None:
    """Omitting the ACL must be a TypeError, never a silent full-archive search.

    `allowed_account_ids=None` is a legitimate value ("no ACL", used by the CLI
    and the acceptance harnesses), so it cannot also be the default: a new
    caller that forgets the kwarg would inherit unscoped access with no type
    error and no test failure. Requiring it makes "no ACL" a decision recorded
    at the call site.
    """
    import inspect

    param = inspect.signature(Searcher.search).parameters["allowed_account_ids"]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
