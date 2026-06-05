"""HTTP routes for /v1/admin/imports (Sub-plan 2A.5).

Thin wrapper over api/admin/imports. Admin-gated; mutating routes verify a
method-bound CSRF token. IDs are strings on the wire (#33). Path validation
uses the configured [imports].roots allowlist.
"""
from __future__ import annotations

import psycopg
from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel

from localmail.api.admin import imports as svc
from localmail.api.admin.auth import AdminUser
from localmail.api.errors import NotFound
from localmail.api.ids import parse_int_id
from localmail.importer.paths import ImportPathError, resolve_import_path
from localmail.serve.admin.csrf import check_csrf
from localmail.serve.admin.dependencies import require_admin_session

router = APIRouter(tags=["admin-imports"])


class _ImportIn(BaseModel):
    account_id: str
    source_kind: str
    source_path: str


def _job_dict(j: svc.ImportJob) -> dict:
    return {
        "id": str(j.id),
        "account_id": str(j.account_id),
        "source_kind": j.source_kind,
        "source_path": j.source_path,
        "status": j.status,
        "total_messages": j.total_messages,
        "processed": j.processed,
        "inserted": j.inserted,
        "skipped_dup": j.skipped_dup,
        "failed": j.failed,
        "error_msg": j.error_msg,
        "cancel_requested": j.cancel_requested,
        "last_progress_at": j.last_progress_at.isoformat() if j.last_progress_at else None,
        "created_at": j.created_at.isoformat(),
        "started_at": j.started_at.isoformat() if j.started_at else None,
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
    }


@router.get("/imports")
def list_imports(request: Request, admin: AdminUser = require_admin_session()) -> dict:
    pool = request.app.state.pool
    with pool.connection() as conn:
        rows = svc.list_jobs(conn)
    return {"imports": [_job_dict(r) for r in rows]}


@router.post("/imports", status_code=201)
def create_import(
    body: _ImportIn, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> dict:
    check_csrf(request, admin, x_csrf_token, "/v1/admin/imports")
    if request.app.state.attachments_root is None:
        raise HTTPException(status_code=503, detail="attachments_root not configured")
    aid = parse_int_id(body.account_id, field="account_id")
    cfg = request.app.state.imports_config
    try:
        resolved = resolve_import_path(body.source_path, cfg.roots)
    except ImportPathError as e:
        raise HTTPException(status_code=400, detail=str(e))
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            jid = svc.create_job(
                conn, account_id=aid, source_kind=body.source_kind,
                source_path=str(resolved))
        except svc.ImportBusyError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except svc.ImportFieldError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
        job = svc.get_job(conn, jid)
    dsn = request.app.state.db_dsn
    svc.start_job(
        lambda: psycopg.connect(dsn, autocommit=False), jid,
        attachments_root=request.app.state.attachments_root,
        checkpoint_every=cfg.checkpoint_every)
    return _job_dict(job)


@router.get("/imports/{job_id}")
def get_import(
    job_id: str, request: Request, admin: AdminUser = require_admin_session(),
) -> dict:
    jid = parse_int_id(job_id, field="job_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            job = svc.get_job(conn, jid)
        except NotFound:
            raise HTTPException(status_code=404, detail="import job not found")
    return _job_dict(job)


@router.post("/imports/{job_id}/cancel", status_code=204)
def cancel_import(
    job_id: str, request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
) -> Response:
    jid = parse_int_id(job_id, field="job_id")
    check_csrf(request, admin, x_csrf_token, f"/v1/admin/imports/{jid}/cancel")
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            svc.cancel_job(conn, jid)
        except NotFound:
            raise HTTPException(status_code=404, detail="import job not found")
    return Response(status_code=204)
