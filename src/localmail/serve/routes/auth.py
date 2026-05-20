"""Auth endpoints: login, logout, refresh, whoami."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel

from localmail.api import auth as auth_svc
from localmail.serve.middleware import get_authenticated_user

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    token: str
    expires_at: str


class WhoamiResponse(BaseModel):
    username: str
    user_id: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, request: Request) -> TokenResponse:
    pool = request.app.state.pool
    client_ip = request.client.host if request.client else None
    cfg = request.app.state.auth_config
    with pool.connection() as conn:
        token, expires_at = auth_svc.login(
            conn, req.username, req.password, client_ip=client_ip, cfg=cfg
        )
        conn.commit()
    return TokenResponse(token=token, expires_at=expires_at.isoformat())


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, _user=Depends(get_authenticated_user)) -> Response:
    auth = request.headers.get("Authorization", "")
    token = auth[len("Bearer "):]
    pool = request.app.state.pool
    with pool.connection() as conn:
        auth_svc.logout(conn, token)
        conn.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/refresh", response_model=TokenResponse)
def refresh(request: Request, _user=Depends(get_authenticated_user)) -> TokenResponse:
    auth = request.headers.get("Authorization", "")
    token = auth[len("Bearer "):]
    pool = request.app.state.pool
    with pool.connection() as conn:
        new_token, expires_at = auth_svc.refresh_token(conn, token)
        conn.commit()
    return TokenResponse(token=new_token, expires_at=expires_at.isoformat())


@router.get("/whoami", response_model=WhoamiResponse)
def whoami(user=Depends(get_authenticated_user)) -> WhoamiResponse:
    return WhoamiResponse(username=user.username, user_id=str(user.id))


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    user=Depends(get_authenticated_user),
) -> Response:
    pool = request.app.state.pool
    with pool.connection() as conn:
        auth_svc.change_password(conn, user.id, body.old_password, body.new_password)
        conn.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
