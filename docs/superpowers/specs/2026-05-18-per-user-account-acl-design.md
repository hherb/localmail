# Per-user account ACL — design

> **Status:** Draft 2026-05-18. Closes #31 (api: thread `AuthenticatedUser`
> through every route + add per-user account ACL) and #8 (Auth: per-user
> account ACL — `user_accounts` join table).

## Motivation

`localmail serve` ships argon2id-hashed passwords, SHA-256 token storage, and
solid rate-limiting (PR #30, migration `0014_api_users.sql`), but the
resulting `AuthenticatedUser` is **not** referenced by any route handler under
`/v1/accounts`, `/v1/messages`, `/v1/messages/{id}/raw`, `/v1/attachments/…`,
`/v1/changes`, or `/v1/search`. Every authenticated user can therefore read
every account's mail and every blob in the archive.

This was a documented deferred concern: `add-api-user` already warns when a
second user is created, and `is_shared` in the capabilities response is a
hard-coded `False` stub. We now wire the join table, thread
`AuthenticatedUser` through the transport-free service library, and filter
every account-scoped query at the SQL boundary.

## Scope

In:

- New migration `0016_user_accounts.sql`: `user_accounts (user_id, account_id)`.
- New service-layer module `localmail.api.acl` for grant / revoke / membership
  lookups, plus a single helper `allowed_account_ids(conn, user_id) -> list[int]`.
- Thread an `allowed_account_ids: list[int]` (or `None` for unrestricted —
  reserved for future admin role, not used yet) parameter into every public
  function under `localmail.api.*` that returns account-scoped rows.
- SQL filter every `list_*`/`get_*` by membership (intersecting with any
  user-supplied account filter on `/v1/search`).
- `Searcher.search()` / `continue_page()` / `grow_pool()` learn an
  `allowed_account_ids` parameter. The internal `SearchFilters` already has
  `account_ids: list[int] | None`; the route layer intersects this with the
  ACL before invoking the searcher.
- `PageCache` key derivation: tokens are namespaced by `user_id` so a cursor
  cannot be replayed across users.
- New CLI: `localmail grant-account USERNAME ACCOUNT_NAME` and
  `localmail revoke-account USERNAME ACCOUNT_NAME`.
- `list_accounts` returns `capabilities.is_shared = True` once a user has
  access to ≥2 accounts (or any account, given the multi-tenant promise) —
  see *Capabilities semantics* below.
- Drop the `add-api-user` "no per-account ACL" warning.

Out:

- No admin / role table — every user is a regular user. The `disabled_at`
  column on `api_users` already covers lockout.
- No per-folder / per-message ACL. Account is the grain.
- No bulk grant CLI (e.g. `grant-all`). Per-account is explicit on purpose
  — the prior posture was "everyone sees everything"; we want intent to be
  visible in shell history.
- No backfill that grants existing users access to all accounts. Existing
  single-operator deployments must run one `grant-account` per account they
  want their existing user to see. README + handoff call this out clearly so
  the upgrade is not silent.
- MCP integration. Phase 3 lands later; once it does, the MCP server will
  call `localmail.api.*` with the same `AuthenticatedUser` and inherit the
  filtering for free.

## Threat model addressed

1. **Multi-tenant read disclosure.** User B holds a valid token; user B's
   queries against `/v1/messages/123` (an account-A message) now return
   404, not the message body.
2. **Search cursor replay.** Today a cursor minted by user A can be replayed
   by user B and reveals A's pool. New `PageCache` keying makes the cursor
   user-scoped — replays from a different `user_id` miss the cache and a
   fresh, B-scoped search runs instead.
3. **Indirect leak via `/v1/changes` polling.** Same SQL boundary fix.
4. **Indirect leak via attachment streaming.** A user with no access to any
   account that carries blob X cannot stream blob X. The ACL filter is
   applied via a `messages → message_labels → mailboxes → accounts` join,
   matching the per-message ACL.

Not addressed (out of scope, tracked separately):

- IP-based / global login rate limiter (#7).
- HTTP-side bot defences. The argon2 cost + rate limiter already bounds
  cost; adding WAF rules is downstream.

## Schema

`0016_user_accounts.sql`:

```sql
CREATE TABLE user_accounts (
    user_id     BIGINT      NOT NULL REFERENCES api_users(id) ON DELETE CASCADE,
    account_id  BIGINT      NOT NULL REFERENCES accounts(id)  ON DELETE CASCADE,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, account_id)
);

CREATE INDEX user_accounts_account_id_idx ON user_accounts (account_id);
```

`granted_at` is informational — useful for auditing who got access when.
Both FKs cascade so account deletion or user removal cleans up automatically.

## Service-layer API

New module `src/localmail/api/acl.py` (≈ 80 lines):

```python
def allowed_account_ids(conn, user_id: int) -> list[int]: ...
def grant_account(conn, user_id: int, account_id: int) -> None: ...
def revoke_account(conn, user_id: int, account_id: int) -> int: ...   # affected rows
def grants_for_user(conn, user_id: int) -> list[tuple[int, str, datetime]]: ...
def user_has_account(conn, user_id: int, account_id: int) -> bool: ...
```

The existing modules grow one new keyword-only argument each:

```python
# accounts.py
def list_accounts(conn, *, allowed_account_ids: list[int]) -> list[dict]: ...
def list_folders(conn, account_id: int, *, allowed_account_ids: list[int]) -> list[dict]: ...

# messages.py
def get_message(conn, message_id: int, *, allowed_account_ids: list[int],
                full_headers: bool = False) -> dict: ...
def get_message_raw(conn, message_id: int, *, allowed_account_ids: list[int]) -> bytes: ...

# attachments.py
def get_attachment_metadata(conn, sha256_hex: str, *, allowed_account_ids: list[int]) -> dict: ...
def open_attachment_bytes(conn, sha256_hex: str, *, allowed_account_ids: list[int]
                         ) -> tuple[BinaryIO, str, int]: ...
def get_attachment_text(conn, sha256_hex: str, *, allowed_account_ids: list[int]) -> str: ...
```

Semantics for every accessor:

- `allowed_account_ids == []` → raise `NotFound` immediately (no rows
  reachable). This is the explicit "user has no grants" case; we surface 404
  rather than 403 so an attacker can't enumerate whether resource X exists.
- `allowed_account_ids == [...]` → SQL `WHERE account_id = ANY(%s)` clause
  added to every query. For attachments the filter joins through `messages`
  (a blob is reachable iff *any* carrying message belongs to an allowed
  account); the filter survives the dedup model in CLAUDE.md because each
  carrying-message row carries its own `account_id`.

Why a `list[int]` and not the full `AuthenticatedUser`? The service library
must not know about HTTP. Passing the resolved list keeps the API library
testable in isolation (no `user_id → grant lookup` round-trip per call) and
lets the route layer cache the resolution across the small handful of DB
hits a single request makes.

## Routes

Each route handler in `src/localmail/serve/routes/` resolves the ACL **once**
per request (a single `SELECT account_id FROM user_accounts WHERE user_id = %s`)
and threads the list into the api-layer call:

```python
@router.get("")
def get_accounts(request, user=Depends(get_authenticated_user)):
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        return list_accounts(conn, allowed_account_ids=allowed)
```

For `/v1/search` the route also intersects any caller-supplied
`filters.account_ids` with `allowed` before handing off to `run_search` —
caller-supplied IDs outside the ACL silently drop, so the query runs against
the intersection, never the union. (Returning 403 would tell an attacker
which account IDs exist; intersection-and-empty-result mirrors the
`/v1/messages/{id}` behaviour.)

For `/v1/changes` the same `account_id = ANY(%s)` is appended to both SQL
branches (`since_id is None` and `since_id is not None`).

`/v1/accounts` is the one route that legitimately may show **zero** rows for
a user with no grants — and that's the correct behaviour. No 403, no error;
the GUI shows "no mailboxes available" and the user knows to contact the
operator.

## Capabilities semantics

`accounts.list_accounts` returns one row per account the user can read. We
set `capabilities.is_shared` per-account using:

```python
"is_shared": len(allowed_account_ids) > 1
```

Reading: *if this user has access to more than one account, the GUI should
show account chips/grouping*. A single-account user sees the same UI as
today. This matches the GUI's pre-existing `is_shared` semantics — the
field is about presentation, not authorisation.

(An alternative: per-account membership counts, exposing "this account is
shared with 3 users". Out of scope for this PR — it leaks data the operator
might not want clients to see; and we'd need a privacy decision before
shipping.)

## Search internals

`Searcher.search()` already accepts a `query` string. Internally,
`parse_query` produces `SearchFilters.account_ids: list[int] | None`. The
route layer now sets `account_ids` to the **intersection** of:

1. The caller's filter (if any).
2. The user's ACL.

Then `_filter_sql` in `arms.py` already appends `m.account_id = ANY(%s)` for
non-None `account_ids`. No SQL change needed in arms; this is exactly the
reuse path the existing filter was designed for.

**Empty intersection.** If the intersection is `[]`, the route short-circuits
to an empty `SearchPage` without running any arm queries. This is both a
correctness fix (the `arms.py` SQL would happily generate `account_id = ANY('{}')`
which is `FALSE`, so the underlying rank-cd ORDER BY scan still runs) and a
small perf win.

**Cursor scoping.** `PageCache` keys today are
`uuid.uuid4().hex[:16]`. We extend the cached payload with `user_id` and
have `continue_page` / `grow_pool` verify the cached `user_id` matches the
caller's `user_id`; mismatch raises `CacheMissError` so the route falls back
to a fresh search rather than reusing the pool. Tokens minted by user A
under user B's request thus behave as if the cache had simply expired —
opaque from the client's perspective, but correct.

## CLI

Two new commands paralleling `add-api-user`:

```
$ localmail grant-account horst gmail-work
granted user 'horst' access to account 'gmail-work' (id=2)

$ localmail revoke-account horst gmail-work
revoked user 'horst' access to account 'gmail-work' (id=2)

$ localmail revoke-account ghost gmail-work
Error: no such user 'ghost'
```

Both look up `user_id` / `account_id` by name, fail loudly on either missing,
and run inside a single transaction. `grant-account` is idempotent —
re-granting an already-granted pair is a no-op + a friendly "already
granted" message, exit code 0 (so cron / Ansible / manual repeats don't
break).

`list-api-users` grows an optional flag to display per-user grants:

```
$ localmail list-api-users --with-grants
horst
  gmail-work (granted 2026-05-18)
  proton-personal (granted 2026-05-19)
ghost [disabled]
  (no grants)
```

Default `list-api-users` output is unchanged so existing scripts keep
working.

The `add-api-user` warning is removed — the implicit "this user can see
everything" failure mode is gone. Replaced by a one-line hint:

```
created user 'horst' (id=3)
note: no account grants yet. Use `localmail grant-account horst <name>` to
give this user read access to mail.
```

## Testing strategy (TDD)

New file `tests/test_api_acl.py` (~120 lines):

- `test_grant_account_inserts_row`
- `test_grant_account_idempotent`
- `test_revoke_account_returns_affected_count`
- `test_allowed_account_ids_empty_for_unknown_user`
- `test_allowed_account_ids_filters_disabled_users`
  (a disabled user still has grants — but disabled users can't authenticate,
  so we don't need to drop their grants. Verify this is the case.)
- `test_grant_account_cascade_on_user_delete`
- `test_grant_account_cascade_on_account_delete`

New file `tests/test_api_acl_filtering.py` (~200 lines):

- For each of `list_accounts`, `list_folders`, `get_message`,
  `get_message_raw`, `get_attachment_metadata`, `open_attachment_bytes`,
  `get_attachment_text`:
  - returns expected rows for granted account
  - returns `NotFound` / empty for ungranted account
  - returns `NotFound` / empty for user with **no** grants
- `test_search_filters_by_acl` — alice and bob both run the same query;
  alice (grants: account A) sees A-only hits; bob (grants: account B) sees
  B-only hits.
- `test_search_cursor_scoped_to_user` — alice mints a token; bob presents it
  to `continue_page`; bob gets a fresh search, not alice's pool.
- `test_search_account_ids_filter_intersects_acl` — caller passes
  `filters.account_ids=[A, B]` but only has access to A; result set scoped
  to A.

New file `tests/test_serve_acl_routes.py` (~150 lines): end-to-end via
TestClient — alice and bob both authenticate; verify HTTP status codes:

- `GET /v1/accounts` returns only allowed accounts
- `GET /v1/accounts/{denied}/folders` → 404 (not 403)
- `GET /v1/messages/{denied}` → 404
- `GET /v1/messages/{denied}/raw` → 404
- `GET /v1/attachments/{sha denied}` → 404
- `GET /v1/attachments/{sha denied}/text` → 404
- `GET /v1/changes` returns only allowed-account messages
- `POST /v1/search` ditto

CLI tests extend `tests/test_cli_serve.py`:

- `test_grant_account_command`
- `test_grant_account_idempotent`
- `test_grant_account_unknown_user`
- `test_grant_account_unknown_account`
- `test_revoke_account_command`
- `test_list_api_users_with_grants`
- `test_add_api_user_no_longer_warns` (verifies the warning text is removed)

Existing tests need updates:

- `tests/test_serve_accounts_routes.py` / `tests/test_serve_messages_routes.py`
  / `tests/test_serve_attachments_routes.py` / `tests/test_serve_changes_route.py`
  / `tests/test_api_search.py` / `tests/test_e2e_serve.py` — all need to
  grant the test user access to the test account in setup. We add a tiny
  helper in `tests/conftest.py`:

  ```python
  @pytest.fixture
  def authorized_user(db_conn, ...) -> AuthenticatedUser:
      """Create a test api_user with grants to every existing account."""
  ```

- `tests/test_searcher.py` — Searcher tests don't go through the route
  layer so they pass the ACL explicitly (or `account_ids=None` to skip the
  filter — see *Internal API choice* below).

## Internal API choice: `None` vs `[]` vs always-required

| Option | Pro | Con | Choice |
|---|---|---|---|
| `allowed_account_ids: list[int]` required everywhere | strictest, no fallback | every internal caller (Searcher tests, CLI sync helpers) must pass it | A |
| `allowed_account_ids: list[int] | None`, `None`=unrestricted | flexible, ergonomic | one missed `None` and the ACL silently goes away in prod | B |
| `allowed_account_ids: list[int]`, `[]` = empty (correct), no None | strict + obvious | callers that legitimately have no scope (CLI, daemon) must pass every-account list | C |

**Chosen: A.** The `localmail.api.*` modules are the *transport-free service
library*; their callers today are the HTTP routes (must pass ACL) and the
MCP server (will pass ACL). Internal CLI code that wants to operate
unfiltered (sync daemon, backfill commands) does not call `localmail.api.*`
— it talks to `localmail.sync`, `localmail.search.embed_worker`, etc. So
making the parameter required at this layer costs nothing for legitimate
use and prevents the entire `None`-fallback footgun.

If we ever surface an admin role, the service layer keeps the typed
`list[int]` signature and admin's resolver returns "all account ids in the
system". The library remains agnostic.

## Migration & upgrade story

The migration is additive (new table, FKs to existing tables). Running
`localmail init-db` on a pre-0016 archive applies cleanly. Operators with
an existing single-operator deployment must, post-upgrade:

```bash
localmail list-accounts                # discover account names
localmail list-api-users               # discover existing usernames
localmail grant-account horst gmail
localmail grant-account horst proton
```

Without the grants, the user's existing API calls return empty result sets
and 404s. **This is intentional** — silent default-allow would mean an
operator with multiple users in their config never notices the change.
README's "Upgrading to 0016" section walks through it.

## Risks & open questions

1. **The grants resolver runs per request.** A `SELECT account_id FROM
   user_accounts WHERE user_id = %s` is sub-millisecond on a primary key
   lookup, but the read happens on every authenticated request. Could move
   to a per-token cache later if profiling shows it; not needed v1.

2. **`/v1/changes` polling cadence.** A client polling every 5 seconds will
   issue the grants query at the same cadence. With one user and a handful
   of accounts this is fine. If a future deployment has 100+ users the
   grants resolver should land behind a small in-process LRU keyed on
   `user_id` with sub-minute TTL.

3. **No grant-all bulk command.** If an operator has 20 accounts and adds a
   new user, that's 20 shell invocations. Acceptable for v1; a future
   `grant-account USERNAME --all` is a one-liner if it becomes painful.

4. **`is_shared = len(allowed) > 1`** assumes the GUI uses it for
   "show account chips" only. Verify with the GUI client (Tauri / Svelte 5)
   when we hook it up — if `is_shared` is wired to something else, revisit.

5. **Disabled users keep grants.** If we want grants to vanish on disable,
   that's a one-line WHERE in `allowed_account_ids`. Decision: keep them.
   Re-enable should restore the prior access posture, not require
   re-granting every account.

## Done definition

- Migration 0016 is in `migrations/`, applies cleanly via `localmail init-db`.
- `tests/test_api_acl.py`, `tests/test_api_acl_filtering.py`,
  `tests/test_serve_acl_routes.py` all pass.
- All pre-existing serve route tests still pass after the conftest helper
  is added (no behavioural regressions for the single-account-single-user
  case).
- `localmail grant-account` / `revoke-account` / `list-api-users
  --with-grants` work end-to-end against a real Postgres.
- `add-api-user` no longer prints the per-account warning; emits the
  "next: grant-account …" hint instead.
- `is_shared` in `/v1/accounts` reflects multi-account membership.
- README has an "ACL upgrade" section; CLAUDE.md gains one paragraph in
  "GUI server" explaining the join table.
- PR description closes #31 and #8.
