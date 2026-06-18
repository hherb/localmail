# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unauthenticated /v1/version and /v1/health endpoints + /v1/capabilities."""
from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from fastapi import APIRouter, Depends, Request

from localmail.api.errors import FeatureUnavailable
from localmail.serve.middleware import get_authenticated_user

API_MAJOR = 1
API_MINOR = 0

logger = logging.getLogger("localmail.serve")


def _server_version() -> str:
    try:
        return _pkg_version("localmail")
    except PackageNotFoundError:
        return "0.0.0+unknown"


SERVER_VERSION = _server_version()

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
    return {
        "api_major": API_MAJOR,
        "api_minor": API_MINOR,
        "server_version": SERVER_VERSION,
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
