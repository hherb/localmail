# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Admin user-management HTML screens (2A.4).

Thin server-rendered HTMX router mounted at /admin. Renders Jinja fragments and
dispatches to api/admin/users; all form parsing lives in user_forms. Mutating
routes verify a method-bound CSRF token (X-CSRF-Token header) via check_csrf.
JSON machine clients use /v1/admin/users.

The last-admin guard is enforced by the service; the self-action guard is
enforced here (only the router knows the caller's identity). The edit screen
also renders unsafe controls `disabled` (UX only — POSTing anyway still hits
the guards).
"""
from __future__ import annotations

from pathlib import Path

import psycopg
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from localmail.api.admin import users as svc
from localmail.api.admin.auth import AdminUser, UserNotFound
from localmail.api.ids import parse_int_id
from localmail.serve.admin import user_forms as forms
from localmail.serve.admin.csrf import check_csrf, csrf_token_context, session_signing_key
from localmail.serve.admin.dependencies import require_admin_session

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter()


def _base_context(request: Request, admin: AdminUser) -> dict:
    s_key = session_signing_key(request)
    return {
        "current_user": admin,
        "flashes": [],
        **csrf_token_context(user_id=admin.id, key=s_key),
    }


def _detail_and_flags(
    conn: psycopg.Connection, user_id: int, self_id: int,
) -> tuple[svc.UserDetail, dict[str, bool]]:
    """(detail, flags) for the edit screen. Raises UserNotFound."""
    detail = svc.get_user(conn, user_id)
    flags = svc.action_flags(
        target_is_active_admin=(detail.is_admin and not detail.disabled),
        active_admin_count=svc.active_admin_count(conn),
        is_self=(user_id == self_id),
    )
    return detail, flags


def _status_fragment(
    request: Request, admin: AdminUser, conn: psycopg.Connection, user_id: int, *,
    error: str | None = None, status: int = 200,
) -> HTMLResponse:
    detail, flags = _detail_and_flags(conn, user_id, admin.id)
    ctx = _base_context(request, admin)
    ctx.update({"detail": detail, "flags": flags, "error": error})
    return templates.TemplateResponse(
        request=request, name="users/_status.html", context=ctx, status_code=status)


def _render_grants(
    request: Request, admin: AdminUser, detail: svc.UserDetail, flags: dict[str, bool],
) -> HTMLResponse:
    ctx = _base_context(request, admin)
    ctx.update({"detail": detail, "flags": flags})
    return templates.TemplateResponse(
        request=request, name="users/_grants.html", context=ctx)


def _message(request: Request, admin: AdminUser, message: str) -> HTMLResponse:
    ctx = _base_context(request, admin)
    ctx["message"] = message
    return templates.TemplateResponse(
        request=request, name="users/_message.html", context=ctx)


def _rerender_create_error(
    request: Request,
    admin: AdminUser,
    raw: dict,
    err: forms.FormError | svc.UserFieldError,
) -> HTMLResponse:
    ctx = _base_context(request, admin)
    ctx.update({
        "values": {"username": raw.get("username", ""),
                   "is_admin": bool(raw.get("is_admin"))},
        "field_errors": forms.field_errors_from(err),
    })
    return templates.TemplateResponse(
        request=request, name="users/_create_fields.html", context=ctx, status_code=400)


@router.get("/users", response_class=HTMLResponse)
def list_users(request: Request, admin: AdminUser = require_admin_session()) -> HTMLResponse:
    pool = request.app.state.pool
    with pool.connection() as conn:
        rows = svc.list_users(conn)
    ctx = _base_context(request, admin)
    ctx["users"] = rows
    return templates.TemplateResponse(request=request, name="users/list.html", context=ctx)


@router.get("/users/new", response_class=HTMLResponse)
def new_user_form(request: Request, admin: AdminUser = require_admin_session()) -> HTMLResponse:
    ctx = _base_context(request, admin)
    ctx.update({"values": {"username": "", "is_admin": False}, "field_errors": {}})
    return templates.TemplateResponse(request=request, name="users/new.html", context=ctx)


@router.post("/users")
async def create_user(
    request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    check_csrf(request, admin, x_csrf_token, "/admin/users")
    raw = dict(await request.form())
    try:
        kwargs = forms.form_to_create_kwargs(raw)
    except forms.FormError as e:
        return _rerender_create_error(request, admin, raw, e)
    pool = request.app.state.pool

    def _create() -> int:
        with pool.connection() as conn:
            return svc.create_user(conn, **kwargs)

    try:
        uid = await run_in_threadpool(_create)
    except svc.UserFieldError as e:
        return _rerender_create_error(request, admin, raw, e)
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = f"/admin/users/{uid}"
    return resp


@router.get("/users/{user_id}", response_class=HTMLResponse)
def edit_user_form(
    user_id: int, request: Request, admin: AdminUser = require_admin_session(),
) -> HTMLResponse:
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            detail, flags = _detail_and_flags(conn, user_id, admin.id)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
    ctx = _base_context(request, admin)
    ctx.update({"detail": detail, "flags": flags, "error": None})
    return templates.TemplateResponse(request=request, name="users/edit.html", context=ctx)


@router.post("/users/{user_id}/admin-toggle", response_class=HTMLResponse)
def admin_toggle(
    user_id: int, request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token, f"/admin/users/{user_id}/admin-toggle")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            detail = svc.get_user(conn, user_id)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
        target = not detail.is_admin
        if not target and user_id == admin.id:
            return _status_fragment(request, admin, conn, user_id,
                                    error="you cannot revoke your own admin")
        try:
            svc.set_admin(conn, user_id, target)
        except svc.LastAdminError as e:
            return _status_fragment(request, admin, conn, user_id, error=str(e))
        return _status_fragment(request, admin, conn, user_id)


@router.post("/users/{user_id}/disable-toggle", response_class=HTMLResponse)
def disable_toggle(
    user_id: int, request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token, f"/admin/users/{user_id}/disable-toggle")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            detail = svc.get_user(conn, user_id)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
        try:
            svc.set_disabled(conn, user_id, not detail.disabled)
        except svc.LastAdminError as e:
            return _status_fragment(request, admin, conn, user_id, error=str(e))
        return _status_fragment(request, admin, conn, user_id)


@router.post("/users/{user_id}/password", response_class=HTMLResponse)
async def store_password(
    user_id: int, request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token, f"/admin/users/{user_id}/password")
    raw = await request.form()
    password = str(raw.get("password", ""))
    pool = request.app.state.pool

    def _store() -> None:
        with pool.connection() as conn:
            svc.set_password(conn, user_id, password)

    try:
        await run_in_threadpool(_store)
    except UserNotFound:
        raise HTTPException(status_code=404, detail="user not found")
    except svc.UserFieldError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _message(request, admin, "Password updated.")


@router.post("/users/{user_id}/revoke-sessions", response_class=HTMLResponse)
def revoke_sessions(
    user_id: int, request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token, f"/admin/users/{user_id}/revoke-sessions")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.revoke_sessions(conn, user_id)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
    return _message(request, admin, "Sessions revoked.")


@router.post("/users/{user_id}/grants", response_class=HTMLResponse)
async def set_grant(
    user_id: int, request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token, f"/admin/users/{user_id}/grants")
    raw = await request.form()
    account_id = parse_int_id(str(raw.get("account_id", "")), field="account_id")
    granted = str(raw.get("granted", "")).lower() == "true"
    pool = request.app.state.pool

    def _apply() -> tuple[svc.UserDetail, dict[str, bool]]:
        with pool.connection() as conn:
            svc.set_grant(conn, user_id, account_id, granted)
            return _detail_and_flags(conn, user_id, admin.id)

    try:
        detail, flags = await run_in_threadpool(_apply)
    except UserNotFound:
        raise HTTPException(status_code=404, detail="user not found")
    except svc.UserFieldError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _render_grants(request, admin, detail, flags)


@router.post("/users/{user_id}/delete")
def delete_user(
    user_id: int, request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    check_csrf(request, admin, x_csrf_token, f"/admin/users/{user_id}/delete")
    if user_id == admin.id:
        ctx = _base_context(request, admin)
        ctx["error"] = "You cannot delete your own account."
        return templates.TemplateResponse(
            request=request, name="users/_delete_blocked.html", context=ctx,
            status_code=409)
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.delete_user(conn, user_id)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
        except svc.LastAdminError as e:
            ctx = _base_context(request, admin)
            ctx["error"] = str(e)
            return templates.TemplateResponse(
                request=request, name="users/_delete_blocked.html", context=ctx,
                status_code=409)
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = "/admin/users"
    return resp
