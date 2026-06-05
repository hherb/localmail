# Search Phase 3 — MCP server (design)

**Status:** approved 2026-06-05. Implements the long-planned "Phase 3 — MCP
server" sketched in
[2026-05-16-hybrid-search-design.md](2026-05-16-hybrid-search-design.md) §"MCP
server (Phase 3)", **reconciled with the architecture that shipped since**: the
transport-free `localmail.api` service layer, the per-user account ACL
(`user_accounts`, `allowed_account_ids`), and the HTTPS GUI server
(`localmail serve`). The original sketch assumed a single-user **stdio** server
with full-archive access; this design instead builds a **remote, multi-user,
ACL-scoped HTTP** server **mounted into the existing `serve` app**.

## Problem

AI agents (Claude Desktop, Claude Code, other MCP clients) have no first-class
way to query a localmail archive. Today they would have to speak the raw
`/v1/*` HTTP+JSON API. MCP is the standard agent-tool protocol; exposing the
archive's read surface as MCP tools lets an agent search, browse, read
messages, and read extracted attachment text directly, with results scoped to
exactly the accounts that agent's `api_user` is granted.

Everything *under* the tools already exists: `localmail.api` accessors
(`search.py`, `messages.py`, `browse.py`, `attachments.py`, `accounts.py`),
bearer-token auth (`api/auth.py` + `serve/middleware.py::get_authenticated_user`),
and the per-user ACL resolver (`api/acl.py::allowed_account_ids`). Phase 3 is
the **MCP tool layer + its mount into `serve`**.

## Scope

**In scope:** an MCP server, Streamable-HTTP transport, mounted at `/mcp` in the
existing `serve` FastAPI app, gated by a new `[mcp]` uv extra and a config
toggle. Bearer-token auth reusing `api_tokens`. Six read-only tools (`search`,
`search_page`, `search_grow`, `get_message`, `get_attachment`, `list_messages`)
plus `list_accounts`. A small refactor extracting wire-shaping out of the HTTP
routes into a shared pure module so the MCP and HTTP surfaces serialize
identically.

**Out of scope (v1):** full OAuth 2.1 resource-server discovery
(`WWW-Authenticate` challenges, protected-resource-metadata) — Approach B, a
clean later follow-up; raw-attachment-**bytes** over MCP (raw download stays the
HTTP `/v1/attachments` route with its Content-Disposition / Range / ETag
machinery); `--smart` query expansion (search Phase 4); stdio transport; any
write path; per-tool rate limiting beyond the existing login limiter; a new
migration (none needed).

## Decisions (from brainstorming)

1. **Consumer = remote, multi-user HTTP**, not single-user stdio. Each agent
   authenticates as an `api_user`; the per-user ACL scopes every result.
2. **Mounted into `serve`**, not a separate process/port. Reuses TLS,
   login rate-limiting, the connection pool, the shared `Searcher`, and the
   one auth path. One server, one port.
3. **Auth = Approach A (opaque bearer).** A `TokenVerifier` wraps the existing
   `verify_token`. Agents log in via `/v1/auth/login` + `/refresh` and pass the
   token as `Authorization: Bearer …` to `/mcp`. No OAuth discovery; clients
   configure the token directly (Claude Desktop / Code support this). Confirmed
   acceptable.
4. **Tool surface = all four groups:** search + pagination, `get_message`,
   `get_attachment` (text/metadata, never bytes), browse + `list_accounts`.
5. **Wire-shaping is extracted and shared**, not duplicated — one serializer
   feeds both HTTP routes and MCP tools.
6. **`[mcp] enabled = false` by default** (opt-in, like `reranker_enabled`);
   mounting additionally requires the optional `mcp` package to be importable.

## Architecture

```
MCP client (agent)
  │  Streamable HTTP, Authorization: Bearer <api_token>
  ▼
FastAPI `serve` app
  └── mount /mcp ──► FastMCP ASGI app
                       │  TokenVerifier → AuthenticatedUser
                       ▼
                     tool body (mcp/tools.py)
                       │  allowed_account_ids(conn, user.id)
                       ▼
                     localmail.api accessor (ACL at the SQL boundary)
                       │
                       ▼  result → dict (mcp/wire.py, shared with HTTP routes)
                     MCP structured tool result
```

The MCP tools call the **same** `localmail.api` accessors and use the **same**
`app.state.pool` and `app.state.searcher` as the HTTP routes — no second
connection pool, no second Searcher, no HTTP hop. Because the `Searcher` page
cache is already `user_id`-namespaced, search cursors are shared across the HTTP
and MCP surfaces within the process and cross-user cursor replay is a cache miss
(unchanged invariant).

### Module layout (`src/localmail/mcp/`, gated by the `[mcp]` extra)

```
src/localmail/mcp/
  __init__.py   # public boundary: build_mcp_app(pool, searcher, *, config) -> ASGI app
  server.py     # FastMCP instance + @tool registrations (thin glue)
  auth.py       # TokenVerifier bridging verify_token -> principal (AuthenticatedUser)
  tools.py      # tool bodies: (conn, user, **params) -> dict; each a thin api/ call
  wire.py       # pure result -> dict shaping, SHARED with the HTTP routes
```

Every file stays well under 500 lines; `server.py` is glue, `tools.py` holds the
thin bodies, `wire.py` is pure. No numeric tunables live in MCP code — page
sizes / candidate counts come from `SearchConfig` exactly as the HTTP routes
read them.

### Wire-shaping extraction (targeted improvement)

Where `/v1/search`, `/v1/messages`, `/v1/attachments`, and `/v1/accounts`
currently shape their response dicts inline in `serve/routes/`, that shaping
moves into a shared pure module (`mcp/wire.py`, or a neutral `api/wire.py` if
the HTTP routes import it more naturally — decided in the plan). Both surfaces
then serialize from one source. This preserves the CLAUDE.md invariant that the
wire `date` field is `COALESCE(internal_date, date_sent)` everywhere, and
prevents the two surfaces drifting. The HTTP routes are updated to call the
extracted shapers; their existing tests (`test_serve_search_route.py`,
`test_serve_browse_route.py`, `test_serve_changes_route.py`) must stay green,
pinning behaviour-preservation of the refactor.

## Tools

All tools are read-only and ACL-scoped. Each resolves
`allowed_account_ids(conn, user.id)` once and passes it to the accessor; an
empty grant list yields empty results / not-found exactly as the HTTP API does.
IDs cross the wire as **strings** (reusing `api.ids.parse_int_id` for inbound,
`str(id)` outbound) per the #33 invariant.

| Tool | Maps to | Returns |
|------|---------|---------|
| `search(query, *, sort="rank", page_size=None, candidates_per_arm=None, account=None, folder=None, after=None, before=None, from_addr=None, to=None, subject=None, has_attachment=None, label=None)` | `api.search` | `SearchPage` dict (`results`, `next_cursor`, …) |
| `search_page(cursor)` | `api.search` continue | next `SearchPage` |
| `search_grow(cursor, candidates_per_arm=None)` | `api.search` grow | grown `SearchPage` |
| `get_message(message_id, *, include_body=True, include_attachments=False)` | `api.messages.get_message` | message dict |
| `get_attachment(sha256, *, mode="text")` | `api.attachments` (text/metadata) | `{mode, sha256, text|metadata}` — **never raw bytes** |
| `list_messages(*, account=None, folder=None, cursor=None, page_size=None)` | `api.browse.list_messages` | keyset browse page (`messages`, `next_cursor`) |
| `list_accounts()` | `api.accounts.list_accounts` | the accounts this agent may see |

Both search cursor flavours (`"<token>:<page>"` pool cursor and `"K|<base64>"`
lexical-date keyset) pass through unchanged — the tool is a thin translation, so
recall and pagination semantics are identical to `/v1/search`.

## Auth (Approach A)

`mcp/auth.py` provides a `TokenVerifier` whose `verify_token(token)` opens a
pooled connection, calls the existing `api.auth.verify_token(conn, token)`, and
on success returns a principal carrying the `AuthenticatedUser` (in particular
`user.id`). Invalid / expired / revoked / malformed → the MCP SDK's 401. The
tool bodies read the authenticated user from the MCP request context, then
resolve `allowed_account_ids(conn, user.id)`. No new tables; the token lifecycle
(login, refresh, revoke, `sessions_invalidated_at`) is entirely the existing
system.

## Mounting & lifecycle

`create_app(..., enable_mcp: bool = False)` (mirroring `enable_control_socket`):

- Mount the FastMCP ASGI app at `/mcp` **only when** the `mcp` extra is
  importable **and** `enable_mcp` is true. When the extra is absent, skip the
  mount and log one INFO line — `serve` still runs normally.
- Streamable HTTP's session manager requires its own startup/shutdown, so the
  parent `lifespan` enters the MCP app's lifespan
  (`async with mcp_app.router.lifespan_context(app): yield`) alongside the
  existing control-socket / reconcile startup work.
- The FastMCP server is built closing over the shared `pool` and `searcher`, so
  no state duplication.

Config: a new `McpConfig` (`LocalmailConfig.mcp`) with `enabled: bool = False`.
The `serve` CLI passes `enable_mcp=cfg.mcp.enabled` into `create_app`. TLS and
the `--no-tls`/`--bind 127.0.0.1` rules are inherited unchanged (the MCP mount
adds no new listener).

## Error & wire contract

Tool bodies return structured dicts; failures map to MCP tool errors with the
same problem semantics the HTTP layer uses:

- **search-cursor-expired** (page-cache miss: TTL/LRU/cross-user) → a structured,
  *retriable* tool error instructing the agent to re-run the query without a
  cursor (mirrors the HTTP **409** the GUI already recovers from). Never a crash.
- **ACL miss** (account not granted / unknown id) → empty result or
  not-found-equivalent, exactly as the HTTP API (a non-granted account is
  indistinguishable from a non-existent one).
- **Validation** (non-digit id, bad cursor, bad filter) → a clean tool error via
  the existing `parse_int_id` / query-parse boundaries.

No tool ever leaks a raw traceback to the agent.

## Testing (TDD)

**Unit:**
- `auth.py` TokenVerifier — valid / expired / revoked / disabled-user /
  malformed-header paths against `db_conn` + seeded `api_tokens`.
- `wire.py` pure shapers — golden dicts for search / message / browse /
  attachment results, including the `date = COALESCE(internal_date, date_sent)`
  invariant and string-typed IDs.
- Each tool body — against `db_conn` with a multi-account ACL fixture (granted
  vs non-granted account) asserting results are scoped; `get_attachment`
  text-vs-metadata; `search_page`/`search_grow` cursor handling; the
  cursor-expired → retriable-error mapping.

**Integration:**
- Build the mounted app (`create_app(enable_mcp=True)`), drive the `/mcp`
  Streamable-HTTP endpoint via the MCP SDK client: `initialize` handshake,
  `tools/list` shape, call each tool, assert ACL scoping end-to-end, and assert
  a cursor minted by user A and replayed by user B is a cache miss.
- A `serve`-without-the-extra test: `enable_mcp=True` but `mcp` not importable →
  app builds, `/mcp` absent, INFO logged, the rest of `serve` unaffected.

**No new migration** — reuses `api_users` / `api_tokens` / `user_accounts`.

## Deliberately not shipped (v1)

- OAuth 2.1 discovery / `WWW-Authenticate` (Approach B) — a later follow-up if a
  strict-discovery MCP client ever needs auto-negotiation.
- Raw attachment bytes over MCP — agents consume extracted text; byte download
  stays the HTTP route.
- `--smart` query expansion (search Phase 4).
- stdio transport — HTTP is the chosen consumer; the tool layer is
  transport-agnostic enough that a stdio entrypoint is a trivial later add.
- Write paths, thread grouping, saved searches — out of localmail's posture.

## Open risks

1. **MCP SDK version pin.** Approach A needs an SDK version whose `FastMCP`
   supports a custom `TokenVerifier` for resource-server auth without forcing a
   full authorization-server. The plan's first task spikes the mount + verifier
   against the pinned `mcp` version and pins the minimum in `pyproject.toml`.
2. **Sub-app lifespan composition.** Mounting an ASGI app with its own lifespan
   into FastAPI requires explicitly entering the child lifespan from the parent;
   if mis-wired the Streamable-HTTP session manager won't start. Covered by the
   integration test (a failed handshake fails the test).
3. **Opaque bearer ≠ spec-strict discovery.** Clients that *require* the OAuth
   dance won't auto-negotiate; they must be handed the token. Accepted for v1
   (decision 3); Approach B is the escape hatch.
4. **Wire-shaping refactor touches live HTTP routes.** Behaviour-preservation is
   pinned by the existing route tests staying green; the refactor is in-scope as
   "improve the code you're working in," not a speculative rewrite.
