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
    res = refresh.rotate_refresh(db_conn, old, ttl_s=100)
    db_conn.commit()
    assert res.outcome == "rotated" and res.new_token and res.new_token != old
    assert refresh.load_refresh(db_conn, old) is None
    assert refresh.load_refresh(db_conn, res.new_token) is not None


def test_expired_refresh_does_not_load(db_conn):
    uid = _seed(db_conn)
    raw = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=-1)
    db_conn.commit()
    assert refresh.load_refresh(db_conn, raw) is None


def _disable_user(conn, uid):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_users SET disabled_at = now() WHERE id = %s", (uid,)
        )
    conn.commit()


def test_disabled_user_refresh_does_not_load(db_conn):
    uid = _seed(db_conn)
    raw = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    assert refresh.load_refresh(db_conn, raw) is not None
    _disable_user(db_conn, uid)
    assert refresh.load_refresh(db_conn, raw) is None


def test_rotate_rejected_for_disabled_user(db_conn):
    uid = _seed(db_conn)
    raw = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    _disable_user(db_conn, uid)
    assert refresh.rotate_refresh(db_conn, raw, ttl_s=100).outcome == "unknown"


def test_re_enabled_user_refresh_loads_again(db_conn):
    uid = _seed(db_conn)
    raw = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    _disable_user(db_conn, uid)
    assert refresh.load_refresh(db_conn, raw) is None
    with db_conn.cursor() as cur:
        cur.execute("UPDATE api_users SET disabled_at = NULL WHERE id = %s", (uid,))
    db_conn.commit()
    assert refresh.load_refresh(db_conn, raw) is not None


def _raw_lookup(conn, raw):
    from localmail.api.auth import hash_token
    with conn.cursor() as cur:
        cur.execute(
            "SELECT family_id, consumed_at FROM oauth_refresh_tokens "
            "WHERE token_sha256 = %s",
            (hash_token(raw),),
        )
        return cur.fetchone()


def test_rotate_tombstones_old_keeps_family(db_conn):
    uid = _seed(db_conn)
    old = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    res = refresh.rotate_refresh(db_conn, old, ttl_s=100)
    db_conn.commit()
    assert res.outcome == "rotated" and res.new_token
    old_row = _raw_lookup(db_conn, old)
    assert old_row is not None and old_row[1] is not None
    assert refresh.load_refresh(db_conn, old) is None
    new_live = refresh.load_refresh(db_conn, res.new_token)
    assert new_live is not None
    assert new_live.family_id == old_row[0]


def test_replay_consumed_token_revokes_family(db_conn):
    uid = _seed(db_conn)
    old = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    res = refresh.rotate_refresh(db_conn, old, ttl_s=100)
    db_conn.commit()
    new = res.new_token
    assert refresh.load_refresh(db_conn, new) is not None
    replay = refresh.rotate_refresh(db_conn, old, ttl_s=100)
    db_conn.commit()
    assert replay.outcome == "reuse" and replay.new_token is None
    assert refresh.load_refresh(db_conn, new) is None
    assert _raw_lookup(db_conn, new) is None


def test_replay_revokes_only_its_own_family(db_conn):
    uid = _seed(db_conn)
    a = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    b = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    a2 = refresh.rotate_refresh(db_conn, a, ttl_s=100).new_token
    db_conn.commit()
    refresh.rotate_refresh(db_conn, a, ttl_s=100)
    db_conn.commit()
    assert refresh.load_refresh(db_conn, a2) is None
    assert refresh.load_refresh(db_conn, b) is not None


def test_rotate_unknown_returns_unknown(db_conn):
    res = refresh.rotate_refresh(db_conn, "bogus", ttl_s=100)
    assert res.outcome == "unknown" and res.new_token is None


def test_expired_not_consumed_is_unknown_not_reuse(db_conn):
    uid = _seed(db_conn)
    raw = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=-1)
    db_conn.commit()
    res = refresh.rotate_refresh(db_conn, raw, ttl_s=100)
    assert res.outcome == "unknown"


def test_disabled_user_not_consumed_is_unknown_not_reuse(db_conn):
    uid = _seed(db_conn)
    raw = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    _disable_user(db_conn, uid)
    res = refresh.rotate_refresh(db_conn, raw, ttl_s=100)
    assert res.outcome == "unknown"


def test_family_id_stable_across_rotations(db_conn):
    uid = _seed(db_conn)
    t = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    fam0 = refresh.load_refresh(db_conn, t).family_id
    for _ in range(3):
        t = refresh.rotate_refresh(db_conn, t, ttl_s=100).new_token
        db_conn.commit()
        assert refresh.load_refresh(db_conn, t).family_id == fam0


def test_sweep_consumed_deletes_only_expired_tombstones(db_conn):
    uid = _seed(db_conn)
    live = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    keep = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    gone = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=-1)
    db_conn.commit()
    with db_conn.cursor() as cur:
        from localmail.api.auth import hash_token
        cur.execute("UPDATE oauth_refresh_tokens SET consumed_at = now() "
                    "WHERE token_sha256 = ANY(%s)",
                    ([hash_token(keep), hash_token(gone)],))
    db_conn.commit()
    deleted = refresh.sweep_consumed(db_conn)
    db_conn.commit()
    assert deleted == 1
    assert _raw_lookup(db_conn, gone) is None
    assert _raw_lookup(db_conn, keep) is not None
    assert _raw_lookup(db_conn, live) is not None
