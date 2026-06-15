from localmail.api import auth as api_auth
from localmail.mcp.oauth import access, clients


def _seed(conn):
    clients.register_client(
        conn, client_id="cid", client_secret_sha256=None,
        redirect_uris=["https://c/cb"], client_name=None,
        grant_types=["authorization_code"], response_types=["code"],
        token_endpoint_auth_method="none", scope=None,
    )
    uid = api_auth.create_user(conn, "access-user", "pw")
    conn.commit()
    return uid


def test_minted_access_token_verifies_via_existing_verifier(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(db_conn, user_id=uid, client_id="cid", ttl_s=3600)
    db_conn.commit()
    user = api_auth.verify_token(db_conn, raw)
    assert user is not None and user.id == uid


def test_minted_access_token_records_client_id(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(db_conn, user_id=uid, client_id="cid", ttl_s=3600)
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT oauth_client_id FROM api_tokens WHERE token_sha256 = %s",
            (api_auth.hash_token(raw),),
        )
        row = cur.fetchone()
        assert row is not None and row[0] == "cid"


def test_load_access_returns_subject(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(db_conn, user_id=uid, client_id="cid", ttl_s=3600)
    db_conn.commit()
    at = access.load_access(db_conn, raw)
    assert at is not None and at.subject == str(uid) and at.client_id == "cid"


def test_load_unknown_returns_none(db_conn):
    assert access.load_access(db_conn, "bogus") is None


def test_revoke_access(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(db_conn, user_id=uid, client_id="cid", ttl_s=3600)
    db_conn.commit()
    assert access.revoke_access(db_conn, raw) is True
    db_conn.commit()
    assert access.load_access(db_conn, raw) is None
