# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Admin API-key HTML screens.

Thin server-rendered HTMX router mounted at /admin. One screen: name the
consumer, tick the accounts it may read, receive the key once. JSON machine
clients use /v1/admin/api-keys.

The raw key is rendered by exactly one fragment — the create response — because
it is stored as a SHA-256 and cannot be recovered afterwards.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
from fastapi import APIRouter, Header, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from localmail.api.admin import api_keys as svc
from localmail.api.admin.auth import AdminUser
from localmail.api.ids import parse_int_id
from localmail.serve.admin import api_key_forms as forms
from localmail.serve.admin.csrf import check_csrf, csrf_token_context, session_signing_key
from localmail.serve.admin.dependencies import require_admin_session

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter()


def _base_context(request: Request, admin: AdminUser) -> dict:
    return {
        "current_user": admin,
        "flashes": [],
        **csrf_token_context(user_id=admin.id, key=session_signing_key(request)),
    }


def _accounts(conn: psycopg.Connection) -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM accounts ORDER BY name")
        return [(int(i), n) for i, n in cur.fetchall()]


def _list_context(request: Request, admin: AdminUser) -> dict:
    pool = request.app.state.pool
    with pool.connection() as conn:
        keys = svc.list_keys(conn)
        accounts = _accounts(conn)
    ctx = _base_context(request, admin)
    ctx.update({"keys": keys, "accounts": accounts, "field_errors": {}, "created": None})
    return ctx


@router.get("/api-keys", response_class=HTMLResponse)
def list_api_keys(
    request: Request, admin: AdminUser = require_admin_session(),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name="api_keys/list.html",
        context=_list_context(request, admin),
    )


@router.post("/api-keys", response_class=HTMLResponse)
async def create_api_key(
    request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token, "/admin/api-keys")
    form = await request.form()
    try:
        kwargs = forms.form_to_create_kwargs(
            form.get("name"), [str(v) for v in form.getlist("account_ids")]
        )
    except forms.FormError as e:
        ctx = _base_context(request, admin)
        ctx.update({"field_errors": forms.field_errors_from(e), "created": None})
        return templates.TemplateResponse(
            request=request, name="api_keys/_created.html", context=ctx,
            status_code=400,
        )
    pool = request.app.state.pool

    def _create() -> svc.CreatedKey:
        with pool.connection() as conn:
            created = svc.create_key(conn, **kwargs)
            conn.commit()
            return created

    try:
        created = await run_in_threadpool(_create)
    except svc.ApiKeyFieldError as e:
        ctx = _base_context(request, admin)
        ctx.update({"field_errors": forms.field_errors_from(e), "created": None})
        return templates.TemplateResponse(
            request=request, name="api_keys/_created.html", context=ctx,
            status_code=400,
        )
    ctx = _base_context(request, admin)
    ctx.update({"created": created, "field_errors": {}})
    return templates.TemplateResponse(
        request=request, name="api_keys/_created.html", context=ctx
    )


@router.post("/api-keys/{key_id}/revoke", response_class=HTMLResponse)
def revoke_api_key(
    key_id: str, request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token, f"/admin/api-keys/{key_id}/revoke")
    uid = parse_int_id(key_id, field="key_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.revoke_key(conn, uid)
            conn.commit()
        except svc.ApiKeyNotFound:
            # Already gone IS success for an idempotent delete, and re-rendering
            # the table shows the operator the current truth. Raising instead
            # would leave the button inert under htmx, which is the #148 defect.
            conn.rollback()
    return templates.TemplateResponse(
        request=request, name="api_keys/_table.html",
        context=_list_context(request, admin),
    )


@router.post("/api-keys/{key_id}/delete", response_class=HTMLResponse)
def delete_api_key_principal(
    key_id: str, request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token, f"/admin/api-keys/{key_id}/delete")
    uid = parse_int_id(key_id, field="key_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.delete_key_principal(conn, uid)
            conn.commit()
        except svc.ApiKeyNotFound:
            # Idempotent, for the reason given in revoke_api_key above.
            conn.rollback()
    return templates.TemplateResponse(
        request=request, name="api_keys/_table.html",
        context=_list_context(request, admin),
    )
