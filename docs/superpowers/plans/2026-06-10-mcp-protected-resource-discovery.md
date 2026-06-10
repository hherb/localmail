# MCP protected-resource discovery (RFC 9728) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve the RFC 9728 protected-resource-metadata at its canonical origin-root path and align the MCP `WWW-Authenticate` challenge to it, so a spec-strict MCP client can discover `/mcp` as a protected resource — without localmail becoming an OAuth authorization server.

**Architecture:** A new pure module `localmail/mcp/discovery.py` derives the RFC 9728 resource URL (`<origin>/mcp`) and the authorization-server list, and wraps the SDK's `create_protected_resource_routes` into a route list. `build_mcp_server` passes the full resource URL to `AuthSettings.resource_server_url` so the SDK's challenge advertises the canonical metadata URL; `create_app` registers the discovery routes on the **top-level** serve app (public) within the existing guarded-import path.

**Tech Stack:** Python 3.12, `mcp` SDK (>=1.13, resolves to 1.27.2), pydantic v2 (`AnyHttpUrl`), FastAPI/Starlette, pytest. The `mcp` extra gates everything (`uv run --extra mcp pytest`).

**Design:** [docs/superpowers/specs/2026-06-10-mcp-protected-resource-discovery-design.md](../specs/2026-06-10-mcp-protected-resource-discovery-design.md)

**Key SDK facts (verified against mcp 1.27.2):**
- `str(AnyHttpUrl("https://host:8443"))` → `"https://host:8443/"` (pydantic always appends a trailing slash) — so `mcp_resource_url` must `rstrip("/")` before appending.
- `create_protected_resource_routes(resource_url=AnyHttpUrl("https://host:8443/mcp"), authorization_servers=[…], scopes_supported=[], resource_name="localmail")` returns a single `starlette.routing.Route` at path `/.well-known/oauth-protected-resource/mcp` whose JSON doc is `{"resource": "https://host:8443/mcp", "authorization_servers": ["https://host:8443/"], "scopes_supported": [], "bearer_methods_supported": ["header"], "resource_name": "localmail"}`. Note `authorization_servers` entries carry a trailing slash from `AnyHttpUrl` normalization.
- Passing `resource_server_url="https://host:8443/mcp"` to `AuthSettings` makes the unauthenticated-`/mcp` 401 carry `WWW-Authenticate: Bearer error="invalid_token", error_description="Authentication required", resource_metadata="https://host:8443/.well-known/oauth-protected-resource/mcp"`.

---

## File Structure

- **Create** `src/localmail/mcp/discovery.py` — pure helpers (`MCP_MOUNT_PATH`, `RESOURCE_NAME`, `mcp_resource_url`, `resolve_authorization_servers`) + the one SDK-touching wrapper `build_protected_resource_routes`. The SDK import is **function-level** inside `build_protected_resource_routes` so the module's top level stays import-safe; the package `__init__` still gates the whole package behind the extra (existing pattern).
- **Modify** `src/localmail/config.py` — add `McpConfig.authorization_servers`, update the docstring.
- **Modify** `src/localmail/mcp/server.py` — pass `mcp_resource_url(...)` to `AuthSettings.resource_server_url`.
- **Modify** `src/localmail/mcp/__init__.py` — export `build_protected_resource_routes`.
- **Modify** `src/localmail/serve/app.py` — `_try_build_mcp` also returns discovery routes; `create_app` registers them on the top-level app.
- **Create** `tests/test_mcp_discovery.py` — pure-helper + route-content + 401-challenge + create_app-wiring tests.
- **Modify** `tests/test_mcp_config.py` — `authorization_servers` config tests.
- **Modify** `config.example.toml`, `docs/mcp-usage.md` — document the new knob and the discovery surface.

**Deviation from the spec (intentional):** `create_app`'s `app.mount("/mcp", …)` keeps its existing `"/mcp"` string literal rather than importing `MCP_MOUNT_PATH`. Importing the constant would execute `localmail/mcp/__init__.py` (which eagerly imports the SDK-bound `server` module) **outside** the guarded-import path and crash `serve` when the extra is absent. `MCP_MOUNT_PATH` therefore stays internal to `discovery.py`, where it keeps the resource URL and the SDK-derived route path consistent. The mount literal and the well-known path are independent strings, so no real single-source coupling is lost.

---

## Task 1: `McpConfig.authorization_servers`

**Files:**
- Modify: `src/localmail/config.py:500-515`
- Test: `tests/test_mcp_config.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_config.py`:

```python
def test_mcp_authorization_servers_default_none():
    assert McpConfig().authorization_servers is None


def test_config_parses_mcp_authorization_servers():
    cfg = McpConfig(authorization_servers=["https://idp.example/"])
    assert [str(u) for u in cfg.authorization_servers] == ["https://idp.example/"]


def test_mcp_rejects_malformed_authorization_server_url():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        McpConfig(authorization_servers=["not-a-url"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_config.py -v`
Expected: the three new tests FAIL (`authorization_servers` is not a field / `TypeError` on unexpected kwarg).

- [ ] **Step 3: Add the field + docstring**

In `src/localmail/config.py`, replace the `McpConfig` class body docstring tail and fields:

```python
class McpConfig(BaseModel):
    """Model Context Protocol server settings.

    The MCP server is mounted into `localmail serve` at `/mcp` only when
    `enabled` is true AND the optional `mcp` extra is installed. Disabled by
    default; set `enabled = true` to opt in, mirroring `search.reranker_enabled`.

    `issuer_url` / `resource_server_url` are advertised in the RFC 9728
    protected-resource metadata. `resource_server_url` is the **public origin**
    of the serve deployment (no `/mcp` suffix — the mount path is appended
    internally); set it to the externally reachable URL so the metadata served
    at `/.well-known/oauth-protected-resource/mcp` and the `WWW-Authenticate`
    challenge agree. Tokens stay opaque-bearer, obtained out-of-band via
    `/v1/auth/login`; localmail is not an OAuth authorization server.

    `authorization_servers` is advertised in the metadata's required
    `authorization_servers` field. `None` falls back to `[issuer_url]`; set an
    explicit list to point spec-strict clients at a real external authorization
    server whose tokens `LocalmailTokenVerifier` accepts.
    """

    enabled: bool = False
    issuer_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8443")
    resource_server_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8443")
    authorization_servers: list[AnyHttpUrl] | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_config.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/config.py tests/test_mcp_config.py
git commit -m "feat(mcp): add McpConfig.authorization_servers for PRM discovery"
```

---

## Task 2: pure helpers `mcp_resource_url` + `resolve_authorization_servers`

**Files:**
- Create: `src/localmail/mcp/discovery.py`
- Test: `tests/test_mcp_discovery.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_discovery.py`:

```python
"""RFC 9728 protected-resource discovery helpers for the MCP server."""
from pydantic import AnyHttpUrl

from localmail.mcp.discovery import (
    MCP_MOUNT_PATH,
    mcp_resource_url,
    resolve_authorization_servers,
)


def test_mount_path_constant():
    assert MCP_MOUNT_PATH == "/mcp"


def test_mcp_resource_url_appends_mount_path():
    assert mcp_resource_url("https://host:8443") == "https://host:8443/mcp"


def test_mcp_resource_url_is_trailing_slash_safe():
    # pydantic's str(AnyHttpUrl(...)) always yields a trailing slash;
    # the helper must not produce "https://host:8443//mcp".
    assert mcp_resource_url("https://host:8443/") == "https://host:8443/mcp"
    assert mcp_resource_url(str(AnyHttpUrl("https://host:8443"))) == "https://host:8443/mcp"


def test_resolve_authorization_servers_falls_back_to_issuer():
    issuer = AnyHttpUrl("https://host:8443")
    assert resolve_authorization_servers(None, issuer) == [issuer]


def test_resolve_authorization_servers_uses_explicit_list():
    issuer = AnyHttpUrl("https://host:8443")
    configured = [AnyHttpUrl("https://idp.example/")]
    assert resolve_authorization_servers(configured, issuer) == configured


def test_resolve_authorization_servers_empty_list_falls_back():
    # An empty list is meaningless (the field is required to be non-empty);
    # treat it like None so we never emit an empty authorization_servers.
    issuer = AnyHttpUrl("https://host:8443")
    assert resolve_authorization_servers([], issuer) == [issuer]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_discovery.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'localmail.mcp.discovery'`.

- [ ] **Step 3: Create the module with the pure helpers**

Create `src/localmail/mcp/discovery.py`:

```python
"""RFC 9728 protected-resource discovery for the MCP server.

Pure helpers (no IO) that derive the resource URL and authorization-server list,
plus one thin wrapper over the SDK's `create_protected_resource_routes`. The SDK
import is function-level so this module's top level stays import-safe; the
package still gates the SDK behind the `mcp` extra via `localmail.mcp` (see
`__init__`).

localmail is **not** an OAuth authorization server: tokens are opaque bearers
obtained out-of-band via `/v1/auth/login`. This surface only lets a spec-strict
client *discover* that `/mcp` is a protected resource (the WWW-Authenticate
challenge + the metadata document), nothing more.
"""
from __future__ import annotations

from pydantic import AnyHttpUrl

from localmail.config import McpConfig

# The path FastMCP is mounted at inside `localmail serve` (app.mount("/mcp", …)).
# Kept here so the RFC 9728 resource URL and the SDK-derived metadata route path
# stay consistent.
MCP_MOUNT_PATH = "/mcp"

# RFC 9728 `resource_name` advertised in the metadata document.
RESOURCE_NAME = "localmail"


def mcp_resource_url(base_url: str) -> str:
    """The RFC 9728 resource identifier: the public origin + the MCP mount path.

    `base_url` is `McpConfig.resource_server_url` stringified — pydantic's
    `AnyHttpUrl` always renders a trailing slash, so strip it before appending
    to avoid a doubled separator.
    """
    return base_url.rstrip("/") + MCP_MOUNT_PATH


def resolve_authorization_servers(
    configured: list[AnyHttpUrl] | None, issuer_url: AnyHttpUrl
) -> list[AnyHttpUrl]:
    """The metadata's required `authorization_servers` list.

    Falls back to `[issuer_url]` when the operator configured nothing (None or
    an empty list); an explicit non-empty list wins.
    """
    return configured if configured else [issuer_url]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_discovery.py -v`
Expected: the six helper tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/discovery.py tests/test_mcp_discovery.py
git commit -m "feat(mcp): pure RFC 9728 resource-URL + authz-server helpers"
```

---

## Task 3: `build_protected_resource_routes` + package export

**Files:**
- Modify: `src/localmail/mcp/discovery.py`
- Modify: `src/localmail/mcp/__init__.py:1-4`
- Test: `tests/test_mcp_discovery.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_discovery.py`:

```python
import pytest

pytest.importorskip("mcp")  # build_protected_resource_routes needs the SDK

from localmail.config import McpConfig  # noqa: E402
from localmail.mcp.discovery import build_protected_resource_routes  # noqa: E402


def test_build_routes_registers_canonical_path():
    routes = build_protected_resource_routes(
        McpConfig(resource_server_url="https://host:8443")
    )
    paths = [r.path for r in routes]
    assert paths == ["/.well-known/oauth-protected-resource/mcp"]


def test_build_routes_serves_expected_document():
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    routes = build_protected_resource_routes(
        McpConfig(
            resource_server_url="https://host:8443",
            issuer_url="https://host:8443",
        )
    )
    client = TestClient(Starlette(routes=routes))
    resp = client.get("/.well-known/oauth-protected-resource/mcp")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resource"] == "https://host:8443/mcp"
    assert body["authorization_servers"] == ["https://host:8443/"]
    assert body["resource_name"] == "localmail"
    assert body["bearer_methods_supported"] == ["header"]


def test_build_routes_honours_explicit_authorization_servers():
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    routes = build_protected_resource_routes(
        McpConfig(
            resource_server_url="https://host:8443",
            authorization_servers=["https://idp.example/"],
        )
    )
    client = TestClient(Starlette(routes=routes))
    body = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert body["authorization_servers"] == ["https://idp.example/"]


def test_build_protected_resource_routes_exported_from_package():
    import localmail.mcp as pkg
    assert hasattr(pkg, "build_protected_resource_routes")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_discovery.py -v`
Expected: the four new tests FAIL (`ImportError: cannot import name 'build_protected_resource_routes'`).

- [ ] **Step 3: Implement the wrapper + export**

Append to `src/localmail/mcp/discovery.py`:

```python
def build_protected_resource_routes(config: McpConfig) -> list:
    """RFC 9728 protected-resource-metadata route(s) for the top-level serve app.

    Returns a list of `starlette.routing.Route`; conformance of the emitted
    document comes from the SDK. The single route lands at the canonical
    `/.well-known/oauth-protected-resource/mcp` (origin root, well-known segment
    inserted between host and resource path per RFC 9728 §3.1).
    """
    from mcp.server.auth.routes import create_protected_resource_routes

    return create_protected_resource_routes(
        resource_url=AnyHttpUrl(mcp_resource_url(str(config.resource_server_url))),
        authorization_servers=resolve_authorization_servers(
            config.authorization_servers, config.issuer_url
        ),
        scopes_supported=[],
        resource_name=RESOURCE_NAME,
    )
```

Replace `src/localmail/mcp/__init__.py` in full:

```python
"""localmail MCP server (Search Phase 3). Gated by the [mcp] extra."""
from localmail.mcp.discovery import build_protected_resource_routes
from localmail.mcp.server import build_mcp_server

__all__ = ["build_mcp_server", "build_protected_resource_routes"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_discovery.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/discovery.py src/localmail/mcp/__init__.py tests/test_mcp_discovery.py
git commit -m "feat(mcp): build_protected_resource_routes wrapper + export"
```

---

## Task 4: align the `WWW-Authenticate` challenge in `build_mcp_server`

**Files:**
- Modify: `src/localmail/mcp/server.py:11-13` (imports), `:88-99` (AuthSettings)
- Test: `tests/test_mcp_discovery.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_discovery.py`:

```python
def test_challenge_resource_metadata_matches_canonical_url(db_dsn):
    from psycopg_pool import ConnectionPool
    from starlette.testclient import TestClient

    from localmail.mcp import build_mcp_server

    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        server = build_mcp_server(
            pool,
            searcher=None,
            config=McpConfig(enabled=True, resource_server_url="https://host:8443"),
        )
        client = TestClient(server.streamable_http_app())
        resp = client.post(
            "/",
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
            headers={"Accept": "application/json, text/event-stream"},
        )
    finally:
        pool.close()
    assert resp.status_code == 401
    challenge = resp.headers["www-authenticate"]
    assert (
        'resource_metadata="https://host:8443/.well-known/oauth-protected-resource/mcp"'
        in challenge
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_discovery.py::test_challenge_resource_metadata_matches_canonical_url -v`
Expected: FAIL — challenge currently advertises `https://host:8443/.well-known/oauth-protected-resource` (no `/mcp` suffix) because `resource_server_url` is passed bare.

- [ ] **Step 3: Pass the full resource URL to AuthSettings**

In `src/localmail/mcp/server.py`, add to the imports block (near the other `from localmail.mcp...` imports, around line 19):

```python
from localmail.mcp.discovery import mcp_resource_url
```

Add `AnyHttpUrl` to the existing pydantic import (line 18 `from pydantic import Field`):

```python
from pydantic import AnyHttpUrl, Field
```

Then change the `AuthSettings(...)` block inside `build_mcp_server` (currently passing `resource_server_url=config.resource_server_url`):

```python
        auth=AuthSettings(
            issuer_url=config.issuer_url,
            resource_server_url=AnyHttpUrl(
                mcp_resource_url(str(config.resource_server_url))
            ),
            required_scopes=[],
        ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_discovery.py -v`
Expected: all PASS (including the existing tool-build tests are unaffected).

- [ ] **Step 5: Verify the existing MCP suite still passes**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_server_build.py tests/test_mcp_mount.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/mcp/server.py tests/test_mcp_discovery.py
git commit -m "feat(mcp): point WWW-Authenticate challenge at canonical PRM URL"
```

---

## Task 5: register discovery routes on the top-level serve app

**Files:**
- Modify: `src/localmail/serve/app.py:54-64` (`_try_build_mcp`), `:104-107` + `:166-167` (`create_app`)
- Test: `tests/test_mcp_discovery.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_discovery.py`:

```python
_PRM_PATH = "/.well-known/oauth-protected-resource/mcp"


def _has_prm_route(app) -> bool:
    return any(getattr(r, "path", None) == _PRM_PATH for r in app.routes)


def test_prm_route_present_when_mcp_enabled(db_dsn):
    from localmail.serve.app import create_app

    app = create_app(
        db_dsn=db_dsn, enable_mcp=True, mcp_config=McpConfig(enabled=True)
    )
    try:
        assert _has_prm_route(app)
    finally:
        app.state.pool.close()


def test_prm_route_absent_by_default(db_dsn):
    from localmail.serve.app import create_app

    app = create_app(db_dsn=db_dsn)
    try:
        assert not _has_prm_route(app)
    finally:
        app.state.pool.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_discovery.py::test_prm_route_present_when_mcp_enabled tests/test_mcp_discovery.py::test_prm_route_absent_by_default -v`
Expected: `test_prm_route_present_when_mcp_enabled` FAILS (no such route); `test_prm_route_absent_by_default` PASSES (route never added).

- [ ] **Step 3: Return discovery routes from `_try_build_mcp`**

In `src/localmail/serve/app.py`, replace `_try_build_mcp` (lines 54-64):

```python
def _try_build_mcp(pool, searcher, mcp_config):
    """Build the FastMCP server + ASGI app + RFC 9728 discovery routes.

    Returns (None, None, []) if the [mcp] extra is absent.
    """
    try:
        from localmail.mcp import build_mcp_server, build_protected_resource_routes
    except ImportError:
        logging.getLogger("localmail.serve").info(
            "MCP enabled but the [mcp] extra is not installed; skipping /mcp mount"
        )
        return None, None, []
    server = build_mcp_server(pool, searcher=searcher, config=mcp_config)
    routes = build_protected_resource_routes(mcp_config)
    return server, server.streamable_http_app(), routes
```

- [ ] **Step 4: Register the routes in `create_app`**

In `src/localmail/serve/app.py`, replace the MCP-build block (lines 104-107):

```python
    mcp_server = None
    mcp_app = None
    mcp_discovery_routes: list = []
    if enable_mcp:
        mcp_server, mcp_app, mcp_discovery_routes = _try_build_mcp(
            pool, searcher, mcp_config or McpConfig()
        )
```

Then, where the app mounts `/mcp` (lines 166-167), append the discovery routes right after the mount:

```python
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)
        app.router.routes.extend(mcp_discovery_routes)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_discovery.py tests/test_mcp_mount.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/serve/app.py tests/test_mcp_discovery.py
git commit -m "feat(mcp): serve PRM discovery route on top-level serve app"
```

---

## Task 6: documentation — example config + usage guide

**Files:**
- Modify: `config.example.toml:158-162`
- Modify: `docs/mcp-usage.md` (the auth section, around lines 65-86)

- [ ] **Step 1: Update `config.example.toml`**

Replace lines 158-162 (the `[mcp]` block) with:

```toml
# [mcp]
# enabled = false              # mount the MCP server at /mcp inside `localmail serve`
#                              # (requires the optional extra: uv sync --extra mcp)
# issuer_url = "https://your-host:8443"          # advertised in RFC 9728 metadata
# resource_server_url = "https://your-host:8443" # public origin (NO /mcp suffix;
#                              # appended internally). Set to the externally
#                              # reachable URL so the metadata at
#                              # /.well-known/oauth-protected-resource/mcp and the
#                              # 401 WWW-Authenticate challenge agree.
# authorization_servers = ["https://your-host:8443"]   # required PRM field;
#                              # defaults to [issuer_url] when unset. Point at a
#                              # real external authorization server only if one
#                              # mints tokens this server accepts — localmail
#                              # itself is not an OAuth authorization server.
```

- [ ] **Step 2: Update `docs/mcp-usage.md`**

In the auth section, replace the sentence (around line 83) that reads:

```
There is **no OAuth authorization-server flow** for MCP in this model: the
client configures the token directly.
```

with:

```
**Discovery (RFC 9728):** a spec-strict MCP client can discover that `/mcp` is a
protected resource. An unauthenticated request to `/mcp` returns `401` with a
`WWW-Authenticate: Bearer … resource_metadata="…"` challenge pointing at
`/.well-known/oauth-protected-resource/mcp` (served at the origin root), whose
JSON document advertises the `resource`, `authorization_servers`, and supported
bearer methods. Set `[mcp].resource_server_url` to the externally reachable
origin so the challenge and the metadata are correct behind a proxy.

There is still **no OAuth authorization-server flow** — localmail does not
implement `/authorize`, `/token`, or dynamic client registration. Discovery only
tells the client *where* the resource is and that it is bearer-protected; the
token itself is obtained out-of-band via `/v1/auth/login` and configured on the
client directly. A client that *requires* the full OAuth dance will not
auto-negotiate end-to-end.
```

- [ ] **Step 3: Commit**

```bash
git add config.example.toml docs/mcp-usage.md
git commit -m "docs(mcp): document RFC 9728 discovery surface + authorization_servers"
```

---

## Task 7: full-suite + type gate

**Files:** none (verification only)

- [ ] **Step 1: Run the MCP-relevant suite under the extra**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_discovery.py tests/test_mcp_config.py tests/test_mcp_mount.py tests/test_mcp_server_build.py tests/test_mcp_auth.py -v`
Expected: all PASS.

- [ ] **Step 2: Run the full suite (deselect the macOS-only socket-path failure)**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/ --deselect tests/test_daemon_control_socket.py`
Expected: prior baseline + the new tests pass (no regressions).

- [ ] **Step 3: Type-check**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: clean. If mypy flags `build_protected_resource_routes(config: McpConfig) -> list` as too loose, tighten the return annotation to `list["Route"]` with a `TYPE_CHECKING` import of `starlette.routing.Route` in `discovery.py`:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from starlette.routing import Route
```

and change the signature to `-> list["Route"]`; likewise annotate `mcp_discovery_routes: list["Route"]` in `app.py` (guarded the same way) if mypy complains about the bare `list`.

- [ ] **Step 4: Commit any type-annotation fixes (if needed)**

```bash
git add -A
git commit -m "chore(mcp): tighten discovery route type annotations"
```

---

## Self-Review notes

- **Spec coverage:** config field (Task 1), pure helpers (Task 2), SDK-route wrapper + export (Task 3), `WWW-Authenticate` alignment (Task 4), top-level route registration + enabled/disabled behaviour (Task 5), docs (Task 6), verification (Task 7). The "SDK sub-mounted non-canonical copy stays" note in the spec needs no task (it is the SDK's default behaviour we intentionally leave alone).
- **Type consistency:** `mcp_resource_url(base_url: str) -> str` is always called as `mcp_resource_url(str(config.resource_server_url))`. `resolve_authorization_servers(configured, issuer_url)` is called only inside `build_protected_resource_routes`. The PRM path constant `_PRM_PATH` / literal `/.well-known/oauth-protected-resource/mcp` is identical across Tasks 3 and 5. `build_protected_resource_routes` returns the SDK route list in both its definition (Task 3) and consumer (Task 5).
- **No placeholders:** every code/test step shows complete content.
