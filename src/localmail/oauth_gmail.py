"""Gmail OAuth2 (XOAUTH2) for IMAP access.

The interactive consent flow is run by `localmail oauth-login NAME` and writes
a *refresh token* to the OS keyring under (service="localmail",
username="<name>:refresh"). Thereafter `imap_client.open_connection` reads the
refresh token, mints a short-lived access token, and authenticates the IMAP
session via XOAUTH2.

The Google Cloud client (client_id + client_secret) is loaded from the JSON
file Google generates when you create a "Desktop" OAuth client. The path lives
in config.toml under `[gmail_oauth] client_secrets_file = "..."`.
"""

from __future__ import annotations

import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

GMAIL_SCOPES = ["https://mail.google.com/"]


def _read_client_secrets(path: Path) -> dict:
    with open(path) as f:
        data = json.load(f)
    payload = data.get("installed") or data.get("web")
    if not payload:
        raise ValueError(
            f"{path}: expected an 'installed' or 'web' key (Google OAuth client JSON)"
        )
    missing = [k for k in ("client_id", "client_secret", "token_uri") if k not in payload]
    if missing:
        raise ValueError(f"{path}: missing keys in client_secrets: {missing}")
    return payload


def run_consent_flow(client_secrets_file: Path) -> Credentials:
    """Run the desktop OAuth flow (browser + local callback server).

    Returns a Credentials whose refresh_token MUST be persisted by the caller.
    """
    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_secrets_file), scopes=GMAIL_SCOPES
    )
    creds = flow.run_local_server(port=0, prompt="consent")
    if not creds.refresh_token:
        raise RuntimeError(
            "OAuth completed but Google did not return a refresh_token. "
            "Revoke localmail's access at https://myaccount.google.com/permissions "
            "and run oauth-login again — the first authorization is the only one that "
            "yields a refresh_token."
        )
    return creds


def credentials_from_refresh(
    refresh_token: str, client_secrets_file: Path
) -> Credentials:
    """Build Credentials from a stored refresh_token + client app secrets.

    The access_token slot is empty; call .refresh(Request()) to fill it.
    """
    payload = _read_client_secrets(client_secrets_file)
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=payload["token_uri"],
        client_id=payload["client_id"],
        client_secret=payload["client_secret"],
        scopes=GMAIL_SCOPES,
    )


def fresh_access_token(refresh_token: str, client_secrets_file: Path) -> str:
    creds = credentials_from_refresh(refresh_token, client_secrets_file)
    creds.refresh(Request())
    if not creds.token:
        raise RuntimeError("token refresh returned no access_token")
    return creds.token


def build_xoauth2_string(email: str, access_token: str) -> str:
    """The SASL XOAUTH2 payload (Gmail IMAP/SMTP).

    `imapclient.IMAPClient.oauth2_login(...)` will build this internally; this
    helper exists for testing and for any non-imapclient consumer.
    """
    return f"user={email}\x01auth=Bearer {access_token}\x01\x01"
