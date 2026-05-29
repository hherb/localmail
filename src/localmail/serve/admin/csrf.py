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


def csrf_action(method: str, action_path: str) -> str:
    """Bind a CSRF action to the HTTP method as well as the path.

    PATCH and DELETE share the URL ``/v1/admin/accounts/{id}``; binding the
    action to the method means a token minted for one method can't be
    replayed against another on the same path (#122). Pure helper so the
    token-mint side (UI / tests) and the verify side derive the same string.
    """
    return f"{method.upper()}:{action_path}"


def check_csrf(
    request: Request,
    admin: AdminUser,
    csrf_token: str,
    action: str,
) -> None:
    """Raise HTTPException(400) if the CSRF token is missing or invalid.

    ``action`` is the URL path; the bound action additionally carries the
    request method (see ``csrf_action``) so tokens are method-scoped.
    """
    if not csrf_token:
        raise HTTPException(status_code=400, detail="CSRF token missing")
    key = session_signing_key(request)
    bound = csrf_action(request.method, action)
    try:
        verify_csrf_token(csrf_token, user_id=admin.id, action=bound, key=key)
    except CSRFError:
        raise HTTPException(status_code=400, detail="CSRF token invalid")
