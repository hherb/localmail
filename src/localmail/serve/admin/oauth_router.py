# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""HTTP routes for the admin web OAuth flow (Sub-plan 2A).

Split into two APIRouters because the URL prefixes differ:
  - router_v1 mounts at /v1/admin (POST /accounts/{id}/oauth/start)
  - router_admin mounts at /admin (GET /oauth/callback)

Google's OAuth callback redirect carries the admin session cookie
(cookie path is /) but NOT an Authorization header, so the callback
endpoint runs under the cookie-session admin gate.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import RedirectResponse

from localmail.api.admin import oauth as svc
from localmail.api.admin.auth import AdminUser
from localmail.api.admin.oauth_state import StateExpired, StateInvalid
from localmail.api.errors import NotFound
from localmail.api.ids import parse_int_id
from localmail.serve.admin.csrf import check_csrf
from localmail.serve.admin.dependencies import require_admin_session
from localmail.serve.admin.middleware import get_unscrubbed_query_params


logger = logging.getLogger("localmail.serve")

_HTTP_SEE_OTHER = 303


router_v1 = APIRouter(tags=["admin-oauth-api"])
router_admin = APIRouter(tags=["admin-oauth-callback"])


def _state_signing_key(request: Request) -> bytes:
    cfg = request.app.state.serve_config
    key = cfg.state_signing_key
    if not key:
        raise RuntimeError("state_signing_key is empty; admin UI disabled")
    return key.encode("ascii") if isinstance(key, str) else key


def _oauth_callback_url(request: Request) -> str:
    cfg = request.app.state.serve_config
    return cfg.oauth_callback_url


def _gmail_client_secrets_file(request: Request) -> Path | None:
    """Resolve the Gmail client-secrets path once from app state.

    Threaded into the service layer so it never calls load_config() per
    request (#120). None when no [gmail_oauth] section is configured —
    the service raises a clean RuntimeError at flow-build time.
    """
    return getattr(request.app.state, "gmail_client_secrets_file", None)


@router_v1.post("/accounts/{account_id}/oauth/start")
def oauth_start(
    account_id: str,
    request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> dict:
    """Mint a Google consent URL with a signed state token."""
    aid = parse_int_id(account_id, field="account_id")
    check_csrf(
        request, admin, x_csrf_token,
        f"/v1/admin/accounts/{aid}/oauth/start",
    )
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            url = svc.start_oauth(
                conn, aid,
                admin_user_id=admin.id,
                signing_key=_state_signing_key(request),
                redirect_uri=_oauth_callback_url(request),
                client_secrets_file=_gmail_client_secrets_file(request),
            )
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
        except svc.AccountFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except svc.OAuthNotConfigured as e:
            raise HTTPException(status_code=503, detail=str(e))
    return {'auth_url': url}


@router_admin.get("/oauth/callback")
def oauth_callback(
    request: Request,
    admin: AdminUser = require_admin_session(),
) -> RedirectResponse:
    """Google redirects here with state + code; resolves to /admin/accounts/{id}.

    No CSRF token check on a GET callback — Google can't supply one. SameSite=Lax
    on the session cookie and the HMAC nonce inside the state token are the
    defenses against cross-site replay.

    Reads ``state`` and ``code`` from the unscrubbed query params — the
    ``ScrubSensitiveQueryParamsMiddleware`` rewrites both to ``REDACTED`` in
    ``request.query_params`` before logs see them. The originals live in
    ``request.scope`` under the middleware's private key.
    """
    raw = get_unscrubbed_query_params(request)
    state = raw.get('state', '')
    code = raw.get('code', '')
    pool = request.app.state.pool
    try:
        with pool.connection() as conn:
            account = svc.complete_oauth(
                conn,
                state=state, code=code,
                admin_user_id=admin.id,
                signing_key=_state_signing_key(request),
                redirect_uri=_oauth_callback_url(request),
                client_secrets_file=_gmail_client_secrets_file(request),
            )
    except (StateInvalid, StateExpired, svc.PermissionDenied, NotFound) as e:
        logger.warning("oauth_callback rejected: %s: %s", type(e).__name__, e)
        return RedirectResponse(
            '/admin?oauth=failed',
            status_code=_HTTP_SEE_OTHER,
        )
    except Exception:
        # Defense in depth: any unexpected exception (e.g. Google API hiccup,
        # keyring failure) also yields a clean failed-redirect.
        logger.exception("oauth_callback unexpected error")
        return RedirectResponse(
            '/admin?oauth=failed',
            status_code=_HTTP_SEE_OTHER,
        )
    return RedirectResponse(
        f'/admin/accounts/{account.id}?oauth=success',
        status_code=_HTTP_SEE_OTHER,
    )
