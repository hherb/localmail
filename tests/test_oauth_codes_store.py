# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

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


def _disable_user(conn, uid):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_users SET disabled_at = now() WHERE id = %s", (uid,)
        )
    conn.commit()


def _revoke_sessions(conn, uid):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_users SET sessions_invalidated_at = now() WHERE id = %s",
            (uid,),
        )
    conn.commit()


def _mint(conn, uid, *, ttl_s=60):
    raw = codes.mint_code(
        conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
        redirect_uri_provided_explicitly=True, code_challenge="chal",
        scopes=[], ttl_s=ttl_s,
    )
    conn.commit()
    return raw


def test_disabled_user_code_does_not_load(db_conn):
    """Mirrors `load_refresh`'s M1 containment: a disabled user's in-flight
    authorization code must read as absent, not exchange into fresh tokens."""
    uid = _seed_client_and_user(db_conn)
    raw = _mint(db_conn, uid)
    assert codes.load_code(db_conn, raw) is not None
    _disable_user(db_conn, uid)
    assert codes.load_code(db_conn, raw) is None


def test_session_revocation_kills_authorization_code(db_conn):
    """The third credential kind must honour the revocation cutoff too: an
    exchanged code mints an access + refresh pair stamped `created_at = now()`
    — past the cutoff, hence valid — so leaving the code exchangeable reopens
    the door revocation just closed."""
    uid = _seed_client_and_user(db_conn)
    raw = _mint(db_conn, uid)
    assert codes.load_code(db_conn, raw) is not None
    _revoke_sessions(db_conn, uid)
    assert codes.load_code(db_conn, raw) is None


def test_code_minted_after_revocation_still_loads(db_conn):
    """The cutoff is a moment, not a ban: re-consenting after revocation has to
    work or the operator has locked the user out permanently."""
    uid = _seed_client_and_user(db_conn)
    _revoke_sessions(db_conn, uid)
    raw = _mint(db_conn, uid)
    assert codes.load_code(db_conn, raw) is not None


def test_re_enabled_user_code_loads_again(db_conn):
    uid = _seed_client_and_user(db_conn)
    raw = _mint(db_conn, uid)
    _disable_user(db_conn, uid)
    assert codes.load_code(db_conn, raw) is None
    with db_conn.cursor() as cur:
        cur.execute("UPDATE api_users SET disabled_at = NULL WHERE id = %s", (uid,))
    db_conn.commit()
    assert codes.load_code(db_conn, raw) is not None


def test_mint_and_load_code_round_trips_resource(db_conn):
    uid = _seed_client_and_user(db_conn)
    raw = codes.mint_code(
        db_conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
        redirect_uri_provided_explicitly=True, code_challenge="chal",
        scopes=[], ttl_s=60, resource="https://h/mcp",
    )
    db_conn.commit()
    row = codes.load_code(db_conn, raw)
    assert row is not None and row.resource == "https://h/mcp"


def test_mint_code_defaults_resource_none(db_conn):
    uid = _seed_client_and_user(db_conn)
    raw = codes.mint_code(
        db_conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
        redirect_uri_provided_explicitly=True, code_challenge="chal",
        scopes=[], ttl_s=60,
    )
    db_conn.commit()
    row = codes.load_code(db_conn, raw)
    assert row is not None and row.resource is None
