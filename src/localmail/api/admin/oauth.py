# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Admin-UI web OAuth flow for Gmail (HMAC-signed stateless state).

Consumes [serve].state_signing_key (closes issue #114) and
[serve].oauth_callback_url. The CLI desktop loopback flow in
oauth_gmail.py stays in place; they coexist.
"""

from __future__ import annotations

import secrets as _stdlib_secrets
import time
from pathlib import Path

import psycopg

from localmail import secrets as _secrets
from localmail.api.admin.accounts import Account, AccountFieldError, get_account, touch_account_updated_at
from localmail.api.admin.oauth_state import (
    StatePayload, decode_state, encode_state,
)


_GOOGLE_SCOPES = ['https://mail.google.com/']
_NONCE_BYTES = 16
# 15 minutes — comfortably covers a Google consent screen even with
# account-picker re-auth, while still keeping replay windows short.
_STATE_TTL_SECONDS = 900


class PermissionDenied(RuntimeError):
    """Raised when the completing admin's user_id does not match the start."""


class OAuthNotConfigured(RuntimeError):
    """Raised when Gmail OAuth client secrets are not configured on the server.

    This is an operator misconfiguration (no ``[gmail_oauth]`` section or no
    ``client_secrets_file``), not a client error or a server bug — the route
    layer maps it to a clean 503 with an actionable detail rather than letting
    a bare RuntimeError escape as a 500 (#126). Subclasses RuntimeError so the
    callback's broad ``except Exception`` (and any legacy ``except
    RuntimeError``) still catch it.
    """


def _build_flow(*, redirect_uri: str, client_secrets_file: Path | None):
    """Real Google OAuth Flow builder — pure over the secrets path.

    The route layer resolves ``client_secrets_file`` once from app state
    and hands it in, so the service layer never reaches back into config
    IO (#120). Wrapped in a private helper so tests can monkeypatch.
    """
    from google_auth_oauthlib.flow import Flow  # type: ignore[import-not-found]

    if client_secrets_file is None:
        raise OAuthNotConfigured(
            "Gmail OAuth is not configured on this server "
            "(gmail_oauth.client_secrets_file is unset in config.toml)"
        )
    flow = Flow.from_client_secrets_file(
        client_secrets_file=str(client_secrets_file),
        scopes=_GOOGLE_SCOPES,
        redirect_uri=redirect_uri,
    )
    return flow


def start_oauth(conn: psycopg.Connection, account_id: int, *,
                admin_user_id: int,
                signing_key: bytes,
                redirect_uri: str,
                client_secrets_file: Path | None) -> str:
    """Return a Google consent URL with a signed state token."""
    account = get_account(conn, account_id)
    if account.auth_method != 'oauth2' or account.oauth_provider != 'gmail':
        raise AccountFieldError("start_oauth requires Gmail OAuth account")
    payload = StatePayload(
        user_id=admin_user_id,
        account_id=account_id,
        nonce=_stdlib_secrets.token_urlsafe(_NONCE_BYTES),
        exp=int(time.time()) + _STATE_TTL_SECONDS,
    )
    state = encode_state(payload, key=signing_key)
    flow = _build_flow(
        redirect_uri=redirect_uri, client_secrets_file=client_secrets_file)
    url, _state_echo = flow.authorization_url(
        state=state, prompt='consent', access_type='offline')
    return url


def complete_oauth(conn: psycopg.Connection, *,
                   state: str, code: str,
                   admin_user_id: int,
                   signing_key: bytes,
                   redirect_uri: str,
                   client_secrets_file: Path | None) -> Account:
    """Verify the state, exchange code, store refresh token, return account."""
    payload = decode_state(state, key=signing_key)
    if payload.user_id != admin_user_id:
        raise PermissionDenied(
            "OAuth state was minted for a different admin user"
        )
    account = get_account(conn, payload.account_id)
    flow = _build_flow(
        redirect_uri=redirect_uri, client_secrets_file=client_secrets_file)
    flow.fetch_token(code=code)
    refresh_token = flow.credentials.refresh_token
    _secrets.set_refresh_token(account.name, refresh_token)
    touch_account_updated_at(conn, account.id)
    return account
