# Access-token family containment on refresh-token reuse — design

> Closes the accepted-limitation follow-up carried from #186 / the §2 item in
> the 2026-06-16 handoff. Builds directly on
> [2026-06-16-oauth-refresh-token-family-revocation-design.md](2026-06-16-oauth-refresh-token-family-revocation-design.md).
> Date: 2026-06-16. Branch: `feat/access-token-family-containment`.

## Problem

The MCP OAuth 2.1 authorization server revokes the entire refresh-token
**family** when it detects refresh-token reuse (RFC 9700 §4.14.2, shipped in
#186): `refresh.rotate_refresh` → `RotateResult("reuse")` → the provider's reuse
branch persists a `DELETE FROM oauth_refresh_tokens WHERE family_id = X`.

That DELETE revokes refresh tokens only. **Access tokens** already minted along
that chain live in `api_tokens` with no `family_id` correlation, so they stay
valid at `/mcp` until their ≤1h TTL (`oauth_access_token_ttl_s`) regardless of
the reuse detection. Reuse contains the 30-day refresh window immediately, but
the ≤1h access window is bounded by expiry, not revoked — the explicit accepted
limitation in the #186 design.

This design closes that window: on reuse detection, also immediately delete the
access tokens minted **within the same refresh family**.

## Approach (decisions taken)

- **Family-precise containment.** A new nullable `oauth_refresh_family_id` UUID
  column on `api_tokens` correlates each OAuth-minted access token to the exact
  refresh chain it belongs to. The reuse path deletes only that family's access
  tokens. (Chosen over per-`oauth_client_id` revocation, which would kill
  unrelated concurrent sessions of the same client+user. The column already has
  a sibling — `oauth_client_id` from #186/0028 — so the pattern is established.)
- **Reuse-only trigger.** Normal rotation leaves the predecessor's access token
  to expire within its ≤1h TTL (standard short-TTL bearer practice; eager
  revocation would break legitimate in-flight requests still holding a
  just-issued access token). Explicit RFC 7009 revocation behaviour is
  unchanged. Both extensions are explicitly out of scope.
- **Store-boundary discipline.** `refresh.py` continues to touch only
  `oauth_refresh_tokens`; `access.py` continues to own `api_tokens`. The
  provider orchestrates both stores inside the one existing transaction. The
  family id crosses the boundary as data (`RotateResult.family_id`), not as a
  cross-store DELETE inside `refresh.py`.

## Schema — migration `0030_api_tokens_refresh_family.sql`

```sql
ALTER TABLE api_tokens
    ADD COLUMN oauth_refresh_family_id UUID;   -- NULL for login + non-family tokens

CREATE INDEX api_tokens_oauth_refresh_family_id_idx
    ON api_tokens (oauth_refresh_family_id)
    WHERE oauth_refresh_family_id IS NOT NULL;
```

- Nullable, no backfill. Existing rows — `/v1/auth/login` tokens and any
  pre-migration OAuth access tokens — stay `NULL` and are structurally immune to
  the family purge (the reuse DELETE filters on a concrete UUID, never matches
  NULL).
- Partial index (`WHERE … IS NOT NULL`) — the overwhelming majority of
  `api_tokens` rows are login tokens with a NULL value, so the index only
  carries the OAuth-minted rows it actually serves.
- `UUID` type matches `oauth_refresh_tokens.family_id` (added in 0029) so the
  reuse DELETE binds the same value with no cast.

## Store changes — `mcp/oauth/access.py`

- `mint_access(conn, *, user_id, client_id, ttl_s, family_id: uuid.UUID | None =
  None) -> str` — writes `oauth_refresh_family_id` into the INSERT.
  `family_id=None` (the code-only / non-OAuth path, and the default) leaves the
  column NULL.
- **New** `revoke_access_family(conn, family_id: uuid.UUID) -> int`:

  ```python
  def revoke_access_family(conn, family_id):
      with conn.cursor() as cur:
          cur.execute(
              "DELETE FROM api_tokens WHERE oauth_refresh_family_id = %s",
              (family_id,),
          )
          return cur.rowcount
  ```

  Single indexed DELETE, returns the deleted count, caller commits. Mirrors the
  shape of `refresh._delete_family`.

## Store changes — `mcp/oauth/refresh.py`

- **`mint_refresh` signature is unchanged** (still returns the raw token `str`).
  The code-exchange path obtains the new family by calling the existing,
  already-tested `load_refresh(conn, refresh_raw).family_id` on the row it just
  minted (a single PK lookup in the once-per-login path). This deliberately
  avoids changing `mint_refresh`'s return type, which would ripple to ~12
  existing `raw = mint_refresh(...)` call sites in the store tests purely to save
  that one lookup. (Considered and rejected: returning `tuple[str, uuid.UUID]`.)
- **`RotateResult` gains `family_id: uuid.UUID | None = None`** — populated on the
  `"reuse"` outcome so the provider knows which family's access tokens to purge.
  `None` on `rotated` / `unknown` (the provider doesn't purge access tokens on
  those outcomes). `refresh.py` itself does **not** touch `api_tokens`.
- `rotate_refresh`'s reuse branch returns `RotateResult("reuse",
  family_id=family_id)` (it already holds `family_id` from `_raw_state`); the
  claim-lost concurrency branch returns `RotateResult("reuse",
  family_id=row.family_id)`.

## Provider changes — `mcp/oauth/provider.py`

- **`_exchange_code_sync`** — reorder so the refresh token is minted first, then
  read its family and tag the access token:

  ```python
  refresh_raw = refresh.mint_refresh(
      conn, client_id=client_id, user_id=user_id,
      scopes=auth_code.scopes, ttl_s=self._cfg.oauth_refresh_token_ttl_s,
  )
  new_row = refresh.load_refresh(conn, refresh_raw)
  assert new_row is not None
  access_raw = access.mint_access(
      conn, user_id=user_id, client_id=client_id,
      ttl_s=self._cfg.oauth_access_token_ttl_s, family_id=new_row.family_id,
  )
  ```

- **`_exchange_refresh_sync`** — two edits:
  - `rotated` branch: it already does `load_refresh(new_token)` → has
    `row.family_id` → pass `family_id=row.family_id` into `mint_access`.
    `mint_refresh` is not called here (rotation mints via `rotate_refresh`).
  - `reuse` branch: before the existing `conn.commit()`, call
    `purged = access.revoke_access_family(conn, result.family_id)` (with
    `assert result.family_id is not None`). The WARNING log gains the purged
    count:
    `refresh-token reuse detected; revoked family for client_id=%s (access tokens purged=%d)`.

No change to any SDK-facing signature (`exchange_*`, `load_*`, `revoke_token`),
no wire-shape change, no new config knob.

## Data flow on reuse

```
client replays consumed refresh token
  → rotate_refresh: _raw_state finds tombstone (consumed)
      → _delete_family(family_id)           # oauth_refresh_tokens rows gone
      → RotateResult("reuse", family_id=fam)
  → provider reuse branch:
      → access.revoke_access_family(fam)     # api_tokens rows gone
      → conn.commit()                        # both deletes atomic
      → log WARNING (with purged count)
      → raise TokenError("invalid_grant")
```

Both deletes commit together or not at all (single connection/transaction). A
crash before commit leaves both the refresh tombstone and the access rows in
place; the next replay re-detects reuse and re-purges — safe and idempotent.

## Error handling & invariants

- All work stays inside the one existing pool connection / transaction; the
  refresh-family DELETE and the access-family DELETE are atomic with the commit.
- `revoke_access_family` on a family with zero access tokens (e.g. the access
  token already expired and was GC'd, or never minted) returns 0 — a no-op, not
  an error.
- Login tokens (`oauth_refresh_family_id IS NULL`) can never be matched by the
  family DELETE — structurally immune.
- The `rotated` and `unknown` outcomes never call `revoke_access_family`
  (guarded by `result.outcome == "reuse"`), so normal rotation and natural
  expiry leave access tokens to their TTL.
- No raw exception text crosses the wire; the WARNING log remains the only
  reuse-detail surface (now also carrying the purged count).

## Testing (TDD)

All DB tests run against `localmail_test` and TRUNCATE per the existing
fixtures. Write each test first, watch it fail, then implement.

**Access store — `test_oauth_access_store.py`:**
- `mint_access(family_id=fam)` persists `oauth_refresh_family_id = fam`;
  `mint_access()` (no family) persists NULL.
- `revoke_access_family(fam)` deletes only rows with that family, leaves
  other-family rows and NULL rows intact; returns the correct count.
- `revoke_access_family` on an absent family returns 0.

**Refresh store — `test_oauth_refresh_store.py`:**
- `rotate_refresh` on a consumed token → `RotateResult.outcome == "reuse"` **and**
  `RotateResult.family_id == <the chain's family>` (the family the minted row
  carries, read via `load_refresh`).
- `rotated` / `unknown` outcomes carry `family_id is None`.

**Provider — `test_oauth_provider.py` (acceptance):**
- mint (code exchange) → rotate once → replay the **old** refresh token →
  exchange raises `TokenError("invalid_grant")` **and** the access token minted
  within that family no longer loads (`access.load_access` → None / `/mcp` would
  401), while a same-user `/v1/auth/login` access token (NULL family) still
  loads.

**Migration — `test_oauth_migration.py`:**
- `api_tokens.oauth_refresh_family_id` column exists (UUID, nullable); the
  partial index `api_tokens_oauth_refresh_family_id_idx` exists.

## Out of scope (confirmed)

- Per-`oauth_client_id` (broad) access-token revocation.
- Eager access-token revocation on **normal** rotation.
- Access-token revocation on explicit RFC 7009 `revoke_token`.
- Correlating/cleaning up already-expired access-token rows (pre-existing
  `api_tokens` GC behaviour is unchanged).

## Migration bookkeeping

Latest applied migration is `0029_oauth_refresh_token_family.sql`; this adds
`0030_api_tokens_refresh_family.sql`. No new uv dependency. Default-off AS path
unchanged when `[mcp] authorization_server_enabled = false`.
