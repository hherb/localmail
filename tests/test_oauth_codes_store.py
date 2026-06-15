import pytest

from localmail.api import auth as api_auth
from localmail.mcp.oauth import clients, codes


def _seed_client_and_user(conn):
    clients.register_client(
        conn, client_id="cid", client_secret_sha256=None,
        redirect_uris=["https://c/cb"], client_name=None,
        grant_types=["authorization_code"], response_types=["code"],
        token_endpoint_auth_method="none", scope=None,
    )
    uid = api_auth.create_user(conn, "code-user", "pw")
    conn.commit()
    return uid


def test_mint_then_load(db_conn):
    uid = _seed_client_and_user(db_conn)
    raw = codes.mint_code(
        db_conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
        redirect_uri_provided_explicitly=True, code_challenge="chal",
        scopes=[], ttl_s=60,
    )
    db_conn.commit()
    loaded = codes.load_code(db_conn, raw)
    assert loaded is not None
    assert loaded.client_id == "cid"
    assert loaded.user_id == uid
    assert loaded.code_challenge == "chal"
    assert loaded.redirect_uri == "https://c/cb"
    assert loaded.redirect_uri_provided_explicitly is True


def test_consume_is_single_use(db_conn):
    uid = _seed_client_and_user(db_conn)
    raw = codes.mint_code(
        db_conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
        redirect_uri_provided_explicitly=True, code_challenge="chal",
        scopes=[], ttl_s=60,
    )
    db_conn.commit()
    assert codes.consume_code(db_conn, raw) is True
    db_conn.commit()
    assert codes.load_code(db_conn, raw) is None
    assert codes.consume_code(db_conn, raw) is False


def test_expired_code_does_not_load(db_conn):
    uid = _seed_client_and_user(db_conn)
    raw = codes.mint_code(
        db_conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
        redirect_uri_provided_explicitly=True, code_challenge="chal",
        scopes=[], ttl_s=-1,
    )
    db_conn.commit()
    assert codes.load_code(db_conn, raw) is None
