# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""An API key must not be able to turn itself into a session token.

``verify_token`` accepts keys, and two of its callers went on to mint or
destroy an ordinary session credential from whatever it returned. The point-of-
use gate (Rule 1) cannot see that: it judges the credential in hand, and a
laundered token is a *different* credential of a different kind.
"""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.admin import api_keys as svc
from localmail.api.auth import (
    SessionCredentialRefused,
    hash_token,
    issue_token,
    logout,
    refresh_token,
    verify_token,
)
from localmail.serve.app import create_app


def _stray_session_token(conn: psycopg.Connection, user_id: int, raw: str) -> None:
    """A session token on a service principal, as a pre-fix archive would hold."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_tokens (token_sha256, user_id, expires_at) "
            "VALUES (%s, %s, now() + interval '30 days')",
            (hash_token(raw), user_id),
        )


def test_refresh_refuses_an_api_key(db_conn):
    """The wording is the point of the guard sitting here as well as at the
    mint: falling through to `issue_token` also refuses, but tells a bot about
    minting rather than about refreshing."""
    created = svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    with pytest.raises(SessionCredentialRefused, match="must not be refreshed"):
        refresh_token(db_conn, created.raw_key)
    db_conn.rollback()


def test_a_refused_refresh_leaves_the_key_working_and_revocable(db_conn):
    """The bot bricking itself is the other half of the defect: the old code
    deleted the presented row, and the key is unrecoverable."""
    created = svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    with pytest.raises(SessionCredentialRefused):
        refresh_token(db_conn, created.raw_key)
    db_conn.rollback()

    user = verify_token(db_conn, created.raw_key)
    assert user is not None and user.is_api_key is True
    assert svc.list_keys(db_conn)[0].has_key is True
    svc.revoke_key(db_conn, created.user_id)
    db_conn.commit()
    assert verify_token(db_conn, created.raw_key) is None


def test_issue_token_refuses_a_service_principal(db_conn):
    """The by-construction pin: the guard is at the mint, so a future caller
    cannot rediscover this hole."""
    created = svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    with pytest.raises(SessionCredentialRefused):
        issue_token(db_conn, created.user_id)
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM api_tokens WHERE user_id = %s AND api_key_name IS NULL",
            (created.user_id,),
        )
        row = cur.fetchone()
    assert row is not None and row[0] == 0


def test_logout_refuses_an_api_key(db_conn):
    """A generic client's shutdown-logout would otherwise destroy an
    unrecoverable credential."""
    created = svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    with pytest.raises(SessionCredentialRefused, match="cannot be logged out"):
        logout(db_conn, created.raw_key)
    db_conn.rollback()
    assert verify_token(db_conn, created.raw_key) is not None


def test_logout_still_revokes_a_session_token_and_ignores_a_bogus_one(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash) VALUES ('amy', 'x') "
            "RETURNING id"
        )
        row = cur.fetchone()
    assert row is not None
    tok, _ = issue_token(db_conn, int(row[0]))
    db_conn.commit()
    logout(db_conn, tok)
    db_conn.commit()
    assert verify_token(db_conn, tok) is None
    logout(db_conn, "not-a-token")


def test_revoke_key_sweeps_every_token_the_principal_holds(db_conn):
    """Terminal even on an archive that already carries a laundered token."""
    created = svc.create_key(db_conn, name="bot", account_ids=[])
    _stray_session_token(db_conn, created.user_id, "laundered")
    db_conn.commit()
    svc.revoke_key(db_conn, created.user_id)
    db_conn.commit()
    assert verify_token(db_conn, created.raw_key) is None
    assert verify_token(db_conn, "laundered") is None


def test_revoke_key_still_refuses_a_human_principal(db_conn):
    """Sweeping must not become a second way to cut off a person's sessions."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash) VALUES ('amy', 'x') "
            "RETURNING id"
        )
        row = cur.fetchone()
    assert row is not None
    uid = int(row[0])
    tok, _ = issue_token(db_conn, uid)
    db_conn.commit()
    with pytest.raises(svc.ApiKeyNotFound):
        svc.revoke_key(db_conn, uid)
    db_conn.rollback()
    assert verify_token(db_conn, tok) is not None


@pytest.fixture
def http_client(db_dsn):
    """A closed-down client. Built inline, this test leaked its pool and its
    committed rows into whatever file ran next -- observed twice as duplicate-key
    failures in files that truncate correctly."""
    with TestClient(create_app(db_dsn=db_dsn, searcher=None)) as client:
        yield client


def test_over_http_a_key_cannot_launder_itself(db_conn, http_client):
    """The reviewer's end-to-end reproduction, as a pin."""
    created = svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    client = http_client
    headers = {"Authorization": f"Bearer {created.raw_key}"}

    assert client.get("/v1/accounts", headers=headers).status_code == 200
    assert client.post("/v1/auth/refresh", headers=headers).status_code == 400
    assert client.post("/v1/auth/logout", headers=headers).status_code == 400
    assert client.get("/v1/accounts", headers=headers).status_code == 200
    assert svc.list_keys(db_conn)[0].has_key is True


def test_mint_access_refuses_a_service_principal(db_conn):
    """The second writer of a session-kind api_tokens row.

    issue_token's guard is claimed to close laundering *by construction*; that
    was true of its own two callers and not of this one, which reaches the same
    table with a bare user_id. Unreachable for a service principal today only
    because Rule 2 refuses the consent login it descends from -- a rule three
    modules away. Guarded here, the claim holds for both writers.
    """
    pytest.importorskip("mcp")
    from localmail.mcp.oauth import access

    created = svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    with pytest.raises(SessionCredentialRefused, match="API-key principal"):
        access.mint_access(
            db_conn, user_id=created.user_id, client_id="cid", ttl_s=3600
        )
    db_conn.rollback()
    assert verify_token(db_conn, created.raw_key) is not None


def test_revoke_access_refuses_an_api_key(db_conn):
    """The unhardened sibling of logout's refusal.

    verify_token accepts keys, so the OAuth revocation endpoint resolves one
    through load_access_token and would delete it -- destroying an
    unrecoverable credential on a machine client's routine shutdown. The SDK's
    client_id match happens to block it today; that is a coincidence of two
    constants, not a rule.
    """
    pytest.importorskip("mcp")
    from localmail.mcp.oauth import access

    created = svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    assert access.revoke_access(db_conn, created.raw_key) is False
    db_conn.commit()
    assert verify_token(db_conn, created.raw_key) is not None
    assert svc.list_keys(db_conn)[0].has_key is True
