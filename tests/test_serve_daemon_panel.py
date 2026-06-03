"""Admin daemon-control panel (2B.5): GET /admin/daemon page + the
/admin/_partials/daemon-status HTMX partial. Auth-gated; renders normal /
stale / external states; mutating controls carry method-bound CSRF tokens.
"""
from __future__ import annotations

import re

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.admin.csrf import CSRFError, verify_csrf_token
from localmail.api.auth import hash_password
from localmail.config import ServeConfig
from localmail.serve.admin.csrf import csrf_action
from localmail.serve.app import create_app
from localmail.serve.daemon_supervisor import ExternalDaemonSupervisor

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
    return client


def _seed_heartbeat(conn, *, stale: bool, error: str | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "imap_host, imap_port) VALUES "
            "('acct', 'a@example.com', 'password', 'imap.example.com', 993) "
            "RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        aid = row[0]
        beat = "now() - interval '1 hour'" if stale else "now()"
        cur.execute(
            f"INSERT INTO daemon_heartbeats (worker_kind, account_id, state, "
            f"current_folder, last_error_msg, started_at, last_heartbeat_at) "
            f"VALUES ('idle', %s, 'idle', 'INBOX', %s, now(), {beat})",
            (aid, error),
        )
    conn.commit()
    return aid


def test_panel_redirects_unauthenticated(app) -> None:
    client = TestClient(app, follow_redirects=False)
    r = client.get("/admin/daemon")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/login")


def test_panel_renders_authenticated(admin_client) -> None:
    r = admin_client.get("/admin/daemon")
    assert r.status_code == 200
    assert "Daemon control" in r.text
    assert 'id="daemon-status"' in r.text


def test_partial_shows_external_note_and_disables_buttons(admin_client, app) -> None:
    app.state.daemon_supervisor = ExternalDaemonSupervisor()
    r = admin_client.get("/admin/_partials/daemon-status")
    assert r.status_code == 200
    assert "supervised externally" in r.text.lower()
    assert re.search(r"<button[^>]*disabled[^>]*>\s*Stop\s*</button>", r.text)


def test_partial_marks_stale_heartbeat(admin_client, db_conn) -> None:
    _seed_heartbeat(db_conn, stale=True)
    r = admin_client.get("/admin/_partials/daemon-status")
    assert r.status_code == 200
    assert "daemon-stale" in r.text


def test_partial_shows_last_error(admin_client, db_conn) -> None:
    _seed_heartbeat(db_conn, stale=False, error="boom: connection reset")
    r = admin_client.get("/admin/_partials/daemon-status")
    assert "boom: connection reset" in r.text


def test_restart_sync_button_carries_method_bound_csrf(admin_client, db_conn, admin_user_id) -> None:
    aid = _seed_heartbeat(db_conn, stale=False)
    r = admin_client.get("/admin/_partials/daemon-status")
    action = f"/v1/admin/accounts/{aid}/restart-sync"
    m = re.search(
        r'hx-post="' + re.escape(action) + r'"[^>]*hx-headers=\'[^\']*'
        r'"X-CSRF-Token":\s*"([^"]+)"',
        r.text,
    )
    assert m, r.text
    token = m.group(1)
    key = _SIGNING_KEY.encode("ascii")
    verify_csrf_token(
        token, user_id=admin_user_id,
        action=csrf_action("POST", action), key=key,
    )
    with pytest.raises(CSRFError):
        verify_csrf_token(
            token, user_id=admin_user_id,
            action=csrf_action("GET", action), key=key,
        )


def test_partial_escapes_error_message(admin_client, db_conn) -> None:
    _seed_heartbeat(db_conn, stale=False, error="<script>alert(1)</script>")
    r = admin_client.get("/admin/_partials/daemon-status")
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_panel_has_toast_region_and_script(admin_client) -> None:
    """#148: the full page carries the toast region + the error-feedback
    script so a 409/400 from a lifecycle button is surfaced immediately."""
    r = admin_client.get("/admin/daemon")
    assert r.status_code == 200
    assert 'id="daemon-toast"' in r.text
    assert 'aria-live="polite"' in r.text
    assert "/admin/static/daemon-panel.js" in r.text


def test_toast_region_lives_outside_polling_partial(admin_client) -> None:
    """The toast must NOT be inside the self-swapping #daemon-status fragment,
    or the 2s `outerHTML` poll would wipe an in-flight error message. The
    partial (what the poll replaces) must not contain it."""
    r = admin_client.get("/admin/_partials/daemon-status")
    assert r.status_code == 200
    assert "daemon-toast" not in r.text


def test_daemon_panel_js_handles_error_statuses(app) -> None:
    """The served script binds an htmx:afterRequest listener and maps the
    busy-guard (409) / CSRF (400) statuses to operator messages. Served from
    'self' so it satisfies the /admin `script-src 'self'` CSP."""
    client = TestClient(app, follow_redirects=False)
    r = client.get("/admin/static/daemon-panel.js")
    assert r.status_code == 200
    body = r.text
    assert "htmx:afterRequest" in body
    assert "daemon-toast" in body
    assert "409" in body
    assert "400" in body


def test_stop_button_carries_method_bound_csrf(admin_client, admin_user_id) -> None:
    r = admin_client.get("/admin/_partials/daemon-status")
    m = re.search(
        r'hx-post="/v1/admin/daemon/stop"[^>]*hx-headers=\'[^\']*'
        r'"X-CSRF-Token":\s*"([^"]+)"',
        r.text,
    )
    assert m, r.text
    token = m.group(1)
    key = _SIGNING_KEY.encode("ascii")
    verify_csrf_token(
        token, user_id=admin_user_id,
        action=csrf_action("POST", "/v1/admin/daemon/stop"), key=key,
    )
    with pytest.raises(CSRFError):
        verify_csrf_token(
            token, user_id=admin_user_id,
            action=csrf_action("GET", "/v1/admin/daemon/stop"), key=key,
        )
