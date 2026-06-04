"""Admin account-management HTML screens (2A.3)."""
from __future__ import annotations

import re

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.admin import accounts as svc
from localmail.api.auth import hash_password
from localmail.config import ServeConfig
from localmail.serve.app import create_app

_SIGNING_KEY = "x" * 43


@pytest.fixture
def serve_cfg() -> ServeConfig:
    return ServeConfig(
        session_signing_key=_SIGNING_KEY,
        state_signing_key="y" * 43,
        cookie_secure=False,
    )


@pytest.fixture
def app(db_dsn, serve_cfg):
    return create_app(db_dsn=db_dsn, serve_config=serve_cfg)


@pytest.fixture
def admin_user_id(db_conn: psycopg.Connection) -> int:
    pwh = hash_password("hunter2")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, %s, TRUE) RETURNING id",
            ("horst", pwh),
        )
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    return int(row[0])


@pytest.fixture
def admin_client(app, admin_user_id):
    client = TestClient(app, follow_redirects=False)
    form = client.get("/admin/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', form)
    assert m
    r = client.post(
        "/admin/login",
        data={"username": "horst", "password": "hunter2", "csrf_token": m.group(1)},
    )
    assert r.status_code == 303, r.text
    client.app_state_admin_id = admin_user_id
    return client


def _seed_account(db_conn, **over) -> int:
    kw = dict(
        name="fastmail", email_address="me@fastmail.com", auth_method="password",
        imap_host="imap.fastmail.com", imap_port=993, oauth_provider=None,
        folder_allow=None, folder_deny=None, folder_deny_flags=None,
    )
    kw.update(over)
    acct = svc.create_account(db_conn, **kw)
    db_conn.commit()
    return acct.id


def test_list_redirects_unauthenticated(app):
    client = TestClient(app, follow_redirects=False)
    r = client.get("/admin/accounts")
    assert r.status_code in (302, 303)
    assert "/admin/login" in r.headers["location"]


def test_list_renders_accounts(admin_client, db_conn):
    _seed_account(db_conn, name="fastmail")
    _seed_account(db_conn, name="work-gmail", email_address="me@company.com",
                  auth_method="oauth2", imap_host="imap.gmail.com",
                  oauth_provider="gmail")
    r = admin_client.get("/admin/accounts")
    assert r.status_code == 200
    assert "fastmail" in r.text
    assert "work-gmail" in r.text
    assert "New account" in r.text


def _csrf_header(client, path: str) -> dict:
    """Mint a method-bound POST token by scraping it from a rendered form/page.

    The new-account form embeds the create token in a hx-headers attribute on
    the <form>; we read it from there so the test exercises the real mint.
    """
    page = client.get("/admin/accounts/new").text
    m = re.search(r'data-create-csrf="([^"]+)"', page)
    assert m, "create CSRF token not found in new-account form"
    return {"X-CSRF-Token": m.group(1)}


def test_new_account_form_renders(admin_client):
    r = admin_client.get("/admin/accounts/new")
    assert r.status_code == 200
    assert 'name="auth_method"' in r.text
    assert "/admin/static/accounts-panel.js" in r.text


def test_create_account_happy_path(admin_client, db_conn):
    headers = _csrf_header(admin_client, "/admin/accounts")
    r = admin_client.post(
        "/admin/accounts",
        data={
            "name": "fastmail", "email_address": "me@fastmail.com",
            "auth_method": "password", "imap_host": "imap.fastmail.com",
            "imap_port": "993", "oauth_provider": "",
            "folder_allow": "INBOX", "folder_deny": "",
        },
        headers=headers,
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect", "").startswith("/admin/accounts/")
    with db_conn.cursor() as cur:
        cur.execute("SELECT name FROM accounts WHERE name = 'fastmail'")
        assert cur.fetchone() is not None


def test_create_account_validation_error_inline(admin_client):
    headers = _csrf_header(admin_client, "/admin/accounts")
    r = admin_client.post(
        "/admin/accounts",
        data={
            "name": "x", "email_address": "x@x.com", "auth_method": "password",
            "imap_host": "h", "imap_port": "70000", "oauth_provider": "",
            "folder_allow": "", "folder_deny": "",
        },
        headers=headers,
    )
    assert r.status_code == 400
    assert "imap_port" in r.text


def test_create_account_missing_csrf_rejected(admin_client):
    r = admin_client.post(
        "/admin/accounts",
        data={"name": "y", "email_address": "y@y.com", "auth_method": "archive"},
    )
    assert r.status_code == 400


def test_edit_form_prefills(admin_client, db_conn):
    aid = _seed_account(db_conn, name="fastmail", folder_allow=["INBOX"])
    r = admin_client.get(f"/admin/accounts/{aid}")
    assert r.status_code == 200
    assert "fastmail" in r.text
    assert "INBOX" in r.text


def test_edit_unknown_account_404(admin_client):
    r = admin_client.get("/admin/accounts/999999")
    assert r.status_code == 404


def test_edit_form_shows_oauth_success_flash(admin_client, db_conn):
    aid = _seed_account(db_conn, name="g", email_address="g@gmail.com",
                        auth_method="oauth2", imap_host="imap.gmail.com",
                        oauth_provider="gmail")
    r = admin_client.get(f"/admin/accounts/{aid}?oauth=success")
    assert r.status_code == 200
    assert "Gmail connected" in r.text


def test_store_password_for_password_account(admin_client, db_conn):
    aid = _seed_account(db_conn, name="fastmail")
    path = f"/admin/accounts/{aid}/password"
    page = admin_client.get(f"/admin/accounts/{aid}").text
    # the password sub-form embeds its own method-bound token
    m = re.search(r'data-password-csrf="([^"]+)"', page)
    assert m
    r = admin_client.post(
        path, data={"password": "s3cret"}, headers={"X-CSRF-Token": m.group(1)}
    )
    assert r.status_code == 200
    assert "stored" in r.text.lower()


def test_store_password_rejected_for_oauth_account(admin_client, db_conn):
    aid = _seed_account(db_conn, name="g", email_address="g@gmail.com",
                        auth_method="oauth2", imap_host="imap.gmail.com",
                        oauth_provider="gmail")
    # mint a token via csrf helper directly
    from localmail.api.admin.csrf import make_csrf_token
    from localmail.serve.admin.csrf import csrf_action
    tok = make_csrf_token(
        user_id=admin_client.app_state_admin_id,
        action=csrf_action("POST", f"/admin/accounts/{aid}/password"),
        key=_SIGNING_KEY.encode("ascii"),
    )
    r = admin_client.post(
        f"/admin/accounts/{aid}/password",
        data={"password": "x"}, headers={"X-CSRF-Token": tok},
    )
    assert r.status_code == 400


def test_store_password_blank_rejected(admin_client, db_conn):
    aid = _seed_account(db_conn, name="fastmail")
    page = admin_client.get(f"/admin/accounts/{aid}").text
    m = re.search(r'data-password-csrf="([^"]+)"', page)
    assert m
    r = admin_client.post(
        f"/admin/accounts/{aid}/password",
        data={"password": ""}, headers={"X-CSRF-Token": m.group(1)},
    )
    assert r.status_code == 400
    # nothing stored in the keyring
    from localmail import secrets as _secrets
    assert _secrets.get_password("fastmail") is None


def test_update_account_changes_field(admin_client, db_conn):
    aid = _seed_account(db_conn, name="fastmail")
    path = f"/admin/accounts/{aid}"
    page = admin_client.get(path).text
    m = re.search(r'data-create-csrf="([^"]+)"', page)
    assert m
    r = admin_client.post(
        path,
        data={
            "name": "fastmail", "email_address": "new@fastmail.com",
            "auth_method": "password", "imap_host": "imap.fastmail.com",
            "imap_port": "993", "oauth_provider": "",
            "folder_allow": "", "folder_deny": "",
        },
        headers={"X-CSRF-Token": m.group(1)},
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == path
    with db_conn.cursor() as cur:
        cur.execute("SELECT email_address FROM accounts WHERE id = %s", (aid,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "new@fastmail.com"


def test_test_connection_lists_folders(admin_client, db_conn, monkeypatch):
    aid = _seed_account(db_conn, name="fastmail")
    monkeypatch.setattr(
        svc, "probe_connection",
        lambda conn, account_id, gmail_client_secrets=None: [
            svc.FolderInfo(name="INBOX", flags=(r"\HasNoChildren",)),
            svc.FolderInfo(name="Spam", flags=(r"\Junk",)),
        ],
    )
    from localmail.api.admin.csrf import make_csrf_token
    from localmail.serve.admin.csrf import csrf_action
    tok = make_csrf_token(
        user_id=admin_client.app_state_admin_id,
        action=csrf_action("POST", f"/admin/accounts/{aid}/test-connection"),
        key=_SIGNING_KEY.encode("ascii"),
    )
    r = admin_client.post(
        f"/admin/accounts/{aid}/test-connection", headers={"X-CSRF-Token": tok}
    )
    assert r.status_code == 200
    assert "INBOX" in r.text and "Spam" in r.text


def test_test_connection_error_renders_inline(admin_client, db_conn, monkeypatch):
    aid = _seed_account(db_conn, name="fastmail")
    def boom(conn, account_id, gmail_client_secrets=None):
        raise svc.AccountFieldError("no password stored")
    monkeypatch.setattr(svc, "probe_connection", boom)
    from localmail.api.admin.csrf import make_csrf_token
    from localmail.serve.admin.csrf import csrf_action
    tok = make_csrf_token(
        user_id=admin_client.app_state_admin_id,
        action=csrf_action("POST", f"/admin/accounts/{aid}/test-connection"),
        key=_SIGNING_KEY.encode("ascii"),
    )
    r = admin_client.post(
        f"/admin/accounts/{aid}/test-connection", headers={"X-CSRF-Token": tok}
    )
    assert r.status_code == 200
    assert "no password stored" in r.text


def _post_with_token(admin_client, path):
    from localmail.api.admin.csrf import make_csrf_token
    from localmail.serve.admin.csrf import csrf_action
    tok = make_csrf_token(
        user_id=admin_client.app_state_admin_id,
        action=csrf_action("POST", path),
        key=_SIGNING_KEY.encode("ascii"),
    )
    return admin_client.post(path, headers={"X-CSRF-Token": tok})


def test_sync_toggle_disables_then_enables(admin_client, db_conn):
    aid = _seed_account(db_conn, name="fastmail")  # sync_enabled defaults TRUE
    r = _post_with_token(admin_client, f"/admin/accounts/{aid}/sync-toggle")
    assert r.status_code == 200
    assert f'id="account-row-{aid}"' in r.text
    assert "Enable" in r.text  # now paused → button offers Enable
    with db_conn.cursor() as cur:
        cur.execute("SELECT sync_enabled FROM accounts WHERE id = %s", (aid,))
        assert cur.fetchone()[0] is False
    r2 = _post_with_token(admin_client, f"/admin/accounts/{aid}/sync-toggle")
    assert "Disable" in r2.text


def test_sync_toggle_unknown_404(admin_client):
    r = _post_with_token(admin_client, "/admin/accounts/999999/sync-toggle")
    assert r.status_code == 404


def _seed_message_for(db_conn, account_id):
    """Insert one minimal messages row for the account (enough to make
    delete_account refuse without force). Mirrors tests/test_api_accounts.py."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_bytes, raw_sha256, "
            "                       size_bytes, headers, attachments) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)",
            (account_id, "<x@test>", b"raw", b"\x00" * 32, 3, "{}", "[]"),
        )
    db_conn.commit()


def test_delete_empty_account_removes_row(admin_client, db_conn):
    aid = _seed_account(db_conn, name="fastmail")
    r = _post_with_token(admin_client, f"/admin/accounts/{aid}/delete")
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == "/admin/accounts"
    with db_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM accounts WHERE id = %s", (aid,))
        assert cur.fetchone() is None


def test_delete_in_use_offers_force_confirm(admin_client, db_conn):
    aid = _seed_account(db_conn, name="fastmail")
    _seed_message_for(db_conn, aid)
    r = _post_with_token(admin_client, f"/admin/accounts/{aid}/delete")
    assert r.status_code == 409
    assert "force" in r.text.lower()
    with db_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM accounts WHERE id = %s", (aid,))
        assert cur.fetchone() is not None


def test_delete_force_removes_in_use_account(admin_client, db_conn):
    aid = _seed_account(db_conn, name="fastmail")
    _seed_message_for(db_conn, aid)
    from localmail.api.admin.csrf import make_csrf_token
    from localmail.serve.admin.csrf import csrf_action
    tok = make_csrf_token(
        user_id=admin_client.app_state_admin_id,
        action=csrf_action("POST", f"/admin/accounts/{aid}/delete"),
        key=_SIGNING_KEY.encode("ascii"),
    )
    r = admin_client.post(
        f"/admin/accounts/{aid}/delete?force=1", headers={"X-CSRF-Token": tok}
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == "/admin/accounts"
    with db_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM accounts WHERE id = %s", (aid,))
        assert cur.fetchone() is None


def test_oauth_start_redirects_to_google(admin_client, db_conn, monkeypatch):
    aid = _seed_account(db_conn, name="g", email_address="g@gmail.com",
                        auth_method="oauth2", imap_host="imap.gmail.com",
                        oauth_provider="gmail")
    from localmail.api.admin import oauth as oauth_svc
    monkeypatch.setattr(
        oauth_svc, "start_oauth",
        lambda conn, account_id, **kw: "https://accounts.google.com/o/oauth2/auth?x=1",
    )
    r = _post_with_token(admin_client, f"/admin/accounts/{aid}/oauth/start")
    assert r.status_code == 303
    assert r.headers["location"].startswith("https://accounts.google.com/")


def test_oauth_start_not_configured_is_503(admin_client, db_conn, monkeypatch):
    aid = _seed_account(db_conn, name="g", email_address="g@gmail.com",
                        auth_method="oauth2", imap_host="imap.gmail.com",
                        oauth_provider="gmail")
    from localmail.api.admin import oauth as oauth_svc
    def boom(conn, account_id, **kw):
        raise oauth_svc.OAuthNotConfigured("Gmail OAuth is not configured")
    monkeypatch.setattr(oauth_svc, "start_oauth", boom)
    r = _post_with_token(admin_client, f"/admin/accounts/{aid}/oauth/start")
    assert r.status_code == 503


def test_form_references_static_js_not_inline(admin_client, db_conn):
    aid = _seed_account(db_conn, name="fastmail")
    r = admin_client.get(f"/admin/accounts/{aid}")
    assert "/admin/static/accounts-panel.js" in r.text
    # No inline event handlers / inline <script> bodies (CSP script-src 'self')
    assert "onclick=" not in r.text
    assert "hx-on:" not in r.text


def test_accounts_panel_js_is_served(admin_client):
    r = admin_client.get("/admin/static/accounts-panel.js")
    assert r.status_code == 200
    assert "data-auth-select" in r.text
