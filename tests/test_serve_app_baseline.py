from fastapi.testclient import TestClient

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
    assert isinstance(body["server_version"], str)


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
