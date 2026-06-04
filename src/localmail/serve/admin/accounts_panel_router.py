"""Admin account-management HTML screens (2A.3).

Thin server-rendered HTMX router mounted at /admin. Renders Jinja fragments
and dispatches to the api/admin/accounts service; all form parsing lives in
account_forms. Mutating routes verify a method-bound CSRF token (X-CSRF-Token
header) via the shared check_csrf. JSON machine clients use /v1/admin/accounts.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Header, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from localmail.api.admin import accounts as svc
from localmail.api.admin.auth import AdminUser
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


_BLANK_VALUES: dict = {
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
    ctx = _form_context(request, admin, values=dict(_BLANK_VALUES), account_id=None)
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
    with pool.connection() as conn:
        try:
            acct = svc.create_account(conn, **kwargs)
        except svc.AccountFieldError as e:
            return _rerender_form_error(request, admin, raw_dict, deny, e, account_id=None)
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = f"/admin/accounts/{acct.id}"
    return resp
