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
