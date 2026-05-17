"""Account + folder routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from localmail.api.accounts import list_accounts, list_folders
from localmail.serve.middleware import get_authenticated_user

router = APIRouter()


@router.get("")
def get_accounts(request: Request, _user=Depends(get_authenticated_user)) -> list[dict[str, Any]]:
    pool = request.app.state.pool
    with pool.connection() as conn:
        return list_accounts(conn)


@router.get("/{account_id}/folders")
def get_folders(account_id: int, request: Request, _user=Depends(get_authenticated_user)) -> list[dict[str, Any]]:
    pool = request.app.state.pool
    with pool.connection() as conn:
        return list_folders(conn, account_id)
