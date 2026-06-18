# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Account + folder routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from localmail.api.accounts import list_accounts, list_folders
from localmail.api.acl import allowed_account_ids
from localmail.api.ids import parse_int_id
from localmail.serve.middleware import get_authenticated_user

router = APIRouter()


@router.get("")
def get_accounts(request: Request, user=Depends(get_authenticated_user)) -> list[dict[str, Any]]:
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        return list_accounts(conn, allowed_account_ids=allowed)


@router.get("/{account_id}/folders")
def get_folders(account_id: str, request: Request, user=Depends(get_authenticated_user)) -> list[dict[str, Any]]:
    aid = parse_int_id(account_id, field="account_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        return list_folders(conn, aid, allowed_account_ids=allowed)
