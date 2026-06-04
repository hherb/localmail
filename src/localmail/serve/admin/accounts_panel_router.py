"""Admin account-management HTML screens (2A.3).

Thin server-rendered HTMX router mounted at /admin. Renders Jinja fragments
and dispatches to the api/admin/accounts service; all form parsing lives in
account_forms. Mutating routes verify a method-bound CSRF token (X-CSRF-Token
header) via the shared check_csrf. JSON machine clients use /v1/admin/accounts.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from localmail.api.admin import accounts as svc
from localmail.api.admin import oauth as oauth_svc
from localmail.api.admin.auth import AdminUser
from localmail.api.errors import NotFound
from localmail.serve.admin import account_forms as forms
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


@router.get("/accounts", response_class=HTMLResponse)
def list_accounts(
    request: Request, admin: AdminUser = require_admin_session()
) -> HTMLResponse:
    pool = request.app.state.pool
    with pool.connection() as conn:
        rows = svc.list_accounts(conn)
    ctx = _base_context(request, admin)
    ctx["accounts"] = rows
    return templates.TemplateResponse(
        request=request, name="accounts/list.html", context=ctx
    )


def _blank_form_values() -> dict:
    """Fresh blank values for the create form (own `set()` per call — never a
    shared mutable)."""
    return {
        "name": "", "email_address": "", "auth_method": "password",
        "oauth_provider": "", "imap_host": "", "imap_port": "",
        "folder_allow": "", "folder_deny": "", "deny_flags_checked": set(),
        "sync_enabled": True,
    }


def _form_context(
    request: Request,
    admin: AdminUser,
    *,
    values: dict,
    account_id: int | None,
    field_errors: dict | None = None,
    oauth: str | None = None,
) -> dict:
    ctx = _base_context(request, admin)
    ctx.update({
        "values": values,
        "account_id": account_id,
        "field_errors": field_errors or {},
        "deny_flags": forms.DENY_FLAGS,
        "deny_flags_checked": values.get("deny_flags_checked", set()),
        "oauth": oauth,
    })
    return ctx


def _rerender_form_error(
    request: Request,
    admin: AdminUser,
    raw: dict,
    deny: list,
    err: forms.FormError | svc.AccountFieldError,
    *,
    account_id: int | None,
) -> HTMLResponse:
    """Re-render the field fragment (400) with inline errors and submitted values preserved."""
    values = {k: raw.get(k, "") for k in (
        "name", "email_address", "auth_method", "oauth_provider",
        "imap_host", "imap_port", "folder_allow", "folder_deny",
    )}
    values["deny_flags_checked"] = set(deny)
    ctx = _form_context(
        request, admin, values=values, account_id=account_id,
        field_errors=forms.field_errors_from(err),
    )
    return templates.TemplateResponse(
        request=request, name="accounts/_form_fields.html", context=ctx,
        status_code=400,
    )


@router.get("/accounts/new", response_class=HTMLResponse)
def new_account_form(
    request: Request, admin: AdminUser = require_admin_session()
) -> HTMLResponse:
    ctx = _form_context(request, admin, values=_blank_form_values(), account_id=None)
    return templates.TemplateResponse(
        request=request, name="accounts/form.html", context=ctx,
    )


@router.post("/accounts")
async def create_account(
    request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    check_csrf(request, admin, x_csrf_token, "/admin/accounts")
    raw = await request.form()
    deny: list[str] = [v for v in raw.getlist("deny_flags") if isinstance(v, str)]
    raw_dict = dict(raw)
    try:
        kwargs = forms.form_to_create_kwargs(raw_dict, deny_flags_selected=deny)
    except forms.FormError as e:
        return _rerender_form_error(request, admin, raw_dict, deny, e, account_id=None)
    pool = request.app.state.pool

    def _create() -> svc.Account:
        with pool.connection() as conn:
            return svc.create_account(conn, **kwargs)

    try:
        acct = await run_in_threadpool(_create)
    except svc.AccountFieldError as e:
        return _rerender_form_error(request, admin, raw_dict, deny, e, account_id=None)
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = f"/admin/accounts/{acct.id}"
    return resp


@router.get("/accounts/{account_id}", response_class=HTMLResponse)
def edit_account_form(
    account_id: int, request: Request,
    oauth: str | None = None,
    admin: AdminUser = require_admin_session(),
) -> HTMLResponse:
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            acct = svc.get_account(conn, account_id)
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
    values = forms.account_to_form_values(acct)
    ctx = _form_context(request, admin, values=values, account_id=account_id,
                        oauth=oauth)
    return templates.TemplateResponse(
        request=request, name="accounts/form.html", context=ctx
    )


@router.post("/accounts/{account_id}/password", response_class=HTMLResponse)
async def store_password(
    account_id: int, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token,
               f"/admin/accounts/{account_id}/password")
    raw = await request.form()
    password = str(raw.get("password", ""))
    if not password:
        raise HTTPException(status_code=400, detail="password must not be blank")
    pool = request.app.state.pool

    def _store() -> None:
        with pool.connection() as conn:
            account = svc.get_account(conn, account_id)
        svc.store_password(account, password)
        with pool.connection() as conn:
            svc.touch_account_updated_at(conn, account_id)

    try:
        await run_in_threadpool(_store)
    except NotFound:
        raise HTTPException(status_code=404, detail="account not found")
    except svc.AccountFieldError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return templates.TemplateResponse(
        request=request, name="accounts/_secret_status.html",
        context=_base_context(request, admin),
    )


@router.post("/accounts/{account_id}/test-connection", response_class=HTMLResponse)
def test_connection(
    account_id: int, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token,
               f"/admin/accounts/{account_id}/test-connection")
    secrets_path = getattr(request.app.state, "gmail_client_secrets_file", None)
    ctx = _base_context(request, admin)
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            folders = svc.probe_connection(
                conn, account_id, gmail_client_secrets=secrets_path
            )
            ctx["folders"] = folders
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
        except svc.AccountFieldError as e:
            ctx["error"] = str(e)
        except svc.CONNECT_FAILURE_EXC_TYPES as e:
            # Genuine connect failure (wrong host/port/password, DNS, TLS) — the
            # whole point of "Test connection" is to report it inline, not 500 (#158).
            ctx["error"] = str(e)
    return templates.TemplateResponse(
        request=request, name="accounts/_test_result.html", context=ctx
    )


@router.post("/accounts/{account_id}/sync-toggle", response_class=HTMLResponse)
def sync_toggle(
    account_id: int, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token,
               f"/admin/accounts/{account_id}/sync-toggle")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            acct = svc.get_account(conn, account_id)
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
        if acct.auth_method == "archive":
            raise HTTPException(status_code=400, detail="archive accounts do not sync")
        updated = svc.update_account(
            conn, account_id, sync_enabled=not acct.sync_enabled
        )
    ctx = _base_context(request, admin)
    ctx["acct"] = updated
    return templates.TemplateResponse(
        request=request, name="accounts/_row.html", context=ctx
    )


_HTTP_SEE_OTHER = 303


@router.post("/accounts/{account_id}/oauth/start")
def oauth_start(
    account_id: int, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> RedirectResponse:
    check_csrf(request, admin, x_csrf_token,
               f"/admin/accounts/{account_id}/oauth/start")
    cfg = request.app.state.serve_config
    signing_key = cfg.state_signing_key
    signing_key = signing_key.encode("ascii") if isinstance(signing_key, str) else signing_key
    secrets_path = getattr(request.app.state, "gmail_client_secrets_file", None)
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            url = oauth_svc.start_oauth(
                conn, account_id,
                admin_user_id=admin.id,
                signing_key=signing_key,
                redirect_uri=cfg.oauth_callback_url,
                client_secrets_file=secrets_path,
            )
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
        except svc.AccountFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except oauth_svc.OAuthNotConfigured as e:
            raise HTTPException(status_code=503, detail=str(e))
    return RedirectResponse(url, status_code=_HTTP_SEE_OTHER)


@router.post("/accounts/{account_id}/delete", response_class=HTMLResponse)
def delete_account(
    account_id: int, request: Request,
    force: bool = Query(False),
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    check_csrf(request, admin, x_csrf_token,
               f"/admin/accounts/{account_id}/delete")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.delete_account(conn, account_id, force=force)
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
        except svc.AccountInUse:
            ctx = _base_context(request, admin)
            ctx["account_id"] = account_id
            return templates.TemplateResponse(
                request=request, name="accounts/_delete_confirm.html",
                context=ctx, status_code=409,
            )
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = "/admin/accounts"
    return resp


@router.post("/accounts/{account_id}")
async def update_account(
    account_id: int, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    check_csrf(request, admin, x_csrf_token, f"/admin/accounts/{account_id}")
    raw = await request.form()
    deny: list[str] = [v for v in raw.getlist("deny_flags") if isinstance(v, str)]
    raw_dict = dict(raw)
    try:
        fields = forms.form_to_patch_fields(raw_dict, deny_flags_selected=deny)
    except forms.FormError as e:
        return _rerender_form_error(request, admin, raw_dict, deny, e,
                                    account_id=account_id)
    pool = request.app.state.pool

    def _update() -> None:
        with pool.connection() as conn:
            svc.update_account(conn, account_id, **fields)

    try:
        await run_in_threadpool(_update)
    except NotFound:
        raise HTTPException(status_code=404, detail="account not found")
    except svc.AccountFieldError as e:
        return _rerender_form_error(request, admin, raw_dict, deny, e,
                                    account_id=account_id)
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = f"/admin/accounts/{account_id}"
    return resp
