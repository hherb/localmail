# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

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
    assert codes.consume_code(db_conn, raw).burned is True
    db_conn.commit()
    assert codes.load_code(db_conn, raw) is None
    assert codes.consume_code(db_conn, raw).burned is False


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


def test_consume_reports_a_live_user_as_valid(db_conn):
    uid = _seed_client_and_user(db_conn)
    raw = _mint(db_conn, uid)
    result = codes.consume_code(db_conn, raw)
    assert result.burned is True
    assert result.still_valid is True


def test_consume_of_an_absent_code_is_not_burned(db_conn):
    _seed_client_and_user(db_conn)
    result = codes.consume_code(db_conn, "never-minted")
    assert result.burned is False
    assert result.still_valid is False


def test_consume_burns_a_revoked_users_code_but_reports_it_invalid(db_conn):
    """#241: the revocation check on `load_code` is load-time only, so a
    revocation landing between the SDK's load and its exchange used to yield a
    full token pair. The burn itself has to decide validity, atomically.

    Burning regardless is deliberate — single-use (RFC 6749 §4.1.2) must not
    become conditional on the user's state, or a rejected exchange would leave
    a replayable code behind for the rest of its TTL (the #219 invariant).
    """
    uid = _seed_client_and_user(db_conn)
    raw = _mint(db_conn, uid)
    _revoke_sessions(db_conn, uid)
    result = codes.consume_code(db_conn, raw)
    assert result.burned is True
    assert result.still_valid is False
    db_conn.commit()
    assert codes.consume_code(db_conn, raw).burned is False


def test_consume_reports_a_disabled_user_as_invalid(db_conn):
    uid = _seed_client_and_user(db_conn)
    raw = _mint(db_conn, uid)
    _disable_user(db_conn, uid)
    result = codes.consume_code(db_conn, raw)
    assert result.burned is True
    assert result.still_valid is False


def test_consume_accepts_a_code_minted_after_the_revocation(db_conn):
    """The cutoff is a moment, not a ban — re-consenting after a revocation
    has to complete, or the operator has locked the user out permanently."""
    uid = _seed_client_and_user(db_conn)
    _revoke_sessions(db_conn, uid)
    raw = _mint(db_conn, uid)
    result = codes.consume_code(db_conn, raw)
    assert result.burned is True
    assert result.still_valid is True


def test_consume_burns_an_expired_code_but_reports_it_invalid(db_conn):
    """Expiry is decided by the burn too, for the same reason the user's state
    is (#241): the SDK checks `expires_at` during its own separate
    `load_authorization_code` call, so that verdict is already stale here.

    The residual window is far narrower than the revocation one — a code can
    only cross its own deadline, not be revoked by an operator mid-round-trip —
    so this is defence in depth rather than a live leak. But it costs one
    conjunct, and it is what makes the burn self-sufficient instead of trusting
    a caller to have checked; that is precisely the assumption #241 punished.
    """
    uid = _seed_client_and_user(db_conn)
    raw = _mint(db_conn, uid, ttl_s=-1)
    result = codes.consume_code(db_conn, raw)
    assert result.burned is True, "an expired code must still be burned"
    assert result.still_valid is False


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


def test_consume_of_an_orphaned_code_reports_it_invalid(db_conn):
    """A burned code whose user row is gone must report ``still_valid=False``.

    The natural reading — LEFT JOIN misses, so the predicate is NULL, so
    ``COALESCE(..., FALSE)`` fails closed — is wrong, and this test exists
    because the fix shipped that way first. Against an all-NULL row
    ``disabled_at IS NULL`` and ``sessions_invalidated_at IS NULL`` are both
    TRUE, so the predicate returns TRUE and no COALESCE ever fires. Only an
    explicit ``u.id IS NOT NULL`` closes it.

    The FK is dropped inside the test transaction (never committed, so the
    schema is intact for every other test) because ``ON DELETE CASCADE``
    otherwise makes an orphaned code unconstructible — which is exactly why the
    guard reads as unnecessary until the day someone relaxes that FK.
    """
    uid = _seed_client_and_user(db_conn)
    raw = _mint(db_conn, uid)
    with db_conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE oauth_authorization_codes "
            "DROP CONSTRAINT oauth_authorization_codes_user_id_fkey"
        )
        cur.execute("DELETE FROM api_users WHERE id = %s", (uid,))
    result = codes.consume_code(db_conn, raw)
    db_conn.rollback()
    assert result.burned is True
    assert result.still_valid is False
