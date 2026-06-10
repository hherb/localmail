# MCP protected-resource discovery (RFC 9728) — design

> **Status:** approved for planning, 2026-06-10. Scope: the **discovery surface
> only** half of the long-deferred "Approach B" MCP follow-up. Localmail does
> **not** become an OAuth authorization server.

## Problem

localmail's MCP server (Phase 3) authenticates with an **opaque bearer token**
reused from `api_tokens` — agents obtain one via `POST /v1/auth/login` and pass
it as `Authorization: Bearer …` to `/mcp`. This was deliberate ("Approach A");
"Approach B" — full OAuth 2.1 resource-server discovery — was left as a clean
later follow-up.

A *spec-strict* MCP client (per the 2025-06-18 MCP auth spec + RFC 9728) expects
to **discover** that `/mcp` is a protected resource before it can attach a token:

1. `GET /mcp` with no token → `401` with
   `WWW-Authenticate: Bearer resource_metadata="<url>"`.
2. `GET <url>` → an RFC 9728 *protected resource metadata* (PRM) document.
3. The client reads the document, then attaches a token it already holds and
   proceeds.

The MCP SDK (`mcp` 1.27.2, well above our `>=1.13` floor) **already** wires both
halves whenever a `token_verifier` + `AuthSettings` are present — which localmail
already supplies:

- `RequireAuthMiddleware` emits the `401` + `WWW-Authenticate` challenge.
- `create_protected_resource_routes(...)` serves the PRM document.

But two gaps make the surface unreachable for a spec client:

1. **Wrong path.** Because FastMCP is sub-mounted at `/mcp`, the SDK's PRM route
   lands at `/mcp/.well-known/oauth-protected-resource`. RFC 9728 §3.1 puts the
   metadata at the **origin root** with the well-known segment inserted between
   host and resource path: for resource `https://host/mcp` the canonical URL is
   `https://host/.well-known/oauth-protected-resource/mcp`. A spec client
   computes that root URL itself; it never probes under `/mcp/.well-known/`.
2. **Challenge advertises a dead URL.** The `WWW-Authenticate` `resource_metadata`
   value is built by the SDK from `AuthSettings.resource_server_url` via
   `build_resource_metadata_url`. localmail's `resource_server_url` defaults to
   the bare origin (`http://localhost:8443`), so the challenge advertises a
   root-level URL that **no route serves** today.

## Scope decision (from brainstorming)

**Discovery surface only.** localmail stays opaque-bearer; tokens are still
obtained out-of-band via `/v1/auth/login`. We do **not** build `/authorize`,
`/token`, `/.well-known/oauth-authorization-server`, or dynamic client
registration (RFC 7591).

Rationale: localmail is single-host, single-operator — every connecting agent is
an `api_user` the operator provisions by hand, then hands a bearer. A full
authorization server would be a large, security-sensitive build (authorization-
code handling, redirect-URI validation, PKCE, client-registration spam) that
*nobody in this deployment would exercise*: it exists to let an unprovisioned
client acquire a token interactively, which never happens here. The real gap is
that strict clients can't cleanly *discover* the resource — that is what we fix.

`authorization_servers` (a **required** field in the SDK's PRM model — we cannot
omit it) is made **operator-configurable**, defaulting to `[issuer_url]`. An
operator who fronts localmail with a real external IdP (one that mints tokens our
`LocalmailTokenVerifier` accepts) can point clients at it; the default keeps the
honest soft-pointer behaviour.

## The fix has two halves

1. **Serve the PRM at the canonical root path.** Register the RFC 9728 route on
   the **top-level** serve app at `/.well-known/oauth-protected-resource/mcp`
   (public — well-known metadata is unauthenticated per RFC 9728).
2. **Make the challenge point there.** Pass the **full resource URL including
   `/mcp`** as `AuthSettings.resource_server_url` so the SDK's
   `WWW-Authenticate` advertises the same canonical root URL the top-level route
   serves. Both URLs derive from one helper, so they cannot drift.

## Components

### 1. `McpConfig` ([src/localmail/config.py](../../../src/localmail/config.py))

- **New field** `authorization_servers: list[AnyHttpUrl] | None = None`.
  `None` → falls back to `[issuer_url]` at build time. An explicit list lets an
  operator advertise an external authorization server.
- `resource_server_url` keeps its current meaning: the **public origin** of the
  serve deployment (no `/mcp` suffix). The mount path is appended internally so
  operators don't have to remember to include it.
- Docstring updated: the discovery surface is now served at the canonical root
  path; tokens remain out-of-band; `authorization_servers` semantics explained.

### 2. New pure module `src/localmail/mcp/discovery.py`

No IO, no FastAPI app state — fully unit-testable in isolation.

- `MCP_MOUNT_PATH = "/mcp"` — the single source of truth for the mount path.
  `create_app`'s `app.mount("/mcp", …)` references this constant rather than a
  bare string literal (no magic strings).
- `mcp_resource_url(base_url: str) -> str` — append `MCP_MOUNT_PATH` to the
  public origin, trailing-slash-safe (so `https://host/` and `https://host`
  both yield `https://host/mcp`). The result is the RFC 9728 *resource
  identifier*.
- `resolve_authorization_servers(configured, issuer_url) -> list[AnyHttpUrl]` —
  `configured or [issuer_url]`.
- `build_protected_resource_routes(config: McpConfig) -> list[Route]` — a thin
  wrapper over the SDK's `create_protected_resource_routes(...)`, passing the
  computed resource URL, the resolved authorization servers, and
  `resource_name="localmail"`. Conformance of the emitted document comes from
  the SDK; localmail owns only the wiring. This function imports from
  `mcp.server.auth.routes`, so it lives behind the same guarded-import path as
  `build_mcp_server` (callers invoke it only when the extra is importable).

### 3. `build_mcp_server` ([src/localmail/mcp/server.py](../../../src/localmail/mcp/server.py))

Pass `AuthSettings(resource_server_url=mcp_resource_url(config.resource_server_url), …)`
(was `config.resource_server_url`). This is the **only** behavioural change in
this file: the SDK's `WWW-Authenticate` challenge now advertises the canonical
root metadata URL. `issuer_url` is unchanged.

### 4. `create_app` / `_try_build_mcp` ([src/localmail/serve/app.py](../../../src/localmail/serve/app.py))

Within the existing guarded-import path (`_try_build_mcp`), also build the
discovery routes via `build_protected_resource_routes(mcp_config)` and register
them on the **top-level** app (public, before any auth-bearing routes). When the
`mcp` extra is absent **or** `enable_mcp` is false, no route is added — behaviour
unchanged. The mount string becomes `MCP_MOUNT_PATH`.

## Data flow — spec client, cold connect

```
1. GET /mcp                (no Authorization header)
   ← 401 WWW-Authenticate: Bearer
       resource_metadata="https://host:8443/.well-known/oauth-protected-resource/mcp"

2. GET /.well-known/oauth-protected-resource/mcp     (top-level route, public)
   ← 200 application/json
     {
       "resource": "https://host:8443/mcp",
       "authorization_servers": ["https://host:8443"],   # or operator override
       "bearer_methods_supported": ["header"],
       "resource_name": "localmail"
     }

3. Client attaches its pre-provisioned bearer → normal MCP session.
```

Token acquisition stays out-of-band (`POST /v1/auth/login`), documented in
[docs/mcp-usage.md](../../mcp-usage.md).

## Deliberate non-goals

- **No authorization server.** No `/authorize`, `/token`,
  `/.well-known/oauth-authorization-server`, or dynamic client registration. A
  client that *requires* the full OAuth dance still dead-ends at the (absent) AS
  metadata — the explicit scope boundary. Adding a full AS is a clean future
  follow-up for which this PRM surface is a prerequisite, so this is not
  throwaway work.
- **The SDK's non-canonical sub-mounted PRM copy stays.** Because we pass the
  full resource URL (`<origin>/mcp`) to `AuthSettings.resource_server_url`, the
  SDK's in-mount copy derives its own path from that and lands at
  `/mcp/.well-known/oauth-protected-resource/mcp` (the well-known segment plus a
  duplicated `/mcp`) — harmless, as no spec client queries it; the top-level root
  route is authoritative. If the operator sets
  `authorization_servers` to something other than `[issuer_url]`, the
  sub-mounted copy (built by the SDK from `AuthSettings.issuer_url`) can list a
  different value than the canonical root doc — accepted as a minor known wart,
  documented; the root doc is the one spec clients read.

## Testing

All MCP tests are `importorskip("mcp")`-gated and run under `--extra mcp`.

- **Pure unit** — `tests/test_mcp_discovery.py`:
  - `mcp_resource_url` trailing-slash handling (`https://host/` and
    `https://host` both → `https://host/mcp`).
  - `resolve_authorization_servers` fallback (`None` → `[issuer_url]`) and
    explicit override.
  - `build_protected_resource_routes` registers exactly the canonical path
    `/.well-known/oauth-protected-resource/mcp`.
- **Route** — extend the MCP serve tests:
  - `GET /.well-known/oauth-protected-resource/mcp` → 200 with `resource ==
    https://host/mcp` and `authorization_servers` matching config.
  - Unauthenticated `GET /mcp` → 401 whose `WWW-Authenticate` `resource_metadata`
    equals that exact canonical URL.
  - Route **absent** (404) when `enable_mcp=False`.
- **Config** — `McpConfig.authorization_servers` default `None` round-trips to
  the `[issuer_url]` fallback; an explicit list is accepted and validated as
  `AnyHttpUrl`.

## No migration, no new dependency

Reuses `api_users` / `api_tokens` / `user_accounts` and the already-present
`mcp` extra. No schema change.
