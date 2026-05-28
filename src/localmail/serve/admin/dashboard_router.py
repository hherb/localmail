"""GET /admin/ — authenticated dashboard."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from localmail.api.admin.auth import AdminUser
from localmail.api.admin.csrf import make_csrf_token
from localmail.serve.admin.dependencies import require_admin_session

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def get_dashboard(
    request: Request,
    admin: AdminUser = require_admin_session(),
) -> HTMLResponse:
    s_key = request.app.state.serve_config.session_signing_key.encode("latin1")
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "current_user": admin,
            "flashes": [],
            "csrf_token_for": lambda action: make_csrf_token(
                user_id=admin.id, action=action, key=s_key,
            ),
        },
    )
