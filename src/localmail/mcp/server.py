"""FastMCP server exposing five ACL-scoped localmail tools.

Each tool resolves the authenticated user from the request's access token
(minted by `LocalmailTokenVerifier`), derives that user's allowed account ids,
and delegates to the transport-free tool bodies in `localmail.mcp.tools`.
Domain errors are mapped to `ToolError` so the agent sees a clean message
instead of a stack trace.
"""
from __future__ import annotations

from typing import Any, Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from psycopg_pool import ConnectionPool
from pydantic import AnyHttpUrl

from localmail.api.acl import allowed_account_ids
from localmail.api.errors import NotFound, SearchCursorExpired, ValidationFailed
from localmail.api.ids import parse_int_id
from localmail.config import McpConfig
import localmail.mcp.tools as tools
from localmail.mcp.auth import LocalmailTokenVerifier, user_id_from_access_token
from localmail.search.searcher import Searcher

SERVER_NAME = "localmail"


def _current_user_id() -> int:
    """The localmail user id for the in-flight MCP request.

    Raises `ToolError` outside an authenticated request (no access token).
    """
    access_token = get_access_token()
    if access_token is None:
        raise ToolError("not authenticated")
    return user_id_from_access_token(access_token)


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
) -> FastMCP:
    """Construct the FastMCP server with five ACL-scoped tools.

    `token_verifier` requires `auth` settings (the SDK rejects one without the
    other); the issuer / resource-server URLs come from `config`.
    """
    server = FastMCP(
        SERVER_NAME,
        stateless_http=True,
        json_response=True,
        token_verifier=LocalmailTokenVerifier(pool),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(config.issuer_url),
            resource_server_url=AnyHttpUrl(config.resource_server_url),
            required_scopes=[],
        ),
    )

    @server.tool()
    def search(
        query: str,
        sort: Literal["rank", "date"] = "rank",
        limit: int = 50,
        cursor: str | None = None,
        account_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        from_addr: str | None = None,
        to: str | None = None,
        subject: str | None = None,
        has_attachment: bool | None = None,
        lang: str | None = None,
    ) -> dict[str, Any]:
        """Hybrid lexical+vector search over messages and attachment text.

        ACL-scoped to the caller's accounts. Page forward by passing back
        `next_cursor`; a `null` next_cursor means the pool is exhausted.
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
            )
        except SearchCursorExpired as exc:
            raise ToolError(
                "search cursor expired; re-run search without a cursor"
            ) from exc
        except ValidationFailed as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    def get_message(
        message_id: str,
        full_headers: bool = False,
    ) -> dict[str, Any]:
        """One message — headers, body, attachment list — ACL-scoped."""
        user_id = _current_user_id()
        mid = parse_int_id(message_id, field="message_id")
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
        sha256: str,
        mode: str = "text",
    ) -> dict[str, Any]:
        """Extracted attachment text (`mode="text"`) or blob metadata
        (`mode="metadata"`), ACL-scoped. Never raw bytes.
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
            except ValueError as exc:
                raise ToolError(str(exc)) from exc

    @server.tool()
    def list_messages(
        account_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Keyset date-ordered browse page, ACL-scoped."""
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
        except ValidationFailed as exc:
            raise ToolError(str(exc)) from exc
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

    @server.tool()
    def list_accounts() -> list[dict[str, Any]]:
        """The accounts this caller may read."""
        user_id = _current_user_id()
        with pool.connection() as conn:
            allowed = allowed_account_ids(conn, user_id)
            return tools.tool_list_accounts(conn, allowed_account_ids=allowed)

    return server
