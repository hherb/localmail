# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""LocalmailASProvider — the MCP SDK OAuthAuthorizationServerProvider backed by
the localmail OAuth stores.

`authorize` does NOT mint a code: it packs the authorization params into a
signed consent blob and redirects to the interactive consent router, which mints
the code after a verified login. PKCE S256 + redirect_uri matching are done by
the SDK's TokenHandler using the AuthorizationCode we return from
`load_authorization_code`; this provider never sees the code_verifier.
"""
from __future__ import annotations

import logging
import time
from typing import Literal, cast
from urllib.parse import urlencode

import anyio.to_thread
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from psycopg_pool import ConnectionPool

from localmail.config import McpConfig
from localmail.mcp.discovery import mcp_resource_url
from localmail.mcp.oauth import access, clients, codes, refresh
from localmail.mcp.oauth.consent_state import ConsentPayload, encode_consent_state
from localmail.mcp.oauth.resource_indicator import (
    decide_resource,
    resolve_accepted_resources,
)

logger = logging.getLogger("localmail.mcp.oauth")

TokenEndpointAuthMethod = Literal[
    "none", "client_secret_post", "client_secret_basic", "private_key_jwt"
]


class LocalmailASProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(
        self,
        pool: ConnectionPool,
        *,
        config: McpConfig,
        signing_key: bytes,
        consent_path: str,
    ) -> None:
        self._pool = pool
        self._cfg = config
        self._key = signing_key
        self._consent_path = consent_path
        self._accepted = resolve_accepted_resources(
            [str(u) for u in config.resource_indicators]
            if config.resource_indicators else None,
            mcp_resource_url(str(config.resource_server_url)),
        )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await anyio.to_thread.run_sync(self._get_client_sync, client_id)

    def _get_client_sync(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._pool.connection() as conn:
            row = clients.get_client(conn, client_id)
        if row is None:
            return None
        return OAuthClientInformationFull(
            client_id=row.client_id,
            redirect_uris=[AnyUrl(u) for u in row.redirect_uris],
            client_name=row.client_name,
            grant_types=row.grant_types or [],
            response_types=row.response_types or [],
            token_endpoint_auth_method=cast(
                "TokenEndpointAuthMethod",
                row.token_endpoint_auth_method or "none",
            ),
            scope=row.scope,
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await anyio.to_thread.run_sync(self._register_client_sync, client_info)

    def _register_client_sync(self, ci: OAuthClientInformationFull) -> None:
        assert ci.client_id is not None
        with self._pool.connection() as conn:
            clients.register_client(
                conn,
                client_id=ci.client_id,
                client_secret_sha256=None,
                redirect_uris=[str(u) for u in (ci.redirect_uris or [])],
                client_name=ci.client_name,
                grant_types=list(ci.grant_types or []),
                response_types=list(ci.response_types or []),
                token_endpoint_auth_method=ci.token_endpoint_auth_method,
                scope=ci.scope,
            )
            conn.commit()

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        assert client.client_id is not None
        decision = decide_resource(
            params.resource, self._accepted,
            require=self._cfg.oauth_require_resource_indicator,
        )
        if not decision.ok:
            raise AuthorizeError("invalid_request", decision.error)
        payload = ConsentPayload(
            client_id=client.client_id,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge,
            scopes=list(params.scopes or []),
            state=params.state,
            exp=int(time.time()) + self._cfg.oauth_consent_state_ttl_s,
            resource=decision.bound,
        )
        blob = encode_consent_state(payload, key=self._key)
        return f"{self._consent_path}?{urlencode({'req': blob})}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return await anyio.to_thread.run_sync(
            self._load_code_sync, client.client_id, authorization_code
        )

    def _load_code_sync(
        self, client_id: str | None, raw_code: str
    ) -> AuthorizationCode | None:
        with self._pool.connection() as conn:
            row = codes.load_code(conn, raw_code)
        if row is None or row.client_id != client_id:
            return None
        return AuthorizationCode(
            code=raw_code,
            scopes=row.scopes,
            expires_at=row.expires_at.timestamp(),
            client_id=row.client_id,
            code_challenge=row.code_challenge,
            redirect_uri=AnyUrl(row.redirect_uri),
            redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
            subject=str(row.user_id),
            resource=row.resource,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        return await anyio.to_thread.run_sync(
            self._exchange_code_sync, client.client_id, authorization_code
        )

    def _exchange_code_sync(
        self, client_id: str | None, auth_code: AuthorizationCode
    ) -> OAuthToken:
        assert client_id is not None
        assert auth_code.subject is not None
        user_id = int(auth_code.subject)
        user_vanished = False
        # Bound up front rather than only inside the mint branch: the return
        # below is reachable only once three separate guards have passed, and a
        # future edit to any of them would otherwise turn a rejected exchange
        # into an UnboundLocalError (an HTTP 500) instead of a TokenError.
        # Mirrors _exchange_refresh_sync.
        access_raw: str | None = None
        refresh_raw: str | None = None
        with self._pool.connection() as conn:
            # Atomic single-use guard (RFC 6749 §4.1.2). The SDK already loaded +
            # validated the code, but two concurrent exchanges can both pass that
            # check; only the DELETE that actually removed the row may mint. Raise
            # AFTER the connection context exits — TokenError is a frozen
            # dataclass and the contextmanager's __exit__ cannot set __traceback__
            # on it.
            # The burn also re-decides the code's expiry and the user's
            # revocation state under its own snapshot (#241). The SDK's
            # load_authorization_code ran in a separate call, so its checks are
            # already stale by the time we get here; without re-deciding, a
            # revocation landing in that gap still minted a token pair, because
            # the successors carry `created_at = now()` — past the cutoff, hence
            # valid.
            burn = codes.consume_code(conn, auth_code.code)
            # Commit the burn on its own, before anything below can fail. Sharing
            # one transaction with the mint meant a rollback took the DELETE with
            # it and resurrected the code for the rest of its TTL, so a client
            # auto-retry — or a replay by anyone holding a copy — could still
            # exchange it (#219). A post-burn failure now costs a fresh consent
            # round trip, which is the correct trade against a replayable code.
            # Note this commits the burn even when the code is no longer valid:
            # single-use must not become conditional on validity.
            conn.commit()
            consumed = burn.burned
            if consumed and burn.still_valid:
                refresh_raw = refresh.mint_refresh(
                    conn, client_id=client_id, user_id=user_id,
                    scopes=auth_code.scopes,
                    ttl_s=self._cfg.oauth_refresh_token_ttl_s,
                    resource=auth_code.resource,
                )
                new_row = refresh.load_refresh(conn, refresh_raw)
                if new_row is None:
                    # User disabled in the window between consent and exchange:
                    # load_refresh filters disabled users, so the just-minted row
                    # reads back as absent. Fail closed (mirror
                    # _exchange_refresh_sync) rather than asserting -> HTTP 500.
                    conn.rollback()
                    user_vanished = True
                else:
                    access_raw = access.mint_access(
                        conn, user_id=user_id, client_id=client_id,
                        ttl_s=self._cfg.oauth_access_token_ttl_s,
                        family_id=new_row.family_id,
                        resource=auth_code.resource,
                    )
                    clients.touch_last_used(conn, client_id)
                    conn.commit()
        if not consumed:
            # Not "or expired": nothing sweeps oauth_authorization_codes, so an
            # expired row is still there to be burned and reports itself through
            # `still_valid` on the branch below. Reaching here means no row at
            # all — already used, or never minted.
            raise TokenError(
                "invalid_grant", "authorization code already used or unknown"
            )
        if not burn.still_valid or user_vanished:
            raise TokenError("invalid_grant", "authorization code is no longer valid")
        assert access_raw is not None and refresh_raw is not None
        return OAuthToken(
            access_token=access_raw,
            token_type="Bearer",
            expires_in=self._cfg.oauth_access_token_ttl_s,
            refresh_token=refresh_raw,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return await anyio.to_thread.run_sync(
            self._load_refresh_sync, client.client_id, refresh_token
        )

    def _load_refresh_sync(
        self, client_id: str | None, raw: str
    ) -> RefreshToken | None:
        with self._pool.connection() as conn:
            row = refresh.load_refresh(conn, raw)
        if row is None or row.client_id != client_id:
            return None
        return RefreshToken(
            token=raw, client_id=row.client_id, scopes=row.scopes,
            expires_at=int(row.expires_at.timestamp()),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        return await anyio.to_thread.run_sync(
            self._exchange_refresh_sync, client.client_id, refresh_token
        )

    def _exchange_refresh_sync(
        self, client_id: str | None, rt: RefreshToken
    ) -> OAuthToken:
        assert client_id is not None
        access_raw: str | None = None
        purged = 0
        with self._pool.connection() as conn:
            result = refresh.rotate_refresh(
                conn, rt.token, ttl_s=self._cfg.oauth_refresh_token_ttl_s
            )
            if result.outcome == "rotated":
                assert result.new_token is not None
                row = refresh.load_refresh(conn, result.new_token)
                assert row is not None
                access_raw = access.mint_access(
                    conn, user_id=row.user_id, client_id=client_id,
                    ttl_s=self._cfg.oauth_access_token_ttl_s,
                    family_id=row.family_id,
                    resource=row.resource,
                )
                # A refresh is client activity too — keep last_used_at honest so
                # the unused-client cleanup never reaps an active client.
                clients.touch_last_used(conn, client_id)
                conn.commit()
            elif result.outcome == "reuse":
                assert result.family_id is not None
                purged = access.revoke_access_family(conn, result.family_id)
                # The family DELETE (refresh) + access purge must persist.
                conn.commit()
            else:
                conn.rollback()
        # Raise AFTER the connection context exits — TokenError is a frozen
        # dataclass and the contextmanager's __exit__ cannot set __traceback__
        # on it (same constraint as _exchange_code_sync).
        if result.outcome == "reuse":
            logger.warning(
                "refresh-token reuse detected; revoked family for client_id=%s "
                "(access tokens purged=%d)",
                client_id, purged,
            )
            raise TokenError("invalid_grant", "refresh token reuse detected")
        if result.outcome != "rotated":
            raise TokenError("invalid_grant", "refresh token is no longer valid")
        assert access_raw is not None
        return OAuthToken(
            access_token=access_raw,
            token_type="Bearer",
            expires_in=self._cfg.oauth_access_token_ttl_s,
            refresh_token=result.new_token,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        return await anyio.to_thread.run_sync(self._load_access_sync, token)

    def _load_access_sync(self, token: str) -> AccessToken | None:
        with self._pool.connection() as conn:
            at = access.load_access(conn, token, accepted_resources=self._accepted)
            conn.commit()
        return at

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await anyio.to_thread.run_sync(self._revoke_sync, token.token)

    def _revoke_sync(self, raw: str) -> None:
        with self._pool.connection() as conn:
            if not access.revoke_access(conn, raw):
                refresh.revoke_refresh(conn, raw)
            conn.commit()
