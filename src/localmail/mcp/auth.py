# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

from __future__ import annotations

import anyio.to_thread
from mcp.server.auth.provider import AccessToken, TokenVerifier
from psycopg_pool import ConnectionPool

from localmail.mcp.oauth.access import load_access

# The authenticated *user* is carried in AccessToken.subject (OAuth `sub`),
# recovered via user_id_from_access_token. expires_at is left None
# deliberately: the authoritative expiry check lives in api.auth.verify_token's
# WHERE clause (AND (t.expires_at IS NULL OR t.expires_at > now())), so an
# expired token already resolves to None before we ever build an AccessToken.
# A NULL expiry is an API key, not an expired row — it never expires.


class LocalmailTokenVerifier(TokenVerifier):
    """Resource-server token verifier backed by `api_tokens`.

    The MCP Protocol requires an async `verify_token`; localmail's store is
    synchronous psycopg, so the blocking lookup is offloaded to a worker
    thread to avoid stalling the event loop.

    It verifies through `access.load_access`, the AS path's own check, so a
    token the authorization server bound to an RFC 8707 audience keeps that
    binding after the AS is switched off (#401). `accepted_resources` is
    keyword-only with no default: there is no "skip the check" value to
    reach by forgetting it.
    """

    def __init__(self, pool: ConnectionPool, *, accepted_resources: list[str]) -> None:
        self._pool = pool
        self._accepted = accepted_resources

    def _lookup(self, token: str) -> AccessToken | None:
        with self._pool.connection() as conn:
            access_token = load_access(conn, token, accepted_resources=self._accepted)
            conn.commit()
        return access_token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        return await anyio.to_thread.run_sync(self._lookup, token)


def user_id_from_access_token(access_token: AccessToken) -> int:
    """Recover the localmail user id from an AccessToken built by
    `access.load_access`, which both /mcp verifiers use (stored in `subject`).

    Raises ValueError if the token was not built there (no digit `subject`).
    """
    subject = access_token.subject
    if subject is None or not subject.isdigit():
        raise ValueError("access token was not built by access.load_access")
    return int(subject)
