# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""HTTP routes for /v1/admin/users (Sub-plan 2A.4).

Thin wrapper over `localmail.api.admin.users`. Every route requires an admin
session; every mutating route validates a method-bound CSRF token from the
`X-CSRF-Token` header. IDs are strings on the wire (#33).

Guard mapping (mirrors accounts): validation (`UserFieldError`) → 400; absence
(`UserNotFound`) → 404; lock-out guards (`LastAdminError`) → 409; the
self-action rule (`uid == admin.id`) is enforced inline as a 409 — structured,
actionable conflicts, never an opaque 500.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from localmail.api.admin import users as svc
from localmail.api.admin.auth import AdminUser, UserNotFound
from localmail.api.ids import parse_int_id
from localmail.serve.admin.csrf import check_csrf
from localmail.serve.admin.dependencies import require_admin

router = APIRouter(tags=["admin-users"])


class _UserIn(BaseModel):
    username: str
    password: str = Field(min_length=1)
    is_admin: bool = False


class _UserPatch(BaseModel):
    is_admin: bool | None = None
    disabled: bool | None = None


class _PasswordIn(BaseModel):
    password: str = Field(min_length=1)


class _GrantIn(BaseModel):
    account_id: str
    granted: bool


def _summary_dict(u: svc.UserSummary) -> dict:
    return {
        "id": str(u.id),
        "username": u.username,
        "is_admin": u.is_admin,
        "disabled": u.disabled,
        "is_service": u.is_service,
        "created_at": u.created_at.isoformat(),
    }


def _detail_dict(u: svc.UserDetail) -> dict:
    return {
        "id": str(u.id),
        "username": u.username,
        "is_admin": u.is_admin,
        "disabled": u.disabled,
        "is_service": u.is_service,
        "created_at": u.created_at.isoformat(),
        "account_grants": [
            {"account_id": str(g.account_id), "account_name": g.account_name,
             "granted": g.granted}
            for g in u.account_grants
        ],
    }


@router.get("/users")
def list_users(request: Request, admin: AdminUser = require_admin()) -> dict:
    pool = request.app.state.pool
    with pool.connection() as conn:
        rows = svc.list_users(conn)
    return {"users": [_summary_dict(r) for r in rows]}


@router.post("/users", status_code=201)
def create_user(
    body: _UserIn, request: Request,
    admin: AdminUser = require_admin(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> dict:
    check_csrf(request, admin, x_csrf_token, "/v1/admin/users")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            uid = svc.create_user(
                conn, username=body.username, password=body.password,
                is_admin=body.is_admin)
        except svc.UserFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # read-your-writes in the same txn; the roster is small so the list scan is fine
        summary = next(r for r in svc.list_users(conn) if r.id == uid)
    return _summary_dict(summary)


@router.get("/users/{user_id}")
def get_user(
    user_id: str, request: Request,
    admin: AdminUser = require_admin(),
) -> dict:
    uid = parse_int_id(user_id, field="user_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            detail = svc.get_user(conn, uid)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
    return _detail_dict(detail)


@router.patch("/users/{user_id}")
def patch_user(
    user_id: str, body: _UserPatch, request: Request,
    admin: AdminUser = require_admin(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> dict:
    uid = parse_int_id(user_id, field="user_id")
    check_csrf(request, admin, x_csrf_token, f"/v1/admin/users/{uid}")
    if body.is_admin is False and uid == admin.id:
        raise HTTPException(status_code=409, detail="you cannot revoke your own admin")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            if body.is_admin is not None:
                svc.set_admin(conn, uid, body.is_admin)
            if body.disabled is not None:
                svc.set_disabled(conn, uid, body.disabled)
            detail = svc.get_user(conn, uid)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
        except svc.LastAdminError as e:
            raise HTTPException(status_code=409, detail=str(e))
    return _detail_dict(detail)


@router.post("/users/{user_id}/password", status_code=204)
def post_password(
    user_id: str, body: _PasswordIn, request: Request,
    admin: AdminUser = require_admin(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    uid = parse_int_id(user_id, field="user_id")
    check_csrf(request, admin, x_csrf_token, f"/v1/admin/users/{uid}/password")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.set_password(conn, uid, body.password)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
        except svc.UserFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return Response(status_code=204)


@router.post("/users/{user_id}/grants")
def post_grant(
    user_id: str, body: _GrantIn, request: Request,
    admin: AdminUser = require_admin(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> dict:
    uid = parse_int_id(user_id, field="user_id")
    check_csrf(request, admin, x_csrf_token, f"/v1/admin/users/{uid}/grants")
    aid = parse_int_id(body.account_id, field="account_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.set_grant(conn, uid, aid, body.granted)
            detail = svc.get_user(conn, uid)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
        except svc.UserFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return _detail_dict(detail)


@router.post("/users/{user_id}/revoke-sessions", status_code=204)
def post_revoke_sessions(
    user_id: str, request: Request,
    admin: AdminUser = require_admin(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    uid = parse_int_id(user_id, field="user_id")
    check_csrf(request, admin, x_csrf_token, f"/v1/admin/users/{uid}/revoke-sessions")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.revoke_sessions(conn, uid)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
    return Response(status_code=204)


@router.delete("/users/{user_id}", status_code=204)
def delete_user(
    user_id: str, request: Request,
    admin: AdminUser = require_admin(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    uid = parse_int_id(user_id, field="user_id")
    check_csrf(request, admin, x_csrf_token, f"/v1/admin/users/{uid}")
    if uid == admin.id:
        raise HTTPException(status_code=409, detail="you cannot delete your own account")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.delete_user(conn, uid)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="user not found")
        except svc.LastAdminError as e:
            raise HTTPException(status_code=409, detail=str(e))
    return Response(status_code=204)
