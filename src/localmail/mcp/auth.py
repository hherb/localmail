"""Opaque-bearer TokenVerifier for the MCP server.

Validates a bearer against the existing `api_tokens` store via
`api.auth.verify_token`. localmail mints tokens through `/v1/auth/login`;
this verifier only *checks* them — there is no OAuth authorization server.
"""
from __future__ import annotations

from mcp.server.auth.provider import AccessToken, TokenVerifier
from psycopg_pool import ConnectionPool

from localmail.api.auth import verify_token as api_verify_token

# The localmail user id is carried in AccessToken.client_id (free-form string
# in the MCP model); tools read it back via `user_id_from_access_token`.
_NO_EXPIRY: int | None = None
_NO_SCOPES: list[str] = []


class LocalmailTokenVerifier(TokenVerifier):
    """Resource-server token verifier backed by `api_tokens`."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return an AccessToken if the bearer is valid, else None."""
        if not token:
            return None
        with self._pool.connection() as conn:
            user = api_verify_token(conn, token)
            conn.commit()
        if user is None:
            return None
        return AccessToken(
            token=token,
            client_id=str(user.id),
            scopes=_NO_SCOPES,
            expires_at=_NO_EXPIRY,
        )


def user_id_from_access_token(access_token: AccessToken) -> int:
    """Recover the localmail user id stashed in `client_id`."""
    return int(access_token.client_id)
