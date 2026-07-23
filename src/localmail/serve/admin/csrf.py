# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

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
from localmail.api.admin.csrf import CSRFError, make_csrf_token, verify_csrf_token


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


def csrf_token_context(*, user_id: int, key: bytes) -> dict:
    """Jinja context helpers for minting CSRF tokens for one admin user.

    Returns two callables:
      * ``csrf_token_for(action)`` — legacy single-arg (non-method-bound), used
        by ``base.html``'s body-wide htmx header and the logout form.
      * ``csrf_token_for_method(method, action)`` — method-bound (#122/#125):
        the form HTML UIs MUST use this for any route guarded by ``check_csrf``,
        which binds the action to the request method via ``csrf_action``.

    Sharing the mint here keeps every admin HTML template deriving the identical
    bound string the verify side expects (reused by the daemon panel + future
    account screens, 2A.3).
    """
    def csrf_token_for(action: str) -> str:
        return make_csrf_token(user_id=user_id, action=action, key=key)

    def csrf_token_for_method(method: str, action: str) -> str:
        return make_csrf_token(
            user_id=user_id, action=csrf_action(method, action), key=key
        )

    return {
        "csrf_token_for": csrf_token_for,
        "csrf_token_for_method": csrf_token_for_method,
    }


def check_csrf(
    request: Request,
    admin: AdminUser,
    csrf_token: str,
    action: str,
) -> None:
    """Raise HTTPException(400) if the CSRF token is missing or invalid.

    ``action`` is the URL path; the bound action additionally carries the
    request method (see ``csrf_action``) so tokens are method-scoped.

    Bearer-authenticated admin requests (native clients) carry no ambient
    cookie credential, so CSRF does not apply and is skipped.
    """
    if getattr(request.state, "admin_auth_kind", "cookie") == "bearer":
        return
    if not csrf_token:
        raise HTTPException(status_code=400, detail="CSRF token missing")
    key = session_signing_key(request)
    bound = csrf_action(request.method, action)
    try:
        verify_csrf_token(csrf_token, user_id=admin.id, action=bound, key=key)
    except CSRFError:
        raise HTTPException(status_code=400, detail="CSRF token invalid")
