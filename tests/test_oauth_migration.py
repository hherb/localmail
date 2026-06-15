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
