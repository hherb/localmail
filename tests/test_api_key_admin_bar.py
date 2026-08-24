# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""An API key is refused at every admin route, even when its principal is an
admin. A bot key must never be able to mint another bot key."""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.auth import hash_password, hash_token, issue_token
from localmail.config import ServeConfig
from localmail.serve.app import create_app

_SIGNING_KEY = "x" * 43


@pytest.fixture
def app(db_dsn):
    cfg = ServeConfig(
        session_signing_key=_SIGNING_KEY, state_signing_key="y" * 43,
        cookie_secure=False,
    )
    return create_app(db_dsn=db_dsn, serve_config=cfg)


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


def _admin_key(db_conn: psycopg.Connection) -> str:
    """A service user promoted to admin by direct SQL.

    users.set_admin refuses this through the UI (Task 3) — which is the point:
    the gate must hold for a state the UI will not produce today, but a
    migration, a repair script, or a relaxed toggle could.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_service, is_admin) "
            "VALUES ('bot', 'x', TRUE, TRUE) RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        uid = int(row[0])
        cur.execute(
            "INSERT INTO api_tokens (token_sha256, user_id, expires_at, api_key_name) "
            "VALUES (%s, %s, NULL, 'bot')",
            (hash_token("lmk_raw"), uid),
        )
    db_conn.commit()
    return "lmk_raw"


def test_an_admin_principals_api_key_is_still_refused(client, db_conn):
    key = _admin_key(db_conn)
    resp = client.get("/v1/admin/users", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 403


def test_a_real_admin_bearer_still_passes(client, db_conn):
    """Positive control: the guard must not close the native-client path."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('root', %s, TRUE) RETURNING id",
            (hash_password("pw"),),
        )
        row = cur.fetchone()
    assert row is not None
    tok, _ = issue_token(db_conn, int(row[0]))
    db_conn.commit()
    resp = client.get("/v1/admin/users", headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 200
