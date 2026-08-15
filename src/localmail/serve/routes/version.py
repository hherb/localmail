# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unauthenticated /v1/version and /v1/health endpoints + /v1/capabilities."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from localmail import __version__ as SERVER_VERSION
from localmail import __version_source__ as VERSION_SOURCE
from localmail.api.errors import FeatureUnavailable
from localmail.build_report import resolve_build_info
from localmail.serve.middleware import get_authenticated_user

API_MAJOR = 1
API_MINOR = 0

logger = logging.getLogger("localmail.serve")

router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict[str, str]:
    """Liveness + DB-reachability check for load balancers.

    Returns 503 (via FastAPI's response model) only when the DB is unreachable;
    a 200 means the server can both accept connections and round-trip a SELECT.
    """
    pool = request.app.state.pool
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as exc:
        logger.warning("health check db ping failed: %s", exc)
        raise FeatureUnavailable("database unreachable") from exc
    return {"status": "ok"}


@router.get("/version")
def version() -> dict[str, object]:
    """Identity of this server: protocol, version, and which build it is.

    `build_source` and `version_source` are always present and never null;
    only `build_hash` is nullable. Without them, "installed from a wheel"
    (normal) and "git ran and failed" (notable) are the same `null` — the shape
    #291 removed from the version line, which #278 would have reintroduced one
    field over.

    The human diagnostic is deliberately absent: this route is unauthenticated
    and `__version_diagnostic__` embeds rendered exception text carrying errno
    values and filesystem paths (#303). Identifiers yes; paths no. The human
    line stays in the server's logs, where #295 put it.
    """
    build = resolve_build_info()
    return {
        "api_major": API_MAJOR,
        "api_minor": API_MINOR,
        "server_version": SERVER_VERSION,
        "build_hash": build.build_hash,
        "build_source": build.source.wire_name,
        "version_source": VERSION_SOURCE.wire_name,
    }


@router.get("/capabilities")
def capabilities(_user=Depends(get_authenticated_user)) -> dict[str, bool]:
    return {
        "search": True,
        "attachments": True,
        "attachment_text": True,
        "threading": False,
        "send": False,
    }
