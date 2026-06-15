"""Interactive login + consent interstitial for the OAuth authorization flow.

GET  /oauth/consent?req=<blob>  -> render the login + Allow/Deny form.
POST /oauth/consent             -> verify the signed blob; on Allow, rate-limited
                                  credential check + mint a single-use code and
                                  303 to the client redirect_uri; on Deny, 303
                                  with error=access_denied.

Credential checks reuse the /v1/auth/login rate-limit path so this surface is
not a brute-force bypass.

No CSRF token guards the POST: the form carries no ambient authority. Auth is
the username+password typed each time (there is no session cookie to ride), and
the redirect target is the HMAC-signed, SDK-pre-validated redirect_uri inside
`req` — not anything the submitting page controls. A cross-site auto-submit
therefore can't act as the victim, so a CSRF token would add no protection.
"""
from __future__ import annotations

import functools
from pathlib import Path
from urllib.parse import urlencode

import anyio.to_thread
import psycopg
from psycopg_pool import ConnectionPool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from localmail.api import auth as api_auth
from localmail.api.errors import RateLimited
from localmail.config import AuthConfig, McpConfig
from localmail.mcp.oauth import clients, codes
from localmail.mcp.oauth.consent_forms import (
    ConsentDecision,
    ConsentFormError,
    parse_consent_form,
)
from localmail.mcp.oauth.consent_state import (
    ConsentPayload,
    ConsentStateExpired,
    ConsentStateInvalid,
    decode_consent_state,
)

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _redirect_with(redirect_uri: str, **params: str) -> RedirectResponse:
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(redirect_uri + sep + urlencode(params), status_code=303)


def build_consent_router(
    *,
    pool: ConnectionPool,
    signing_key: bytes,
    mcp_config: McpConfig,
    auth_config: AuthConfig,
) -> list[Route]:
    def _client_name(client_id: str, conn: psycopg.Connection | None = None) -> str:
        if conn is not None:
            row = clients.get_client(conn, client_id)
            return row.client_name if row and row.client_name else client_id
        with pool.connection() as c:
            row = clients.get_client(c, client_id)
        return row.client_name if row and row.client_name else client_id

    async def get_consent(request: Request) -> Response:
        blob = request.query_params.get("req", "")
        try:
            payload = decode_consent_state(blob, key=signing_key)
        except (ConsentStateInvalid, ConsentStateExpired):
            return HTMLResponse(
                "invalid or expired authorization request", status_code=400
            )
        # _client_name hits the DB; keep it off the event loop.
        client_name = await anyio.to_thread.run_sync(_client_name, payload.client_id)
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="consent.html",
            context={"req": blob, "client_name": client_name, "error": None},
        )

    def _authorize_and_mint(
        request: Request,
        payload: ConsentPayload,
        decision: ConsentDecision,
        client_ip: str | None,
    ) -> Response:
        # Runs in a worker thread (blocking DB + bcrypt). Builds and returns the
        # full Response so the event-loop handler only awaits the offload.
        assert decision.username is not None and decision.password is not None
        with pool.connection() as conn:
            try:
                api_auth.check_login_rate_limits(
                    conn, decision.username, client_ip, cfg=auth_config
                )
            except RateLimited as exc:
                return HTMLResponse(str(exc), status_code=429)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, password_hash FROM api_users "
                    "WHERE username = %s AND disabled_at IS NULL",
                    (decision.username,),
                )
                row = cur.fetchone()
            if row is None:
                # Equalize response time with the wrong-password path so an attacker
                # cannot enumerate usernames by timing (mirrors api.auth.login).
                api_auth.verify_password(decision.password, api_auth.DUMMY_PASSWORD_HASH)
                ok = False
            else:
                ok = api_auth.verify_password(decision.password, row[1])
            api_auth.record_login_attempt(
                conn,
                decision.username,
                client_ip,
                "success" if ok else "failure",
            )
            if not ok:
                return _TEMPLATES.TemplateResponse(
                    request=request,
                    name="consent.html",
                    context={
                        "req": decision.req,
                        "client_name": _client_name(payload.client_id, conn),
                        "error": "invalid username or password",
                    },
                    status_code=401,
                )
            assert row is not None  # ok=True implies the else branch ran
            raw_code = codes.mint_code(
                conn,
                client_id=payload.client_id,
                user_id=row[0],
                redirect_uri=payload.redirect_uri,
                redirect_uri_provided_explicitly=payload.redirect_uri_provided_explicitly,
                code_challenge=payload.code_challenge,
                scopes=payload.scopes,
                ttl_s=mcp_config.oauth_authorization_code_ttl_s,
            )
            conn.commit()
        return _redirect_with(
            payload.redirect_uri,
            code=raw_code,
            **({"state": payload.state} if payload.state else {}),
        )

    async def post_consent(request: Request) -> Response:
        form = await request.form()
        try:
            decision = parse_consent_form({k: str(v) for k, v in form.items()})
        except ConsentFormError as exc:
            return HTMLResponse(str(exc), status_code=400)
        try:
            payload = decode_consent_state(decision.req, key=signing_key)
        except (ConsentStateInvalid, ConsentStateExpired):
            return HTMLResponse(
                "invalid or expired authorization request", status_code=400
            )

        if not decision.allow:
            return _redirect_with(
                payload.redirect_uri,
                error="access_denied",
                **({"state": payload.state} if payload.state else {}),
            )

        client_ip = request.client.host if request.client else None
        return await anyio.to_thread.run_sync(
            functools.partial(
                _authorize_and_mint, request, payload, decision, client_ip
            )
        )

    return [
        Route("/oauth/consent", get_consent, methods=["GET"]),
        Route("/oauth/consent", post_consent, methods=["POST"]),
    ]
