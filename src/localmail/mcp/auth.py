from __future__ import annotations

import anyio.to_thread
from mcp.server.auth.provider import AccessToken, TokenVerifier
from psycopg_pool import ConnectionPool

from localmail.api.auth import AuthenticatedUser, verify_token as api_verify_token

# Localmail is the sole MCP client identity; the authenticated *user* is
# carried in AccessToken.subject (OAuth `sub`), recovered via
# user_id_from_access_token. expires_at is left None deliberately: the
# authoritative expiry check lives in api.auth.verify_token's WHERE clause
# (AND expires_at > now()), so an expired token already resolves to None
# before we ever build an AccessToken.
CLIENT_ID = "localmail"


class LocalmailTokenVerifier(TokenVerifier):
    """Resource-server token verifier backed by `api_tokens`.

    The MCP Protocol requires an async `verify_token`; localmail's store is
    synchronous psycopg, so the blocking lookup is offloaded to a worker
    thread to avoid stalling the event loop.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def _lookup(self, token: str) -> AuthenticatedUser | None:
        with self._pool.connection() as conn:
            user = api_verify_token(conn, token)
            conn.commit()
        return user

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        user = await anyio.to_thread.run_sync(self._lookup, token)
        if user is None:
            return None
        return AccessToken(
            token=token,
            client_id=CLIENT_ID,
            scopes=[],
            subject=str(user.id),
        )


def user_id_from_access_token(access_token: AccessToken) -> int:
    """Recover the localmail user id from a token minted by
    LocalmailTokenVerifier (stored in `subject`).

    Raises ValueError if the token was not minted by this verifier (no
    digit `subject`).
    """
    subject = access_token.subject
    if subject is None or not subject.isdigit():
        raise ValueError("access token was not minted by LocalmailTokenVerifier")
    return int(subject)
