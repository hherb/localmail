# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""FastAPI dependencies for admin routes.

require_admin_session() reads the session cookie, verifies its HMAC, looks
up the user, and asserts is_admin=TRUE. Failures redirect to /admin/login
(no cookie / tampered / expired / deleted) or return 403 (valid cookie,
user no longer admin).
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from localmail.api.admin.auth import (
    AdminUser,
    NotAnAdmin,
    SessionInvalidated,
    UserNotFound,
    get_admin_user,
)
from localmail.api.admin.session_tokens import (
    SessionTokenError,
    decode_session_token,
)


SESSION_COOKIE_NAME = "localmail_admin_session"


class _AdminRedirect(HTTPException):
    """Signals 'redirect to /admin/login'. Caught by app-wide handler."""
    def __init__(self) -> None:
        super().__init__(status_code=303, detail="redirect-to-login")


def _signing_key(request: Request) -> bytes:
    cfg = request.app.state.serve_config
    key_str = getattr(cfg, "session_signing_key", "")
    if not key_str:
        raise RuntimeError(
            "ServeConfig.session_signing_key is empty; admin UI requires it. "
            "Set [serve] session_signing_key in config.toml."
        )
    return key_str.encode("ascii") if isinstance(key_str, str) else key_str


def require_admin_session():
    """Dependency factory; returns the AdminUser or raises redirect/403."""
    def _dep(request: Request) -> AdminUser:
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if not token:
            raise _AdminRedirect()
        key = _signing_key(request)
        try:
            payload = decode_session_token(token, key=key)
        except SessionTokenError:
            raise _AdminRedirect()
        pool = request.app.state.pool
        with pool.connection() as conn:
            try:
                return get_admin_user(
                    conn,
                    user_id=payload.user_id,
                    issued_at=payload.issued_at,
                )
            except UserNotFound:
                raise _AdminRedirect()
            except SessionInvalidated:
                raise _AdminRedirect()
            except NotAnAdmin:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="not an admin",
                )
    return Depends(_dep)


def install_admin_redirect_handler(app) -> None:
    """Translate the internal _AdminRedirect exception to a 303 redirect."""
    @app.exception_handler(_AdminRedirect)
    async def _on_redirect(_request, _exc):
        return RedirectResponse("/admin/login", status_code=303)
