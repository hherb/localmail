from localmail.mcp.oauth import clients


def _register(conn, **over):
    kwargs = dict(
        client_id="cid-abc",
        client_secret_sha256=None,
        redirect_uris=["https://c.example/cb"],
        client_name="Test Client",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope=None,
    )
    kwargs.update(over)
    clients.register_client(conn, **kwargs)
    conn.commit()
    return kwargs["client_id"]


def test_register_then_get(db_conn):
    cid = _register(db_conn)
    row = clients.get_client(db_conn, cid)
    assert row is not None
    assert row.client_id == cid
    assert row.redirect_uris == ["https://c.example/cb"]
    assert row.client_secret_sha256 is None


def test_get_unknown_returns_none(db_conn):
    assert clients.get_client(db_conn, "nope") is None


def test_touch_last_used(db_conn):
    cid = _register(db_conn)
    assert clients.get_client(db_conn, cid).last_used_at is None
    clients.touch_last_used(db_conn, cid)
    db_conn.commit()
    assert clients.get_client(db_conn, cid).last_used_at is not None


def test_cleanup_unused_deletes_only_stale_unused(db_conn):
    used = _register(db_conn, client_id="used")
    clients.touch_last_used(db_conn, used)
    _register(db_conn, client_id="fresh-unused")
    db_conn.commit()
    # retention 0 → every unused client is stale; used one is kept.
    deleted = clients.cleanup_unused(db_conn, retention_s=0)
    db_conn.commit()
    assert deleted == 1
    assert clients.get_client(db_conn, "fresh-unused") is None
    assert clients.get_client(db_conn, "used") is not None
