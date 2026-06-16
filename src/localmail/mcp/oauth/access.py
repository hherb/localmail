"""Access-token bridge: OAuth access tokens live in `api_tokens` so the existing
resource-server verifier (`api.auth.verify_token`) and per-user ACL apply
unchanged. `oauth_client_id` attributes the token to its client.

`load_access` returns the SDK's `AccessToken` (subject = user id) for
`provider.load_access_token`. The SDK import is function-local so this module
stays import-safe without the `mcp` extra.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import psycopg

from localmail.api.auth import generate_token, hash_token, verify_token

if TYPE_CHECKING:
    from mcp.server.auth.provider import AccessToken

# Sentinel client_id for api_tokens rows with no OAuth client (i.e. login-issued
# tokens via /v1/auth/login, where oauth_client_id is NULL). Matches the
# CLIENT_ID constant the opaque-bearer LocalmailTokenVerifier uses.
_NO_OAUTH_CLIENT_ID = "localmail"


def mint_access(
    conn: psycopg.Connection,
    *,
    user_id: int,
    client_id: str,
    ttl_s: int,
    family_id: uuid.UUID | None = None,
) -> str:
    """Mint an access token into api_tokens; return the raw token. Caller commits.

    ``family_id`` ties the token to a refresh family so reuse detection can purge
    it (see ``revoke_access_family``); ``None`` (login/non-OAuth) leaves it NULL.
    """
    raw = generate_token()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_tokens "
            "(token_sha256, user_id, expires_at, oauth_client_id, "
            " oauth_refresh_family_id) "
            "VALUES (%s, %s, now() + make_interval(secs => %s), %s, %s)",
            (hash_token(raw), user_id, ttl_s, client_id, family_id),
        )
    return raw


def load_access(conn: psycopg.Connection, raw_token: str) -> "AccessToken | None":
    """Verify an access token and return the SDK AccessToken, or None."""
    from mcp.server.auth.provider import AccessToken

    user = verify_token(conn, raw_token)
    if user is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT oauth_client_id FROM api_tokens WHERE token_sha256 = %s",
            (hash_token(raw_token),),
        )
        row = cur.fetchone()
    client_id = row[0] if row and row[0] is not None else _NO_OAUTH_CLIENT_ID
    return AccessToken(
        token=raw_token, client_id=client_id, scopes=[], subject=str(user.id)
    )


def revoke_access(conn: psycopg.Connection, raw_token: str) -> bool:
    """Delete an access token row. Returns True if a row was deleted."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM api_tokens WHERE token_sha256 = %s",
            (hash_token(raw_token),),
        )
        return cur.rowcount > 0


def revoke_access_family(conn: psycopg.Connection, family_id: uuid.UUID) -> int:
    """Delete every access token in a refresh family. Returns the deleted count.

    Called from the provider's reuse branch so a detected refresh-token reuse
    contains the access window immediately. Caller commits.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM api_tokens WHERE oauth_refresh_family_id = %s",
            (family_id,),
        )
        return cur.rowcount
