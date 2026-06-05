"""HTML-screen tests for /admin/imports (2A.5)."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from localmail.api.auth import hash_password
from localmail.config import ImportsConfig, ServeConfig
from localmail.serve.app import create_app

_KEY = "x" * 43


@pytest.fixture
def serve_cfg():
    return ServeConfig(session_signing_key=_KEY, state_signing_key="y" * 43,
                       cookie_secure=False)


@pytest.fixture
def admin_id(db_conn) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('horst', %s, TRUE) RETURNING id", (hash_password("pw"),))
        row = cur.fetchone()
    db_conn.commit()
    return int(row[0])


def _client(app, admin_id):
    c = TestClient(app, follow_redirects=False)
    form = c.get("/admin/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', form)
    c.post("/admin/login", data={"username": "horst", "password": "pw",
                                 "csrf_token": m.group(1)})
    return c


def test_panel_disabled_when_no_roots(db_dsn, serve_cfg, admin_id, tmp_path):
    app = create_app(db_dsn=db_dsn, serve_config=serve_cfg,
                     imports_config=ImportsConfig(roots=[]),
                     attachments_root=tmp_path / "b")
    c = _client(app, admin_id)
    r = c.get("/admin/imports")
    assert r.status_code == 200
    assert "disabled" in r.text.lower() or "not configured" in r.text.lower()


def test_panel_lists_with_roots(db_dsn, serve_cfg, admin_id, tmp_path):
    root = tmp_path / "imports"
    root.mkdir()
    app = create_app(db_dsn=db_dsn, serve_config=serve_cfg,
                     imports_config=ImportsConfig(roots=[root]),
                     attachments_root=tmp_path / "b")
    c = _client(app, admin_id)
    r = c.get("/admin/imports")
    assert r.status_code == 200
    assert 'name="source_path"' in r.text


def test_progress_partial_renders(db_dsn, serve_cfg, admin_id, db_conn, tmp_path):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, imap_host, "
            "imap_port, config) "
            "VALUES ('arch', 'a@b.test', 'archive', NULL, NULL, '{}') RETURNING id")
        aid = int(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO import_jobs (account_id, source_kind, source_path, status) "
            "VALUES (%s, 'mbox', '/x', 'completed') RETURNING id", (aid,))
        jid = int(cur.fetchone()[0])
    db_conn.commit()
    root = tmp_path / "imports"
    root.mkdir()
    app = create_app(db_dsn=db_dsn, serve_config=serve_cfg,
                     imports_config=ImportsConfig(roots=[root]),
                     attachments_root=tmp_path / "b")
    c = _client(app, admin_id)
    r = c.get(f"/admin/_partials/import-status/{jid}")
    assert r.status_code == 200
    assert "completed" in r.text


def test_panel_requires_auth(db_dsn, serve_cfg, tmp_path):
    root = tmp_path / "imports"
    root.mkdir()
    app = create_app(db_dsn=db_dsn, serve_config=serve_cfg,
                     imports_config=ImportsConfig(roots=[root]),
                     attachments_root=tmp_path / "b")
    c = TestClient(app, follow_redirects=False)
    r = c.get("/admin/imports")
    assert r.status_code in (302, 303)
