# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""FastMCP server exposing five ACL-scoped localmail tools.

Each tool resolves the authenticated user from the request's access token
(minted by `LocalmailTokenVerifier`), derives that user's allowed account ids,
and delegates to the transport-free tool bodies in `localmail.mcp.tools`.
Domain errors are mapped to `ToolError` so the agent sees a clean message
instead of a stack trace.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import OAuthAuthorizationServerProvider
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from psycopg_pool import ConnectionPool
from pydantic import AnyHttpUrl, Field

from localmail.api.acl import allowed_account_ids
from localmail.api.errors import NotFound, SearchCursorExpired, ValidationFailed
from localmail.api.ids import parse_int_id
from localmail.config import McpConfig
import localmail.mcp.tools as tools
from localmail.mcp.auth import LocalmailTokenVerifier, user_id_from_access_token
from localmail.mcp.discovery import mcp_resource_url
from localmail.search.searcher import Searcher

SERVER_NAME = "localmail"


def _current_user_id() -> int:
    """The localmail user id for the in-flight MCP request.

    Raises `ToolError` outside an authenticated request (no access token).
    """
    access_token = get_access_token()
    if access_token is None:
        raise ToolError("not authenticated")
    try:
        return user_id_from_access_token(access_token)
    except ValueError as exc:
        raise ToolError("not authenticated") from exc


def _build_filters(
    *,
    account_ids: list[str] | None,
    folder_ids: list[str] | None,
    date_from: str | None,
    date_to: str | None,
    from_addr: str | None,
    to: str | None,
    subject: str | None,
    has_attachment: bool | None,
    lang: str | None,
) -> dict[str, Any]:
    """Compose the filter dict `tool_search` forwards to `run_search`.

    Keys align with `api.search._filter_tokens`; `from_addr` maps to the DSL
    `from` key. None values are dropped so absent filters don't tokenize.
    """
    raw: dict[str, Any] = {
        "account_ids": account_ids,
        "folder_ids": folder_ids,
        "date_from": date_from,
        "date_to": date_to,
        "from": from_addr,
        "to": to,
        "subject": subject,
        "has_attachment": has_attachment,
        "lang": lang,
    }
    return {k: v for k, v in raw.items() if v is not None}


def build_mcp_server(
    pool: ConnectionPool,
    *,
    searcher: Searcher | None,
    config: McpConfig,
    auth_server_provider: OAuthAuthorizationServerProvider | None = None,
) -> FastMCP:
    """Construct the FastMCP server with five ACL-scoped tools.

    Auth wiring requires `auth` settings (the SDK rejects a verifier/provider
    without them); the issuer / resource-server URLs come from `config`. When
    `auth_server_provider` is supplied, localmail acts as the OAuth
    authorization server: the SDK uses the provider for the `/mcp` resource
    check and mounts `/authorize`, `/token`, `/register`, `/revoke`, and the
    AS metadata. Otherwise the opaque-bearer `token_verifier` path is used.
    """
    auth_settings = AuthSettings(
        issuer_url=config.issuer_url,
        resource_server_url=AnyHttpUrl(
            mcp_resource_url(str(config.resource_server_url))
        ),
        required_scopes=[],
    )
    common_kwargs: dict[str, Any] = dict(
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        # localmail serves /mcp on a network bind (see [serve] --bind), but the
        # SDK auto-enables DNS-rebinding protection with a localhost-only Host
        # allow-list whenever transport_security is unset. That rejects every
        # non-localhost client with 421 Misdirected Request. /mcp is gated by a
        # bearer token and is not browser-facing, so rebinding protection buys
        # nothing here — disable it so network clients can connect.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    if auth_server_provider is not None:
        auth_settings.client_registration_options = ClientRegistrationOptions(
            enabled=True
        )
        auth_settings.revocation_options = RevocationOptions(enabled=True)
        server = FastMCP(
            SERVER_NAME,
            auth_server_provider=auth_server_provider,
            auth=auth_settings,
            **common_kwargs,
        )
    else:
        server = FastMCP(
            SERVER_NAME,
            token_verifier=LocalmailTokenVerifier(pool),
            auth=auth_settings,
            **common_kwargs,
        )

    @server.tool()
    def search(
        query: Annotated[str, Field(description=(
            "Free-text query matched against message subjects/bodies and "
            "extracted attachment text. An empty string lists recent mail "
            "(date-ordered) — prefer `list_messages` for that intent."))],
        sort: Annotated[Literal["rank", "date"], Field(description=(
            'Result ordering: "rank" (hybrid relevance, the default) or '
            '"date" (strictly newest first).'))] = "rank",
        limit: Annotated[int, Field(ge=1, le=200, description=(
            "Maximum results in this page (1–200)."))] = 50,
        cursor: Annotated[str | None, Field(description=(
            "Opaque pagination cursor — pass back a previous response's "
            "`next_cursor` to get the next page; omit for the first page."))]
            = None,
        account_ids: Annotated[list[str] | None, Field(description=(
            "Restrict to these account ids (string integers). Omit to search "
            "every account you may read; discover ids via `list_accounts`."))]
            = None,
        folder_ids: Annotated[list[str] | None, Field(description=(
            "Restrict to these folder/mailbox ids (string integers)."))]
            = None,
        date_from: Annotated[str | None, Field(description=(
            "Lower bound (inclusive) on the sender's header Date, as "
            "YYYY-MM-DD. Note this filters the header Date, which may differ "
            "from the displayed/sort date (newest-first uses the IMAP arrival "
            "date when present)."))]
            = None,
        date_to: Annotated[str | None, Field(description=(
            "Upper bound (exclusive) on the sender's header Date, as "
            "YYYY-MM-DD. Filters the header Date — see `date_from`."))]
            = None,
        from_addr: Annotated[str | None, Field(description=(
            "Case-insensitive substring the From address or display name "
            "must contain."))]
            = None,
        to: Annotated[str | None, Field(description=(
            "Case-insensitive substring any To address must contain."))]
            = None,
        subject: Annotated[str | None, Field(description=(
            "Case-insensitive substring the subject must contain."))]
            = None,
        has_attachment: Annotated[bool | None, Field(description=(
            "True for only messages with attachments, False for only those "
            "without; omit to match either."))] = None,
        lang: Annotated[str | None, Field(description=(
            'Restrict to a detected body language by ISO 639-1 code '
            '(e.g. "en", "de", "ja").'))] = None,
        smart: Annotated[bool, Field(description=(
            "Opt into an LLM query rewrite of the free-text query before "
            "searching (page 1 only): a richer vector query, synonym expansion "
            "OR-ed into the keyword arms, and natural-language filters. The "
            "response carries `rewrite_status` (one of `applied`, "
            "`unavailable`, `failed`, `not_attempted`, `not_requested`), an "
            "optional curated `rewrite_note` with an actionable detail, and a "
            "machine-readable `rewrite_note_code` (`missing_model`, "
            "`unreachable`, `unparseable`, `not_configured`, "
            "`continuation_page`, or null); "
            "`rewrite_skipped` (kept for back-compat) is true only for "
            "`unavailable` and `failed`. On a continuation page `smart` is "
            "ignored and the status is `not_attempted`. Defaults to false."))]
            = False,
    ) -> dict[str, Any]:
        """Hybrid lexical + vector search over message text and extracted
        attachment text — the default way to answer "find mail about X".

        Results are ACL-scoped: only the accounts you have been granted are
        searched. Rank-ordered by default; pass `sort="date"` for strictly
        newest-first. Page forward by calling again with the returned
        `next_cursor` in `cursor`; a `null` next_cursor means there are no
        more results. If the cursor has expired (the result pool was evicted),
        re-run the same query without a cursor and skip rows you already hold.

        Use `list_messages` when you have no query and just want recent mail;
        use `get_message` to read a full message once a result surfaces its id.
        """
        if searcher is None:
            raise ToolError("search is unavailable: no searcher configured")
        user_id = _current_user_id()
        with pool.connection() as conn:
            allowed = allowed_account_ids(conn, user_id)
        filters = _build_filters(
            account_ids=account_ids,
            folder_ids=folder_ids,
            date_from=date_from,
            date_to=date_to,
            from_addr=from_addr,
            to=to,
            subject=subject,
            has_attachment=has_attachment,
            lang=lang,
        )
        try:
            return tools.tool_search(
                searcher=searcher,
                user_id=user_id,
                allowed_account_ids=allowed,
                query=query,
                sort=sort,
                limit=limit,
                cursor=cursor,
                filters=filters,
                smart=smart,
            )
        except SearchCursorExpired as exc:
            raise ToolError(
                "search cursor expired; re-run search without a cursor"
            ) from exc
        except ValidationFailed as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    def get_message(
        message_id: Annotated[str, Field(description=(
            "The message id to fetch (string integer), as returned by "
            "`search` or `list_messages`."))],
        full_headers: Annotated[bool, Field(description=(
            "True to include the complete raw header set; False (default) "
            "returns the common subset (From/To/Subject/Date/…)."))] = False,
    ) -> dict[str, Any]:
        """Fetch one message — headers, body, and attachment list — by id,
        ACL-scoped to your granted accounts.

        Call this after `search` or `list_messages` surfaces a message id.
        Returns not-found if the id doesn't exist *or* isn't in an account you
        may read (the two are indistinguishable by design). The returned
        attachment list carries the `sha256` values to pass to `get_attachment`.
        """
        user_id = _current_user_id()
        try:
            mid = parse_int_id(message_id, field="message_id")
        except ValidationFailed as exc:
            raise ToolError(str(exc)) from exc
        with pool.connection() as conn:
            allowed = allowed_account_ids(conn, user_id)
            try:
                return tools.tool_get_message(
                    conn,
                    message_id=mid,
                    allowed_account_ids=allowed,
                    full_headers=full_headers,
                )
            except NotFound as exc:
                raise ToolError(f"message {message_id} not found") from exc

    @server.tool()
    def get_attachment(
        sha256: Annotated[str, Field(description=(
            "The attachment's content hash (hex sha256), taken from a "
            "message's attachment list (see `get_message`)."))],
        mode: Annotated[Literal["text", "metadata"], Field(description=(
            'What to return: "text" (extracted plain text, the default) or '
            '"metadata" (filename, mime type, size).'))] = "text",
    ) -> dict[str, Any]:
        """Read an attachment's extracted text (`mode="text"`) or its blob
        metadata (`mode="metadata"`), keyed by sha256, ACL-scoped to your
        granted accounts.

        Never returns raw bytes — raw download is intentionally HTTP-only via
        `GET /v1/attachments/{sha256}` (stored HTML/SVG blobs are an XSS sink).
        `mode="text"` is not-found until the extraction worker has processed
        the blob; retry later or fall back to `mode="metadata"`.
        """
        user_id = _current_user_id()
        with pool.connection() as conn:
            allowed = allowed_account_ids(conn, user_id)
            try:
                return tools.tool_get_attachment(
                    conn,
                    sha256=sha256,
                    allowed_account_ids=allowed,
                    mode=mode,
                )
            except NotFound as exc:
                raise ToolError(f"attachment {sha256} not found") from exc
            except ValidationFailed as exc:
                raise ToolError(str(exc)) from exc
            except ValueError as exc:
                raise ToolError(str(exc)) from exc

    @server.tool()
    def list_messages(
        account_ids: Annotated[list[str] | None, Field(description=(
            "Restrict to these account ids (string integers). Omit for every "
            "account you may read; discover ids via `list_accounts`."))]
            = None,
        folder_ids: Annotated[list[str] | None, Field(description=(
            "Restrict to these folder/mailbox ids (string integers)."))]
            = None,
        limit: Annotated[int, Field(ge=1, le=200, description=(
            "Maximum messages in this page (1–200)."))] = 50,
        cursor: Annotated[str | None, Field(description=(
            "Opaque pagination cursor — pass back a previous response's "
            "`next_cursor` to get the next page; omit for the first page."))]
            = None,
    ) -> dict[str, Any]:
        """Browse messages newest-first with no search query — "show me recent
        mail" — ACL-scoped to your granted accounts.

        Keyset-paginated and date-ordered (most recent first). Narrow with
        `account_ids` / `folder_ids`, and page forward with `next_cursor`. Use
        `search` instead whenever you have a query to match against.
        """
        user_id = _current_user_id()
        try:
            account_id_ints = (
                [parse_int_id(a, field="account_id") for a in account_ids]
                if account_ids is not None
                else None
            )
            folder_id_ints = (
                [parse_int_id(f, field="folder_id") for f in folder_ids]
                if folder_ids is not None
                else None
            )
            with pool.connection() as conn:
                allowed = allowed_account_ids(conn, user_id)
                return tools.tool_list_messages(
                    conn,
                    allowed_account_ids=allowed,
                    account_ids=account_id_ints,
                    folder_ids=folder_id_ints,
                    limit=limit,
                    cursor=cursor,
                )
        except ValidationFailed as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    def list_accounts() -> list[dict[str, Any]]:
        """List the accounts you are allowed to read (your per-user ACL grants).

        Use this first to discover the `account_ids` you can pass as filters to
        `search` and `list_messages`. Returns an empty list when you have been
        granted no accounts.
        """
        user_id = _current_user_id()
        with pool.connection() as conn:
            allowed = allowed_account_ids(conn, user_id)
            return tools.tool_list_accounts(conn, allowed_account_ids=allowed)

    return server
