"""Admin HTML daemon-control panel (2B.5).

`GET /admin/daemon` renders the full page; `GET /admin/_partials/daemon-status`
renders the self-polling status fragment (HTMX `hx-get … every Ns`). Both reuse
`daemon_router.build_daemon_view` for the fusion and `csrf_token_context` for
method-bound CSRF tokens on the mutating controls.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from localmail.api.admin.auth import AdminUser
from localmail.serve.admin.csrf import csrf_token_context
from localmail.serve.admin.daemon_router import build_daemon_view
from localmail.serve.admin.dependencies import require_admin_session

DAEMON_PANEL_POLL_SECONDS = 2

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter()


def _render(name: str, request: Request, admin: AdminUser) -> HTMLResponse:
    s_key = request.app.state.serve_config.session_signing_key.encode("ascii")
    supervisor = request.app.state.daemon_supervisor
    daemon_cfg = request.app.state.daemon_config
    pool = request.app.state.pool
    with pool.connection() as conn:
        view = build_daemon_view(
            supervisor, conn, stale_seconds=daemon_cfg.heartbeat_stale_seconds
        )
    context = {
        "current_user": admin,
        "flashes": [],
        "view": view,
        "poll_seconds": DAEMON_PANEL_POLL_SECONDS,
        **csrf_token_context(user_id=admin.id, key=s_key),
    }
    return templates.TemplateResponse(request=request, name=name, context=context)


@router.get("/daemon", response_class=HTMLResponse)
def daemon_panel(
    request: Request, admin: AdminUser = require_admin_session()
) -> HTMLResponse:
    return _render("daemon/panel.html", request, admin)


@router.get("/_partials/daemon-status", response_class=HTMLResponse)
def daemon_status_partial(
    request: Request, admin: AdminUser = require_admin_session()
) -> HTMLResponse:
    return _render("daemon/_status.html", request, admin)
