"""JSON-route tests for /v1/admin/imports (2A.5)."""
from __future__ import annotations

import mailbox as _mailbox
import re
import time

import pytest
from fastapi.testclient import TestClient

from localmail.api.admin.csrf import make_csrf_token
from localmail.api.auth import hash_password
from localmail.config import ImportsConfig, ServeConfig
from localmail.serve.admin.csrf import csrf_action
from localmail.serve.app import create_app

_KEY = "x" * 43


@pytest.fixture
def serve_cfg() -> ServeConfig:
    return ServeConfig(
        session_signing_key=_KEY, state_signing_key="y" * 43, cookie_secure=False)


@pytest.fixture
def app(db_dsn, serve_cfg, tmp_path):
    root = tmp_path / "imports"
    root.mkdir()
    return create_app(
        db_dsn=db_dsn, serve_config=serve_cfg,
        imports_config=ImportsConfig(roots=[root]),
        attachments_root=tmp_path / "blobs")


@pytest.fixture
def admin_id(db_conn) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('horst', %s, TRUE) RETURNING id", (hash_password("pw"),))
        row = cur.fetchone()
    db_conn.commit()
    return int(row[0])


@pytest.fixture
def client(app, admin_id):
    c = TestClient(app, follow_redirects=False)
    form = c.get("/admin/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', form)
    c.post("/admin/login", data={"username": "horst", "password": "pw",
                                 "csrf_token": m.group(1)})

    def csrf(action, method="POST"):
        return make_csrf_token(
            user_id=admin_id, action=csrf_action(method, action), key=_KEY.encode())
    c.csrf = csrf  # type: ignore[attr-defined]
    return c


def _archive(db_conn) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, imap_host, "
            "imap_port, config) "
            "VALUES ('arch', 'a@b.test', 'archive', NULL, NULL, '{}') RETURNING id")
        row = cur.fetchone()
    db_conn.commit()
    return int(row[0])


def test_list_imports_empty(client):
    r = client.get("/v1/admin/imports")
    assert r.status_code == 200
    assert r.json() == {"imports": []}


def test_create_rejects_path_outside_root(client, db_conn):
    aid = _archive(db_conn)
    r = client.post("/v1/admin/imports", json={
        "account_id": str(aid), "source_kind": "mbox", "source_path": "/etc/passwd"},
        headers={"X-CSRF-Token": client.csrf("/v1/admin/imports")})
    assert r.status_code == 400


def test_create_requires_csrf(client, db_conn):
    aid = _archive(db_conn)
    r = client.post("/v1/admin/imports", json={
        "account_id": str(aid), "source_kind": "mbox", "source_path": "/x"})
    assert r.status_code == 400


def test_cancel_unknown_job_404(client):
    r = client.post("/v1/admin/imports/999/cancel",
                    headers={"X-CSRF-Token": client.csrf("/v1/admin/imports/999/cancel")})
    assert r.status_code == 404


def test_create_happy_path_201_and_completes(client, db_conn, tmp_path):
    aid = _archive(db_conn)
    # The app fixture allowlists tmp_path/"imports"; place the mbox there.
    root = tmp_path / "imports"
    box_path = root / "a.mbox"
    box = _mailbox.mbox(str(box_path))
    box.lock()
    box.add(_mailbox.mboxMessage(b"From: a@b.test\r\nSubject: hi\r\n\r\nbody\r\n"))
    box.flush()
    box.unlock()

    r = client.post("/v1/admin/imports", json={
        "account_id": str(aid), "source_kind": "mbox", "source_path": str(box_path)},
        headers={"X-CSRF-Token": client.csrf("/v1/admin/imports")})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending"
    assert body["source_kind"] == "mbox"
    # full-object wire shape includes the new fields
    for key in ("total_messages", "last_progress_at", "started_at", "finished_at"):
        assert key in body
    jid = body["id"]

    # Poll until the background worker reaches a terminal status.
    deadline = time.time() + 30
    status = body["status"]
    while time.time() < deadline:
        g = client.get(f"/v1/admin/imports/{jid}")
        assert g.status_code == 200
        status = g.json()["status"]
        if status in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.1)
    assert status == "completed"
