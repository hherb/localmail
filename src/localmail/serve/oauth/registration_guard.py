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

from localmail.config import McpConfig
from localmail.mcp.oauth import clients, registration


class RegistrationRateLimit:
    def __init__(
        self,
        app: ASGIApp,
        *,
        pool: ConnectionPool,
        config: McpConfig,
        register_path_suffix: str = "/register",
    ) -> None:
        self._app = app
        self._pool = pool
        self._cfg = config
        self._suffix = register_path_suffix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or not scope.get("path", "").endswith(self._suffix)
        ):
            await self._app(scope, receive, send)
            return
        client = scope.get("client")
        ip = client[0] if client else None
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
