from fastapi.testclient import TestClient

from localmail.serve.app import create_app


def _client(db_dsn: str) -> TestClient:
    return TestClient(create_app(db_dsn=db_dsn, searcher=None))


def test_login_success_returns_token(db_dsn: str, api_user) -> None:
    c = _client(db_dsn)
    r = c.post("/v1/auth/login", json={"username": api_user.username, "password": api_user.password})
    assert r.status_code == 200
    body = r.json()
    assert "token" in body
    assert "expires_at" in body


def test_login_bad_password(db_dsn: str, api_user) -> None:
    c = _client(db_dsn)
    r = c.post("/v1/auth/login", json={"username": api_user.username, "password": "wrong"})
    assert r.status_code == 401


def test_whoami_returns_username(db_dsn: str, api_token: str, api_user) -> None:
    c = _client(db_dsn)
    r = c.get("/v1/auth/whoami", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    assert r.json()["username"] == api_user.username


def test_refresh_returns_new_token_and_invalidates_old(db_dsn: str, api_token: str) -> None:
    c = _client(db_dsn)
    r = c.post("/v1/auth/refresh", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    new_token = r.json()["token"]
    assert new_token != api_token
    r_old = c.get("/v1/auth/whoami", headers={"Authorization": f"Bearer {api_token}"})
    assert r_old.status_code == 401
    r_new = c.get("/v1/auth/whoami", headers={"Authorization": f"Bearer {new_token}"})
    assert r_new.status_code == 200


def test_logout_revokes_token(db_dsn: str, api_token: str) -> None:
    c = _client(db_dsn)
    r = c.post("/v1/auth/logout", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 204
    r2 = c.get("/v1/auth/whoami", headers={"Authorization": f"Bearer {api_token}"})
    assert r2.status_code == 401


def test_change_password_success_and_rotates_credentials(
    db_conn, db_dsn: str, api_token: str, api_user
) -> None:
    c = _client(db_dsn)
    r = c.post(
        "/v1/auth/change-password",
        json={"old_password": api_user.password, "new_password": "rotated"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 204
    from localmail.api.auth import reset_login_rate_limiter
    reset_login_rate_limiter(db_conn)
    db_conn.commit()
    r_old = c.post(
        "/v1/auth/login",
        json={"username": api_user.username, "password": api_user.password},
    )
    assert r_old.status_code == 401
    r_new = c.post(
        "/v1/auth/login",
        json={"username": api_user.username, "password": "rotated"},
    )
    assert r_new.status_code == 200


def test_change_password_wrong_old_returns_401(db_dsn: str, api_token: str) -> None:
    c = _client(db_dsn)
    r = c.post(
        "/v1/auth/change-password",
        json={"old_password": "not-the-real-one", "new_password": "rotated"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 401


def test_change_password_empty_new_returns_400(
    db_dsn: str, api_token: str, api_user
) -> None:
    c = _client(db_dsn)
    r = c.post(
        "/v1/auth/change-password",
        json={"old_password": api_user.password, "new_password": ""},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 400


def test_change_password_requires_auth(db_dsn: str, api_user) -> None:
    c = _client(db_dsn)
    r = c.post(
        "/v1/auth/change-password",
        json={"old_password": api_user.password, "new_password": "rotated"},
    )
    assert r.status_code == 401


def test_change_password_keeps_token_valid(
    db_dsn: str, api_token: str, api_user
) -> None:
    c = _client(db_dsn)
    r = c.post(
        "/v1/auth/change-password",
        json={"old_password": api_user.password, "new_password": "rotated"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 204
    r2 = c.get("/v1/auth/whoami", headers={"Authorization": f"Bearer {api_token}"})
    assert r2.status_code == 200
    assert r2.json()["username"] == api_user.username


def test_route_driven_login_failures_persist_audit_rows(db_dsn: str, api_user) -> None:
    """Failed logins through the HTTP route must persist audit rows
    despite the route's outer-rollback-on-error transaction semantics.

    Regression for the bug found in the final cross-task review of #7:
    _record_login_attempt previously deferred its commit to the route,
    but the route raises AuthenticationFailed (or rate-limited variants)
    and the outer rollback discarded the audit row — defeating the
    per-user and per-IP failure caps in production.
    """
    import psycopg

    c = _client(db_dsn)
    for _ in range(3):
        resp = c.post(
            "/v1/auth/login",
            json={"username": api_user.username, "password": "wrong"},
        )
        assert resp.status_code == 401

    # A FRESH connection (modeling a different worker) sees the rows.
    other = psycopg.connect(db_dsn)
    try:
        with other.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM api_login_attempts "
                "WHERE username = %s AND outcome = 'failure'",
                (api_user.username,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 3
    finally:
        other.close()


def test_login_429_carries_retry_after_and_cap(db_dsn: str, api_user, db_conn) -> None:
    """Global cap trips, 429 carries Retry-After header and cap field."""
    from localmail.api.auth import _record_login_attempt
    from localmail.config import AuthConfig

    for u in ("a", "b", "c", "d", "e"):
        _record_login_attempt(db_conn, u, "9.9.9.9", "failure")
    db_conn.commit()

    c = _client(db_dsn)
    original = c.app.state.auth_config
    c.app.state.auth_config = AuthConfig(login_global_max=5, login_global_window_s=60)
    try:
        resp = c.post(
            "/v1/auth/login",
            json={"username": api_user.username, "password": api_user.password},
        )
    finally:
        c.app.state.auth_config = original

    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "60"
    body = resp.json()
    assert body["status"] == 429
    assert body["cap"] == "global"
    assert body["retry_after_s"] == 60
