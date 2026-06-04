"""Admin account-management HTML screens (2A.3).

Thin server-rendered HTMX router mounted at /admin. Renders Jinja fragments
and dispatches to the api/admin/accounts service; all form parsing lives in
account_forms. Mutating routes verify a method-bound CSRF token (X-CSRF-Token
header) via the shared check_csrf. JSON machine clients use /v1/admin/accounts.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from localmail.api.admin import accounts as svc
from localmail.api.admin.auth import AdminUser
from localmail.serve.admin.csrf import csrf_token_context, session_signing_key
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
