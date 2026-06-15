from localmail.api import auth as api_auth
from localmail.mcp.oauth import clients, refresh


def _seed(conn):
    clients.register_client(
        conn, client_id="cid", client_secret_sha256=None,
        redirect_uris=["https://c/cb"], client_name=None,
        grant_types=["refresh_token"], response_types=["code"],
        token_endpoint_auth_method="none", scope=None,
    )
    uid = api_auth.create_user(conn, "refresh-user", "pw")
    conn.commit()
    return uid


def test_mint_then_load(db_conn):
    uid = _seed(db_conn)
    raw = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    row = refresh.load_refresh(db_conn, raw)
    assert row is not None and row.user_id == uid and row.client_id == "cid"


def test_revoke(db_conn):
    uid = _seed(db_conn)
    raw = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    assert refresh.revoke_refresh(db_conn, raw) is True
    db_conn.commit()
    assert refresh.load_refresh(db_conn, raw) is None


def test_rotate_revokes_old_returns_new(db_conn):
    uid = _seed(db_conn)
    old = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    new = refresh.rotate_refresh(db_conn, old, ttl_s=100)
    db_conn.commit()
    assert new is not None and new != old
    assert refresh.load_refresh(db_conn, old) is None
    assert refresh.load_refresh(db_conn, new) is not None


def test_rotate_unknown_returns_none(db_conn):
    assert refresh.rotate_refresh(db_conn, "bogus", ttl_s=100) is None


def test_expired_refresh_does_not_load(db_conn):
    uid = _seed(db_conn)
    raw = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=-1)
    db_conn.commit()
    assert refresh.load_refresh(db_conn, raw) is None
