# MCP server usage

`localmail serve` can expose the archive's read surface to AI agents over the
[Model Context Protocol](https://modelcontextprotocol.io/) (MCP). The MCP server
is **mounted into the existing `localmail serve` FastAPI app at `/mcp`** over
[Streamable HTTP](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports) —
it adds no new listener and inherits the same TLS posture as `serve`.

It is **read-only** (the same promise as the rest of localmail): agents can
search, browse, and read; they can never send, delete, or modify mail, and
cannot pull raw attachment bytes.

The endpoint is:

```
https://<host>:<port>/mcp
```

## Why use it

The MCP tools call the `localmail.api` accessors directly (no internal HTTP
hop), so an agent gets the same ACL-scoped, wire-shaped results the `/v1/*`
HTTP API returns — but presented as MCP tools that a client like Claude Desktop
or Claude Code can call autonomously.

## Setup

### 1. Install the optional extra

The MCP server is gated behind the optional `mcp` uv extra (it pulls
`mcp>=1.13.0`):

```bash
uv sync --extra mcp
```

If the extra is **not** installed, `localmail serve` still runs normally — it
logs a single INFO line noting the MCP mount was skipped, and the `/mcp`
endpoint is absent.

### 2. Enable it in config

The mount is also gated by config (default **false** — opt-in, mirroring
`search.reranker_enabled`):

```toml
[mcp]
enabled = true
```

Both gates must be satisfied: the extra installed **and** `enabled = true`.

### 3. Create an API user and grant accounts

MCP reuses the existing API-user / token model. Every tool result is scoped to
the accounts the user has been granted (per-user ACL), so a freshly-created user
sees **no mail** until you grant accounts:

```bash
uv run localmail add-api-user agent
uv run localmail grant-account agent horst-gmail      # one per account
uv run localmail grant-account agent work-fastmail
```

### 4. Obtain a bearer token (opaque-bearer mode)

This is the default mode when the OAuth authorization server is disabled (see
[OAuth onboarding](#oauth-onboarding-zero-config) below for the alternative).

Authentication is an **opaque bearer token** — the same `api_tokens` the HTTP
API uses. The agent obtains one by logging in with the user's credentials:

```bash
# Against a running `localmail serve` (TLS on by default):
curl -sk https://localhost:8443/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"username": "agent", "password": "…"}'
# → {"token": "…", "expires_at": "2026-…T…Z"}
```

The response is `{"token": "<bearer>", "expires_at": "<iso8601>"}`. Use that
`token` as `Authorization: Bearer <token>` against `/mcp`. There is **no
separate refresh-token credential** — `POST /v1/auth/refresh` rotates the
existing bearer: send the current token in `Authorization: Bearer <token>` and
receive a new `{"token", "expires_at"}` in return; the old token is revoked.
**Discovery (RFC 9728):** a spec-strict MCP client can discover that `/mcp` is a
protected resource. An unauthenticated request to `/mcp` returns `401` with a
`WWW-Authenticate: Bearer … resource_metadata="…"` challenge pointing at
`/.well-known/oauth-protected-resource/mcp` (served at the origin root), whose
JSON document advertises the `resource`, `authorization_servers`, and supported
bearer methods. Set `[mcp].resource_server_url` to the externally reachable
origin so the challenge and the metadata are correct behind a proxy.

When the authorization server is **off** (the default), there is no OAuth
`/authorize`, `/token`, or dynamic client registration. Discovery only tells the
client *where* the resource is and that it is bearer-protected; the token is
obtained out-of-band via `/v1/auth/login` and configured on the client directly.
A spec-strict client that requires the full OAuth dance should use the OAuth mode
described below. (See the [GUI server](../README.md#gui-server) section of the
README for the full auth route reference.)

## OAuth onboarding (zero-config)

localmail can act as an OAuth 2.1 **authorization server** so spec-strict MCP
clients self-onboard via a browser login and consent — no hand-pasted bearer
token required. This is opt-in and off by default.

### Enabling the authorization server

Three config settings are required (plus the public URL):

```toml
[serve]
state_signing_key = "<at-least-32-random-characters>"   # REQUIRED; serve fails loud if absent

[mcp]
enabled = true
authorization_server_enabled = true
resource_server_url = "https://mail.example.com:8443"   # your public origin
```

`state_signing_key` must be at least 32 characters; `create_app` raises an
error at startup if the AS is enabled without it. `resource_server_url` is the
only URL the operator needs to set — the AS issuer and all OAuth endpoints are
auto-derived as `<resource_server_url>/mcp`, so discovery and the endpoint URLs
are self-consistent. Setting `[mcp] authorization_servers` explicitly is still
honoured for pointing at an external IdP instead.

The `mcp` extra must also be installed (`uv sync --extra mcp`), as with the
opaque-bearer mode.

### The cold-connect flow

A spec-strict MCP client (e.g. Claude Desktop) that encounters `/mcp` for the
first time performs these steps automatically:

1. **Resource discovery** — `GET /.well-known/oauth-protected-resource/mcp`
   (RFC 9728) returns a JSON document that advertises the authorization server
   URL (`<origin>/mcp`).
2. **AS metadata** — `GET <origin>/mcp/.well-known/oauth-authorization-server`
   (RFC 8414) returns the `registration_endpoint`, `authorization_endpoint`, and
   `token_endpoint`.
3. **Dynamic client registration** — `POST <registration_endpoint>` (RFC 7591,
   open/unauthenticated) returns a `client_id`. No client secret is issued.
4. **Authorization redirect** — the client opens the user's browser at
   `<authorization_endpoint>` with PKCE (S256 required). localmail redirects to
   `/oauth/consent`, where the user logs in with their **existing api_user
   username and password** and clicks Allow.
5. **Code exchange** — localmail redirects back to the client's redirect URI
   with an authorization code; the client exchanges it at `<token_endpoint>`
   (with the PKCE `code_verifier`) for an access token and a refresh token.
6. **Authenticated call** — the client passes `Authorization: Bearer
   <access_token>` to `/mcp` on every subsequent request.

Steps 1–5 happen once per client; the refresh token is then used to renew
access tokens automatically.

### Token lifetimes and re-authentication

- **Access tokens** expire after 1 hour (configurable: `[mcp]
  oauth_access_token_ttl_s`). The client's OAuth library refreshes them
  silently; the 1-hour expiry is invisible to the user.
- **Refresh tokens** are valid for 30 days and are **sliding**: each refresh
  resets the clock (`[mcp] oauth_refresh_token_ttl_s`). An actively-used client
  never needs to re-authenticate.
- A **browser re-login** (repeating steps 4–5) is required only after
  approximately 30 days of total inactivity, on explicit token revocation, or if
  the api_user is disabled.

Access tokens are stored in the existing `api_tokens` table, so the per-user
account ACL applies unchanged — the agent sees only the accounts the user has
been granted via `localmail grant-account`.

### Open DCR safeguards

The `/register` endpoint is unauthenticated (required for zero-config), but a
registered client is inert until a real user completes the consent step. Two
safeguards bound abuse:

- **Per-IP rate limit** — `[mcp] oauth_registration_max` registrations per
  `oauth_registration_window_s` (default 20 per hour). Excess requests receive
  `429 Too Many Requests`.
- **Unused-client cleanup** — registered clients that never complete a token
  exchange are deleted after `[mcp] oauth_client_unused_retention_s` (default
  24 hours).

The consent login at `/oauth/consent` reuses the same Postgres-backed
rate-limit and timing-parity protections as `/v1/auth/login`, so credential
stuffing is bounded the same way.

### Known limitations

- **RFC 8414 metadata location.** localmail serves AS metadata at
  `<origin>/mcp/.well-known/oauth-authorization-server` (the OIDC-style
  path-suffix form used by the MCP SDK and spec-compliant MCP clients). A
  hypothetical client that probes only the strict RFC 8414 §3.1 insertion form
  (`<origin>/.well-known/oauth-authorization-server/mcp`) will not find it.
  Real MCP clients work correctly.
- **RFC 8707 resource indicators** are not carried through the flow or bound
  onto tokens. localmail is a single resource server, so audience-restriction
  adds nothing here.

### 5. Run the server

```bash
uv run localmail serve --bind 127.0.0.1 --port 8443 \
  --tls-cert ~/.config/localmail/tls.crt \
  --tls-key  ~/.config/localmail/tls.key
```

TLS is on by default. `--no-tls` is only honoured with `--bind 127.0.0.1`
(local dev). These rules are inherited from `serve` unchanged — the MCP mount
does not relax them.

### 6. Configure the MCP client

Point your MCP client (Claude Desktop, Claude Code, etc.) at the Streamable HTTP
endpoint and pass the bearer token in the `Authorization` header:

```
URL:    https://<host>:<port>/mcp
Header: Authorization: Bearer <token>
```

For a self-signed TLS cert on localhost / LAN, configure your client to trust
the cert (or run behind a reverse proxy with a real cert).

## Tools

All five tools are read-only and ACL-scoped — results only ever include the
accounts the token's user has been granted.

| Tool | Parameters | When to use |
| --- | --- | --- |
| `search` | `query`, `sort="rank"\|"date"`, `limit`, `cursor`, `account_ids`, `folder_ids`, `date_from`, `date_to`, `from_addr`, `to`, `subject`, `has_attachment`, `lang`, `smart` | Hybrid lexical + vector search over the archive. The default entry point for "find mail about X". Pass `smart=true` for a local LLM query rewrite (page 1 only). The response carries `rewrite_status` (`applied`, `unavailable`, `failed`, `not_attempted`, or `not_requested`) and an optional curated `rewrite_note` with an actionable detail; `rewrite_skipped` (kept for back-compat) is true only for `unavailable`/`failed`. On a continuation page `smart` is ignored and the status is `not_attempted`. |
| `get_message` | `message_id`, `full_headers=False` | Fetch one message's headers, body, and attachment list once search/browse has surfaced its ID. |
| `get_attachment` | `sha256`, `mode="text"\|"metadata"` | Read an attachment's **extracted text** or its metadata. Never returns raw bytes. |
| `list_messages` | `account_ids`, `folder_ids`, `limit`, `cursor` | Keyset date-ordered browse (newest first) when there's no query — "show me recent mail". |
| `list_accounts` | — | Enumerate the accounts this agent is allowed to read. |

### Paging

`search` and `list_messages` return a `next_cursor`. To page forward, call the
tool again with that value in `cursor`. The `search` tool transparently grows
its candidate pool as you page deeper.

If a `search` cursor has expired (its underlying result pool was evicted from
the in-process cache — TTL, LRU, or a `serve` restart), the tool returns a
cursor-expired error. The recovery is the same as the HTTP API: **re-run the
query without a cursor** and skip past rows you already hold.

### Attachments are never raw bytes over MCP

`get_attachment` deliberately exposes only **extracted text** (`mode="text"`)
or **metadata** (`mode="metadata"`). Raw attachment bytes are *not* available
over MCP — stored HTML/SVG/XML blobs are an XSS sink, and binary download is a
transport concern, not an agent one. To download the original bytes, use the
authenticated HTTP route instead:

```
GET /v1/attachments/{sha256}
```

That route forces `Content-Disposition: attachment`, supports range requests
and conditional GET, and applies the same per-user ACL.

## Config reference

```toml
[serve]
# Required when authorization_server_enabled = true (>= 32 chars; serve fails
# loud at startup if absent).
state_signing_key = ""

[mcp]
enabled = false                                 # mount /mcp inside `localmail serve`
                                                # (requires: uv sync --extra mcp)
resource_server_url = "https://your-host:8443"  # public origin; set this behind a proxy
                                                # so RFC 9728 discovery is correct

# OAuth authorization-server mode (opt-in, default off):
authorization_server_enabled = false
oauth_access_token_ttl_s = 3600                 # 1 hour
oauth_refresh_token_ttl_s = 2592000             # 30 days, sliding
oauth_registration_max = 20                     # per-IP DCR rate-limit
oauth_registration_window_s = 3600              # window for the rate-limit above
oauth_client_unused_retention_s = 86400         # 24h; unused registered clients pruned
```

`resource_server_url` is advertised in the RFC 9728 protected-resource metadata.
Opaque-bearer clients configure their token directly and do not use it — set it
to the public serve URL only if you have a spec-strict client or the AS enabled.
Defaults to `http://localhost:8443`.

When `authorization_server_enabled = false` (the default), there is no OAuth AS
and no new tables — MCP reuses the existing `api_users`, `api_tokens`, and
`user_accounts` tables. When the AS is enabled, migration `0028_oauth_server.sql`
adds `oauth_clients`, `oauth_authorization_codes`, `oauth_refresh_tokens`,
`oauth_registration_attempts`, and a nullable `api_tokens.oauth_client_id`.
