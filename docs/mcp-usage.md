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

### 4. Obtain a bearer token

Authentication is an **opaque bearer token** — the same `api_tokens` the HTTP
API uses. The agent obtains one by logging in with the user's credentials:

```bash
# Against a running `localmail serve` (TLS on by default):
curl -sk https://localhost:8443/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"username": "agent", "password": "…"}'
# → {"access_token": "…", "refresh_token": "…", …}
```

`POST /v1/auth/refresh` mints a fresh access token from a refresh token. There
is **no OAuth authorization-server flow** for MCP in this model: the client
configures the token directly. (See the [GUI server](../README.md#gui-server)
section of the README for the full auth route reference.)

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
Header: Authorization: Bearer <access_token>
```

For a self-signed TLS cert on localhost / LAN, configure your client to trust
the cert (or run behind a reverse proxy with a real cert).

## Tools

All five tools are read-only and ACL-scoped — results only ever include the
accounts the token's user has been granted.

| Tool | Parameters | When to use |
| --- | --- | --- |
| `search` | `query`, `sort="rank"\|"date"`, `limit`, `cursor`, `account_ids`, `folder_ids`, `date_from`, `date_to`, `from_addr`, `to`, `subject`, `has_attachment`, `lang` | Hybrid lexical + vector search over the archive. The default entry point for "find mail about X". |
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
[mcp]
enabled = false                                # mount /mcp inside `localmail serve`
                                               # (requires: uv sync --extra mcp)
issuer_url = "https://your-host:8443"          # advertised in OAuth resource metadata
resource_server_url = "https://your-host:8443" # opaque-bearer clients ignore these
```

`issuer_url` / `resource_server_url` are advertised in the MCP SDK's OAuth
resource-metadata. Opaque-bearer clients (the v1 model described here) configure
their token directly and ignore them — set them to the **public** serve URL only
if you have a spec-strict MCP client that reads resource metadata. They default
to `http://localhost:8443`.

No new database migration is required — MCP reuses the existing `api_users`,
`api_tokens`, and `user_accounts` (per-user ACL) tables.
