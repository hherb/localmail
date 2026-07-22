# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

import uuid

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


def _family_id(conn, raw):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT oauth_refresh_family_id FROM api_tokens WHERE token_sha256 = %s",
            (api_auth.hash_token(raw),),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


def test_mint_access_persists_family_id(db_conn):
    uid = _seed(db_conn)
    fam = uuid.uuid4()
    raw = access.mint_access(
        db_conn, user_id=uid, client_id="cid", ttl_s=3600, family_id=fam
    )
    db_conn.commit()
    assert _family_id(db_conn, raw) == fam


def test_mint_access_without_family_is_null(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(db_conn, user_id=uid, client_id="cid", ttl_s=3600)
    db_conn.commit()
    assert _family_id(db_conn, raw) is None


def test_revoke_access_family_deletes_only_matching(db_conn):
    uid = _seed(db_conn)
    fam = uuid.uuid4()
    other = uuid.uuid4()
    in_fam = access.mint_access(
        db_conn, user_id=uid, client_id="cid", ttl_s=3600, family_id=fam
    )
    other_fam = access.mint_access(
        db_conn, user_id=uid, client_id="cid", ttl_s=3600, family_id=other
    )
    no_fam = access.mint_access(db_conn, user_id=uid, client_id="cid", ttl_s=3600)
    db_conn.commit()
    deleted = access.revoke_access_family(db_conn, fam)
    db_conn.commit()
    assert deleted == 1
    assert access.load_access(db_conn, in_fam) is None
    assert access.load_access(db_conn, other_fam) is not None
    assert access.load_access(db_conn, no_fam) is not None


def test_revoke_access_family_absent_returns_zero(db_conn):
    assert access.revoke_access_family(db_conn, uuid.uuid4()) == 0


def test_mint_access_binds_resource(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(
        db_conn, user_id=uid, client_id="cid", ttl_s=3600,
        resource="https://h/mcp",
    )
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT oauth_resource FROM api_tokens WHERE token_sha256 = %s",
            (api_auth.hash_token(raw),),
        )
        row = cur.fetchone()
        assert row is not None and row[0] == "https://h/mcp"


def test_load_access_accepts_matching_resource(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(
        db_conn, user_id=uid, client_id="cid", ttl_s=3600,
        resource="https://h/mcp",
    )
    db_conn.commit()
    at = access.load_access(db_conn, raw, accepted_resources=["https://h/mcp"])
    assert at is not None and at.subject == str(uid)


def test_load_access_rejects_unlisted_resource(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(
        db_conn, user_id=uid, client_id="cid", ttl_s=3600,
        resource="https://other/mcp",
    )
    db_conn.commit()
    assert access.load_access(
        db_conn, raw, accepted_resources=["https://h/mcp"]
    ) is None


def test_load_access_null_resource_unrestricted(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(db_conn, user_id=uid, client_id="cid", ttl_s=3600)
    db_conn.commit()
    assert access.load_access(
        db_conn, raw, accepted_resources=["https://h/mcp"]
    ) is not None


def test_load_access_no_accepted_set_skips_enforcement(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(
        db_conn, user_id=uid, client_id="cid", ttl_s=3600,
        resource="https://other/mcp",
    )
    db_conn.commit()
    assert access.load_access(db_conn, raw) is not None  # accepted_resources=None


def test_load_access_canonicalizes_bound_resource_before_matching(db_conn):
    # Stored value is NON-canonical (trailing slash) but must match the
    # canonical accepted entry — this makes canonicalize_resource load-bearing.
    uid = _seed(db_conn)
    raw = access.mint_access(
        db_conn, user_id=uid, client_id="cid", ttl_s=3600,
        resource="https://h/mcp/",
    )
    db_conn.commit()
    at = access.load_access(db_conn, raw, accepted_resources=["https://h/mcp"])
    assert at is not None and at.subject == str(uid)


def test_load_access_rejects_non_canonicalizable_bound_resource(db_conn):
    # A corrupt/hand-inserted resource that canonicalize_resource() can't parse
    # must fail closed (rejected) when an accepted set is enforced.
    uid = _seed(db_conn)
    raw = access.mint_access(
        db_conn, user_id=uid, client_id="cid", ttl_s=3600,
        resource="not a url",
    )
    db_conn.commit()
    assert access.load_access(
        db_conn, raw, accepted_resources=["https://h/mcp"]
    ) is None
