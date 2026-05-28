"""GET /admin/login (render form), POST /admin/login (validate + cookie)."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from localmail.api.admin.auth import (
    AdminUser,
    NotAnAdmin,
    authenticate_admin,
)
from localmail.api.admin.csrf import CSRFError, make_csrf_token, verify_csrf_token
from localmail.api.admin.session_tokens import SessionPayload, encode_session_token
from localmail.api.auth import _check_login_rate_limits, _record_login_attempt
from localmail.api.client_ip import resolve_client_ip
from localmail.api.errors import AuthenticationFailed, RateLimited
from localmail.serve.admin.dependencies import SESSION_COOKIE_NAME, require_admin_session

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_log = logging.getLogger("localmail.serve.admin")

LOGIN_CSRF_ACTION = "/admin/login"
SESSION_TTL_SECONDS = 8 * 3600


def _session_key(request: Request) -> bytes:
    cfg = request.app.state.serve_config
    s_key = cfg.session_signing_key
    if not s_key:
        raise RuntimeError("session_signing_key is empty; admin UI disabled")
    # token_urlsafe(32) is pure ASCII; the config validator enforces ≥ 32 chars.
    return s_key.encode("ascii") if isinstance(s_key, str) else s_key


def _cookie_secure(request: Request) -> bool:
    return bool(getattr(request.app.state.serve_config, "cookie_secure", True))


router = APIRouter()


# The login form's CSRF token is bound to (user_id=0, "/admin/login") and is
# therefore identical for every visitor of a given server install. That's
# acceptable for the unauthenticated login form — login CSRF (forcing a victim
# to sign in to the attacker's account) is a different threat model from
# action CSRF on authenticated forms, and SameSite=Lax on the response cookie
# is the real defense once the session exists. Do NOT replicate user_id=0 for
# any post-login form; bind to the real user_id (see dashboard_router).
@router.get("/login", response_class=HTMLResponse)
def get_login(request: Request) -> HTMLResponse:
    s_key = _session_key(request)
    csrf = make_csrf_token(user_id=0, action=LOGIN_CSRF_ACTION, key=s_key)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"csrf_token": csrf, "current_user": None, "flashes": []},
    )


@router.post("/login")
def post_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
):
    s_key = _session_key(request)
    try:
        verify_csrf_token(csrf_token, user_id=0, action=LOGIN_CSRF_ACTION, key=s_key)
    except CSRFError:
        return HTMLResponse("CSRF token missing or invalid", status_code=400)

    pool = request.app.state.pool
    auth_cfg = request.app.state.auth_config
    client_ip = resolve_client_ip(
        socket_peer=request.client.host if request.client else None,
        xff_header=request.headers.get("X-Forwarded-For"),
        trusted_proxies=auth_cfg.trusted_proxies_parsed,
        max_hops=auth_cfg.trusted_proxies_max_hops,
    )
    with pool.connection() as conn:
        # Rate-limit check before credential verification (no argon2 cost on blocked IPs).
        try:
            _check_login_rate_limits(conn, username, client_ip, cfg=auth_cfg)
        except RateLimited:
            csrf = make_csrf_token(user_id=0, action=LOGIN_CSRF_ACTION, key=s_key)
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "csrf_token": csrf,
                    "error": "Too many login attempts. Please try again later.",
                    "current_user": None,
                    "flashes": [],
                },
                status_code=429,
            )
        # Collapse "wrong password" and "valid creds but not admin" into a
        # single 401 to avoid leaking whether a username exists with a known
        # password (the attacker can't distinguish "found a non-admin user" from
        # "wrong password"). The NotAnAdmin case is logged server-side so a
        # legitimate non-admin user mistyping the admin URL is still visible to
        # operators.
        try:
            admin = authenticate_admin(conn, username=username, password=password)
        except AuthenticationFailed:
            _record_login_attempt(conn, username, client_ip, "failure")
            csrf = make_csrf_token(user_id=0, action=LOGIN_CSRF_ACTION, key=s_key)
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "csrf_token": csrf,
                    "error": "Invalid credentials.",
                    "current_user": None,
                    "flashes": [],
                },
                status_code=401,
            )
        except NotAnAdmin:
            _record_login_attempt(conn, username, client_ip, "failure")
            _log.warning(
                "non-admin login attempt at /admin/login: username=%r client_ip=%s",
                username,
                client_ip,
            )
            csrf = make_csrf_token(user_id=0, action=LOGIN_CSRF_ACTION, key=s_key)
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "csrf_token": csrf,
                    "error": "Invalid credentials.",
                    "current_user": None,
                    "flashes": [],
                },
                status_code=401,
            )
        _record_login_attempt(conn, username, client_ip, "success")

    now = int(time.time())
    token = encode_session_token(
        SessionPayload(user_id=admin.id, issued_at=now, exp=now + SESSION_TTL_SECONDS),
        key=s_key,
    )
    response = RedirectResponse("/admin/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        path="/admin",
        secure=_cookie_secure(request),
        httponly=True,
        samesite="lax",
    )
    return response


LOGOUT_CSRF_ACTION = "/admin/logout"


@router.post("/logout")
def post_logout(
    request: Request,
    csrf_token: str = Form(""),
    admin: AdminUser = require_admin_session(),
):
    s_key = _session_key(request)
    try:
        verify_csrf_token(csrf_token, user_id=admin.id, action=LOGOUT_CSRF_ACTION, key=s_key)
    except CSRFError:
        return HTMLResponse("CSRF token missing or invalid", status_code=400)

    response = RedirectResponse("/admin/login", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        "",
        max_age=0,
        path="/admin",
        secure=_cookie_secure(request),
        httponly=True,
        samesite="lax",
    )
    return response
