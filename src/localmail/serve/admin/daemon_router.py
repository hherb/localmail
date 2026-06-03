"""HTTP routes for daemon control (2B.4), mounted under /v1/admin.

Two planes, one router:
  * Plane B (process lifecycle) — start / stop / restart drive the in-process
    `DaemonSupervisor` on `app.state`. Externally-supervised deployments
    (the stub) refuse with 409.
  * Plane A (DB-mediated) — reload and per-account restart-sync enqueue rows in
    `daemon_commands` for the running daemon to consume. These work regardless
    of who supervises the process.

`GET /v1/admin/daemon` fuses both: supervisor process state + per-thread
liveness from `daemon_heartbeats` + the captured recent log.

Every route is admin-gated; every mutating route validates a method-bound CSRF
token (per #122). `get_daemon_status` has no ACL of its own by design — the
admin gate here is the boundary.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from localmail.api.admin import accounts as accounts_svc
from localmail.api.admin import daemon as daemon_svc
from localmail.api.admin.auth import AdminUser
from localmail.api.errors import NotFound
from localmail.api.ids import parse_int_id
from localmail.serve.admin.csrf import check_csrf
from localmail.serve.admin.dependencies import require_admin_session
from localmail.serve.daemon_supervisor import (
    SupervisorState,
    SupervisorUnavailable,
    status_to_dict,
)

router = APIRouter(tags=["admin-daemon"])

_CSRF_HEADER = Header("", alias="X-CSRF-Token")


def _heartbeat_dict(hb: daemon_svc.HeartbeatRow) -> dict:
    return {
        "worker_kind": hb.worker_kind,
        "account_id": str(hb.account_id) if hb.account_id is not None else None,
        "state": hb.state,
        "current_folder": hb.current_folder,
        "last_error_msg": hb.last_error_msg,
        "started_at": hb.started_at.isoformat(),
        "last_heartbeat_at": hb.last_heartbeat_at.isoformat(),
        "stale": hb.stale,
    }


def build_daemon_view(supervisor, conn, *, stale_seconds: int) -> dict:
    """Fuse supervisor process state + heartbeats + recent log into one view
    dict. Single source of truth shared by the JSON route and the HTML panel.

    Process state, heartbeats, and log are sampled independently (no global
    snapshot lock) — a read-only monitoring view; momentary skew is acceptable.
    """
    proc = status_to_dict(supervisor.status())
    status = daemon_svc.get_daemon_status(conn, stale_seconds=stale_seconds)
    return {
        **proc,
        "supervise_daemon_externally": proc["state"] == SupervisorState.EXTERNAL,
        "heartbeats": [_heartbeat_dict(hb) for hb in status.heartbeats],
        "recent_log": supervisor.recent_log_lines(),
    }


@router.get("/daemon")
def get_daemon(
    request: Request,
    admin: AdminUser = require_admin_session(),
) -> dict:
    supervisor = request.app.state.daemon_supervisor
    daemon_cfg = request.app.state.daemon_config
    pool = request.app.state.pool
    with pool.connection() as conn:
        return build_daemon_view(
            supervisor, conn, stale_seconds=daemon_cfg.heartbeat_stale_seconds
        )


def _lifecycle(
    request: Request, admin: AdminUser, csrf_token: str, op: str
) -> JSONResponse:
    check_csrf(request, admin, csrf_token, f"/v1/admin/daemon/{op}")
    supervisor = request.app.state.daemon_supervisor
    try:
        getattr(supervisor, f"request_{op}")()
    except SupervisorUnavailable as e:
        raise HTTPException(status_code=409, detail=str(e))
    return JSONResponse(status_to_dict(supervisor.status()), status_code=202)


@router.post("/daemon/start")
def start_daemon(
    request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = _CSRF_HEADER,
) -> JSONResponse:
    return _lifecycle(request, admin, x_csrf_token, "start")


@router.post("/daemon/stop")
def stop_daemon(
    request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = _CSRF_HEADER,
) -> JSONResponse:
    return _lifecycle(request, admin, x_csrf_token, "stop")


@router.post("/daemon/restart")
def restart_daemon(
    request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = _CSRF_HEADER,
) -> JSONResponse:
    return _lifecycle(request, admin, x_csrf_token, "restart")


@router.post("/daemon/reload")
def reload_daemon(
    request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = _CSRF_HEADER,
) -> dict:
    check_csrf(request, admin, x_csrf_token, "/v1/admin/daemon/reload")
    pool = request.app.state.pool
    with pool.connection() as conn:
        command_id = daemon_svc.enqueue_command(
            conn, command="reload-now", requested_by=admin.id
        )
    return {"command_id": str(command_id)}


@router.post("/accounts/{account_id}/restart-sync")
def restart_account_sync(
    account_id: str,
    request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = _CSRF_HEADER,
) -> dict:
    aid = parse_int_id(account_id, field="account_id")
    check_csrf(
        request, admin, x_csrf_token,
        f"/v1/admin/accounts/{aid}/restart-sync",
    )
    pool = request.app.state.pool
    with pool.connection() as conn:
        try:
            accounts_svc.get_account(conn, aid)
        except NotFound:
            raise HTTPException(status_code=404, detail="account not found")
        command_id = daemon_svc.enqueue_command(
            conn, command="restart-account", account_id=aid, requested_by=admin.id
        )
    return {"command_id": str(command_id)}
