# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""HTMX admin screens for API keys."""
from __future__ import annotations

import re

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.admin.csrf import make_csrf_token
from localmail.api.auth import hash_password, hash_token
from localmail.config import ServeConfig
from localmail.serve.admin.csrf import csrf_action
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
def admin_id(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('root', %s, TRUE) RETURNING id",
            (hash_password("pw"),),
        )
        row = cur.fetchone()
    assert row is not None
    db_conn.commit()
    return int(row[0])


@pytest.fixture
def client(app, admin_id):
    """Cookie-authenticated admin, mirroring tests/test_serve_admin_bearer_auth.py:
    the login form's own CSRF token must be scraped and posted back."""
    c = TestClient(app, follow_redirects=False)
    form = c.get("/admin/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', form)
    assert m
    resp = c.post(
        "/admin/login",
        data={"username": "root", "password": "pw", "csrf_token": m.group(1)},
    )
    assert resp.status_code == 303, resp.text
    return c


def _csrf(admin_id: int, method: str, path: str) -> str:
    return make_csrf_token(
        user_id=admin_id, action=csrf_action(method, path),
        key=_SIGNING_KEY.encode("ascii"),
    )


def _account(conn: psycopg.Connection, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "imap_host, imap_port, config) "
            "VALUES (%s, %s, 'password', 'imap.example', 993, '{}'::jsonb) RETURNING id",
            (name, f"{name}@b.test"),
        )
        row = cur.fetchone()
    assert row is not None
    conn.commit()
    return int(row[0])


def test_list_screen_renders(client):
    resp = client.get("/admin/api-keys")
    assert resp.status_code == 200
    assert "API keys" in resp.text


def test_nav_links_to_the_panel(client):
    assert 'href="/admin/api-keys"' in client.get("/admin/").text


def test_create_shows_the_key_on_this_response_and_no_later_one(client, db_conn, admin_id):
    """The create response carries the key twice on purpose — the full value in
    the copy field, and a truncated echo in the example header line — so this
    pins that it appears here at all and never again."""
    aid = _account(db_conn, "work")
    resp = client.post(
        "/admin/api-keys",
        data={"name": "my_mail_bot", "account_ids": [str(aid)]},
        headers={"X-CSRF-Token": _csrf(admin_id, "POST", "/admin/api-keys")},
    )
    assert resp.status_code == 200
    # The actual value, not `len(k) > 12`: a proxy for "this is the whole
    # secret" passes just as happily against a longer truncation.
    with db_conn.cursor() as cur:
        cur.execute("SELECT token_sha256 FROM api_tokens WHERE api_key_name = %s",
                    ("my_mail_bot",))
        row = cur.fetchone()
    assert row is not None
    keys = re.findall(r"lmk_[A-Za-z0-9_\-]+", resp.text)
    full = [k for k in keys if hash_token(k) == row[0]]
    assert len(full) == 1
    listed = client.get("/admin/api-keys").text
    assert full[0] not in listed
    assert "lmk_" not in listed


def test_create_updates_the_table_out_of_band(client, admin_id):
    resp = client.post(
        "/admin/api-keys",
        data={"name": "fresh_bot"},
        headers={"X-CSRF-Token": _csrf(admin_id, "POST", "/admin/api-keys")},
    )
    assert resp.status_code == 200
    assert re.search(r"lmk_[A-Za-z0-9_\-]+", resp.text)
    assert 'id="api-key-table" hx-swap-oob="true"' in resp.text
    row = re.search(r'<tr id="api-key-row-(\d+)">.*?</tr>', resp.text, re.DOTALL)
    assert row and "fresh_bot" in row.group(0)
    assert "No API keys." not in resp.text


def test_create_rejects_a_blank_name_inline(client, admin_id):
    resp = client.post(
        "/admin/api-keys",
        data={"name": "  "},
        headers={"X-CSRF-Token": _csrf(admin_id, "POST", "/admin/api-keys")},
    )
    assert resp.status_code == 400
    assert "blank" in resp.text


def test_create_without_csrf_is_400(client):
    resp = client.post("/admin/api-keys", data={"name": "bot"})
    assert resp.status_code == 400


def test_revoke_from_the_panel(client, admin_id):
    client.post(
        "/admin/api-keys", data={"name": "bot"},
        headers={"X-CSRF-Token": _csrf(admin_id, "POST", "/admin/api-keys")},
    )
    listed = client.get("/admin/api-keys").text
    assert "bot" in listed
    uid_match = re.search(r'id="api-key-row-(\d+)"', listed)
    assert uid_match
    uid = uid_match.group(1)
    resp = client.post(
        f"/admin/api-keys/{uid}/revoke",
        headers={
            "X-CSRF-Token": _csrf(admin_id, "POST", f"/admin/api-keys/{uid}/revoke")
        },
    )
    assert resp.status_code == 200
    assert "no key" in client.get("/admin/api-keys").text


def test_delete_from_the_panel(client, admin_id):
    client.post(
        "/admin/api-keys", data={"name": "gone_bot"},
        headers={"X-CSRF-Token": _csrf(admin_id, "POST", "/admin/api-keys")},
    )
    listed = client.get("/admin/api-keys").text
    assert "gone_bot" in listed
    uid_match = re.search(r'id="api-key-row-(\d+)"', listed)
    assert uid_match
    uid = uid_match.group(1)
    resp = client.post(
        f"/admin/api-keys/{uid}/delete",
        headers={
            "X-CSRF-Token": _csrf(admin_id, "POST", f"/admin/api-keys/{uid}/delete")
        },
    )
    assert resp.status_code == 200
    assert "gone_bot" not in client.get("/admin/api-keys").text


def test_a_name_collision_renders_beside_the_name_field(client, db_conn, admin_id):
    """The two likeliest operator errors -- reusing a person's username, and
    re-minting over a live key -- are about the Name field. They were filed as
    form-level errors because the router recovered the field by grepping the
    message for "name", which neither wording contains."""
    from localmail.api.admin import users as users_svc

    users_svc.create_user(db_conn, username="amy", password="pw12345")
    db_conn.commit()
    resp = client.post(
        "/admin/api-keys",
        data={"name": "amy"},
        headers={"X-CSRF-Token": _csrf(admin_id, "POST", "/admin/api-keys")},
    )
    assert resp.status_code == 400
    assert 'id="api-key-name-error"' in resp.text
    slot = re.search(
        r'id="api-key-name-error"[^>]*>([^<]*)<', resp.text
    )
    assert slot and "existing user account" in slot.group(1)


def test_an_unknown_account_stays_a_form_level_error(client, admin_id):
    """An account id is about the request, not about a field of it, so it must
    not be swapped into the Name slot."""
    resp = client.post(
        "/admin/api-keys",
        data={"name": "bot", "account_ids": ["999999"]},
        headers={"X-CSRF-Token": _csrf(admin_id, "POST", "/admin/api-keys")},
    )
    assert resp.status_code == 400
    slot = re.search(r'id="api-key-name-error"[^>]*>([^<]*)<', resp.text)
    assert slot and slot.group(1).strip() == ""
    assert "unknown account" in resp.text


def test_a_successful_create_clears_a_previous_name_error(client, admin_id):
    """The slot swaps out of band on every create response, so a stale error
    from the previous attempt cannot survive beside a working form."""
    hdr = {"X-CSRF-Token": _csrf(admin_id, "POST", "/admin/api-keys")}
    client.post("/admin/api-keys", data={"name": "  "}, headers=hdr)
    resp = client.post("/admin/api-keys", data={"name": "bot"}, headers=hdr)
    assert resp.status_code == 200
    slot = re.search(r'id="api-key-name-error"[^>]*>([^<]*)<', resp.text)
    assert slot and slot.group(1).strip() == ""
