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
