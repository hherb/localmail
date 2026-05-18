"""Account + folder routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from localmail.api.accounts import list_accounts, list_folders
from localmail.api.acl import allowed_account_ids
from localmail.serve.middleware import get_authenticated_user

router = APIRouter()


@router.get("")
def get_accounts(request: Request, user=Depends(get_authenticated_user)) -> list[dict[str, Any]]:
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        return list_accounts(conn, allowed_account_ids=allowed)


@router.get("/{account_id}/folders")
def get_folders(account_id: int, request: Request, user=Depends(get_authenticated_user)) -> list[dict[str, Any]]:
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        return list_folders(conn, account_id, allowed_account_ids=allowed)
