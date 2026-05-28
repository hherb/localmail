"""Shared CSRF helpers for admin JSON routers.

The HTML admin routers (`auth_router`, `dashboard_router`) embed CSRF
tokens in Jinja2 forms and verify with `Form(...)`. The JSON routers
(`accounts_router`, `oauth_router`) consume tokens from the
`X-CSRF-Token` header and need a small, consistent guard — that lives
here so the two JSON routers cannot drift apart.
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from localmail.api.admin.auth import AdminUser
from localmail.api.admin.csrf import CSRFError, verify_csrf_token


def session_signing_key(request: Request) -> bytes:
    """Return the configured session signing key as bytes."""
    cfg = request.app.state.serve_config
    key = cfg.session_signing_key
    if not key:
        raise RuntimeError("session_signing_key is empty; admin UI disabled")
    return key.encode("ascii") if isinstance(key, str) else key


def check_csrf(
    request: Request,
    admin: AdminUser,
    csrf_token: str,
    action: str,
) -> None:
    """Raise HTTPException(400) if the CSRF token is missing or invalid."""
    if not csrf_token:
        raise HTTPException(status_code=400, detail="CSRF token missing")
    key = session_signing_key(request)
    try:
        verify_csrf_token(csrf_token, user_id=admin.id, action=action, key=key)
    except CSRFError:
        raise HTTPException(status_code=400, detail="CSRF token invalid")
