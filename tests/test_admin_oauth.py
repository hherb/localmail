"""Service-layer tests for the admin web OAuth flow."""

from __future__ import annotations

import keyring
import pytest

from localmail.api.admin.oauth import (
    PermissionDenied,
    complete_oauth, start_oauth,
)
from localmail.api.admin.oauth_state import StateExpired, StateInvalid
from tests._fake_google_oauth import FakeFlow


KEY = b"k" * 32
CB = "https://example.test/admin/oauth/callback"


def _make_oauth_account(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "  oauth_provider, imap_host, imap_port, config) "
            "VALUES ('gm', 'g@example.test', 'oauth2', 'gmail', "
            "        'imap.gmail.com', 993, '{}'::jsonb) RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


@pytest.fixture
def fake_flow(monkeypatch):
    flow = FakeFlow()

    def _capture_redirect(*, redirect_uri):
        flow.redirect_uri = redirect_uri
        return flow

    monkeypatch.setattr(
        'localmail.api.admin.oauth._build_flow',
        _capture_redirect,
    )
    return flow


def test_start_oauth_returns_consent_url_with_signed_state(db_conn, fake_flow):
    aid = _make_oauth_account(db_conn)
    url = start_oauth(db_conn, aid, admin_user_id=42,
                      signing_key=KEY, redirect_uri=CB)
    assert url.startswith('https://accounts.google.com/o/oauth2/auth?state=')


def test_complete_oauth_stores_refresh_token(db_conn, fake_flow):
    aid = _make_oauth_account(db_conn)
    url = start_oauth(db_conn, aid, admin_user_id=42,
                      signing_key=KEY, redirect_uri=CB)
    state = url.split('state=')[1]
    acct = complete_oauth(db_conn, state=state, code='good-code',
                          admin_user_id=42, signing_key=KEY,
                          redirect_uri=CB)
    assert acct.id == aid
    assert keyring.get_password('localmail', 'gm:refresh') == 'refresh-xyz'


def test_complete_oauth_rejects_cross_user_replay(db_conn, fake_flow):
    aid = _make_oauth_account(db_conn)
    url = start_oauth(db_conn, aid, admin_user_id=42,
                      signing_key=KEY, redirect_uri=CB)
    state = url.split('state=')[1]
    with pytest.raises(PermissionDenied):
        complete_oauth(db_conn, state=state, code='good-code',
                       admin_user_id=99, signing_key=KEY,
                       redirect_uri=CB)


def test_complete_oauth_rejects_tampered_state(db_conn, fake_flow):
    aid = _make_oauth_account(db_conn)
    url = start_oauth(db_conn, aid, admin_user_id=42,
                      signing_key=KEY, redirect_uri=CB)
    state = url.split('state=')[1]
    head, sig = state.split('.', 1)
    bad_state = head[:-1] + ('A' if head[-1] != 'A' else 'B') + '.' + sig
    with pytest.raises(StateInvalid):
        complete_oauth(db_conn, state=bad_state, code='good-code',
                       admin_user_id=42, signing_key=KEY,
                       redirect_uri=CB)
