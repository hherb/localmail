# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

import asyncio

import pytest
from psycopg_pool import ConnectionPool

from localmail.config import AuthConfig
from localmail.mcp.oauth import clients, registration
from localmail.serve.oauth.registration_guard import resolve_scope_client_ip


def _scope(peer, xff=None):
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return {"type": "http", "client": (peer, 1234), "headers": headers}


def test_resolve_scope_no_proxies_uses_socket_peer():
    ip = resolve_scope_client_ip(
        _scope("10.0.0.1", xff="203.0.113.9"), trusted_proxies=(), max_hops=3
    )
    assert ip == "10.0.0.1"


def test_resolve_scope_peels_xff_for_trusted_peer():
    cfg = AuthConfig(trusted_proxies=["10.0.0.0/8"])
    ip = resolve_scope_client_ip(
        _scope("10.0.0.1", xff="203.0.113.9"),
        trusted_proxies=cfg.trusted_proxies_parsed,
        max_hops=cfg.trusted_proxies_max_hops,
    )
    assert ip == "203.0.113.9"


def test_resolve_scope_untrusted_peer_ignores_xff():
    cfg = AuthConfig(trusted_proxies=["10.0.0.0/8"])
    ip = resolve_scope_client_ip(
        _scope("198.51.100.7", xff="203.0.113.9"),
        trusted_proxies=cfg.trusted_proxies_parsed,
        max_hops=cfg.trusted_proxies_max_hops,
    )
    assert ip == "198.51.100.7"


def test_resolve_scope_no_client_returns_none():
    assert resolve_scope_client_ip(
        {"type": "http", "headers": []}, trusted_proxies=(), max_hops=3
    ) is None


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


def test_middleware_buckets_per_peeled_xff_client(db_conn, db_pool):
    pytest.importorskip("mcp")
    from localmail.config import McpConfig
    from localmail.serve.oauth.registration_guard import RegistrationRateLimit

    registration.reset(db_conn)
    db_conn.commit()

    async def stub_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = RegistrationRateLimit(
        stub_app,
        pool=db_pool,
        config=McpConfig(oauth_registration_max=1, oauth_registration_window_s=3600),
        auth_config=AuthConfig(trusted_proxies=["10.0.0.0/8"]),
        register_path_suffix="/register",
    )

    def post_from(xff_client):
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/register",
            "client": ("10.0.0.1", 1234),
            "headers": [(b"x-forwarded-for", xff_client.encode())],
        }
        status = {}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg["status"]

        asyncio.run(mw(scope, receive, send))
        return status["code"]

    # Cap is per peeled client IP, not per proxy socket peer (10.0.0.1).
    assert post_from("203.0.113.1") == 200
    assert post_from("203.0.113.1") == 429  # same client over cap
    assert post_from("203.0.113.2") == 200  # distinct client, own bucket
