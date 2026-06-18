# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Service-layer tests for the admin web OAuth flow."""

from __future__ import annotations

from pathlib import Path

import keyring
import pytest

from localmail.api.admin.oauth import (
    OAuthNotConfigured,
    PermissionDenied,
    complete_oauth, start_oauth,
)
from localmail.api.admin.oauth_state import StateExpired, StateInvalid
from tests._fake_google_oauth import FakeFlow


KEY = b"k" * 32
CB = "https://example.test/admin/oauth/callback"
SECRETS = Path("/nonexistent/client_secrets.json")  # _build_flow is mocked


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

    def _capture_redirect(*, redirect_uri, client_secrets_file):
        flow.redirect_uri = redirect_uri
        flow.client_secrets_file = client_secrets_file
        return flow

    monkeypatch.setattr(
        'localmail.api.admin.oauth._build_flow',
        _capture_redirect,
    )
    return flow


def test_start_oauth_returns_consent_url_with_signed_state(db_conn, fake_flow):
    aid = _make_oauth_account(db_conn)
    url = start_oauth(db_conn, aid, admin_user_id=42,
                      signing_key=KEY, redirect_uri=CB,
                      client_secrets_file=SECRETS)
    assert url.startswith('https://accounts.google.com/o/oauth2/auth?state=')


def test_start_oauth_threads_client_secrets_path_into_flow(db_conn, fake_flow):
    """The resolved client_secrets_file is handed to _build_flow rather than
    re-read from config inside the service layer (#120)."""
    aid = _make_oauth_account(db_conn)
    start_oauth(db_conn, aid, admin_user_id=42, signing_key=KEY,
                redirect_uri=CB, client_secrets_file=SECRETS)
    assert fake_flow.client_secrets_file == SECRETS


def test_complete_oauth_stores_refresh_token(db_conn, fake_flow):
    aid = _make_oauth_account(db_conn)
    url = start_oauth(db_conn, aid, admin_user_id=42,
                      signing_key=KEY, redirect_uri=CB,
                      client_secrets_file=SECRETS)
    state = url.split('state=')[1]
    acct = complete_oauth(db_conn, state=state, code='good-code',
                          admin_user_id=42, signing_key=KEY,
                          redirect_uri=CB, client_secrets_file=SECRETS)
    assert acct.id == aid
    assert keyring.get_password('localmail', 'gm:refresh') == 'refresh-xyz'


def test_oauth_not_configured_is_runtimeerror_subclass():
    """Subclassing RuntimeError keeps the callback's broad ``except Exception``
    (and any legacy ``except RuntimeError``) catching it (#126)."""
    assert issubclass(OAuthNotConfigured, RuntimeError)


def test_start_oauth_raises_oauth_not_configured_when_secrets_missing(db_conn):
    """An unconfigured client_secrets_file surfaces as a typed
    OAuthNotConfigured (mapped to a clean 503 at the route) rather than a
    bare RuntimeError that the route would let escape as a 500 (#126).

    No ``fake_flow`` fixture here — the real ``_build_flow`` runs and hits
    the ``client_secrets_file is None`` guard.
    """
    aid = _make_oauth_account(db_conn)
    with pytest.raises(OAuthNotConfigured):
        start_oauth(db_conn, aid, admin_user_id=42, signing_key=KEY,
                    redirect_uri=CB, client_secrets_file=None)


def test_complete_oauth_rejects_cross_user_replay(db_conn, fake_flow):
    aid = _make_oauth_account(db_conn)
    url = start_oauth(db_conn, aid, admin_user_id=42,
                      signing_key=KEY, redirect_uri=CB,
                      client_secrets_file=SECRETS)
    state = url.split('state=')[1]
    with pytest.raises(PermissionDenied):
        complete_oauth(db_conn, state=state, code='good-code',
                       admin_user_id=99, signing_key=KEY,
                       redirect_uri=CB, client_secrets_file=SECRETS)


def test_complete_oauth_rejects_tampered_state(db_conn, fake_flow):
    aid = _make_oauth_account(db_conn)
    url = start_oauth(db_conn, aid, admin_user_id=42,
                      signing_key=KEY, redirect_uri=CB,
                      client_secrets_file=SECRETS)
    state = url.split('state=')[1]
    head, sig = state.split('.', 1)
    bad_state = head[:-1] + ('A' if head[-1] != 'A' else 'B') + '.' + sig
    with pytest.raises(StateInvalid):
        complete_oauth(db_conn, state=bad_state, code='good-code',
                       admin_user_id=42, signing_key=KEY,
                       redirect_uri=CB, client_secrets_file=SECRETS)


def test_complete_oauth_bumps_updated_at(db_conn, fake_flow):
    """complete_oauth must bump accounts.updated_at so the daemon's hot-reload
    notices the credential rotation."""
    from localmail.api.admin.accounts import get_account
    aid = _make_oauth_account(db_conn)
    db_conn.commit()
    before = get_account(db_conn, aid).updated_at
    url = start_oauth(db_conn, aid, admin_user_id=42,
                      signing_key=KEY, redirect_uri=CB,
                      client_secrets_file=SECRETS)
    state = url.split('state=')[1]
    complete_oauth(db_conn, state=state, code='good-code',
                   admin_user_id=42, signing_key=KEY,
                   redirect_uri=CB, client_secrets_file=SECRETS)
    db_conn.commit()
    after = get_account(db_conn, aid).updated_at
    assert after > before
