# MCP OAuth 2.1 authorization server — design

> **Status:** approved for planning, 2026-06-15. Scope: the long-deferred
> "Approach B" follow-up — turn localmail's MCP server into a real OAuth 2.1
> **authorization server** so spec-strict MCP clients can self-onboard via a
> browser login + consent, with **no hand-pasted bearer token**. Builds directly
> on the already-shipped RFC 9728 protected-resource *discovery* surface
> ([2026-06-10-mcp-protected-resource-discovery-design.md](2026-06-10-mcp-protected-resource-discovery-design.md)).

## Problem

Today an MCP agent reaches `/mcp` with an **opaque bearer** obtained out-of-band
via `POST /v1/auth/login`. The operator hand-provisions an `api_user`, grants it
accounts, mints a token, and pastes it into the client config. The RFC 9728
discovery surface lets a strict client *discover* that `/mcp` is a protected
resource — but the client still dead-ends at the (absent) authorization-server
metadata and cannot acquire a token on its own.

**Driving need (from brainstorming): zero-config onboarding.** A new MCP client
(Claude.ai / ChatGPT connectors, desktop agents) should discover the server,
register itself, send the user to a browser login + consent screen, and receive
tokens automatically — the operator never pastes a bearer.

The MCP SDK (`mcp` 1.27.2) already ships the full machinery: the
`OAuthAuthorizationServerProvider` Protocol and `create_auth_routes(...)`, which
mount `/authorize`, `/token`, `/register`, `/revoke`, and
`/.well-known/oauth-authorization-server`. localmail must implement the provider
and supply the one piece the SDK cannot: the **interactive resource-owner
login + consent** step.

## Scope decisions (from brainstorming)

1. **Resource owner = an existing `api_user`.** Only operator-provisioned users
   can authorize; they log in at the consent screen with their existing
   username/password. **No new users are created in the OAuth flow** — this fits
   localmail's `grant-account` ACL model (a brand-new user would see an empty
   archive until granted accounts, so self-registration is explicitly out).
2. **Open Dynamic Client Registration (RFC 7591) + safeguards.** Real MCP
   clients register themselves, so `/register` is unauthenticated — required for
   true zero-config. A registered client is **inert until a user logs in and
   consents**, so registration alone grants zero access. Spam is bounded by a
   per-IP registration rate limit and a best-effort cleanup of clients that never
   complete a token exchange.
3. **Access tokens reuse `api_tokens`.** OAuth-minted access tokens are stored in
   the existing `api_tokens` table, so the per-user ACL, `disabled_at` checks,
   and `last_used_at` throttling all apply unchanged. The provider's
   `load_access_token` wraps the existing `verify_token` logic.
4. **Short-lived access + sliding-rotation refresh.** Access tokens default to
   **1 hour** (the expiry is invisible to the client — its OAuth library
   auto-refreshes); refresh tokens default to **30 days, sliding** (each refresh
   exchange issues a fresh 30-day refresh token, resetting the idle clock). An
   actively-used client never needs re-authentication; a browser re-login is
   required only after ~30 days of total inactivity, on explicit revocation, or
   when the `api_user` is disabled.
5. **Opt-in, default off.** Gated by `McpConfig.authorization_server_enabled`,
   mirroring `enabled` / `reranker_enabled`. When off, no AS routes are mounted
   and behaviour is exactly as today (opaque-bearer + discovery only).

### Explicit non-goals

- **No self-registration of users / no account-granting in the flow.** The
  operator still provisions users and grants accounts.
- **No granular OAuth scopes.** The MCP tools have no scope model
  (`required_scopes=[]`); consent is all-or-nothing ("read mail in the accounts
  you have been granted"). Scopes remain empty on the wire.
- **The sync daemon is untouched.** `localmail run` authenticates to IMAP with
  its own Gmail OAuth refresh token / password and writes to Postgres directly;
  it never uses `/mcp` or these tokens. This work does not affect sync
  connectivity in any way.

## Architecture & module layout

A new `src/localmail/mcp/oauth/` sub-package (hashed-token SQL stores + the
provider) plus one HTML router under `serve/oauth/`. Pure logic is isolated so it
unit-tests without a DB or FastAPI app; all IO follows the SAVEPOINT/eager-commit
discipline already established in `api/auth.py`.

```
src/localmail/mcp/oauth/
  provider.py       # LocalmailASProvider(OAuthAuthorizationServerProvider): the 9 SDK methods, thin IO over the stores
  clients.py        # DCR store: register_client / get_client / touch_last_used / cleanup_unused
  codes.py          # authorization-code store: mint / load / consume (single-use), code_sha256-keyed
  refresh.py        # refresh-token store: mint / load / rotate / revoke, token_sha256-keyed
  access.py         # access-token bridge: mint into api_tokens (oauth_client_id set), load (reuse verify_token), revoke
  consent_state.py  # PURE: encode/decode + HMAC sign/verify of the pending-authorization blob (mirrors api/admin/oauth_state.py)
  consent_forms.py  # PURE: validation + decision parsing for the login+consent POST
src/localmail/serve/oauth/
  consent_router.py # GET/POST /oauth/consent — the interactive login+consent interstitial (HTML)
  templates/consent.html
```

**Wiring** (`serve/app.py`, inside the existing guarded `_try_build_mcp` path):
when `mcp.authorization_server_enabled`, construct `LocalmailASProvider(pool,
config)` and pass it to `FastMCP(auth_server_provider=…)` **instead of**
`token_verifier=…` (the SDK uses `provider.load_access_token` for the
resource-server check when a provider is present), with
`AuthSettings(..., client_registration_options=ClientRegistrationOptions(enabled=True),
revocation_options=RevocationOptions(enabled=True))`. Mount the consent router on
the top-level app (public — it is the human login surface). When the flag is off,
fall back to today's `token_verifier=LocalmailTokenVerifier(...)` exactly as now.

## Data model — migration `0028_oauth_server.sql`

All bearer/code values are stored **SHA-256-hashed** (raw value returned to the
client exactly once), mirroring `api_tokens`.

- **`oauth_clients`** — `client_id TEXT PRIMARY KEY` (random), `client_secret_sha256 BYTEA`
  (nullable; NULL = public/PKCE client), `redirect_uris TEXT[] NOT NULL`,
  `client_name TEXT`, `grant_types TEXT[]`, `response_types TEXT[]`,
  `token_endpoint_auth_method TEXT`, `scope TEXT`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`,
  `last_used_at TIMESTAMPTZ`. Open DCR inserts here.
- **`oauth_authorization_codes`** — `code_sha256 BYTEA PRIMARY KEY`, `client_id TEXT NOT NULL REFERENCES oauth_clients ON DELETE CASCADE`,
  `user_id BIGINT NOT NULL REFERENCES api_users ON DELETE CASCADE`,
  `redirect_uri TEXT NOT NULL`, `code_challenge TEXT NOT NULL`,
  `code_challenge_method TEXT NOT NULL`, `scopes TEXT[]`, `expires_at TIMESTAMPTZ NOT NULL`,
  `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`. **Single-use:** deleted on
  exchange.
- **`oauth_refresh_tokens`** — `token_sha256 BYTEA PRIMARY KEY`, `client_id TEXT NOT NULL REFERENCES oauth_clients ON DELETE CASCADE`,
  `user_id BIGINT NOT NULL REFERENCES api_users ON DELETE CASCADE`, `scopes TEXT[]`,
  `expires_at TIMESTAMPTZ NOT NULL`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
  Rotated on every refresh (old row deleted, new inserted).
- **`api_tokens`** gains a nullable **`oauth_client_id TEXT REFERENCES oauth_clients ON DELETE CASCADE`**
  so OAuth-minted access tokens are attributable and cascade-revoke when their
  client is deleted. Login-issued tokens leave it NULL — existing rows and the
  `/v1/auth/login` path are unaffected.

This is migration **`0028_oauth_server.sql`** (next free slot after
`0027_import_jobs_owner.sql`).

## The authorize → consent → code flow

```
1. Client → GET /authorize?response_type=code&client_id&redirect_uri
                 &code_challenge&code_challenge_method=S256&state&...   (SDK route)
   → provider.authorize(client, params):
       validate client exists + redirect_uri is registered;
       pack {client_id, redirect_uri, code_challenge, code_challenge_method,
             scopes, state, exp} into an HMAC-signed blob (consent_state.encode);
       return 302 → /oauth/consent?req=<blob>

2. GET /oauth/consent?req=<blob>
   → decode+verify blob; render login + consent page (username, password, Allow / Deny)

3. POST /oauth/consent   (req blob + username + password + decision)
   → decode+verify blob (reject tampered / expired)
   → Deny:  302 → redirect_uri?error=access_denied&state
   → Allow: rate-limited credential check — reuse check_login_rate_limits +
            record_login_attempt + verify_password against api_users;
            on success mint a single-use auth code bound to
            (user_id, client_id, redirect_uri, code_challenge) → codes.mint;
            302 → redirect_uri?code=<code>&state

4. Client → POST /token (grant_type=authorization_code, code, code_verifier, redirect_uri)  (SDK route)
   → provider.load_authorization_code + exchange_authorization_code:
       SDK verifies PKCE S256 (code_verifier vs stored code_challenge);
       consume the code (single-use delete);
       mint access token into api_tokens (oauth_client_id set, TTL = oauth_access_token_ttl_s)
         + refresh token (oauth_refresh_tokens, TTL = oauth_refresh_token_ttl_s);
       return OAuthToken{access_token, refresh_token, expires_in, token_type=Bearer}

5. Client → /mcp  with  Authorization: Bearer <access_token>
   → provider.load_access_token = existing api_tokens lookup → AccessToken(subject=user_id) → ACL as today
```

**Refresh:** `POST /token` (grant_type=refresh_token) → `exchange_refresh_token`
rotates: mint new access + new refresh (fresh 30-day expiry — sliding), delete
the presented refresh row. **Revoke:** `POST /revoke` → `revoke_token` deletes
from `api_tokens` (access) or `oauth_refresh_tokens` (refresh) by hash.

### Pending-authorization blob (consent_state.py)

The authorization parameters survive the consent round-trip as a **stateless
HMAC-signed blob**, reusing the exact pattern proven by the Gmail admin OAuth
flow (`api/admin/oauth_state.py`: JSON payload + `base64url(hmac_sha256(key,
payload))`), keyed on `[serve].state_signing_key`. No DB row, no cleanup. The
blob carries an `exp` so a stale consent link cannot be replayed. If the AS is
enabled without `state_signing_key` configured, `create_app` fails loud at
startup (same contract as the Gmail flow's `[serve].state_signing_key` consumer).

## Token lifecycle & verification reuse

- **Access tokens** reuse `api_tokens` + `verify_token` (wrapped by
  `provider.load_access_token`), so ACL, `last_used_at` throttling, and
  `disabled_at` enforcement apply unchanged. `oauth_access_token_ttl_s` default
  **3600**.
- **Refresh tokens** `oauth_refresh_token_ttl_s` default **2592000** (30 days),
  sliding on rotation.
- **Auth codes** `oauth_authorization_code_ttl_s` default **60**, single-use.

The `api_tokens` row for an OAuth access token is indistinguishable to the
verifier from a login token except for the non-NULL `oauth_client_id` — which is
purely for attribution/cascade and is never consulted on the read path.

## Config & safeguards — `McpConfig`

New fields (all defaulted; **no magic numbers** leak into the provider):

- `authorization_server_enabled: bool = False` — opt-in master switch.
- `oauth_access_token_ttl_s: int = 3600`
- `oauth_refresh_token_ttl_s: int = 2592000`
- `oauth_authorization_code_ttl_s: int = 60`
- `oauth_consent_state_ttl_s: int = 300` — lifetime of the signed consent blob.
- `oauth_registration_window_s: int = 3600` / `oauth_registration_max: int = 20`
  — per-IP `/register` rate limit (same sliding-window shape as the login caps).
- `oauth_client_unused_retention_s: int = 86400` — best-effort cleanup deletes
  clients with `last_used_at IS NULL` older than this. Advisory-lock-gated like
  the `api_login_attempts` sweep so concurrent workers don't pile up DELETEs.

`state_signing_key` is reused from `[serve]` (not duplicated). The consent login
reuses the existing `AuthConfig` login-rate-limit knobs — the consent POST is a
login and must not be a rate-limit bypass.

## Metadata & endpoint path placement (wiring nuance to resolve in the plan)

FastMCP is sub-mounted at `/mcp`, so the SDK-wired auth routes
(`/authorize`, `/token`, `/register`, `/revoke`,
`/.well-known/oauth-authorization-server`) land **under `/mcp`** by default.
This is the same root-vs-submount problem the discovery design solved for the
RFC 9728 PRM document, and it must be resolved consistently here:

- A spec client reads the PRM `authorization_servers` value (an issuer URL),
  then fetches that issuer's RFC 8414 AS metadata to learn the real
  `authorization_endpoint` / `token_endpoint` / `registration_endpoint` URLs.
  **As long as those three published URLs resolve to the actually-mounted
  endpoints, the absolute path prefix does not matter to the client** — the
  client follows the metadata, it does not assume root paths.
- Therefore the load-bearing invariant is: **the `issuer_url` advertised in the
  PRM `authorization_servers` list must be the same issuer the SDK builds its AS
  metadata for, and the AS metadata's endpoint URLs must point at the mounted
  routes.** The plan must verify this end-to-end with the real `mcp` client
  (the integration test already drives a real client), rather than assume a
  path.
- Concretely the plan will choose one of: (a) advertise the `/mcp`-prefixed
  issuer so the SDK's sub-mounted metadata + endpoints are self-consistent, or
  (b) re-register the AS metadata/endpoints at the origin root next to the PRM
  route (the precedent from the discovery design). The integration test is the
  arbiter — whichever makes the real client complete the dance. This is flagged
  as the **first risk to retire** in the plan, before building the provider.

## Security considerations

- **PKCE is mandatory** (OAuth 2.1 / MCP spec); the SDK enforces S256 — the
  provider only stores/loads the challenge.
- **`redirect_uri` is validated** against the client's registered set at both
  `/authorize` (in `provider.authorize`) and `/token` (exact-match in
  `exchange_authorization_code`) — the primary defence against open-redirect /
  confused-deputy.
- **Authorization codes are single-use, short-lived, and bound** to
  `(client_id, redirect_uri, code_challenge, user_id)`; replay or cross-client
  use is rejected.
- **The consent POST is rate-limited** through the same `check_login_rate_limits`
  path as `/v1/auth/login`, so the new login surface can't be used to bypass the
  brute-force caps.
- **Open DCR writes are inert + bounded** — rate-limited per IP, cleaned up when
  unused, and grant nothing without an interactive user login.
- **CSP / no inline JS** on the consent page, matching the admin panel's
  `script-src 'self'` posture; any JS is a served static file.

## Testing (TDD; all `--extra mcp`-gated, `importorskip("mcp")`)

- **Pure unit** — `consent_state` sign/verify/tamper/expiry round-trips;
  `consent_forms` validation + decision parsing.
- **Store unit (DB)** — `clients`/`codes`/`refresh` round-trips; auth-code
  single-use (second consume fails); refresh rotation (old revoked, new valid);
  expiry enforcement; `access` mint→verify proves an OAuth-minted token passes
  the existing `verify_token`/ACL.
- **Provider (DB)** — each of the 9 methods; PKCE binding (wrong `code_verifier`
  → rejected); unregistered `redirect_uri` → rejected at authorize and token;
  cross-client code use → rejected.
- **Route/integration** — extend `tests/test_mcp_integration.py`: full cold
  connect (discover → register → authorize → consent POST → token →
  authenticated tool call) end-to-end; Deny path → `error=access_denied`;
  expired/replayed code → token error; `/register` rate-limit trip; AS metadata
  document served at `/.well-known/oauth-authorization-server`.
- **Gating** — AS routes **absent** (404) and `token_verifier` fallback active
  when `authorization_server_enabled=False`; `create_app` fails loud when AS
  enabled without `state_signing_key`.

## Migration & dependencies

- **One migration:** `0028_oauth_server.sql` (three tables + one nullable
  `api_tokens` column).
- **No new dependency** — the `mcp` extra already provides
  `OAuthAuthorizationServerProvider` + `create_auth_routes`; HMAC state reuses
  stdlib `hmac`/`hashlib` as the Gmail flow already does.

## Relationship to prior work

This completes the "Approach B" arc whose discovery half shipped in #180
([2026-06-10-mcp-protected-resource-discovery-design.md](2026-06-10-mcp-protected-resource-discovery-design.md)).
The PRM document's `authorization_servers` list — already operator-configurable,
defaulting to `[issuer_url]` — now points at a real authorization server hosted
by localmail itself at the same origin.
