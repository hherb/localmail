"""HTTP-route tests for /v1/admin/daemon + restart-sync (2B.4).

Plane B lifecycle routes drive a dummy-subprocess supervisor swapped onto
app.state (never the real `localmail run`). Plane A routes (reload,
restart-sync) enqueue rows in `daemon_commands` and are asserted against the DB.
All routes are admin-gated with method-bound CSRF.
"""
from __future__ import annotations

import re
import sys
import time

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.admin.csrf import make_csrf_token
from localmail.api.auth import hash_password
from localmail.config import ServeConfig
from localmail.serve.admin.csrf import csrf_action
from localmail.serve.admin.daemon_router import build_daemon_view
from localmail.serve.app import create_app
from localmail.serve.daemon_supervisor import (
    DaemonSupervisor,
    ExternalDaemonSupervisor,
    SupervisorState,
)


_SIGNING_KEY = "x" * 43
_SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


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
    key = _SIGNING_KEY.encode("ascii")

    def csrf_for(action: str, method: str = "POST") -> str:
        return make_csrf_token(
            user_id=admin_user_id, action=csrf_action(method, action), key=key
        )

    client.csrf_for = csrf_for  # type: ignore[attr-defined]
    return client


@pytest.fixture
def account_id(db_conn: psycopg.Connection) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "imap_host, imap_port) VALUES "
            "('acct', 'a@example.com', 'password', 'imap.example.com', 993) "
            "RETURNING id"
        )
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    return int(row[0])


# --- auth / CSRF gates ----------------------------------------------------

def test_get_daemon_requires_admin(app) -> None:
    client = TestClient(app, follow_redirects=False)
    r = client.get("/v1/admin/daemon")
    assert r.status_code == 303  # redirect to login


def test_start_requires_csrf(admin_client) -> None:
    r = admin_client.post("/v1/admin/daemon/start")  # no X-CSRF-Token
    assert r.status_code == 400


def test_csrf_is_method_bound(admin_client) -> None:
    # A token minted for GET can't drive the POST.
    bad = admin_client.csrf_for("/v1/admin/daemon/start", method="GET")
    r = admin_client.post(
        "/v1/admin/daemon/start", headers={"X-CSRF-Token": bad}
    )
    assert r.status_code == 400


# --- GET status -----------------------------------------------------------

def test_get_status_shape(admin_client, app) -> None:
    app.state.daemon_supervisor = ExternalDaemonSupervisor()
    r = admin_client.get("/v1/admin/daemon")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == SupervisorState.EXTERNAL
    assert body["supervise_daemon_externally"] is True
    assert body["heartbeats"] == []
    assert body["recent_log"] == []
    assert "pid" in body and "started_at" in body


# --- Plane B lifecycle ----------------------------------------------------

def _poll_state(client, target: str, timeout: float = 6.0) -> str:
    deadline = time.monotonic() + timeout
    st = None
    while time.monotonic() < deadline:
        st = client.get("/v1/admin/daemon").json()["state"]
        if st == target:
            return st
        time.sleep(0.05)
    raise AssertionError(f"never reached {target}; last {st}")


def test_start_returns_202_and_settles_running(admin_client, app) -> None:
    sup = DaemonSupervisor(argv=_SLEEPER, grace_seconds=2.0)
    app.state.daemon_supervisor = sup
    try:
        r = admin_client.post(
            "/v1/admin/daemon/start",
            headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/daemon/start")},
        )
        assert r.status_code == 202, r.text
        assert r.json()["state"] in (
            SupervisorState.STARTING, SupervisorState.RUNNING
        )
        assert _poll_state(admin_client, SupervisorState.RUNNING)
        r = admin_client.post(
            "/v1/admin/daemon/stop",
            headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/daemon/stop")},
        )
        assert r.status_code == 202
        assert _poll_state(admin_client, SupervisorState.STOPPED)
    finally:
        sup.stop()


_DEAF_SLEEPER = [
    sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print('up', flush=True); time.sleep(60)",
]


def _poll_log_contains(sup: DaemonSupervisor, fragment: str, timeout: float = 6.0) -> bool:
    """Spin until the supervisor's ring buffer contains `fragment`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(fragment in line for line in sup.recent_log_lines()):
            return True
        time.sleep(0.02)
    return False


def test_second_lifecycle_op_while_busy_is_409(admin_client, app) -> None:
    sup = DaemonSupervisor(argv=_DEAF_SLEEPER, grace_seconds=1.0)
    app.state.daemon_supervisor = sup
    try:
        admin_client.post(
            "/v1/admin/daemon/start",
            headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/daemon/start")},
        )
        # Wait for the child to print "up" so its SIGTERM handler is installed.
        assert _poll_log_contains(sup, "up"), "deaf sleeper never printed 'up'"
        # First stop — will block on the grace-period wait because the child
        # ignores SIGTERM. Poll for STOPPING to confirm the lifecycle thread is
        # in the grace wait before firing the second request.
        admin_client.post(
            "/v1/admin/daemon/stop",
            headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/daemon/stop")},
        )
        _poll_state(admin_client, SupervisorState.STOPPING)
        r = admin_client.post(
            "/v1/admin/daemon/stop",
            headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/daemon/stop")},
        )
        assert r.status_code == 409
        assert _poll_state(admin_client, SupervisorState.STOPPED)
    finally:
        sup.stop()


def test_build_daemon_view_matches_get_route_shape(app, db_conn) -> None:
    daemon_cfg = app.state.daemon_config
    with app.state.pool.connection() as conn:
        view = build_daemon_view(
            ExternalDaemonSupervisor(), conn,
            stale_seconds=daemon_cfg.heartbeat_stale_seconds,
        )
    assert view["state"] == SupervisorState.EXTERNAL
    assert view["supervise_daemon_externally"] is True
    assert view["heartbeats"] == []
    assert view["recent_log"] == []
    assert "pid" in view
    assert "started_at" in view


def test_start_on_external_is_409(admin_client, app) -> None:
    app.state.daemon_supervisor = ExternalDaemonSupervisor()
    r = admin_client.post(
        "/v1/admin/daemon/start",
        headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/daemon/start")},
    )
    assert r.status_code == 409
    assert "external" in r.json()["detail"].lower()


# --- Plane A: reload / restart-sync enqueue ------------------------------

def _queued(conn: psycopg.Connection) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT command, account_id FROM daemon_commands "
            "WHERE state = 'queued' ORDER BY id"
        )
        return cur.fetchall()


def test_reload_enqueues_reload_now(admin_client, app, db_conn) -> None:
    r = admin_client.post(
        "/v1/admin/daemon/reload",
        headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/daemon/reload")},
    )
    assert r.status_code == 200, r.text
    assert "command_id" in r.json()
    assert _queued(db_conn) == [("reload-now", None)]


def test_restart_sync_enqueues_restart_account(
    admin_client, app, db_conn, account_id
) -> None:
    action = f"/v1/admin/accounts/{account_id}/restart-sync"
    r = admin_client.post(
        action, headers={"X-CSRF-Token": admin_client.csrf_for(action)}
    )
    assert r.status_code == 200, r.text
    assert _queued(db_conn) == [("restart-account", account_id)]


def test_restart_sync_unknown_account_404(admin_client, app, db_conn) -> None:
    action = "/v1/admin/accounts/999999/restart-sync"
    r = admin_client.post(
        action, headers={"X-CSRF-Token": admin_client.csrf_for(action)}
    )
    assert r.status_code == 404
    assert _queued(db_conn) == []
