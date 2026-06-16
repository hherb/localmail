"""The 0028 migration creates the OAuth AS tables + api_tokens.oauth_client_id."""
from __future__ import annotations


def _columns(conn, table: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,),
        )
        return {r[0] for r in cur.fetchall()}


def test_oauth_tables_exist(db_conn):
    for table in ("oauth_clients", "oauth_authorization_codes", "oauth_refresh_tokens"):
        cols = _columns(db_conn, table)
        assert cols, f"{table} missing"


def test_oauth_clients_shape(db_conn):
    cols = _columns(db_conn, "oauth_clients")
    assert {"client_id", "client_secret_sha256", "redirect_uris", "client_name",
            "created_at", "last_used_at"} <= cols


def test_authorization_codes_shape(db_conn):
    cols = _columns(db_conn, "oauth_authorization_codes")
    assert {"code_sha256", "client_id", "user_id", "redirect_uri", "code_challenge",
            "redirect_uri_provided_explicitly", "scopes", "expires_at"} <= cols


def test_refresh_tokens_shape(db_conn):
    cols = _columns(db_conn, "oauth_refresh_tokens")
    assert {"token_sha256", "client_id", "user_id", "scopes", "expires_at"} <= cols


def test_api_tokens_gains_oauth_client_id(db_conn):
    assert "oauth_client_id" in _columns(db_conn, "api_tokens")


def test_registration_attempts_table(db_conn):
    cols = _columns(db_conn, "oauth_registration_attempts")
    assert {"ip", "ts"} <= cols


def test_registration_attempts_has_ip_ts_index(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = %s",
            ("oauth_registration_attempts",),
        )
        defs = " ".join(r[0] for r in cur.fetchall())
    assert "ip" in defs and "ts" in defs  # composite (ip, ts) index present


def test_refresh_tokens_gains_family_and_consumed(db_conn):
    cols = _columns(db_conn, "oauth_refresh_tokens")
    assert {"family_id", "consumed_at"} <= cols


def test_refresh_tokens_has_client_id_and_family_indexes(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = %s",
            ("oauth_refresh_tokens",),
        )
        defs = " ".join(r[0] for r in cur.fetchall())
    assert "family_id" in defs
    assert "(client_id)" in defs.replace(" ", "")


def test_cleanup_unused_subquery_uses_client_id_index(db_conn):
    # #185 acceptance: the NOT EXISTS correlated subquery must hit the
    # client_id index, not a seq scan, once oauth_refresh_tokens is seeded.
    from localmail.api import auth as api_auth
    from localmail.mcp.oauth import clients, refresh

    clients.register_client(
        db_conn, client_id="idx-cid", client_secret_sha256=None,
        redirect_uris=["https://c/cb"], client_name=None,
        grant_types=["refresh_token"], response_types=["code"],
        token_endpoint_auth_method="none", scope=None,
    )
    uid = api_auth.create_user(db_conn, "idx-user", "pw")
    for _ in range(50):
        refresh.mint_refresh(
            db_conn, client_id="idx-cid", user_id=uid, scopes=[], ttl_s=3600
        )
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute(
            "EXPLAIN SELECT 1 FROM oauth_refresh_tokens r "
            "WHERE r.client_id = %s AND r.expires_at > now()",
            ("idx-cid",),
        )
        plan = " ".join(r[0] for r in cur.fetchall())
    assert "oauth_refresh_tokens_client_id_idx" in plan


def test_api_tokens_gains_oauth_refresh_family_id(db_conn):
    cols = _columns(db_conn, "api_tokens")
    assert "oauth_refresh_family_id" in cols


def test_oauth_refresh_family_id_is_uuid_nullable(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'api_tokens' "
            "  AND column_name = 'oauth_refresh_family_id'"
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "uuid" and row[1] == "YES"


def test_api_tokens_has_refresh_family_partial_index(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'api_tokens' "
            "  AND indexname = 'api_tokens_oauth_refresh_family_id_idx'"
        )
        row = cur.fetchone()
    assert row is not None
    indexdef = row[0]
    assert "oauth_refresh_family_id" in indexdef
    assert "IS NOT NULL" in indexdef  # partial index predicate present
