# MCP Server (Search Phase 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the localmail archive's read surface to AI agents as a remote, multi-user, ACL-scoped MCP server mounted into the existing `localmail serve` app at `/mcp` over Streamable HTTP.

**Architecture:** A new `src/localmail/mcp/` package builds a `FastMCP` server whose tools call the existing transport-free `localmail.api` accessors directly (no HTTP hop), reusing `app.state.pool` and `app.state.searcher`. Auth is opaque-bearer: a `TokenVerifier` wraps the existing `api.auth.verify_token`; every tool resolves `allowed_account_ids` and passes it to the accessor so the ACL applies at the SQL boundary. The server is mounted into `create_app` only when the optional `mcp` extra is importable and `[mcp].enabled` is set.

**Tech Stack:** Python ≥3.12, `uv`, FastAPI/Starlette, the official `mcp` Python SDK (`FastMCP`), psycopg v3, pydantic v2, pytest.

---

## Design reconciliation (plan-time refinements of the spec)

The spec was written before the exact `api/` contract was re-read. Three refinements, none reducing capability:

1. **No `wire.py` module / no route refactor.** Every `api/` accessor already returns a fully API-shaped `dict` (`run_search`, `get_message`, `list_messages`, `list_accounts`, `get_attachment_text`, `get_attachment_metadata`); the HTTP routes are thin pass-throughs. The "single shared serializer" property the spec wanted is *already* satisfied because both surfaces call `api/`. MCP tools call the same accessors. Spec risk #4 is void.

2. **Search is ONE tool, not three.** The shipped `run_search(*, searcher, free_text, filters, limit, allowed_account_ids, user_id, sort, cursor)` takes a single optional `cursor` and **auto-grows** the rerank pool internally (it calls `grow_pool` on `PageOutOfPoolError`). Paging = call `search` again with the returned `next_cursor`; growth is automatic. So the old `search_page`/`search_grow` split doesn't match reality. Final tool set: **`search`, `get_message`, `get_attachment`, `list_messages`, `list_accounts`** (5 tools). This mirrors the HTTP `/v1/search` endpoint 1:1 (one POST taking query+filters+cursor).

3. **`get_message` param is `full_headers`,** not `include_body`/`include_attachments` — the message dict already embeds body + attachments; the only knob the accessor exposes is compact-vs-full headers. `run_search` exposes no `candidates_per_arm` (config-driven) so the tool drops it (YAGNI).

These are recorded here and will be carried into the CLAUDE.md notes at the end.

---

## File Structure

**Create:**
- `src/localmail/mcp/__init__.py` — public boundary: `build_mcp_server(pool, searcher, *, config) -> FastMCP` and `mcp_streamable_app(server) -> ASGI app`.
- `src/localmail/mcp/auth.py` — `LocalmailTokenVerifier(TokenVerifier)` bridging `api.auth.verify_token`; carries `user_id` on the returned `AccessToken`.
- `src/localmail/mcp/tools.py` — pure-ish tool bodies: `tool_search`, `tool_get_message`, `tool_get_attachment`, `tool_list_messages`, `tool_list_accounts`, each `(conn|searcher, *, user_id, allowed_account_ids, **params) -> dict`. Plus `MODE_TEXT`/`MODE_METADATA` constants and an error-mapping helper.
- `src/localmail/mcp/server.py` — `FastMCP` instance creation + `@tool` registrations (thin wrappers: resolve principal → open conn → resolve ACL → call body → map errors).
- `tests/test_mcp_config.py`, `tests/test_mcp_auth.py`, `tests/test_mcp_tools.py`, `tests/test_mcp_mount.py`, `tests/test_mcp_integration.py`.
- `docs/mcp-usage.md` — operator/agent setup guide.

**Modify:**
- `pyproject.toml` — add `[project.optional-dependencies] mcp = [...]`.
- `src/localmail/config.py` — add `McpConfig` + `Config.mcp`.
- `src/localmail/serve/app.py` — `create_app(..., enable_mcp=False)`: conditional mount + lifespan composition.
- `src/localmail/cli.py` — `serve` command passes `enable_mcp=cfg.mcp.enabled`.
- `config.example.toml`, `README.md`, `CLAUDE.md` — docs.

**SDK note (read once before Task 2):** Documented import paths (validate against the pinned SDK version in Task 2; adapt if the installed SDK differs):
- `from mcp.server.fastmcp import FastMCP`
- `from mcp.server.auth.provider import TokenVerifier, AccessToken`
- `from mcp.server.auth.settings import AuthSettings`
- `from mcp.server.auth.middleware.auth_context import get_access_token` (current authenticated `AccessToken` inside a tool)
- `server.streamable_http_app()` → ASGI app; `server.session_manager.run()` → async CM that must wrap the app's lifespan.

---

## Task 1: `[mcp]` extra + `McpConfig`

**Files:**
- Modify: `pyproject.toml:40-43` (optional-dependencies block)
- Modify: `src/localmail/config.py` (new `McpConfig`, add to `Config`)
- Test: `tests/test_mcp_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_config.py
"""McpConfig parsing + defaults."""
from localmail.config import Config, McpConfig


def test_mcp_defaults_disabled():
    cfg = McpConfig()
    assert cfg.enabled is False


def test_config_parses_mcp_block():
    cfg = Config.model_validate({
        "database": {"dsn": "postgresql:///x"},
        "mcp": {"enabled": True},
    })
    assert cfg.mcp.enabled is True


def test_config_mcp_defaults_when_absent():
    cfg = Config.model_validate({"database": {"dsn": "postgresql:///x"}})
    assert cfg.mcp.enabled is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'McpConfig'`.

- [ ] **Step 3: Add `McpConfig` and wire it into `Config`**

In `src/localmail/config.py`, after `class UpgradeEstimateConfig` (near line 453), add:

```python
class McpConfig(BaseModel):
    """Model Context Protocol server settings.

    The MCP server is mounted into `localmail serve` at `/mcp` only when
    `enabled` is true AND the optional `mcp` extra is installed. Opt-in by
    default, mirroring `search.reranker_enabled`.
    """

    enabled: bool = False
```

In `class Config` (line 486), add the field alongside the others:

```python
    mcp: McpConfig = Field(default_factory=McpConfig)
```

- [ ] **Step 4: Add the `mcp` extra to `pyproject.toml`**

Replace the optional-dependencies block (lines 40-43) so it reads:

```toml
[project.optional-dependencies]
extraction = [
    "docling>=2.94.0",
]
mcp = [
    "mcp>=1.13.0",
]
```

(`mcp>=1.13.0` is a known-good floor that ships `TokenVerifier` + `streamable_http_app`; Task 2 pins the exact installed version if it differs.)

- [ ] **Step 5: Sync the extra and run tests**

Run: `unset VIRTUAL_ENV && uv sync --extra mcp && uv run pytest tests/test_mcp_config.py -v`
Expected: PASS (3 tests). `uv sync` resolves `mcp` into the dev environment.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/localmail/config.py tests/test_mcp_config.py
git commit -m "feat(mcp): add [mcp] extra and McpConfig (enabled=false default)"
```

---

## Task 2: `LocalmailTokenVerifier`

A `TokenVerifier` that validates an opaque bearer against `api_tokens` and carries the resolved `user_id` so tools can scope by ACL.

**Files:**
- Create: `src/localmail/mcp/__init__.py` (empty package marker for now)
- Create: `src/localmail/mcp/auth.py`
- Test: `tests/test_mcp_auth.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_auth.py
"""LocalmailTokenVerifier bridges api_tokens -> MCP AccessToken."""
import asyncio

import pytest

from localmail.api.auth import create_user, issue_token
from localmail.mcp.auth import LocalmailTokenVerifier, user_id_from_access_token


def _verify(pool, token):
    v = LocalmailTokenVerifier(pool)
    return asyncio.run(v.verify_token(token))


def test_valid_token_yields_access_token_with_user_id(pool):
    with pool.connection() as conn:
        user = create_user(conn, "agent", "pw-hash-placeholder")
        raw = issue_token(conn, user.id)
        conn.commit()
    at = _verify(pool, raw)
    assert at is not None
    assert user_id_from_access_token(at) == user.id


def test_invalid_token_yields_none(pool):
    assert _verify(pool, "not-a-real-token") is None


def test_malformed_empty_token_yields_none(pool):
    assert _verify(pool, "") is None
```

> NOTE for the implementer: confirm the real helper names in `localmail.api.auth`
> (`create_user`, `issue_token`, `verify_token`, the user object's `.id`). Read
> `src/localmail/api/auth.py` and adjust the fixture calls if the public helpers
> differ; the *behaviour* under test (valid→AccessToken, invalid→None) is fixed.

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'localmail.mcp.auth'`.

- [ ] **Step 3: Implement `auth.py`**

```python
# src/localmail/mcp/auth.py
"""Opaque-bearer TokenVerifier for the MCP server.

Validates a bearer against the existing `api_tokens` store via
`api.auth.verify_token`. No OAuth authorization server — localmail mints
tokens through `/v1/auth/login`; this verifier only *checks* them.
"""
from __future__ import annotations

from mcp.server.auth.provider import AccessToken, TokenVerifier
from psycopg_pool import ConnectionPool

from localmail.api.auth import verify_token as api_verify_token

# We carry the localmail user id in AccessToken.client_id (a free-form string
# in the MCP model). Tools read it back via `user_id_from_access_token`.
_NO_EXPIRY: int | None = None
_NO_SCOPES: list[str] = []


class LocalmailTokenVerifier(TokenVerifier):
    """Resource-server verifier backed by `api_tokens`."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        with self._pool.connection() as conn:
            user = api_verify_token(conn, token)
            conn.commit()
        if user is None:
            return None
        return AccessToken(
            token=token,
            client_id=str(user.id),
            scopes=_NO_SCOPES,
            expires_at=_NO_EXPIRY,
        )


def user_id_from_access_token(access_token: AccessToken) -> int:
    """Recover the localmail user id stashed in `client_id`."""
    return int(access_token.client_id)
```

> The SDK `verify_token` is declared `async`, but `api_verify_token` is sync
> psycopg. A sync DB call inside an async method blocks the event loop briefly;
> that is acceptable here (one indexed SELECT). If the SDK version's signature
> differs, adapt the method shape, not the behaviour.

- [ ] **Step 4: Run tests**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_auth.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/__init__.py src/localmail/mcp/auth.py tests/test_mcp_auth.py
git commit -m "feat(mcp): LocalmailTokenVerifier validating api_tokens bearer"
```

---

## Task 3: Tool bodies — search

**Files:**
- Create: `src/localmail/mcp/tools.py`
- Test: `tests/test_mcp_tools.py`

Tool bodies are plain functions tested directly against `db_conn`, with no MCP transport. This task adds `tool_search`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_tools.py
"""MCP tool bodies — thin api/ wrappers, ACL-scoped."""
from localmail.mcp import tools
from localmail.search import create_searcher


def test_tool_search_scopes_to_allowed_accounts(db_conn, seeded_two_accounts):
    """A query matching mail in BOTH accounts returns only the granted one."""
    granted = seeded_two_accounts["account_a_id"]
    searcher = create_searcher(db_conn_factory=lambda: db_conn)  # see NOTE
    page = tools.tool_search(
        searcher=searcher,
        user_id=seeded_two_accounts["user_id"],
        allowed_account_ids=[granted],
        query="invoice",
        sort="rank",
        limit=20,
        cursor=None,
        filters={},
    )
    assert "results" in page
    assert "next_cursor" in page
    for r in page["results"]:
        assert r["account_id"] == str(granted)


def test_tool_search_empty_grants_returns_empty(db_conn, seeded_two_accounts):
    searcher = create_searcher(db_conn_factory=lambda: db_conn)
    page = tools.tool_search(
        searcher=searcher, user_id=seeded_two_accounts["user_id"],
        allowed_account_ids=[], query="invoice", sort="rank",
        limit=20, cursor=None, filters={},
    )
    assert page == {"results": [], "next_cursor": None,
                    "total_estimate": 0, "took_ms": 0.0}
```

> NOTE: `create_searcher`'s real construction signature lives in
> `src/localmail/search/__init__.py` — read it and build the searcher the way
> the existing search tests (`tests/test_searcher_*.py`) do (they already have a
> working fixture). Reuse that fixture rather than inventing `db_conn_factory`.
> Add a `seeded_two_accounts` fixture to `tests/conftest.py` (or a local fixture)
> that creates a user, two accounts, ONE `user_accounts` grant, and a couple of
> messages per account containing the word "invoice". Mirror the seeding helpers
> already used in `tests/test_api_search*.py` / `tests/test_serve_search_route.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'localmail.mcp.tools'`.

- [ ] **Step 3: Implement `tool_search`**

```python
# src/localmail/mcp/tools.py
"""MCP tool bodies. Each is a thin, ACL-scoped wrapper over a localmail.api
accessor. Transport-free and individually unit-testable against a real conn.
"""
from __future__ import annotations

from typing import Any

import psycopg

from localmail.api.attachments import (
    get_attachment_metadata,
    get_attachment_text,
)
from localmail.api.browse import list_messages as api_list_messages
from localmail.api.accounts import list_accounts as api_list_accounts
from localmail.api.messages import get_message as api_get_message
from localmail.api.search import run_search
from localmail.search import Searcher

MODE_TEXT = "text"
MODE_METADATA = "metadata"


def tool_search(
    *,
    searcher: Searcher,
    user_id: int,
    allowed_account_ids: list[int],
    query: str,
    sort: str = "rank",
    limit: int = 50,
    cursor: str | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Hybrid search, ACL-scoped. Page forward by passing back `next_cursor`."""
    return run_search(
        searcher=searcher,
        free_text=query,
        filters=filters or {},
        limit=limit,
        allowed_account_ids=allowed_account_ids,
        user_id=user_id,
        sort=sort,  # type: ignore[arg-type]
        cursor=cursor,
    )
```

- [ ] **Step 4: Run tests**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_tools.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/tools.py tests/test_mcp_tools.py tests/conftest.py
git commit -m "feat(mcp): tool_search body (ACL-scoped run_search wrapper)"
```

---

## Task 4: Tool bodies — get_message, list_messages, list_accounts

**Files:**
- Modify: `src/localmail/mcp/tools.py`
- Test: `tests/test_mcp_tools.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_mcp_tools.py

def test_tool_get_message_granted(db_conn, seeded_two_accounts):
    mid = seeded_two_accounts["message_in_a_id"]
    msg = tools.tool_get_message(
        db_conn, message_id=mid,
        allowed_account_ids=[seeded_two_accounts["account_a_id"]],
    )
    assert msg["id"] == str(mid)


def test_tool_get_message_denied_raises_notfound(db_conn, seeded_two_accounts):
    from localmail.api.errors import NotFound
    mid = seeded_two_accounts["message_in_b_id"]  # account NOT granted
    import pytest
    with pytest.raises(NotFound):
        tools.tool_get_message(
            db_conn, message_id=mid,
            allowed_account_ids=[seeded_two_accounts["account_a_id"]],
        )


def test_tool_list_messages_scopes(db_conn, seeded_two_accounts):
    page = tools.tool_list_messages(
        db_conn,
        allowed_account_ids=[seeded_two_accounts["account_a_id"]],
    )
    assert "messages" in page and "next_cursor" in page
    for m in page["messages"]:
        assert m["account_id"] == str(seeded_two_accounts["account_a_id"])


def test_tool_list_accounts_returns_only_granted(db_conn, seeded_two_accounts):
    accounts = tools.tool_list_accounts(
        db_conn, allowed_account_ids=[seeded_two_accounts["account_a_id"]],
    )
    ids = {a["id"] for a in accounts}
    assert ids == {str(seeded_two_accounts["account_a_id"])}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_tools.py -v`
Expected: FAIL — `AttributeError: module 'localmail.mcp.tools' has no attribute 'tool_get_message'`.

- [ ] **Step 3: Implement the three bodies**

```python
# append to src/localmail/mcp/tools.py

def tool_get_message(
    conn: psycopg.Connection,
    *,
    message_id: int,
    allowed_account_ids: list[int],
    full_headers: bool = False,
) -> dict[str, Any]:
    """One message (headers, body, attachment list), ACL-scoped.

    Raises localmail.api.errors.NotFound when the message is absent OR the
    caller lacks a grant on its account (indistinguishable by design).
    """
    return api_get_message(
        conn, message_id,
        allowed_account_ids=allowed_account_ids,
        full_headers=full_headers,
    )


def tool_list_messages(
    conn: psycopg.Connection,
    *,
    allowed_account_ids: list[int],
    account_ids: list[int] | None = None,
    folder_ids: list[int] | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Keyset date-ordered browse page, ACL-scoped."""
    return api_list_messages(
        conn,
        allowed_account_ids=allowed_account_ids,
        account_ids=account_ids,
        folder_ids=folder_ids,
        limit=limit,
        cursor=cursor,
    )


def tool_list_accounts(
    conn: psycopg.Connection,
    *,
    allowed_account_ids: list[int],
) -> list[dict[str, Any]]:
    """The accounts this caller may read."""
    return api_list_accounts(conn, allowed_account_ids=allowed_account_ids)
```

- [ ] **Step 4: Run tests**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_tools.py -v`
Expected: PASS (all tool-body tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/tools.py tests/test_mcp_tools.py
git commit -m "feat(mcp): get_message/list_messages/list_accounts tool bodies"
```

---

## Task 5: Tool body — get_attachment (text/metadata)

**Files:**
- Modify: `src/localmail/mcp/tools.py`
- Test: `tests/test_mcp_tools.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_mcp_tools.py

def test_tool_get_attachment_text(db_conn, seeded_attachment):
    out = tools.tool_get_attachment(
        db_conn, sha256=seeded_attachment["sha256"], mode="text",
        allowed_account_ids=[seeded_attachment["account_id"]],
    )
    assert out["mode"] == "text"
    assert out["sha256"] == seeded_attachment["sha256"]
    assert out["text"] == seeded_attachment["expected_text"]


def test_tool_get_attachment_metadata(db_conn, seeded_attachment):
    out = tools.tool_get_attachment(
        db_conn, sha256=seeded_attachment["sha256"], mode="metadata",
        allowed_account_ids=[seeded_attachment["account_id"]],
    )
    assert out["mode"] == "metadata"
    assert "metadata" in out


def test_tool_get_attachment_bad_mode_raises(db_conn, seeded_attachment):
    import pytest
    with pytest.raises(ValueError):
        tools.tool_get_attachment(
            db_conn, sha256=seeded_attachment["sha256"], mode="bytes",
            allowed_account_ids=[seeded_attachment["account_id"]],
        )
```

> NOTE: add a `seeded_attachment` fixture creating an account+message+blob and an
> `attachment_text` row. Reuse the blob/text seeding already used in
> `tests/test_api_attachments*.py` or `tests/test_serve_attachments*.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_tools.py -k attachment -v`
Expected: FAIL — `AttributeError: ... 'tool_get_attachment'`.

- [ ] **Step 3: Implement `tool_get_attachment`**

```python
# append to src/localmail/mcp/tools.py

def tool_get_attachment(
    conn: psycopg.Connection,
    *,
    sha256: str,
    allowed_account_ids: list[int],
    mode: str = MODE_TEXT,
) -> dict[str, Any]:
    """Extracted attachment text or metadata, ACL-scoped. Never raw bytes.

    `mode="text"` returns extracted text (NotFound if not yet extracted);
    `mode="metadata"` returns the blob metadata dict. Any other mode is a
    ValueError (raw bytes are intentionally HTTP-only).
    """
    if mode == MODE_TEXT:
        text = get_attachment_text(
            conn, sha256, allowed_account_ids=allowed_account_ids,
        )
        return {"mode": MODE_TEXT, "sha256": sha256, "text": text}
    if mode == MODE_METADATA:
        meta = get_attachment_metadata(
            conn, sha256, allowed_account_ids=allowed_account_ids,
        )
        return {"mode": MODE_METADATA, "sha256": sha256, "metadata": meta}
    raise ValueError(
        f"unsupported attachment mode {mode!r}; "
        f"expected {MODE_TEXT!r} or {MODE_METADATA!r}"
    )
```

- [ ] **Step 4: Run tests**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_tools.py -k attachment -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/tools.py tests/test_mcp_tools.py
git commit -m "feat(mcp): tool_get_attachment (text/metadata, never bytes)"
```

---

## Task 6: `server.py` — FastMCP instance + tool registration + error mapping

Wire the tool bodies into a `FastMCP` server. Each `@tool` wrapper: reads the authenticated `AccessToken`, recovers `user_id`, opens a pooled connection, resolves `allowed_account_ids`, calls the body, and maps known `api/` exceptions to clean MCP `ToolError`s.

**Files:**
- Create: `src/localmail/mcp/server.py`
- Modify: `src/localmail/mcp/__init__.py`
- Test: `tests/test_mcp_server_build.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_server_build.py
"""build_mcp_server wires five tools onto a FastMCP instance."""
import asyncio

from localmail.config import McpConfig
from localmail.mcp import build_mcp_server


def test_build_registers_expected_tools(pool):
    server = build_mcp_server(pool, searcher=None, config=McpConfig(enabled=True))
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {
        "search", "get_message", "get_attachment",
        "list_messages", "list_accounts",
    }
```

> NOTE: `FastMCP.list_tools()` is async in the SDK; the test drives it with
> `asyncio.run`. If the pinned SDK exposes a sync registry accessor, use that
> instead — the assertion (exactly these five tool names) is what matters.

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_server_build.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_mcp_server'`.

- [ ] **Step 3: Implement `server.py`**

```python
# src/localmail/mcp/server.py
"""FastMCP server construction + tool registration for localmail.

Tools are thin wrappers: authenticate (already done by the TokenVerifier
middleware) -> recover user_id -> resolve ACL -> call a tools.py body ->
map known api errors to ToolError. The bodies live in tools.py and are
unit-tested without the transport.
"""
from __future__ import annotations

from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from psycopg_pool import ConnectionPool

from localmail.api.acl import allowed_account_ids
from localmail.api.errors import NotFound, SearchCursorExpired, ValidationFailed
from localmail.api.ids import parse_int_id
from localmail.config import McpConfig
from localmail.mcp import tools
from localmail.mcp.auth import LocalmailTokenVerifier, user_id_from_access_token

SERVER_NAME = "localmail"


def _current_user_id() -> int:
    at = get_access_token()
    if at is None:  # pragma: no cover - middleware guarantees a token
        raise ToolError("not authenticated")
    return user_id_from_access_token(at)


def build_mcp_server(
    pool: ConnectionPool,
    *,
    searcher: Any,
    config: McpConfig,
) -> FastMCP:
    """Construct the FastMCP server with the five read tools registered."""
    server = FastMCP(
        SERVER_NAME,
        stateless_http=True,
        json_response=True,
        token_verifier=LocalmailTokenVerifier(pool),
        auth=AuthSettings(required_scopes=[]),
    )

    @server.tool()
    def search(
        query: str,
        sort: str = "rank",
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
        """Hybrid lexical+vector search over your mail. Page forward by
        passing the returned `next_cursor` back as `cursor`."""
        filters = _build_filters(
            account_ids=account_ids, folder_ids=folder_ids,
            date_from=date_from, date_to=date_to, from_addr=from_addr,
            to=to, subject=subject, has_attachment=has_attachment, lang=lang,
        )
        user_id = _current_user_id()
        with pool.connection() as conn:
            allowed = allowed_account_ids(conn, user_id)
        try:
            return tools.tool_search(
                searcher=searcher, user_id=user_id, allowed_account_ids=allowed,
                query=query, sort=sort, limit=limit, cursor=cursor,
                filters=filters,
            )
        except SearchCursorExpired:
            raise ToolError(
                "search cursor expired; re-run search without a cursor"
            )
        except ValidationFailed as e:
            raise ToolError(str(e))

    @server.tool()
    def get_message(
        message_id: str,
        full_headers: bool = False,
    ) -> dict[str, Any]:
        """Fetch one message by id: headers, body, attachment list."""
        mid = parse_int_id(message_id, field="message_id")
        user_id = _current_user_id()
        with pool.connection() as conn:
            allowed = allowed_account_ids(conn, user_id)
            try:
                return tools.tool_get_message(
                    conn, message_id=mid, allowed_account_ids=allowed,
                    full_headers=full_headers,
                )
            except NotFound:
                raise ToolError(f"message {message_id} not found")

    @server.tool()
    def get_attachment(sha256: str, mode: str = "text") -> dict[str, Any]:
        """Extracted attachment text (mode='text') or metadata
        (mode='metadata'). Raw bytes are not available over MCP."""
        user_id = _current_user_id()
        with pool.connection() as conn:
            allowed = allowed_account_ids(conn, user_id)
            try:
                return tools.tool_get_attachment(
                    conn, sha256=sha256, allowed_account_ids=allowed, mode=mode,
                )
            except NotFound:
                raise ToolError(f"attachment {sha256} not found or not extracted")
            except ValueError as e:
                raise ToolError(str(e))

    @server.tool()
    def list_messages(
        account_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Browse your mail newest-first (no query). Page with `next_cursor`."""
        user_id = _current_user_id()
        parsed_acc = [parse_int_id(a, field="account_id") for a in account_ids or []]
        parsed_fol = [parse_int_id(f, field="folder_id") for f in folder_ids or []]
        with pool.connection() as conn:
            allowed = allowed_account_ids(conn, user_id)
            try:
                return tools.tool_list_messages(
                    conn, allowed_account_ids=allowed,
                    account_ids=parsed_acc or None,
                    folder_ids=parsed_fol or None,
                    limit=limit, cursor=cursor,
                )
            except ValidationFailed as e:
                raise ToolError(str(e))

    @server.tool()
    def list_accounts() -> list[dict[str, Any]]:
        """The mail accounts you have access to."""
        user_id = _current_user_id()
        with pool.connection() as conn:
            allowed = allowed_account_ids(conn, user_id)
            return tools.tool_list_accounts(conn, allowed_account_ids=allowed)

    return server


def _build_filters(**kwargs: Any) -> dict[str, Any]:
    """Drop None-valued filter keys; map `from_addr` -> the `from` filter key."""
    out: dict[str, Any] = {}
    from_addr = kwargs.pop("from_addr", None)
    if from_addr is not None:
        out["from"] = from_addr
    for key, value in kwargs.items():
        if value is not None:
            out[key] = value
    return out
```

> The exact `api.errors` exception names (`SearchCursorExpired`,
> `ValidationFailed`, `NotFound`) must be confirmed against
> `src/localmail/api/errors.py`. If `SearchCursorExpired` is named differently
> (e.g. the HTTP layer raises it elsewhere), import the right type. The filter
> key names must match what `run_search`/`build_query_string` expect
> (`src/localmail/api/search.py` `_filter_tokens`) — read it and align
> `_build_filters` keys exactly.

- [ ] **Step 4: Export from `__init__.py`**

```python
# src/localmail/mcp/__init__.py
"""localmail MCP server (Search Phase 3). Gated by the [mcp] extra."""
from localmail.mcp.server import build_mcp_server

__all__ = ["build_mcp_server"]
```

- [ ] **Step 5: Run tests**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_server_build.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/mcp/server.py src/localmail/mcp/__init__.py tests/test_mcp_server_build.py
git commit -m "feat(mcp): FastMCP server with five ACL-scoped tools + error mapping"
```

---

## Task 7: Mount into `create_app` (conditional + lifespan composition)

**Files:**
- Modify: `src/localmail/serve/app.py` (`create_app` signature, lifespan, mount)
- Test: `tests/test_mcp_mount.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_mount.py
"""create_app mounts /mcp only when enabled (and the extra is importable)."""
from localmail.serve.app import create_app


def _has_mcp_mount(app) -> bool:
    return any(getattr(r, "path", "").rstrip("/") == "/mcp" for r in app.routes)


def test_mcp_not_mounted_by_default(db_dsn):
    app = create_app(db_dsn=db_dsn)
    assert not _has_mcp_mount(app)


def test_mcp_mounted_when_enabled(db_dsn):
    app = create_app(db_dsn=db_dsn, enable_mcp=True)
    assert _has_mcp_mount(app)
```

> NOTE: `_has_mcp_mount` is a heuristic over `app.routes`; adjust the predicate to
> however Starlette `Mount` exposes its path in the installed version (a `Mount`
> has `.path == "/mcp"`). The assertion intent — mounted iff enabled — is fixed.

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_mount.py -v`
Expected: FAIL — `create_app() got an unexpected keyword argument 'enable_mcp'`.

- [ ] **Step 3: Implement the mount**

In `src/localmail/serve/app.py`:

1. Add `enable_mcp: bool = False` to the `create_app` signature (next to `enable_control_socket`).

2. Add a module-level helper above `create_app`:

```python
def _try_build_mcp(pool, searcher, mcp_config):
    """Return (mcp_server, asgi_app) or (None, None) if the extra is absent."""
    try:
        from localmail.mcp import build_mcp_server
    except ImportError:
        logging.getLogger("localmail.serve").info(
            "MCP enabled but the [mcp] extra is not installed; skipping /mcp mount"
        )
        return None, None
    server = build_mcp_server(pool, searcher=searcher, config=mcp_config)
    return server, server.streamable_http_app()
```

3. Build the server before the `lifespan` definition (so the lifespan can close over its `session_manager`):

```python
    mcp_config = serve_config.mcp if serve_config else McpConfig()
    mcp_server = None
    mcp_app = None
    if enable_mcp:
        mcp_server, mcp_app = _try_build_mcp(pool, searcher, mcp_config)
```

   > `ServeConfig` does not carry `mcp`; the top-level `Config.mcp` does. Pass the
   > `McpConfig` into `create_app` explicitly instead — add a
   > `mcp_config: McpConfig | None = None` parameter and use
   > `mcp_config or McpConfig()`. Update the `serve` CLI (Task 8) to pass
   > `mcp_config=cfg.mcp, enable_mcp=cfg.mcp.enabled`. Adjust the snippet above to
   > read the parameter, not `serve_config.mcp`.

4. In the `lifespan` body, wrap the existing `yield` with the MCP session manager when present:

```python
    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        css: ControlSocketServer | None = None
        # ... existing control-socket startup ...
        try:
            with pool.connection() as conn:
                n = _imports_svc.reconcile_orphaned_jobs(conn)
                conn.commit()
            if n:
                logging.getLogger("localmail.serve").warning(
                    "reconciled %d orphaned import job(s) at startup", n)
            if mcp_server is not None:
                async with mcp_server.session_manager.run():
                    yield
            else:
                yield
        finally:
            # ... existing teardown ...
```

5. After `app = FastAPI(lifespan=lifespan)` and the `app.state.*` assignments, mount the MCP app:

```python
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)
```

   Add `from localmail.config import McpConfig` to the imports if not present.

- [ ] **Step 4: Run tests**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_mount.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the broader serve suite to confirm no regression**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_*.py -q`
Expected: PASS (existing serve tests unaffected — MCP is off by default).

- [ ] **Step 6: Commit**

```bash
git add src/localmail/serve/app.py tests/test_mcp_mount.py
git commit -m "feat(mcp): conditional /mcp mount + session-manager lifespan in create_app"
```

---

## Task 8: `serve` CLI wiring

**Files:**
- Modify: `src/localmail/cli.py` (the `serve` command)
- Test: extend `tests/test_mcp_mount.py` or the existing serve-CLI test

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_mcp_mount.py

def test_serve_cli_passes_mcp_enabled(monkeypatch, db_dsn, tmp_path):
    """`localmail serve` threads cfg.mcp into create_app."""
    captured = {}

    import localmail.cli as cli

    def fake_create_app(**kwargs):
        captured.update(kwargs)
        from localmail.serve.app import create_app as real
        return real(**{k: v for k, v in kwargs.items()})

    # See NOTE: this mirrors however the existing serve-command tests stub
    # create_app / uvicorn.run. Reuse that harness rather than inventing one.
    ...
```

> NOTE: there is almost certainly an existing test that exercises the `serve`
> command (search `tests/` for `def test_serve` and how it stubs `uvicorn.run`).
> Extend that test to assert `enable_mcp` / `mcp_config` are forwarded, rather
> than writing a fresh harness. If no such test exists, assert at the unit level
> that the `serve` command builds the `create_app` kwargs with
> `enable_mcp=cfg.mcp.enabled`.

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_mount.py -k cli -v`
Expected: FAIL — `enable_mcp` not present in captured kwargs.

- [ ] **Step 3: Wire the CLI**

In the `serve` command body in `src/localmail/cli.py`, where `create_app(...)` is
called, add:

```python
        enable_mcp=cfg.mcp.enabled,
        mcp_config=cfg.mcp,
```

- [ ] **Step 4: Run tests**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_mount.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/cli.py tests/test_mcp_mount.py
git commit -m "feat(mcp): serve CLI forwards cfg.mcp to create_app"
```

---

## Task 9: End-to-end integration test (mounted app via MCP client)

Drive the real Streamable-HTTP endpoint through the MCP SDK client to prove the handshake, tool listing, a bearer-authenticated tool call, and ACL scoping end-to-end.

**Files:**
- Test: `tests/test_mcp_integration.py`

- [ ] **Step 1: Write the integration test**

```python
# tests/test_mcp_integration.py
"""End-to-end: mounted /mcp endpoint, bearer auth, ACL scoping."""
import pytest

pytest.importorskip("mcp")  # skip cleanly if the extra is absent

# Mark integration so it can be deselected in fast CI lanes if needed.
pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_initialize_list_and_call_search(db_dsn, seeded_two_accounts_dsn):
    """initialize -> tools/list (5 tools) -> call search scoped to one account."""
    from httpx import ASGITransport, AsyncClient
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client  # path may differ

    from localmail.serve.app import create_app
    from localmail.search import create_searcher

    searcher = create_searcher(...)  # build as the search tests do
    app = create_app(db_dsn=db_dsn, enable_mcp=True, searcher=searcher,
                     mcp_config=__import__("localmail.config",
                                           fromlist=["McpConfig"]).McpConfig(enabled=True))
    token = seeded_two_accounts_dsn["bearer_for_user"]

    # Drive the mounted /mcp app in-process over ASGI with the bearer header.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        # Use the MCP client session over the streamable-http URL /mcp,
        # passing Authorization: Bearer <token>. The exact client wiring
        # (streamablehttp_client signature, how to inject the ASGI transport
        # or a live uvicorn) is SDK-version-specific — see NOTE.
        ...
```

> NOTE — this is the de-risking task for spec risks #1 and #2. The MCP client's
> exact API for connecting over Streamable HTTP (and whether it can ride an ASGI
> transport in-process or needs a real `uvicorn` on a loopback port) depends on
> the pinned SDK version. Acceptable strategies, in order of preference:
> 1. If the SDK client accepts an httpx transport/URL, drive the in-process
>    ASGI app directly.
> 2. Otherwise, start the app on an ephemeral loopback port with `uvicorn` in a
>    thread (mirror any existing serve integration test that does this), then
>    point `streamablehttp_client("http://127.0.0.1:<port>/mcp", headers={...})`.
> Assertions to make once connected:
> - `initialize` succeeds.
> - `session.list_tools()` returns exactly the five tool names.
> - `session.call_tool("list_accounts", {})` with the bearer returns ONLY the
>   granted account.
> - `session.call_tool("search", {"query": "invoice"})` returns results whose
>   `account_id` is the granted account only.
> - A call with NO / a bad bearer is rejected (401 / auth error).

- [ ] **Step 2: Run the integration test**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_integration.py -v`
Expected: PASS. If the SDK client API differs from the documented shape, adapt
the connection wiring (not the assertions) until green. **This task is done only
when an MCP client really talks to the mounted endpoint and ACL scoping holds.**

- [ ] **Step 3: Commit**

```bash
git add tests/test_mcp_integration.py
git commit -m "test(mcp): end-to-end Streamable-HTTP handshake + ACL scoping"
```

---

## Task 10: Full verification gate

- [ ] **Step 1: Full suite**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/`
Expected: all pass (the pre-existing macOS `test_daemon_control_socket` AF_UNIX
failures are the only acceptable reds — confirm they are identical on `main`).

- [ ] **Step 2: Type check**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: clean. Fix any `Any`-leak or missing-annotation findings in `mcp/`.

- [ ] **Step 3: Lint the new files**

Run: `unset VIRTUAL_ENV && uv run ruff check src/localmail/mcp tests/test_mcp_*.py`
Expected: clean (repo-wide pre-existing ruff debt is out of scope).

- [ ] **Step 4: Commit any fixes**

```bash
git add -A && git commit -m "chore(mcp): mypy/ruff cleanups"
```

---

## Task 11: Documentation

**Files:**
- Create: `docs/mcp-usage.md`
- Modify: `config.example.toml`, `README.md`, `CLAUDE.md`

- [ ] **Step 1: `config.example.toml`**

Add a commented block:

```toml
# [mcp]
# enabled = false   # mount the MCP server at /mcp inside `localmail serve`
#                   # (requires installing the optional `mcp` extra:
#                   #   uv sync --extra mcp)
```

- [ ] **Step 2: `docs/mcp-usage.md`**

Write an operator/agent guide covering: install the `mcp` extra; set
`[mcp].enabled = true`; create an `api_user` + grant accounts
(`localmail add-api-user`, `localmail grant-account`); obtain a bearer via
`POST /v1/auth/login`; configure an MCP client (Claude Desktop / Code) to reach
`https://host:8443/mcp` with `Authorization: Bearer <token>`; the five tools and
their parameters; that attachment raw bytes stay on `/v1/attachments`.

- [ ] **Step 3: `README.md`** — add a short "MCP server" subsection under the GUI
server section pointing at `docs/mcp-usage.md` and listing the five tools.

- [ ] **Step 4: `CLAUDE.md`** — add an "MCP server (search Phase 3)" subsection
recording: mounted into `serve` at `/mcp`, Streamable HTTP, opaque-bearer auth
reusing `api_tokens`, five ACL-scoped tools calling `api/` directly (no
`wire.py` — shaping already lives in `api/`), `[mcp]` extra + `[mcp].enabled`
gate, no new migration, and the three design reconciliations (single search
tool, `full_headers`, no `wire.py`).

- [ ] **Step 5: Commit**

```bash
git add docs/mcp-usage.md config.example.toml README.md CLAUDE.md
git commit -m "docs(mcp): usage guide + README/CLAUDE/config notes"
```

---

## Self-review checklist (completed during authoring)

- **Spec coverage:** consumer/mount/auth/tool-surface/error-contract/testing all
  have tasks. The spec's `wire.py` extraction is intentionally dropped (Design
  reconciliation #1) — shaping already shared in `api/`. The spec's
  `search_page`/`search_grow` collapse to one `search` tool (reconciliation #2).
- **Placeholders:** SDK-version-specific connection wiring in Tasks 6 & 9 is
  flagged with explicit fallback strategies, not left vague; every code step has
  real code. The fixtures (`seeded_two_accounts`, `seeded_attachment`) are
  described concretely and pointed at existing seeding helpers to copy.
- **Type/name consistency:** `tool_*` body names, `LocalmailTokenVerifier`,
  `user_id_from_access_token`, `build_mcp_server`, `enable_mcp`/`mcp_config`
  used consistently across tasks.

## Risks carried into execution

1. **MCP SDK API drift** (spec risk #1): exact import paths
   (`get_access_token`, `streamable_http_app`, `session_manager`,
   `AuthSettings` required fields) are validated in Task 2/6/9; adapt shape, keep
   behaviour. Whether `AuthSettings` can be minimal (no `issuer_url`) or forces
   resource-metadata endpoints is settled empirically in Task 6's build test and
   Task 9's handshake.
2. **Sub-app lifespan** (spec risk #2): Task 7 enters `session_manager.run()`
   inside the parent lifespan; Task 9's handshake fails loudly if mis-wired.
3. **Opaque bearer ≠ discovery** (spec decision 3): documented in
   `docs/mcp-usage.md` (clients configure the token directly).
