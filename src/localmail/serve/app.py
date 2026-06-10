"""FastAPI application factory."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.routing import Route

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool

from localmail.api.admin import imports as _imports_svc
from localmail.api.errors import APIError, RateLimited
from localmail.config import (
    AuthConfig,
    DaemonConfig,
    ImportsConfig,
    McpConfig,
    ServeConfig,
)
from localmail.serve.admin import accounts_panel_router as admin_accounts_panel_router
from localmail.serve.admin import users_panel_router as admin_users_panel_router
from localmail.serve.admin import imports_panel_router as admin_imports_panel_router
from localmail.serve.admin import accounts_router as admin_accounts_router
from localmail.serve.admin import users_router as admin_users_router
from localmail.serve.admin import auth_router as admin_auth_router
from localmail.serve.admin import daemon_panel_router as admin_daemon_panel_router
from localmail.serve.admin import daemon_router as admin_daemon_router
from localmail.serve.admin import imports_router as admin_imports_router
from localmail.serve.admin import dashboard_router as admin_dashboard_router
from localmail.serve.admin import oauth_router as admin_oauth_router
from localmail.serve.admin.dependencies import install_admin_redirect_handler
from localmail.serve.admin.middleware import ScrubSensitiveQueryParamsMiddleware
from localmail.serve.daemon_control_socket import ControlSocketServer
from localmail.serve.daemon_supervisor import (
    DaemonSupervisor,
    ExternalDaemonSupervisor,
    default_daemon_argv,
    resolve_runtime_dir,
    socket_path,
)
from localmail.serve.middleware import APIErrorHandlerMiddleware, RequestIdMiddleware
from localmail.serve.routes import accounts as accounts_routes
from localmail.serve.routes import auth as auth_routes
from localmail.serve.routes import attachments as attachments_routes
from localmail.serve.routes import messages as messages_routes
from localmail.serve.routes import changes as changes_routes
from localmail.serve.routes import search as search_routes
from localmail.serve.routes import version as version_routes


def _try_build_mcp(pool, searcher, mcp_config):
    """Build the FastMCP server + ASGI app + RFC 9728 discovery routes.

    Returns (None, None, []) if the [mcp] extra is absent.
    """
    try:
        from localmail.mcp import build_mcp_server, build_protected_resource_routes
    except ImportError:
        logging.getLogger("localmail.serve").info(
            "MCP enabled but the [mcp] extra is not installed; skipping /mcp mount"
        )
        return None, None, []
    server = build_mcp_server(pool, searcher=searcher, config=mcp_config)
    routes = build_protected_resource_routes(mcp_config)
    return server, server.streamable_http_app(), routes


def create_app(
    *,
    db_dsn: str,
    searcher=None,
    serve_config: ServeConfig | None = None,
    auth_config: AuthConfig | None = None,
    gmail_client_secrets_file: Path | None = None,
    daemon_config: DaemonConfig | None = None,
    daemon_config_path: Path | None = None,
    enable_control_socket: bool = False,
    enable_mcp: bool = False,
    mcp_config: McpConfig | None = None,
    imports_config: ImportsConfig | None = None,
    attachments_root: Path | None = None,
) -> FastAPI:
    """Build a FastAPI app bound to a Postgres pool and (optionally) a Searcher.

    `searcher` is None in baseline tests; production runs pass a configured
    Searcher created via `localmail.search.create_searcher`. `serve_config`
    controls pool sizing; the default is fine for a single-user local
    deployment. `gmail_client_secrets_file` is the resolved
    `[gmail_oauth] client_secrets_file` path; the admin OAuth router threads
    it into the service layer so OAuth never calls load_config() per request
    (#120). None disables the web OAuth flow (the service raises at
    flow-build time).
    """
    cfg = serve_config or ServeConfig()
    auth_cfg = auth_config or AuthConfig()
    daemon_cfg = daemon_config or DaemonConfig()
    imports_cfg = imports_config or ImportsConfig()
    pool = ConnectionPool(
        db_dsn,
        min_size=cfg.pool_min_size,
        max_size=cfg.pool_max_size,
        open=True,
    )

    mcp_server = None
    mcp_app = None
    mcp_discovery_routes: list[Route] = []
    if enable_mcp:
        mcp_server, mcp_app, mcp_discovery_routes = _try_build_mcp(
            pool, searcher, mcp_config or McpConfig()
        )

    # Plane B supervisor: a real subprocess owner when we supervise the daemon,
    # else a stub reporting `external`. Constructing it is side-effect-free —
    # the child is spawned only on an explicit start(); the control socket is
    # bound only by the lifespan when `enable_control_socket` (the serve path).
    supervisor: DaemonSupervisor | ExternalDaemonSupervisor
    if cfg.supervise_daemon:
        supervisor = DaemonSupervisor(
            argv=default_daemon_argv(config_path=daemon_config_path),
            grace_seconds=daemon_cfg.shutdown_grace_seconds,
        )
    else:
        supervisor = ExternalDaemonSupervisor()

    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        css: ControlSocketServer | None = None
        if enable_control_socket and isinstance(supervisor, DaemonSupervisor):
            runtime_dir = resolve_runtime_dir(cfg.runtime_dir, env=os.environ)
            css = ControlSocketServer(
                path=socket_path(runtime_dir), supervisor=supervisor
            )
            css.start()
            app_.state.control_socket_server = css
        try:
            with pool.connection() as conn:
                n = _imports_svc.reconcile_orphaned_jobs(conn)
                conn.commit()
            if n:
                logging.getLogger("localmail.serve").warning(
                    "reconciled %d orphaned import job(s) at startup", n)
            mcp_ctx = (
                mcp_server.session_manager.run()
                if mcp_server is not None
                else nullcontext()
            )
            async with mcp_ctx:
                yield
        finally:
            if css is not None:
                css.close()
            if isinstance(supervisor, DaemonSupervisor):
                supervisor.close()
            pool.close()

    app = FastAPI(lifespan=lifespan)
    app.state.pool = pool
    app.state.searcher = searcher
    app.state.serve_config = cfg
    app.state.auth_config = auth_cfg
    app.state.daemon_config = daemon_cfg
    app.state.daemon_supervisor = supervisor
    app.state.control_socket_server = None
    app.state.gmail_client_secrets_file = gmail_client_secrets_file
    app.state.db_dsn = db_dsn
    app.state.imports_config = imports_cfg
    app.state.attachments_root = attachments_root

    if mcp_app is not None:
        app.mount("/mcp", mcp_app)
        app.router.routes.extend(mcp_discovery_routes)

    # Exception handler for APIError raised inside route handlers / dependencies.
    # FastAPI's DI layer catches these before BaseHTTPMiddleware sees them, so we
    # need both this handler and the middleware to cover all cases.
    @app.exception_handler(APIError)
    async def api_error_handler(request, exc: APIError):
        response = JSONResponse(
            exc.to_problem(),
            status_code=exc.http_status,
            media_type="application/problem+json",
        )
        if isinstance(exc, RateLimited) and exc.retry_after_s is not None:
            response.headers["Retry-After"] = str(exc.retry_after_s)
        return response

    # Middleware are added in reverse order of execution: the LAST add_middleware
    # call wraps the OUTERMOST middleware. We want RequestId outermost so every
    # response (including error responses) gets the X-Request-Id header.
    app.add_middleware(APIErrorHandlerMiddleware)
    app.add_middleware(RequestIdMiddleware)

    @app.middleware("http")
    async def add_csp_header(request, call_next):
        response = await call_next(request)
        # frame-ancestors and form-action have no fallback to default-src in
        # the CSP spec, so they must be listed explicitly to deny clickjacking
        # and form-submission abuse on rendered HTML email bodies. base-uri
        # blocks <base href> hijacks that would shift relative URLs (e.g. the
        # /v1/attachments rewrites) to an attacker-controlled origin.
        #
        # Admin UI paths need a relaxed CSP: htmx.min.js and admin.css are
        # served from 'self', and forms POST to 'self'. All other paths keep
        # the locked-down policy. 'unsafe-inline' on style-src is preserved
        # for /admin/* because htmx injects inline `style="..."` attributes
        # during swaps (display:none transitions, optimistic UI), which a
        # strict CSP would break. The risk is contained: admin pages are
        # rendered from Jinja templates we control, not from email bodies.
        if request.url.path.startswith("/admin"):
            csp = (
                "default-src 'none'; img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
            )
        else:
            csp = (
                "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            )
        response.headers["Content-Security-Policy"] = csp
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    # Admin UI: only mount if signing keys are configured. Empty keys mean
    # the operator hasn't opted in; we still build the rest of the app.
    if cfg.session_signing_key:
        if not cfg.state_signing_key:
            raise RuntimeError(
                "ServeConfig.state_signing_key is empty while session_signing_key is "
                "set; admin UI requires both. See "
                "docs/superpowers/specs/2026-05-28-admin-ui-design.md §3."
            )
        app.add_middleware(
            ScrubSensitiveQueryParamsMiddleware,
            sensitive=("code", "state", "password"),
        )
        install_admin_redirect_handler(app)
        app.include_router(admin_auth_router.router, prefix="/admin")
        app.include_router(admin_dashboard_router.router, prefix="/admin")
        app.include_router(admin_daemon_panel_router.router, prefix="/admin")
        app.include_router(admin_accounts_panel_router.router, prefix="/admin")
        app.include_router(admin_users_panel_router.router, prefix="/admin")
        app.include_router(admin_imports_panel_router.router, prefix="/admin")
        app.include_router(admin_accounts_router.router, prefix="/v1/admin")
        app.include_router(admin_users_router.router, prefix="/v1/admin")
        app.include_router(admin_imports_router.router, prefix="/v1/admin")
        app.include_router(admin_daemon_router.router, prefix="/v1/admin")
        app.include_router(admin_oauth_router.router_v1, prefix="/v1/admin")
        app.include_router(admin_oauth_router.router_admin, prefix="/admin")
        admin_static = Path(__file__).parent / "admin" / "static"
        app.mount(
            "/admin/static",
            StaticFiles(directory=str(admin_static)),
            name="admin_static",
        )

    app.include_router(version_routes.router, prefix="/v1")
    app.include_router(auth_routes.router, prefix="/v1/auth")
    app.include_router(accounts_routes.router, prefix="/v1/accounts")
    app.include_router(messages_routes.router, prefix="/v1/messages")
    app.include_router(attachments_routes.router, prefix="/v1/attachments")
    app.include_router(search_routes.router, prefix="/v1/search")
    app.include_router(changes_routes.router, prefix="/v1/changes")
    return app
