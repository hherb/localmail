# RFC 8707 resource indicators for the MCP OAuth AS — design

> Implements the deferred RFC 8707 "resource indicators" follow-up carried in
> CLAUDE.md and the #182/#186 handoffs. Scope: **validate + bind** (audience
> binding), enforced at `/mcp` only.
> Date: 2026-07-22. Branch: `feat/oauth-resource-indicators`.

## Problem

localmail's MCP OAuth 2.1 authorization server issues opaque bearer tokens
(rows in `api_tokens`, SHA-256-hashed) via the auth-code + refresh grants. The
MCP authorization spec (2025-06-18) requires clients to send the RFC 8707
`resource` parameter — an absolute URI naming the resource server the token is
for — and requires the AS to **validate** it and **bind** the issued token to
that audience so a token minted for one resource server can't be replayed at
another.

Today localmail does neither. The MCP SDK parses `resource` at `/authorize`
(surfacing it as `AuthorizationParams.resource`) but our `authorize()` **drops
it on the floor**; the token is bound to no audience. A client that sends a
*wrong* `resource` (misconfiguration, or pointing at a different server) gets a
working token with no signal that anything is off.

localmail has exactly **one** resource server today —
`mcp_resource_url(resource_server_url)` = `<origin>/mcp` — so cross-RS audience
restriction is a tautology (this is the "moot today" note in CLAUDE.md). The
deliverable value is therefore: **MCP-client spec compliance** (validate the
client's `resource`, reject a bad one cleanly instead of silently ignoring it),
**defense-in-depth / correct error signalling**, and a **forward seam** for a
second resource server.

## SDK constraints (discovered; they shape the whole design)

The installed `mcp` SDK (`.venv/.../mcp/server/auth/`) gives us:

1. ✅ `resource` **is** parsed at `/authorize` and reaches our provider as
   `params.resource` (`handlers/authorize.py` → `AuthorizationParams.resource`).
2. ❌ `resource` **is** parsed at `/token` (both grants) but the SDK **never
   passes it** to `exchange_authorization_code` / `exchange_refresh_token` — it
   is swallowed. We cannot independently validate the token-endpoint `resource`
   without wrapping the SDK's `TokenHandler` route.
3. ❌ The SDK's error-code enums `AuthorizationErrorCode` / `TokenErrorCode`
   **do not include `invalid_target`** — the exact code RFC 8707 §2 mandates for
   a bad resource. Emitting it verbatim would require bypassing the SDK's typed
   error path.
4. ✅ The SDK's `AuthorizationCode` dataclass **already has a `resource` field**
   (`provider.py`), so we can round-trip the code's bound resource through
   `load_authorization_code` → `exchange_authorization_code` with no DB re-read.

### Accepted consequences (out of scope, documented)

- **Error code:** a bad/unknown/missing-but-required resource maps to
  `AuthorizeError("invalid_request", "<descriptive>")` — the closest available
  SDK code. The *behavior* (reject the authorize request) is RFC-correct; only
  the *code string* is imperfect. The `error_description` names the cause so an
  operator debugging a client sees "resource indicator …".
- **Validation point:** we validate + bind at **authorize time only**. For the
  auth-code flow the authorize-time `resource` is authoritative; the
  token-endpoint `resource` (swallowed by the SDK) is not independently
  re-validated. Wrapping the SDK token route to see it is explicitly *not* done.
- **Enforcement point:** the bound resource is enforced at `/mcp` only (in
  `load_access`). The `/v1` REST surface is not a declared OAuth resource and its
  shared `verify_token` path is unchanged — outside RFC 8707 scope.
- **Single RS:** cross-resource audience restriction is a tautology today. The
  `resource_indicators` list + accepted-set membership check are the forward seam
  for a future second RS.
- **AS-mode toggle:** audience enforcement lives on the AS provider's `/mcp`
  verification path (`load_access(accepted_resources=self._accepted)`). If an
  operator runs with the OAuth AS enabled (minting `oauth_resource`-bound
  tokens) and later switches to plain opaque-bearer mode
  (`authorization_server_enabled = false`), those already-minted tokens are then
  verified by `LocalmailTokenVerifier`, which performs no resource check — they
  revert to unrestricted. Toggling AS mode is a privileged admin action, so this
  is outside the threat model; noted for completeness.

## Approach (decisions taken)

- **Approach B — validate + bind.** Validate the client's `resource` at
  authorize; carry the bound resource through consent → code → access + refresh
  tokens; enforce audience membership at the `/mcp` resource-server verifier.
- **Absent-resource policy: optional, opt-in strict.** A missing `resource` is
  accepted by default (backward compatible; binds to the derived canonical) and
  rejected only when `oauth_require_resource_indicator = true`.
- **Accepted set: configurable, derived default.** `resource_indicators` config
  list, defaulting to `[mcp_resource_url(resource_server_url)]`, mirroring the
  existing `authorization_servers` / `resolve_authorization_servers` pattern.
- **Enforcement at `/mcp` only.** `load_access` rejects a token whose non-NULL
  bound resource is not in the accepted set. `NULL` resource = unrestricted, so
  `/v1/auth/login` tokens and pre-migration rows are structurally immune.
- **No token-endpoint route wrapping, no `invalid_target` shim.** Both would add
  surface area (an ASGI/route wrapper around SDK internals) for a single-RS
  deployment where the extra fidelity changes no real outcome.

## Config — `McpConfig` (`src/localmail/config.py`)

Two new fields:

```python
resource_indicators: list[AnyHttpUrl] | None = None
oauth_require_resource_indicator: bool = False
```

- `resource_indicators` — accepted resource identifiers. When `None`, resolved
  at use-time to `[mcp_resource_url(resource_server_url)]`. Operator-configurable
  for reverse-proxy / multi-hostname setups and as the seam for a second RS.
- `oauth_require_resource_indicator` — when `True`, an authorize request with no
  `resource` is rejected. Default `False` keeps existing DCR clients working.

## New pure module — `src/localmail/mcp/oauth/resource_indicator.py`

No IO, no SDK import; heavily unit-tested. Public surface:

```python
def canonicalize_resource(raw: str) -> str | None: ...
def resolve_accepted_resources(
    configured: list[str] | None, derived: str
) -> list[str]: ...

@dataclass(frozen=True)
class ResourceDecision:
    ok: bool
    bound: str | None       # canonical resource to bind (when ok)
    error: str | None       # human error_description (when not ok)

def decide_resource(
    requested: str | None, accepted: list[str], *, require: bool
) -> ResourceDecision: ...
```

### `canonicalize_resource` — RFC 8707 §2 canonicalization

Returns the canonical form, or `None` if the value is not a valid resource
identifier:

- must parse as an **absolute** URI with scheme `http` or `https`;
- **reject** if a fragment is present (RFC 8707 §2 forbids fragments);
- lowercase the scheme and host;
- drop a default port (`:80` for http, `:443` for https);
- strip exactly one trailing `/` from the path (so `<origin>/mcp` and
  `<origin>/mcp/` canonicalize equal); an empty path stays empty.

Query strings are preserved as-is (not expected for MCP resources, but not
grounds for rejection). Anything else (relative, non-http, empty, has fragment)
→ `None`.

### `resolve_accepted_resources(configured, derived)`

`configured or [derived]`, each run through `canonicalize_resource`; malformed
entries are dropped (a misconfigured operator list can't silently widen the set
to something un-canonical). Mirrors
`discovery.resolve_authorization_servers(configured, issuer)`.

**Non-empty guarantee** (so `decide_resource`'s `accepted[0]` is safe): `derived`
comes from `mcp_resource_url`, which always produces a canonical value. If
`configured` is `None`, the result is `[canonicalize(derived)]`. If `configured`
is a list whose entries *all* canonicalize to `None`, that is a hard operator
misconfiguration → **raise `ValueError`** (surfaced when the provider first
resolves the set; the alternative — silently falling back to `derived` — would
mask the operator's mistake). A partially-malformed list keeps its valid
entries.

### `decide_resource(requested, accepted, *, require)`

Pure decision table (`accepted` is assumed already canonical and non-empty):

| `requested` | `require` | canonical(requested) ∈ accepted | result |
|---|---|---|---|
| `None` | `False` | — | `ok`, `bound = accepted[0]` |
| `None` | `True` | — | error "resource indicator is required" |
| present | — | yes | `ok`, `bound = canonical(requested)` |
| present | — | no / malformed | error "invalid or unknown resource indicator" |

`accepted[0]` is the derived canonical (or the operator's first configured
value) — the audience a resource-less token is bound to.

## Schema — migration `0031_oauth_resource_indicator.sql`

Three nullable columns, no backfill (NULL = unrestricted):

```sql
ALTER TABLE oauth_authorization_codes ADD COLUMN resource TEXT;
ALTER TABLE oauth_refresh_tokens      ADD COLUMN resource TEXT;
ALTER TABLE api_tokens                ADD COLUMN oauth_resource TEXT;
```

- `oauth_authorization_codes.resource` / `oauth_refresh_tokens.resource` — plain
  `resource` (these tables are OAuth-owned).
- `api_tokens.oauth_resource` — prefixed to match the sibling `oauth_client_id`
  / `oauth_refresh_family_id` naming on that shared table.
- **No new index** — every read is by an already-indexed key (`code_sha256`,
  `token_sha256`) or the row already loaded during rotation.

## Data flow

### authorize → bind

`LocalmailASProvider.authorize()`:

```
accepted = resolve_accepted_resources(
    [str(u) for u in cfg.resource_indicators] if cfg.resource_indicators else None,
    mcp_resource_url(str(cfg.resource_server_url)),
)
decision = decide_resource(
    params.resource, accepted, require=cfg.oauth_require_resource_indicator
)
if not decision.ok:
    raise AuthorizeError("invalid_request", decision.error)
payload = ConsentPayload(..., resource=decision.bound)
```

`ConsentPayload` gains `resource: str | None`. `encode/decode_consent_state`
round-trips it (frozen dataclass → `asdict`; already generic over its fields).

### consent → code

`serve/oauth/consent_router.py` decodes the payload and calls `codes.mint_code`;
add `resource=payload.resource`. `codes.CodeRow` gains `resource: str | None`;
the `oauth_authorization_codes` INSERT/SELECT carry the column.

### code exchange → bind onto access + refresh

`_load_code_sync` maps `row.resource` onto `AuthorizationCode.resource` (SDK
field already exists). `_exchange_code_sync` then binds it with no re-read:

```
refresh.mint_refresh(..., resource=auth_code.resource)
access.mint_access(...,  resource=auth_code.resource)
```

### refresh rotation → carry forward

`refresh.mint_refresh` gains `resource`; `RefreshRow` / `load_refresh` return it.
`rotate_refresh` copies `resource` from the consumed row to the successor row
(alongside the existing `family_id` carry). The provider's rotation branch reads
`row.resource` off the reloaded successor and binds it onto the rotated access
token: `access.mint_access(..., resource=row.resource)`. The reuse-detection /
family-purge branch is untouched.

### enforcement at /mcp

`access.load_access(conn, raw, *, accepted_resources=None)`:

```
user = verify_token(conn, raw)          # unchanged (ACL etc.)
if user is None: return None
row = SELECT oauth_client_id, oauth_resource FROM api_tokens WHERE token_sha256=…
if row.oauth_resource is not None and accepted_resources is not None:
    if canonicalize_resource(row.oauth_resource) not in accepted_resources:
        return None                     # SDK → 401 at /mcp
return AccessToken(...)
```

The provider (`_load_access_sync`) computes the accepted set from `self._cfg`
(same helper as `authorize()`) and passes it in. `accepted_resources=None`
(default) skips enforcement, preserving any non-provider caller. NULL
`oauth_resource` (login/legacy) is always unrestricted.

## Error handling

- Authorize with bad/unknown/malformed resource, or absent when required →
  `AuthorizeError("invalid_request", "<descriptive>")` → SDK renders an error
  redirect. (Code imperfect per SDK limitation; behavior correct.)
- Enforcement mismatch at `/mcp` → `load_access` returns `None` → SDK 401.
- NULL bound resource → unrestricted (never rejected on audience grounds).
- Reuse-detection, family revocation, PKCE, redirect-uri matching → unchanged.

## Testing

- **`resource_indicator.py` (pure, table-driven):** canonicalization edge cases
  — trailing slash, uppercase host, `:443`/`:80` default-port drop, non-default
  port kept, fragment → None, relative/non-http/empty → None; `decide_resource`
  full matrix (absent×require both ways, match, mismatch, malformed);
  `resolve_accepted_resources` (default derivation, configured list,
  malformed-entry drop).
- **Store tests (DB):** `mint_access` / `mint_refresh` persist `resource`;
  `load_refresh` returns it; `rotate_refresh` carries it to the successor;
  `load_access` enforcement matrix (match → token, mismatch → None, NULL → token,
  `accepted_resources=None` → token); `mint_code` / `load_code` round-trip.
- **Provider / integration (DB):** authorize with matching resource → consent
  proceeds and the code row carries it; mismatch → `AuthorizeError`;
  absent × `require` both ways; code exchange binds resource onto access +
  refresh; refresh rotation preserves it; refresh **reuse** still purges the
  family (regression); a resource-less legacy token still verifies at `load_access`.
- **Migration** applies cleanly against `localmail_test`.

## Files touched (all remain < 500 lines)

- **New:** `src/localmail/mcp/oauth/resource_indicator.py` (~100),
  `migrations/0031_oauth_resource_indicator.sql`.
- **Edited:** `config.py` (2 fields), `mcp/oauth/provider.py` (authorize +
  exchange + load_access wiring), `mcp/oauth/access.py` (mint + load enforce),
  `mcp/oauth/refresh.py` (mint + row + rotate carry), `mcp/oauth/codes.py`
  (mint + row), `mcp/oauth/consent_state.py` (payload field),
  `serve/oauth/consent_router.py` (pass resource into mint_code).
- **Docs:** CLAUDE.md (OAuth AS section — resource indicators now shipped;
  remove the "not carried through" limitation note), README (OAuth section),
  NEXT_SESSION.md + handoff.

## Non-goals (explicit)

- No SDK token-route wrapping to validate token-endpoint `resource`.
- No `invalid_target` error-code shim.
- No `/v1` REST audience enforcement.
- No second resource server (the list is the seam, not the feature).
- No JWT / structured access tokens (opaque `api_tokens` rows stay opaque).
