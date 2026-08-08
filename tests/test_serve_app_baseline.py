# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

import pytest
from fastapi.testclient import TestClient

import localmail
from localmail.serve.app import create_app


def _client(db_dsn: str) -> TestClient:
    app = create_app(db_dsn=db_dsn, searcher=None)
    return TestClient(app)


def test_health_unauth(db_dsn: str) -> None:
    r = _client(db_dsn).get("/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_version_unauth(db_dsn: str) -> None:
    r = _client(db_dsn).get("/v1/version")
    assert r.status_code == 200
    body = r.json()
    assert body["api_major"] == 1
    assert body["api_minor"] >= 0
    # The wire field, not just the module constant: this is the only assertion
    # that fails if the route stops reporting `localmail.__version__`.
    assert body["server_version"] == localmail.__version__


def test_authenticated_endpoint_rejects_no_token(db_dsn: str, api_user) -> None:
    r = _client(db_dsn).get("/v1/capabilities")
    assert r.status_code == 401
    body = r.json()
    assert body["type"].startswith("/problems/")


def test_authenticated_endpoint_accepts_valid_token(db_dsn: str, api_token: str) -> None:
    r = _client(db_dsn).get(
        "/v1/capabilities",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "search" in body


def test_response_has_request_id_header(db_dsn: str) -> None:
    r = _client(db_dsn).get("/v1/health")
    assert "X-Request-Id" in r.headers


def test_html_problem_responses_use_problem_json(db_dsn: str) -> None:
    r = _client(db_dsn).get("/v1/capabilities")
    assert r.headers["content-type"].startswith("application/problem+json")


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_health_returns_503_when_db_unreachable(db_dsn: str) -> None:
    """A health endpoint that always returns 200 is useless to a load balancer.
    Patch pool.connection to raise so we simulate "DB unreachable" without
    waiting on a bogus DSN. The filterwarnings is for psycopg_pool's __del__
    quirk: when the test-scoped pool is GC'd inside its own worker thread it
    surfaces a benign "cannot join current thread" — unrelated to the test."""
    from contextlib import contextmanager

    import psycopg

    app = create_app(db_dsn=db_dsn, searcher=None)

    @contextmanager
    def _broken_conn():
        raise psycopg.OperationalError("connection refused")
        yield  # pragma: no cover

    app.state.pool.connection = _broken_conn  # type: ignore[method-assign]

    r = TestClient(app).get("/v1/health")
    assert r.status_code == 503
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "/problems/feature-unavailable"
