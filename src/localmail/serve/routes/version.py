"""Unauthenticated /v1/version and /v1/health endpoints + /v1/capabilities."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from localmail.serve.middleware import get_authenticated_user

SERVER_VERSION = "0.1.0"
API_MAJOR = 1
API_MINOR = 0

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
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
