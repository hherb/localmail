# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""HTTP routes for /v1/admin/api-keys.

Thin wrapper over `localmail.api.admin.api_keys`. Every route requires an admin
credential; every mutating route validates a method-bound CSRF token. IDs are
strings on the wire (#33), and the id of an API key is its principal's id.

Guard mapping: validation (`ApiKeyFieldError`) → 400; absence
(`ApiKeyNotFound`) → 404.

The raw key appears in exactly one response — the 201 from create — and in no
other route, because nothing can recover it afterwards.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel

from localmail.api.admin import api_keys as svc
from localmail.api.admin.auth import AdminUser
from localmail.api.ids import parse_int_id
from localmail.serve.admin.csrf import check_csrf
from localmail.serve.admin.dependencies import require_admin

router = APIRouter(tags=["admin-api-keys"])


class _KeyIn(BaseModel):
    name: str
    account_ids: list[str] = []


class _GrantIn(BaseModel):
    account_id: str
    granted: bool


def _summary_dict(k: svc.ApiKeySummary) -> dict:
    return {
        "id": str(k.user_id),
        "name": k.name,
        "has_key": k.has_key,
        "key_created_at": k.key_created_at.isoformat() if k.key_created_at else None,
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "disabled": k.disabled,
        "account_names": k.account_names,
    }


@router.get("/api-keys")
def list_api_keys(request: Request, admin: AdminUser = require_admin()) -> dict:
    pool = request.app.state.pool
    with pool.connection() as conn:
        rows = svc.list_keys(conn)
    return {"api_keys": [_summary_dict(r) for r in rows]}


@router.post("/api-keys", status_code=201)
def create_api_key(
    body: _KeyIn, request: Request,
    admin: AdminUser = require_admin(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> dict:
    check_csrf(request, admin, x_csrf_token, "/v1/admin/api-keys")
    account_ids = [parse_int_id(a, field="account_id") for a in body.account_ids]
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            created = svc.create_key(
                conn, name=body.name, account_ids=account_ids
            )
        except svc.ApiKeyFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return {
        "id": str(created.user_id),
        "name": created.name,
        "api_key": created.raw_key,
    }


@router.delete("/api-keys/{key_id}", status_code=204)
def revoke_api_key(
    key_id: str, request: Request,
    admin: AdminUser = require_admin(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    check_csrf(request, admin, x_csrf_token, f"/v1/admin/api-keys/{key_id}")
    uid = parse_int_id(key_id, field="key_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.revoke_key(conn, uid)
        except svc.ApiKeyNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
    return Response(status_code=204)


@router.delete("/api-keys/{key_id}/principal", status_code=204)
def delete_api_key_principal(
    key_id: str, request: Request,
    admin: AdminUser = require_admin(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    check_csrf(
        request, admin, x_csrf_token, f"/v1/admin/api-keys/{key_id}/principal"
    )
    uid = parse_int_id(key_id, field="key_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.delete_key_principal(conn, uid)
        except svc.ApiKeyNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
    return Response(status_code=204)


@router.post("/api-keys/{key_id}/grants")
def set_api_key_grant(
    key_id: str, body: _GrantIn, request: Request,
    admin: AdminUser = require_admin(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> dict:
    check_csrf(request, admin, x_csrf_token, f"/v1/admin/api-keys/{key_id}/grants")
    uid = parse_int_id(key_id, field="key_id")
    account_id = parse_int_id(body.account_id, field="account_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.set_grant(conn, uid, account_id, body.granted)
        except svc.ApiKeyNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
        except svc.ApiKeyFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
        conn.commit()
    return {"ok": True}
