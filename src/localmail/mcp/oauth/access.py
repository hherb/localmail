# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

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

from localmail.api.auth import (
    SessionCredentialRefused,
    generate_token,
    hash_token,
    verify_token,
)
from localmail.mcp.oauth.resource_indicator import canonicalize_resource

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
    resource: str | None = None,
) -> str:
    """Mint an access token into api_tokens; return the raw token. Caller commits.

    ``family_id`` ties the token to a refresh family so reuse detection can purge
    it (see ``revoke_access_family``); ``None`` (login/non-OAuth) leaves it NULL.
    ``resource`` binds the RFC 8707 audience (``None`` = unrestricted).
    """
    raw = generate_token()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_tokens "
            "(token_sha256, user_id, expires_at, oauth_client_id, "
            " oauth_refresh_family_id, oauth_resource) "
            "SELECT %s, id, now() + make_interval(secs => %s), %s, %s, %s "
            "  FROM api_users WHERE id = %s AND is_service IS FALSE",
            (hash_token(raw), ttl_s, client_id, family_id, resource, user_id),
        )
        if cur.rowcount == 0:
            # The second writer of a session-kind api_tokens row. Unreachable
            # for a service principal today only because Rule 2 refuses the
            # consent login this descends from -- i.e. by a rule three modules
            # away, not by construction. Guarded here, `issue_token`'s claim to
            # close laundering by construction is true of both writers.
            raise SessionCredentialRefused(
                f"cannot mint an access token for user id={user_id}: "
                "no such user, or an API-key principal"
            )
    return raw


def load_access(
    conn: psycopg.Connection,
    raw_token: str,
    *,
    accepted_resources: list[str] | None = None,
) -> "AccessToken | None":
    """Verify an access token and return the SDK AccessToken, or None.

    When ``accepted_resources`` is given, a token bound to a resource
    (``oauth_resource IS NOT NULL``) is rejected unless its canonical resource is
    in the set (RFC 8707 audience enforcement at /mcp). A NULL resource is always
    unrestricted; ``accepted_resources=None`` skips enforcement entirely.
    """
    from mcp.server.auth.provider import AccessToken

    user = verify_token(conn, raw_token)
    if user is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT oauth_client_id, oauth_resource FROM api_tokens "
            "WHERE token_sha256 = %s",
            (hash_token(raw_token),),
        )
        row = cur.fetchone()
    bound_resource = row[1] if row else None
    if accepted_resources is not None and bound_resource is not None:
        if canonicalize_resource(bound_resource) not in accepted_resources:
            return None
    client_id = row[0] if row and row[0] is not None else _NO_OAUTH_CLIENT_ID
    return AccessToken(
        token=raw_token, client_id=client_id, scopes=[], subject=str(user.id)
    )


def revoke_access(conn: psycopg.Connection, raw_token: str) -> bool:
    """Delete an access token row. Returns True if a row was deleted.

    Never an API key, for the reason `api.auth.logout` refuses one: a key is
    unrecoverable and its holder is a machine, so a client's routine revocation
    would destroy it with no way back. `verify_token` now accepts keys, so
    `load_access_token` resolves one and this endpoint reaches it; today the
    SDK's `token.client_id == client.client_id` check happens to block it
    because DCR ids are uuid4 and a key resolves to the `localmail` sentinel --
    a coincidence of two constants, not a rule. This is the rule.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM api_tokens WHERE token_sha256 = %s AND api_key_name IS NULL",
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
