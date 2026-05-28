"""FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool

from localmail.api.errors import APIError, RateLimited
from localmail.config import AuthConfig, ServeConfig
from localmail.serve.admin import accounts_router as admin_accounts_router
from localmail.serve.admin import auth_router as admin_auth_router
from localmail.serve.admin import dashboard_router as admin_dashboard_router
from localmail.serve.admin import oauth_router as admin_oauth_router
from localmail.serve.admin.dependencies import install_admin_redirect_handler
from localmail.serve.admin.middleware import ScrubSensitiveQueryParamsMiddleware
from localmail.serve.middleware import APIErrorHandlerMiddleware, RequestIdMiddleware
from localmail.serve.routes import accounts as accounts_routes
from localmail.serve.routes import auth as auth_routes
from localmail.serve.routes import attachments as attachments_routes
from localmail.serve.routes import messages as messages_routes
from localmail.serve.routes import changes as changes_routes
from localmail.serve.routes import search as search_routes
from localmail.serve.routes import version as version_routes


def create_app(
    *,
    db_dsn: str,
    searcher=None,
    serve_config: ServeConfig | None = None,
    auth_config: AuthConfig | None = None,
) -> FastAPI:
    """Build a FastAPI app bound to a Postgres pool and (optionally) a Searcher.

    `searcher` is None in baseline tests; production runs pass a configured
    Searcher created via `localmail.search.create_searcher`. `serve_config`
    controls pool sizing; the default is fine for a single-user local
    deployment.
    """
    cfg = serve_config or ServeConfig()
    auth_cfg = auth_config or AuthConfig()
    pool = ConnectionPool(
        db_dsn,
        min_size=cfg.pool_min_size,
        max_size=cfg.pool_max_size,
        open=True,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            pool.close()

    app = FastAPI(lifespan=lifespan)
    app.state.pool = pool
    app.state.searcher = searcher
    app.state.serve_config = cfg
    app.state.auth_config = auth_cfg

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
        app.include_router(admin_accounts_router.router, prefix="/v1/admin")
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
