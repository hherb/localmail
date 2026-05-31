"""HTTP routes for /v1/admin/accounts (Sub-plan 2A).

Thin wrapper over `localmail.api.admin.accounts`. Every route requires an
admin session (cookie + HMAC + is_admin = TRUE); every mutating route
additionally validates a CSRF token bound to (user_id, action).

The CSRF token is supplied as the `X-CSRF-Token` request header on JSON
routes — forms are not used here because the admin UI calls these endpoints
from htmx-driven JS, never a multipart submit. The token is bound to the
URL path (the "action") so a token minted for `/v1/admin/accounts` cannot
be replayed against `/v1/admin/accounts/{id}/password`.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from localmail.api.admin import accounts as svc
from localmail.api.admin.auth import AdminUser
from localmail.api.errors import NotFound
from localmail.api.ids import parse_int_id
from localmail.serve.admin.csrf import check_csrf
from localmail.serve.admin.dependencies import require_admin_session


router = APIRouter(tags=["admin-accounts"])


# ---------- request/response models ----------

class _AccountIn(BaseModel):
    name: str
    email_address: str  # required: column is NOT NULL in 0001_init.sql
    auth_method: Literal['password', 'oauth2', 'archive']
    imap_host: str | None = None
    imap_port: int | None = None
    oauth_provider: Literal['gmail'] | None = None
    folder_allow: list[str] | None = None
    folder_deny: list[str] | None = None
    folder_deny_flags: list[str] | None = None


class _AccountPatch(BaseModel):
    email_address: str | None = None
    auth_method: Literal['password', 'oauth2', 'archive'] | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    oauth_provider: Literal['gmail'] | None = None
    folder_allow: list[str] | None = None
    folder_deny: list[str] | None = None
    folder_deny_flags: list[str] | None = None
    sync_enabled: bool | None = None


class _PasswordIn(BaseModel):
    password: str = Field(min_length=1)


# ---------- helpers ----------

def _summary_dict(s: svc.AccountSummary) -> dict:
    return {
        'id': str(s.id),
        'name': s.name,
        'email_address': s.email_address,
        'auth_method': s.auth_method,
        'sync_enabled': s.sync_enabled,
    }


def _account_dict(a: svc.Account) -> dict:
    return {
        'id': str(a.id),
        'name': a.name,
        'email_address': a.email_address,
        'auth_method': a.auth_method,
        'oauth_provider': a.oauth_provider,
        'imap_host': a.imap_host,
        'imap_port': a.imap_port,
        'folder_allow': a.folder_allow,
        'folder_deny': a.folder_deny,
        'folder_deny_flags': a.folder_deny_flags,
        'sync_enabled': a.sync_enabled,
        'created_at': a.created_at.isoformat(),
        'updated_at': a.updated_at.isoformat(),
    }


# ---------- routes ----------

@router.get("/accounts")
def list_accounts(
    request: Request,
    admin: AdminUser = require_admin_session(),
) -> dict:
    pool = request.app.state.pool
    with pool.connection() as conn:
        rows = svc.list_accounts(conn)
    return {'accounts': [_summary_dict(r) for r in rows]}


@router.post("/accounts", status_code=201)
def create_account(
    body: _AccountIn,
    request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> dict:
    check_csrf(request, admin, x_csrf_token, "/v1/admin/accounts")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            acct = svc.create_account(
                conn,
                name=body.name,
                email_address=body.email_address,
                auth_method=body.auth_method,
                imap_host=body.imap_host,
                imap_port=body.imap_port,
                oauth_provider=body.oauth_provider,
                folder_allow=body.folder_allow,
                folder_deny=body.folder_deny,
                folder_deny_flags=body.folder_deny_flags,
            )
        except svc.AccountFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return _account_dict(acct)


@router.get("/accounts/{account_id}")
def get_account(
    account_id: str,
    request: Request,
    admin: AdminUser = require_admin_session(),
) -> dict:
    aid = parse_int_id(account_id, field="account_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            acct = svc.get_account(conn, aid)
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
    return _account_dict(acct)


@router.patch("/accounts/{account_id}")
def patch_account(
    account_id: str,
    body: _AccountPatch,
    request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> dict:
    aid = parse_int_id(account_id, field="account_id")
    check_csrf(request, admin, x_csrf_token, f"/v1/admin/accounts/{aid}")
    fields = body.model_dump(exclude_unset=True)
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            acct = svc.update_account(conn, aid, **fields)
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
        except svc.AccountFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return _account_dict(acct)


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(
    account_id: str,
    request: Request,
    force: bool = Query(False),
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    aid = parse_int_id(account_id, field="account_id")
    check_csrf(request, admin, x_csrf_token, f"/v1/admin/accounts/{aid}")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.delete_account(conn, aid, force=force)
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
        except svc.AccountInUse as e:
            raise HTTPException(status_code=409, detail=str(e))
    return Response(status_code=204)


@router.post("/accounts/{account_id}/password", status_code=204)
def post_password(
    account_id: str,
    body: _PasswordIn,
    request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    aid = parse_int_id(account_id, field="account_id")
    check_csrf(
        request, admin, x_csrf_token,
        f"/v1/admin/accounts/{aid}/password",
    )
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            account = svc.get_account(conn, aid)
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
    # Keyring write happens outside the connection — it's not a DB op.
    try:
        svc.store_password(account, body.password)
    except svc.AccountFieldError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Bump updated_at so the daemon's hot-reload notices the credential change.
    with pool.connection() as conn:
        svc.touch_account_updated_at(conn, account.id)
    return Response(status_code=204)


@router.post("/accounts/{account_id}/test-connection")
def test_connection(
    account_id: str,
    request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> dict:
    """URL kept as `test-connection` per the design doc; backed by
    ``accounts.probe_connection`` (renamed so pytest's auto-collector
    doesn't pick up the ``test_`` prefix as a test function)."""
    aid = parse_int_id(account_id, field="account_id")
    check_csrf(
        request, admin, x_csrf_token,
        f"/v1/admin/accounts/{aid}/test-connection",
    )
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            folders = svc.probe_connection(conn, aid)
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
        except svc.AccountFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return {
        'folders': [
            {'name': f.name, 'flags': list(f.flags)} for f in folders
        ]
    }
