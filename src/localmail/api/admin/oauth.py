"""Admin-UI web OAuth flow for Gmail (HMAC-signed stateless state).

Consumes [serve].state_signing_key (closes issue #114) and
[serve].oauth_callback_url. The CLI desktop loopback flow in
oauth_gmail.py stays in place; they coexist.
"""

from __future__ import annotations

import secrets as _stdlib_secrets
import time

import psycopg

from localmail import secrets as _secrets
from localmail.api.admin.accounts import Account, AccountFieldError, get_account
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


def _build_flow(*, redirect_uri: str):
    """Real Google OAuth Flow builder.

    Wrapped in a private helper so tests can monkeypatch.
    """
    from localmail.config import load_config  # local import: config may be absent in tests
    from google_auth_oauthlib.flow import Flow  # type: ignore[import-not-found]

    cfg = load_config()
    if cfg.gmail_oauth is None:
        raise RuntimeError(
            "gmail_oauth.client_secrets_file not configured in config.toml; "
            "cannot build OAuth flow"
        )
    flow = Flow.from_client_secrets_file(
        client_secrets_file=str(cfg.gmail_oauth.client_secrets_file),
        scopes=_GOOGLE_SCOPES,
        redirect_uri=redirect_uri,
    )
    return flow


def start_oauth(conn: psycopg.Connection, account_id: int, *,
                admin_user_id: int,
                signing_key: bytes,
                redirect_uri: str) -> str:
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
    flow = _build_flow(redirect_uri=redirect_uri)
    url, _state_echo = flow.authorization_url(
        state=state, prompt='consent', access_type='offline')
    return url


def complete_oauth(conn: psycopg.Connection, *,
                   state: str, code: str,
                   admin_user_id: int,
                   signing_key: bytes,
                   redirect_uri: str) -> Account:
    """Verify the state, exchange code, store refresh token, return account."""
    payload = decode_state(state, key=signing_key)
    if payload.user_id != admin_user_id:
        raise PermissionDenied(
            "OAuth state was minted for a different admin user"
        )
    account = get_account(conn, payload.account_id)
    flow = _build_flow(redirect_uri=redirect_uri)
    flow.fetch_token(code=code)
    refresh_token = flow.credentials.refresh_token
    _secrets.set_refresh_token(account.name, refresh_token)
    return account
