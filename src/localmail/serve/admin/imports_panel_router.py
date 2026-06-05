"""Admin import-management HTML screens (2A.5).

Thin server-rendered HTMX router mounted at /admin. Form parsing lives in
import_forms; execution dispatches to api/admin/imports. The progress partial
self-polls until the job reaches a terminal status.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import psycopg
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from localmail.api.admin import accounts as accounts_svc
from localmail.api.admin import imports as svc
from localmail.api.admin.auth import AdminUser
from localmail.api.errors import NotFound
from localmail.importer.job_state import is_stale, is_terminal
from localmail.importer.paths import ImportPathError, resolve_import_path
from localmail.serve.admin import import_forms as forms
from localmail.serve.admin.csrf import check_csrf, csrf_token_context, session_signing_key
from localmail.serve.admin.dependencies import require_admin_session

IMPORT_PANEL_POLL_SECONDS = 2

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


def _archive_accounts(conn: psycopg.Connection) -> list:
    return [a for a in accounts_svc.list_accounts(conn) if a.auth_method == "archive"]


def _progress_context(request: Request, admin: AdminUser, job: svc.ImportJob) -> dict:
    stale = is_stale(
        status=job.status, last_progress_at=job.last_progress_at,
        now=datetime.now(timezone.utc),
        stale_seconds=request.app.state.imports_config.stale_seconds)
    ctx = _base_context(request, admin)
    ctx.update({
        "job": job,
        "terminal": is_terminal(job.status),
        "stale": stale,
        "poll_seconds": IMPORT_PANEL_POLL_SECONDS,
    })
    return ctx


@router.get("/imports", response_class=HTMLResponse)
def imports_panel(
    request: Request, admin: AdminUser = require_admin_session()
) -> HTMLResponse:
    cfg = request.app.state.imports_config
    with request.app.state.pool.connection() as conn:
        jobs = svc.list_jobs(conn)
        archives = _archive_accounts(conn)
    ctx = _base_context(request, admin)
    ctx.update({
        "jobs": jobs,
        "archive_accounts": archives,
        "imports_enabled": bool(cfg.roots),
        "roots": [str(r) for r in cfg.roots],
        "field_errors": {},
        "values": {"account_id": "", "source_kind": "mbox", "source_path": ""},
    })
    return templates.TemplateResponse(
        request=request, name="imports/list.html", context=ctx)


@router.post("/imports")
async def create_import(
    request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    check_csrf(request, admin, x_csrf_token, "/admin/imports")
    raw = dict(await request.form())
    cfg = request.app.state.imports_config

    def _rerender_error(field_errors: dict) -> HTMLResponse:
        with request.app.state.pool.connection() as conn:
            archives = _archive_accounts(conn)
        ctx = _base_context(request, admin)
        ctx.update({
            "archive_accounts": archives,
            "imports_enabled": bool(cfg.roots),
            "roots": [str(r) for r in cfg.roots],
            "field_errors": field_errors,
            "values": {
                "account_id": raw.get("account_id", ""),
                "source_kind": raw.get("source_kind", "mbox"),
                "source_path": raw.get("source_path", ""),
            },
        })
        return templates.TemplateResponse(
            request=request, name="imports/_form.html", context=ctx, status_code=400)

    try:
        kwargs = forms.form_to_create_kwargs(raw)
        resolved = resolve_import_path(kwargs["source_path"], cfg.roots)
    except forms.FormError as e:
        return _rerender_error(forms.field_errors_from(e))
    except ImportPathError as e:
        return _rerender_error({"source_path": str(e)})

    with request.app.state.pool.connection() as conn:
        try:
            jid = svc.create_job(
                conn, account_id=kwargs["account_id"],
                source_kind=kwargs["source_kind"], source_path=str(resolved))
        except (svc.ImportBusyError, svc.ImportFieldError) as e:
            return _rerender_error(forms.field_errors_from(e))
        except NotFound:
            return _rerender_error({"_form": "account not found"})

    dsn = request.app.state.db_dsn
    svc.start_job(
        lambda: psycopg.connect(dsn, autocommit=False), jid,
        attachments_root=request.app.state.attachments_root,
        checkpoint_every=cfg.checkpoint_every)
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = f"/admin/imports/{jid}"
    return resp


@router.get("/imports/{job_id}", response_class=HTMLResponse)
def import_detail(
    job_id: int, request: Request, admin: AdminUser = require_admin_session()
) -> HTMLResponse:
    with request.app.state.pool.connection() as conn:
        try:
            job = svc.get_job(conn, job_id)
        except NotFound:
            raise HTTPException(status_code=404, detail="import job not found")
    ctx = _progress_context(request, admin, job)
    return templates.TemplateResponse(
        request=request, name="imports/detail.html", context=ctx)


@router.get("/_partials/import-status/{job_id}", response_class=HTMLResponse)
def import_status_partial(
    job_id: int, request: Request, admin: AdminUser = require_admin_session(),
) -> HTMLResponse:
    with request.app.state.pool.connection() as conn:
        try:
            job = svc.get_job(conn, job_id)
        except NotFound:
            raise HTTPException(status_code=404, detail="import job not found")
    ctx = _progress_context(request, admin, job)
    return templates.TemplateResponse(
        request=request, name="imports/_progress.html", context=ctx)


@router.post("/imports/{job_id}/cancel", response_class=HTMLResponse)
def cancel_import(
    job_id: int, request: Request, admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> HTMLResponse:
    check_csrf(request, admin, x_csrf_token, f"/admin/imports/{job_id}/cancel")
    with request.app.state.pool.connection() as conn:
        try:
            svc.cancel_job(conn, job_id)
            job = svc.get_job(conn, job_id)
        except NotFound:
            raise HTTPException(status_code=404, detail="import job not found")
    ctx = _progress_context(request, admin, job)
    return templates.TemplateResponse(
        request=request, name="imports/_progress.html", context=ctx)
