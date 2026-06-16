# OAuth refresh-token family revocation on detected reuse — design

> Closes #183 (RFC 9700 §4.14.2 refresh-token rotation hardening); folds in #185
> (index `oauth_refresh_tokens(client_id)` for `cleanup_unused`).
> Date: 2026-06-16. Branch: `feat/oauth-refresh-family-revocation`.

## Problem

localmail's MCP OAuth 2.1 authorization server sliding-rotates refresh tokens:
`refresh.rotate_refresh` loads the presented token, **hard-DELETEs** it, and mints
a fresh one (`mcp/oauth/refresh.py`, driven by
`LocalmailASProvider._exchange_refresh_sync`).

When a *previously rotated* (already-deleted) refresh token is replayed,
`load_refresh` returns `None` → `rotate_refresh` returns `None` → the SDK returns
`invalid_grant`. The replay fails, but the **currently-active** refresh token in
that chain stays valid. RFC 9700 §4.14.2 recommends that, on detected reuse of an
already-used refresh token, the AS revoke the entire token **family** (the active
refresh chain for that client+user) — reuse is a strong signal that either the
legitimate client or an attacker holds a stolen copy, so containing the theft
requires invalidating the *active* token, not just rejecting the stale one.

## Approach (decisions taken)

- **Lineage = `family_id` UUID** carried across rotations. Reuse → a single
  `DELETE WHERE family_id = X`. One column, two indexes, no recursive CTE. (Chosen
  over a `parent_token_sha256` linked list, which would need a recursive walk for
  no extra benefit at this scale.)
- **Tombstone on rotation, not hard-delete.** The presented token is marked
  `consumed_at = now()` instead of deleted; its continued presence is what lets a
  later replay be *detected* as reuse rather than mistaken for an unknown token.
- **Tombstone GC = sweep past the token's own `expires_at`.** A consumed row is
  deleted once `expires_at < now()`: reuse stays detectable for the full original
  token lifetime, after which the whole family has expired anyway. No new config
  knob (reuses the existing `oauth_refresh_token_ttl_s` horizon implicitly via the
  per-row `expires_at`).

## Schema — migration `0029_oauth_refresh_token_family.sql`

```sql
ALTER TABLE oauth_refresh_tokens
    ADD COLUMN family_id   UUID NOT NULL DEFAULT gen_random_uuid(),
    ADD COLUMN consumed_at TIMESTAMPTZ;   -- NULL = live; set = rotated tombstone

CREATE INDEX oauth_refresh_tokens_family_id_idx ON oauth_refresh_tokens (family_id);
CREATE INDEX oauth_refresh_tokens_client_id_idx ON oauth_refresh_tokens (client_id);  -- #185
```

- `gen_random_uuid()` is built-in on Postgres ≥ 13 (deployment target is PG 16);
  no `pgcrypto` extension required.
- Existing rows each receive a fresh `family_id` via the column default → each
  in-flight token becomes its own **singleton family**. Correct: an upgrade-time
  token has no recorded lineage, so treating it as its own chain is the safe
  default (a replay of *it* nukes only itself).
- `consumed_at` defaults `NULL` → every existing token stays live.

## Store changes — `mcp/oauth/refresh.py`

- `RefreshRow` gains `family_id: str` (uuid as text).
- `mint_refresh(..., family_id: str | None = None)`:
  - `None` → omit the column on INSERT so the DB default mints a **new family**
    (the code-exchange path).
  - supplied → INSERT with that `family_id` so the new token **joins the parent's
    family** (the rotation path).
- `load_refresh` (the *live* loader) gains `AND r.consumed_at IS NULL` so a
  tombstone never loads as a valid token. (The `api_users` JOIN +
  `disabled_at IS NULL` filter from M1 is retained.)
- **New result type** so the provider can distinguish reuse from unknown/expired:

  ```python
  @dataclass(frozen=True)
  class RotateResult:
      outcome: Literal["rotated", "reuse", "unknown"]
      new_token: str | None = None
  ```

- **New `rotate_refresh` flow** (replaces the current load→delete→mint):
  1. Look up the row by `token_sha256` **ignoring** consumed/expired/user state
     (a raw lookup helper, distinct from the live `load_refresh`).
  2. **Not found** → `RotateResult("unknown")` (replay after GC, or never existed).
  3. **Found, `consumed_at IS NOT NULL`** → **reuse detected**:
     `DELETE FROM oauth_refresh_tokens WHERE family_id = <row.family_id>`, return
     `RotateResult("reuse")`.
  4. **Found and live** (confirmed via `load_refresh`: not consumed, not expired,
     user enabled) → `UPDATE ... SET consumed_at = now()` on the old row (tombstone,
     *not* delete), `mint_refresh(family_id=<old.family_id>)`, then
     `sweep_consumed(conn)`, return `RotateResult("rotated", new_token=...)`.
  5. **Found but expired / user-disabled and not consumed** → `RotateResult("unknown")`
     (natural expiry / disable, not theft — must **not** nuke the family; this also
     preserves the M1 disabled-user containment behaviour, which now maps to
     `unknown`).
- **New `sweep_consumed(conn) -> int`**:
  `DELETE FROM oauth_refresh_tokens WHERE consumed_at IS NOT NULL AND expires_at < now()`.
  Returns deleted count. Called opportunistically at the end of a successful
  rotation (low frequency, single indexed DELETE). Caller commits.
- `revoke_refresh` is unchanged (hard-delete by sha256 — used by the SDK's
  explicit `revoke_token`).

## `clients.cleanup_unused` fix

The M2 "client has a live refresh token" guard must not be fooled by a
not-yet-expired tombstone. Add `AND r.consumed_at IS NULL` to the `NOT EXISTS`
subquery:

```sql
... AND NOT EXISTS (
    SELECT 1 FROM oauth_refresh_tokens r
    WHERE r.client_id = c.client_id
      AND r.expires_at > now()
      AND r.consumed_at IS NULL)
```

Otherwise a once-used-then-idle client whose live token has rotated forward could
keep its abandoned predecessor tombstone counting as "live" and never be reaped.

## Provider — `provider.py::_exchange_refresh_sync`

Switch on `RotateResult.outcome`:

- `"rotated"` → mint access token, `touch_last_used`, `conn.commit()`, return the
  `OAuthToken` (the current success path, unchanged).
- `"reuse"` → `conn.commit()` (the family DELETE **must** persist), log a WARNING
  (`refresh-token reuse detected; revoked family for client=…`), then raise
  `TokenError("invalid_grant", "refresh token reuse detected")` after the
  connection context exits (the existing frozen-dataclass raise-after-context
  pattern).
- `"unknown"` → `conn.rollback()`, raise
  `TokenError("invalid_grant", "refresh token is no longer valid")` (current
  behaviour for an unknown/expired/disabled token).

No change to `load_refresh_token` / `exchange_refresh_token` signatures.

## Error handling & invariants

- All rotation work stays inside one pool connection / transaction. On `reuse`
  the family-DELETE and the commit are atomic; a crash before commit leaves the
  tombstone in place (next replay re-detects reuse — safe).
- **Concurrency:** the tombstone UPDATE is guarded by `AND consumed_at IS NULL`
  and a `rowcount == 1` claim check. Under READ COMMITTED two concurrent
  rotations of the same live token serialise on the row lock; exactly one claims
  the row and mints a successor, and the loser's guarded UPDATE matches 0 rows
  (the token was consumed concurrently — a reuse signal) and revokes the family.
  This avoids a double-successor without `SELECT FOR UPDATE`.
- **Accepted limitation (access-token TTL):** the family DELETE revokes refresh
  tokens only. Access tokens already issued along the chain live in `api_tokens`
  with no `family_id` correlation, so they remain valid at `/mcp` until their
  ≤1h TTL (`oauth_access_token_ttl_s`). Reuse contains the 30-day refresh window
  at once; the ≤1h access window is bounded by expiry, not revoked. Instant
  access containment would require a `family_id` column on `api_tokens` + a join
  in the DELETE — a schema change, deliberately out of scope here.
- The M1 disabled-user race is preserved: a user disabled between the SDK's
  `load_refresh_token` and the exchange now lands in branch (5) → `unknown` →
  `invalid_grant`, never an HTTP 500.
- No raw exception text crosses the wire; the WARNING log is the only
  reuse-detail surface.

## Testing (TDD)

All DB tests run against `localmail_test` and TRUNCATE per the existing fixtures.

**Store — `test_oauth_refresh_store.py`:**
- rotation tombstones (does not delete) the old row; `load_refresh(old)` → None,
  but a raw lookup still finds it with `consumed_at` set;
- **acceptance (#183):** mint → rotate → replay the *old* token → assert
  `outcome == "reuse"` **and** the *new* (active) token no longer loads;
- replay of a never-seen token → `outcome == "unknown"`, no other family touched;
- `family_id` is stable across N sequential rotations; reuse at any depth nukes
  the whole chain;
- expired-not-consumed and disabled-user-not-consumed → `unknown` (not `reuse`);
- `sweep_consumed` deletes only consumed rows past `expires_at`, leaves live rows
  and not-yet-expired tombstones.

**Clients — `test_oauth_clients_store.py`:**
- a not-yet-expired **tombstone** does not keep an otherwise-abandoned client
  alive in `cleanup_unused` (regression for the M2 interaction).

**Provider — `test_oauth_provider.py`:**
- a reuse exchange raises `TokenError("invalid_grant")` and, afterward, the
  previously-active refresh token is also rejected.

**Migration — `test_oauth_migration.py`:**
- `family_id` (NOT NULL, default) + `consumed_at` columns exist; both new indexes
  exist; **#185 acceptance:** `EXPLAIN` of the `cleanup_unused` `NOT EXISTS`
  subquery shows an index lookup on `oauth_refresh_tokens(client_id)`, not a seq
  scan, on a seeded table.

## Out of scope

- RFC 8707 resource indicators (single resource server — moot).
- Configurable tombstone retention window (the per-row `expires_at` horizon is
  sufficient; a knob can be added later if reuse-forensics retention is wanted).

## Migration bookkeeping

Latest applied migration is `0028_oauth_server.sql`; this adds
`0029_oauth_refresh_token_family.sql`. No new uv dependency. Default-off AS path
unchanged when `[mcp] authorization_server_enabled = false`.
