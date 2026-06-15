"""Top-level HTTP middleware enforcing the per-IP DCR registration cap before a
request reaches the SDK-owned /register route inside the /mcp sub-mount. On each
admitted POST it records the attempt and opportunistically sweeps both the
attempt audit and unused client rows.
"""
from __future__ import annotations

import anyio.to_thread
from psycopg_pool import ConnectionPool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from localmail.api.client_ip import TrustedProxies, resolve_client_ip
from localmail.config import AuthConfig, McpConfig
from localmail.mcp.oauth import clients, registration


def _scope_xff(scope: Scope) -> str | None:
    """First X-Forwarded-For header value from an ASGI scope, or None.

    Mirrors ``request.headers.get("X-Forwarded-For")`` on the login path: the
    first matching header line wins.
    """
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-for":
            return value.decode("latin-1")
    return None


def resolve_scope_client_ip(
    scope: Scope, *, trusted_proxies: TrustedProxies, max_hops: int
) -> str | None:
    """Originating client IP for an ASGI scope, honouring trusted-proxy XFF
    peeling — the same logic the login rate limiter uses, so the per-IP DCR
    cap survives a reverse proxy instead of collapsing to a global cap.
    """
    client = scope.get("client")
    peer = client[0] if client else None
    return resolve_client_ip(
        peer, _scope_xff(scope),
        trusted_proxies=trusted_proxies, max_hops=max_hops,
    )


class RegistrationRateLimit:
    def __init__(
        self,
        app: ASGIApp,
        *,
        pool: ConnectionPool,
        config: McpConfig,
        auth_config: AuthConfig | None = None,
        register_path_suffix: str = "/register",
    ) -> None:
        self._app = app
        self._pool = pool
        self._cfg = config
        self._suffix = register_path_suffix
        # A default AuthConfig() has no trusted proxies (XFF never peeled) and
        # carries the canonical max_hops default, so the proxy-peeling knobs
        # have a single source of truth instead of a duplicated constant.
        auth = auth_config or AuthConfig()
        self._trusted_proxies = auth.trusted_proxies_parsed
        self._max_hops = auth.trusted_proxies_max_hops

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or not scope.get("path", "").endswith(self._suffix)
        ):
            await self._app(scope, receive, send)
            return
        ip = resolve_scope_client_ip(
            scope, trusted_proxies=self._trusted_proxies, max_hops=self._max_hops
        )
        # The limit check + record/sweep are blocking DB work; keep them off the
        # event loop so a /register burst can't stall the shared listener.
        if await anyio.to_thread.run_sync(self._over_limit, ip):
            resp = JSONResponse(
                {"error": "rate_limited",
                 "error_description": "too many registration attempts"},
                status_code=429,
            )
            await resp(scope, receive, send)
            return
        await anyio.to_thread.run_sync(self._record_and_sweep, ip)
        await self._app(scope, receive, send)

    def _over_limit(self, ip: str | None) -> bool:
        with self._pool.connection() as conn:
            over = registration.over_limit(
                conn, ip, window_s=self._cfg.oauth_registration_window_s,
                max_n=self._cfg.oauth_registration_max,
            )
            conn.commit()
        return over

    def _record_and_sweep(self, ip: str | None) -> None:
        with self._pool.connection() as conn:
            registration.record(conn, ip)
            registration.sweep(conn, retention_s=self._cfg.oauth_registration_window_s)
            clients.cleanup_unused(
                conn, retention_s=self._cfg.oauth_client_unused_retention_s
            )
            conn.commit()
