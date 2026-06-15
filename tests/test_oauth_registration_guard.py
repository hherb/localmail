import pytest
from psycopg_pool import ConnectionPool

from localmail.mcp.oauth import clients, registration


def test_count_and_over_limit(db_conn):
    registration.reset(db_conn)
    db_conn.commit()
    for _ in range(3):
        registration.record(db_conn, "1.2.3.4")
    db_conn.commit()
    assert registration.count_recent(db_conn, "1.2.3.4", window_s=3600) == 3
    assert registration.over_limit(db_conn, "1.2.3.4", window_s=3600, max_n=3) is True
    assert registration.over_limit(db_conn, "1.2.3.4", window_s=3600, max_n=4) is False
    assert registration.count_recent(db_conn, "9.9.9.9", window_s=3600) == 0


def test_sweep_deletes_old(db_conn):
    registration.reset(db_conn)
    registration.record(db_conn, "1.2.3.4")
    db_conn.commit()
    deleted = registration.sweep(db_conn, retention_s=0)
    db_conn.commit()
    assert deleted >= 1


def test_cleanup_unused_runs(db_conn):
    clients.register_client(
        db_conn, client_id="stale", client_secret_sha256=None,
        redirect_uris=["https://c/cb"], client_name=None,
        grant_types=["authorization_code"], response_types=["code"],
        token_endpoint_auth_method="none", scope=None,
    )
    db_conn.commit()
    assert clients.cleanup_unused(db_conn, retention_s=0) == 1
    db_conn.commit()
    assert clients.get_client(db_conn, "stale") is None


@pytest.fixture
def db_pool(db_dsn):
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        yield pool
    finally:
        pool.close()


def test_middleware_caps_registration(db_conn, db_pool):
    pytest.importorskip("mcp")
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from localmail.config import McpConfig
    from localmail.serve.oauth.registration_guard import RegistrationRateLimit
    from localmail.mcp.oauth import registration

    registration.reset(db_conn)
    db_conn.commit()

    async def stub_register(request):
        return JSONResponse({"client_id": "x"})

    app = Starlette(routes=[Route("/register", stub_register, methods=["POST"])])
    app.add_middleware(
        RegistrationRateLimit, pool=db_pool,
        config=McpConfig(oauth_registration_max=2, oauth_registration_window_s=3600),
        register_path_suffix="/register",
    )
    client = TestClient(app)
    assert client.post("/register", json={}).status_code == 200
    assert client.post("/register", json={}).status_code == 200
    assert client.post("/register", json={}).status_code == 429
