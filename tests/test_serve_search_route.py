# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

from unittest.mock import MagicMock

import psycopg
from fastapi.testclient import TestClient

from localmail.serve.app import create_app


def _seed_acct_and_grant(db_conn: psycopg.Connection, user_id: int) -> None:
    """Seed one account + grant `user_id` access so the ACL short-circuit
    doesn't fire — these tests exercise validation/auth/searcher-mock paths,
    not the ACL filter, and need the user to be reachable.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('a','x@y.test','imap.x','password') RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        cur.execute(
            "INSERT INTO user_accounts (user_id, account_id) VALUES (%s, %s)",
            (user_id, int(row[0])),
        )
    db_conn.commit()


def _fake_searcher_returning_one_hit():
    s = MagicMock()
    result = MagicMock()
    result.message_id = 7
    result.account_id = 1
    result.rank = 1
    result.score = 0.9
    result.rrf_score = 0.5
    result.subject = "hello"
    result.from_addr = "a@x"
    result.from_name = "A"
    result.date_sent = None
    result.snippet = "hi"
    result.snippet_source = "body"
    result.attachment_filename = None
    result.matched_chunk_id = None
    result.matched_chunk_table = "message_chunks"
    page = MagicMock()
    page.results = [result]
    page.search_token = "tok-99"
    page.timing_ms = {"total": 5.0}
    page.has_more_in_pool = False
    page.can_grow_pool = False
    page.candidates_per_arm = 50
    page.page = 1
    # Pool-cursor mock — explicit None keeps `_next_cursor` out of the
    # keyset branch (MagicMock's auto-attr would be truthy).
    page.next_keyset = None
    # Explicit values so JSON serialization doesn't choke on a MagicMock attr
    # (run_search reads rewrite_status/rewrite_note off the page on page 1).
    page.rewrite_status = "applied"
    page.rewrite_note = None
    page.rewrite_note_code = None
    s.search.return_value = page
    return s


def test_search_returns_results(db_dsn: str, api_token: str, db_conn, api_user) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    app = create_app(db_dsn=db_dsn, searcher=_fake_searcher_returning_one_hit())
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["message_id"] == "7"
    assert body["next_cursor"] is None


def test_search_sort_param_is_forwarded_to_searcher(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """The HTTP layer must pass `sort` through to `searcher.search()`. A
    silently-ignored sort param is the worst kind of bug: the API
    accepts the field, the client sees a 200, but the user keeps getting
    the same rank-ordered results.
    """
    _seed_acct_and_grant(db_conn, api_user.id)
    fake_searcher = _fake_searcher_returning_one_hit()
    app = create_app(db_dsn=db_dsn, searcher=fake_searcher)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20, "sort": "date"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    fake_searcher.search.assert_called_once()
    call_kwargs = fake_searcher.search.call_args.kwargs
    assert call_kwargs["sort"] == "date"


def test_search_sort_defaults_to_rank_when_omitted(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """Omitting `sort` must default to "rank" — backward-compatible with
    callers who don't know about the new field."""
    _seed_acct_and_grant(db_conn, api_user.id)
    fake_searcher = _fake_searcher_returning_one_hit()
    app = create_app(db_dsn=db_dsn, searcher=fake_searcher)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert fake_searcher.search.call_args.kwargs["sort"] == "rank"


def test_search_sort_invalid_value_is_rejected(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """An out-of-enum `sort` value must 422, not silently fall back."""
    _seed_acct_and_grant(db_conn, api_user.id)
    app = create_app(db_dsn=db_dsn, searcher=_fake_searcher_returning_one_hit())
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20, "sort": "popularity"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 422


def test_sort_date_with_text_paginates_keyset_unbounded(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """End-to-end: sort=date + non-empty query walks every match via the
    wire ``next_cursor`` (no pool cap, no grow_pool dance).

    Uses a real Searcher against the live test DB — the lexical-date
    path is a SQL query, no embeddings/rerank needed.
    """
    from datetime import datetime, timedelta, timezone

    from localmail.config import SearchConfig
    from localmail.db import open_pool
    from localmail.search.searcher import Searcher

    # Seed 30 matching messages on a single account, grant the api_user.
    now = datetime.now(timezone.utc)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('a','x@y.test','imap.x','password') RETURNING id"
        )
        aid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO user_accounts (user_id, account_id) VALUES (%s, %s)",
            (api_user.id, int(aid)),
        )
        ids: list[int] = []
        for i in range(30):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, internal_date)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1, %s) RETURNING id",
                (aid, f"<m{i}@x>", bytes([i + 1]) * 32,
                 f"e-ticket booking #{i:02d}", "body",
                 now - timedelta(hours=i)),
            )
            ids.append(int(cur.fetchone()[0]))
    db_conn.commit()

    pool = open_pool(db_dsn)
    try:
        searcher = Searcher(
            pool=pool, cfg=SearchConfig(),
            embeddings=None, reranker=None, rewriter=None,
        )
        app = create_app(db_dsn=db_dsn, searcher=searcher)
        c = TestClient(app)
        seen: list[str] = []
        body = c.post(
            "/v1/search",
            json={"query": "e-ticket", "filters": {}, "limit": 10, "sort": "date"},
            headers={"Authorization": f"Bearer {api_token}"},
        ).json()
        seen.extend(r["message_id"] for r in body["results"])
        while body["next_cursor"] is not None:
            body = c.post(
                "/v1/search",
                json={"query": "e-ticket", "filters": {}, "limit": 10,
                      "sort": "date", "cursor": body["next_cursor"]},
                headers={"Authorization": f"Bearer {api_token}"},
            ).json()
            seen.extend(r["message_id"] for r in body["results"])
    finally:
        pool.close()
    # All 30 messages must surface, newest first.
    assert seen == [str(i) for i in ids]


def test_search_validation_failure(db_dsn: str, api_token: str, db_conn, api_user) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    app = create_app(db_dsn=db_dsn, searcher=_fake_searcher_returning_one_hit())
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "x", "filters": {"after": "not-a-date"}, "limit": 20},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 400


def test_search_requires_auth(db_dsn: str) -> None:
    app = create_app(db_dsn=db_dsn, searcher=_fake_searcher_returning_one_hit())
    c = TestClient(app)
    r = c.post("/v1/search", json={"query": "x", "filters": {}, "limit": 20})
    assert r.status_code == 401


def test_search_account_ids_filter_returns_200(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    app = create_app(db_dsn=db_dsn, searcher=_fake_searcher_returning_one_hit())
    c = TestClient(app)
    # Search by account_ids=[1] — the seeded account is id=1 so the intersection
    # matches; the mocked searcher returns its canned hit.
    r = c.post(
        "/v1/search",
        json={"query": "", "filters": {"account_ids": ["1"]}, "limit": 5},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200


def test_search_unavailable_when_no_searcher(db_dsn: str, api_token: str) -> None:
    app = create_app(db_dsn=db_dsn, searcher=None)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "x", "filters": {}, "limit": 20},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 503
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "/problems/feature-unavailable"
    assert body["status"] == 503


def test_search_malformed_account_id_in_filter_returns_400(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """Non-digit account_ids filter entry surfaces as problem+json 400 via
    parse_int_id — the same uniform shape as path-param malformed IDs."""
    _seed_acct_and_grant(db_conn, api_user.id)
    app = create_app(db_dsn=db_dsn, searcher=_fake_searcher_returning_one_hit())
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "x", "filters": {"account_ids": ["-1"]}, "limit": 5},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "/problems/validation-failed"
    assert "account_id" in body["detail"]


def test_search_malformed_folder_id_in_filter_returns_400(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """Non-digit folder_ids filter entry surfaces as problem+json 400."""
    _seed_acct_and_grant(db_conn, api_user.id)
    app = create_app(db_dsn=db_dsn, searcher=_fake_searcher_returning_one_hit())
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "x", "filters": {"folder_ids": ["not-a-number"]}, "limit": 5},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "/problems/validation-failed"
    assert "folder_id" in body["detail"]


def test_search_cursor_forwarded_to_run_search(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """Wire-level: when the client sends `cursor`, the route must forward it
    to run_search so continue_page fires instead of search()."""
    _seed_acct_and_grant(db_conn, api_user.id)
    fake = _fake_searcher_returning_one_hit()
    # Make the fake's continue_page return the same shape as .search.
    fake.continue_page = fake.search
    app = create_app(db_dsn=db_dsn, searcher=fake)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20,
              "cursor": "tok-99:2"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    fake.continue_page.assert_called_once()
    args = fake.continue_page.call_args
    assert args.args[0] == "tok-99"
    assert args.args[1] == 2


def test_search_pages_a_date_cursor_when_sort_is_omitted(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """`sort` is null-by-default on the wire so that omitting it is not read
    as asking for "rank": a `K|…` cursor continues a date-sorted walk, and
    the route must hand the Searcher that sort rather than the model's
    default, which selects a different retrieval branch and drops the cursor.
    """
    from datetime import datetime, timezone
    from localmail.api.search_cursor import encode_keyset_cursor
    from localmail.search.searcher import KeysetCursor

    _seed_acct_and_grant(db_conn, api_user.id)
    fake = _fake_searcher_returning_one_hit()
    incoming = KeysetCursor(ts=datetime(2026, 5, 21, tzinfo=timezone.utc), id=100)
    app = create_app(db_dsn=db_dsn, searcher=fake)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20,
              "cursor": encode_keyset_cursor(incoming, "desc")},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    call_kwargs = fake.search.call_args.kwargs
    assert call_kwargs["sort"] == "date"
    assert call_kwargs["keyset_cursor"] == incoming


def test_search_rejects_a_stated_sort_the_cursor_cannot_serve(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """The contradiction is the client's to resolve: honouring the sort means
    dropping the cursor (a restart dressed as page 2), honouring the cursor
    means ignoring a field the caller explicitly set."""
    from datetime import datetime, timezone
    from localmail.api.search_cursor import encode_keyset_cursor
    from localmail.search.searcher import KeysetCursor

    _seed_acct_and_grant(db_conn, api_user.id)
    fake = _fake_searcher_returning_one_hit()
    cursor = encode_keyset_cursor(
        KeysetCursor(ts=datetime(2026, 5, 21, tzinfo=timezone.utc), id=100),
        "desc",
    )
    app = create_app(db_dsn=db_dsn, searcher=fake)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20,
              "sort": "rank", "cursor": cursor},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 400
    assert r.json()["type"] == "/problems/validation-failed"
    fake.search.assert_not_called()


def test_search_serves_a_cursor_whose_query_is_only_filter_operators(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """The shape that must never reach the wire as a 500.

    ``"subject:invoice"`` is a non-blank request field that ``parse_query``
    reduces to an empty free text. That used to be a 400: the blank-query
    branch dropped the cursor, so continuing was impossible. It paginates
    now, so the request is served — and the Searcher must be handed the
    cursor rather than raising ``KeysetCursorUnusable``, whose bare
    ``ValueError`` is not an ``APIError`` and would go out as ``500
    Internal Server Error`` with no problem+json body. Only the transport
    shows that, which is why this test is here and not beside its
    api-level sibling.
    """
    from datetime import datetime, timezone
    from localmail.api.search_cursor import encode_keyset_cursor
    from localmail.search.searcher import KeysetCursor

    _seed_acct_and_grant(db_conn, api_user.id)
    fake = _fake_searcher_returning_one_hit()
    incoming = KeysetCursor(ts=datetime(2026, 5, 21, tzinfo=timezone.utc), id=100)
    app = create_app(db_dsn=db_dsn, searcher=fake)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "subject:invoice", "filters": {}, "limit": 20,
              "cursor": encode_keyset_cursor(incoming, "desc")},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert fake.search.call_args.kwargs["keyset_cursor"] == incoming


def test_search_smart_param_is_forwarded_to_searcher(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    fake = _fake_searcher_returning_one_hit()
    fake.smart_available = True
    app = create_app(db_dsn=db_dsn, searcher=fake)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20, "smart": True},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert fake.search.call_args.kwargs["smart"] is True
    body = r.json()
    # The structured outcome must reach the wire, not just the derived bool.
    assert body["rewrite_status"] == "applied"
    assert body["rewrite_note"] is None
    assert body["rewrite_note_code"] is None
    assert body["rewrite_skipped"] is False


def test_search_smart_defaults_false_and_response_carries_flag(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    fake = _fake_searcher_returning_one_hit()
    fake.smart_available = True
    app = create_app(db_dsn=db_dsn, searcher=fake)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert fake.search.call_args.kwargs["smart"] is False
    assert r.json()["rewrite_skipped"] is False


def test_search_smart_without_rewriter_degrades(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    fake = _fake_searcher_returning_one_hit()
    fake.smart_available = False
    app = create_app(db_dsn=db_dsn, searcher=fake)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20, "smart": True},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert fake.search.call_args.kwargs["smart"] is False
    body = r.json()
    # The full structured outcome must reach the wire, not just the bool.
    assert body["rewrite_status"] == "unavailable"
    assert body["rewrite_note"] == "smart search is not configured on this server"
    assert body["rewrite_note_code"] == "not_configured"
    assert body["rewrite_skipped"] is True


def test_search_sort_order_param_is_forwarded_to_searcher(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """The route must pass `sort_order` through to `searcher.search()`.

    Its sibling `sort` has had this pin since the field shipped; the new
    axis had none, so deleting the one forwarding line left the whole
    suite green. A dropped direction is the worst shape of that bug: the
    caller asks for oldest-first, gets a 200, and is handed newest-first —
    the silent-parameter-drop class #308/#312 exist to end.
    """
    _seed_acct_and_grant(db_conn, api_user.id)
    fake_searcher = _fake_searcher_returning_one_hit()
    app = create_app(db_dsn=db_dsn, searcher=fake_searcher)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20,
              "sort": "date", "sort_order": "asc"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert fake_searcher.search.call_args.kwargs["sort_order"] == "asc"


def test_search_sort_order_defaults_to_desc_when_omitted(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """Omitting it must reproduce today's behaviour byte for byte — the GUI
    never sends the field."""
    _seed_acct_and_grant(db_conn, api_user.id)
    fake_searcher = _fake_searcher_returning_one_hit()
    app = create_app(db_dsn=db_dsn, searcher=fake_searcher)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20, "sort": "date"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert fake_searcher.search.call_args.kwargs["sort_order"] == "desc"


def test_search_rejects_ascending_rank_at_the_http_boundary(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """`sort="rank"` + `sort_order="asc"` is a 400 over the wire, not just
    inside `run_search`.

    The rank path serves a bounded candidate pool, so reversing it returns
    the least relevant *of the top hits* rather than of the archive — a
    result that looks meaningful and is an artifact of where the pool
    stopped. The refusal is the whole reason the axis is orthogonal rather
    than two new `sort` members, so it has to hold where clients meet it.
    """
    _seed_acct_and_grant(db_conn, api_user.id)
    fake = _fake_searcher_returning_one_hit()
    app = create_app(db_dsn=db_dsn, searcher=fake)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20,
              "sort": "rank", "sort_order": "asc"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "/problems/validation-failed"
    assert "sort_order" in body["detail"]
    fake.search.assert_not_called()


def test_search_rejects_ascending_with_no_sort_stated_at_all(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """The commonest way to hit the refusal: `sort_order="asc"` alone.

    An unstated `sort` resolves to "rank", so this is the same 400 — and
    it must not be reached by the alternative the design rejected, where
    `sort_order="asc"` silently *implies* `sort="date"` and the parameter
    stops being a direction.
    """
    _seed_acct_and_grant(db_conn, api_user.id)
    fake = _fake_searcher_returning_one_hit()
    app = create_app(db_dsn=db_dsn, searcher=fake)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20, "sort_order": "asc"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 400
    assert r.json()["type"] == "/problems/validation-failed"
    fake.search.assert_not_called()


def test_search_rejects_a_stated_order_the_cursor_cannot_serve(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """A descending statement against an ascending cursor is a 400 on the
    wire, the sibling of the `sort` contradiction above."""
    from datetime import datetime, timezone

    from localmail.api.search_cursor import encode_keyset_cursor
    from localmail.search.searcher import KeysetCursor

    _seed_acct_and_grant(db_conn, api_user.id)
    fake = _fake_searcher_returning_one_hit()
    cursor = encode_keyset_cursor(
        KeysetCursor(ts=datetime(2026, 5, 21, tzinfo=timezone.utc), id=100),
        "asc",
    )
    app = create_app(db_dsn=db_dsn, searcher=fake)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20,
              "sort_order": "desc", "cursor": cursor},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 400
    assert r.json()["type"] == "/problems/validation-failed"
    fake.search.assert_not_called()


def test_search_continues_an_ascending_cursor_over_the_wire(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """The documented paging call over HTTP: send the cursor back, state
    nothing, and keep walking ascending.

    This one survives deleting the route's `sort_order` forwarding, and
    deliberately so — with a cursor in hand the *cursor* is the authority
    on both axes, which is the whole point of `KA|`. It is here as the
    positive control for the refusals above: a rule that turned away every
    ascending request would satisfy all three of them and break the only
    call the docs tell a client to make.
    """
    from datetime import datetime, timezone

    from localmail.api.search_cursor import encode_keyset_cursor
    from localmail.search.searcher import KeysetCursor

    _seed_acct_and_grant(db_conn, api_user.id)
    fake = _fake_searcher_returning_one_hit()
    ks = KeysetCursor(ts=datetime(2026, 5, 21, tzinfo=timezone.utc), id=100)
    app = create_app(db_dsn=db_dsn, searcher=fake)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20,
              "cursor": encode_keyset_cursor(ks, "asc")},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    kwargs = fake.search.call_args.kwargs
    assert kwargs["sort"] == "date"
    assert kwargs["sort_order"] == "asc"
    assert kwargs["keyset_cursor"] == ks


def test_an_ascending_search_puts_a_directional_cursor_in_the_response_body(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """The outbound half of the direction contract, over HTTP.

    Every other route-level ``sort_order`` test asserts what reached
    ``searcher.search``; none asserted what came back. The shared fake sets
    ``next_keyset = None``, so ``_next_cursor``'s minting branch never ran
    at this layer at all — and that branch is where the direction is
    stamped into the cursor. A client pages on the response body, so a
    ``K|`` minted for an ascending walk is the silent reversal this feature
    exists to prevent, arriving through the one path nothing watched.
    """
    from datetime import datetime, timezone

    from localmail.api.search_cursor import keyset_order
    from localmail.search.searcher import KeysetCursor

    _seed_acct_and_grant(db_conn, api_user.id)
    fake = _fake_searcher_returning_one_hit()
    fake.search.return_value.next_keyset = KeysetCursor(
        ts=datetime(2026, 5, 21, tzinfo=timezone.utc), id=100,
    )
    app = create_app(db_dsn=db_dsn, searcher=fake)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20,
              "sort": "date", "sort_order": "asc"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    cursor = r.json()["next_cursor"]
    assert cursor.startswith("KA|"), cursor
    assert keyset_order(cursor) == "asc"


def test_a_descending_search_still_mints_the_legacy_prefix(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    """The negative control for the test above.

    Asserting only the ascending prefix passes against a mint hardcoded to
    ``"KA|"``, which would break every descending client — the far commoner
    path, and the one the GUI is on.
    """
    from datetime import datetime, timezone

    from localmail.api.search_cursor import keyset_order
    from localmail.search.searcher import KeysetCursor

    _seed_acct_and_grant(db_conn, api_user.id)
    fake = _fake_searcher_returning_one_hit()
    fake.search.return_value.next_keyset = KeysetCursor(
        ts=datetime(2026, 5, 21, tzinfo=timezone.utc), id=100,
    )
    app = create_app(db_dsn=db_dsn, searcher=fake)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20, "sort": "date"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    cursor = r.json()["next_cursor"]
    assert cursor.startswith("K|"), cursor
    assert keyset_order(cursor) == "desc"
