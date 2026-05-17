"""FastAPI application factory."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool

from localmail.api.errors import APIError
from localmail.serve.middleware import APIErrorHandlerMiddleware, RequestIdMiddleware
from localmail.serve.routes import accounts as accounts_routes
from localmail.serve.routes import auth as auth_routes
from localmail.serve.routes import attachments as attachments_routes
from localmail.serve.routes import messages as messages_routes
from localmail.serve.routes import search as search_routes
from localmail.serve.routes import version as version_routes


def create_app(*, db_dsn: str, searcher=None) -> FastAPI:
    """Build a FastAPI app bound to a Postgres pool and (optionally) a Searcher.

    `searcher` is None in baseline tests; production runs pass a configured
    Searcher created via `localmail.search.create_searcher`.
    """
    app = FastAPI()
    app.state.pool = ConnectionPool(db_dsn, min_size=1, max_size=4, open=True)
    app.state.searcher = searcher

    # Exception handler for APIError raised inside route handlers / dependencies.
    # FastAPI's DI layer catches these before BaseHTTPMiddleware sees them, so we
    # need both this handler and the middleware to cover all cases.
    @app.exception_handler(APIError)
    async def api_error_handler(request, exc: APIError):
        return JSONResponse(
            exc.to_problem(),
            status_code=exc.http_status,
            media_type="application/problem+json",
        )

    # Middleware are added in reverse order of execution: the LAST add_middleware
    # call wraps the OUTERMOST middleware. We want RequestId outermost so every
    # response (including error responses) gets the X-Request-Id header.
    app.add_middleware(APIErrorHandlerMiddleware)
    app.add_middleware(RequestIdMiddleware)

    @app.middleware("http")
    async def add_csp_header(request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"
        )
        return response

    app.include_router(version_routes.router, prefix="/v1")
    app.include_router(auth_routes.router, prefix="/v1/auth")
    app.include_router(accounts_routes.router, prefix="/v1/accounts")
    app.include_router(messages_routes.router, prefix="/v1/messages")
    app.include_router(attachments_routes.router, prefix="/v1/attachments")
    app.include_router(search_routes.router, prefix="/v1/search")
    return app
