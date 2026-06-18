# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Cross-cutting middleware: request IDs, auth, error mapping."""
from __future__ import annotations

import logging
import time
import uuid
from typing import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool
from starlette.middleware.base import BaseHTTPMiddleware

from localmail.api.auth import AuthenticatedUser, verify_token
from localmail.api.errors import APIError, InvalidToken, RateLimited

logger = logging.getLogger("localmail.serve")


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable]):
        rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        request.state.request_id = rid
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000
        response.headers["X-Request-Id"] = rid
        logger.info(
            "request",
            extra={"request_id": rid, "path": request.url.path,
                   "status": response.status_code, "duration_ms": duration_ms},
        )
        return response


class APIErrorHandlerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except APIError as err:
            response = JSONResponse(
                err.to_problem(),
                status_code=err.http_status,
                media_type="application/problem+json",
            )
            if isinstance(err, RateLimited) and err.retry_after_s is not None:
                response.headers["Retry-After"] = str(err.retry_after_s)
            return response


def get_authenticated_user(request: Request) -> AuthenticatedUser:
    """FastAPI dependency: extract & verify Bearer token, return the user."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise InvalidToken("missing or malformed Authorization header")
    token = auth[len("Bearer "):]
    pool: ConnectionPool = request.app.state.pool
    with pool.connection() as conn:
        user = verify_token(conn, token)
        conn.commit()
    if user is None:
        raise InvalidToken("token is invalid, expired, or revoked")
    return user
