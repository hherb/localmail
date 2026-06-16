# Access-token Family Containment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On refresh-token reuse detection, immediately revoke the access tokens minted within that same refresh family — closing the ≤1h residual-access window from the #186 design.

**Architecture:** Add a nullable `oauth_refresh_family_id` UUID column to `api_tokens` (migration 0030). `access.mint_access` writes it; a new `access.revoke_access_family` deletes by it. `refresh.RotateResult` carries the family id on the `reuse` outcome; the provider's reuse branch calls `revoke_access_family` inside the existing transaction before committing. Store-boundary discipline preserved: `refresh.py` only touches `oauth_refresh_tokens`, `access.py` only touches `api_tokens`, the provider orchestrates.

**Tech Stack:** Python 3.12, psycopg v3, raw SQL + numbered migrations, pytest against `localmail_test`, mypy. Reference spec: [docs/superpowers/specs/2026-06-16-access-token-family-containment-design.md](../specs/2026-06-16-access-token-family-containment-design.md).

**Branch:** `feat/access-token-family-containment` (already created; design committed at `70b49c7`).

**Test command (full):**
```bash
unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/ --deselect tests/test_daemon_control_socket.py
unset VIRTUAL_ENV && uv run mypy src/localmail
```

---

### Task 1: Migration `0030_api_tokens_refresh_family.sql`

**Files:**
- Create: `migrations/0030_api_tokens_refresh_family.sql`
- Test: `tests/test_oauth_migration.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_oauth_migration.py`:

```python
def test_api_tokens_gains_oauth_refresh_family_id(db_conn):
    cols = _columns(db_conn, "api_tokens")
    assert "oauth_refresh_family_id" in cols


def test_oauth_refresh_family_id_is_uuid_nullable(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'api_tokens' "
            "  AND column_name = 'oauth_refresh_family_id'"
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "uuid" and row[1] == "YES"


def test_api_tokens_has_refresh_family_partial_index(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'api_tokens' "
            "  AND indexname = 'api_tokens_oauth_refresh_family_id_idx'"
        )
        row = cur.fetchone()
    assert row is not None
    indexdef = row[0]
    assert "oauth_refresh_family_id" in indexdef
    assert "IS NOT NULL" in indexdef  # partial index predicate present
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_migration.py -q`
Expected: the 3 new tests FAIL (column / index absent). The migration runner applies all migrations to `localmail_test` on the `db_conn` fixture, so failures are "column does not exist" / empty index lookup.

- [ ] **Step 3: Write the migration**

Create `migrations/0030_api_tokens_refresh_family.sql`:

```sql
-- Access-token family containment on refresh-token reuse. OAuth-minted access
-- tokens are tagged with the refresh family they belong to so that, on
-- refresh-token reuse detection (oauth_refresh_tokens family DELETE), the
-- access tokens in the same family can be purged immediately rather than
-- lingering until their <=1h TTL (closes the #186 accepted limitation).
-- NULL on /v1/auth/login tokens and pre-migration rows -- structurally immune
-- to the family purge. UUID matches oauth_refresh_tokens.family_id (0029).

ALTER TABLE api_tokens
    ADD COLUMN oauth_refresh_family_id UUID;

CREATE INDEX api_tokens_oauth_refresh_family_id_idx
    ON api_tokens (oauth_refresh_family_id)
    WHERE oauth_refresh_family_id IS NOT NULL;
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_migration.py -q`
Expected: all migration tests PASS (the `db_conn` fixture re-applies migrations).

- [ ] **Step 5: Commit**

```bash
git add migrations/0030_api_tokens_refresh_family.sql tests/test_oauth_migration.py
git commit -m "feat(mcp): migration 0030 — api_tokens.oauth_refresh_family_id"
```

---

### Task 2: `access.mint_access` tags family + `access.revoke_access_family`

**Files:**
- Modify: `src/localmail/mcp/oauth/access.py`
- Test: `tests/test_oauth_access_bridge.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_oauth_access_bridge.py` (top of file already has
`from localmail.api import auth as api_auth` and
`from localmail.mcp.oauth import access, clients`; add `import uuid` at the top):

```python
def _family_id(conn, raw):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT oauth_refresh_family_id FROM api_tokens WHERE token_sha256 = %s",
            (api_auth.hash_token(raw),),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


def test_mint_access_persists_family_id(db_conn):
    uid = _seed(db_conn)
    fam = uuid.uuid4()
    raw = access.mint_access(
        db_conn, user_id=uid, client_id="cid", ttl_s=3600, family_id=fam
    )
    db_conn.commit()
    assert _family_id(db_conn, raw) == fam


def test_mint_access_without_family_is_null(db_conn):
    uid = _seed(db_conn)
    raw = access.mint_access(db_conn, user_id=uid, client_id="cid", ttl_s=3600)
    db_conn.commit()
    assert _family_id(db_conn, raw) is None


def test_revoke_access_family_deletes_only_matching(db_conn):
    uid = _seed(db_conn)
    fam = uuid.uuid4()
    other = uuid.uuid4()
    in_fam = access.mint_access(
        db_conn, user_id=uid, client_id="cid", ttl_s=3600, family_id=fam
    )
    other_fam = access.mint_access(
        db_conn, user_id=uid, client_id="cid", ttl_s=3600, family_id=other
    )
    no_fam = access.mint_access(db_conn, user_id=uid, client_id="cid", ttl_s=3600)
    db_conn.commit()
    deleted = access.revoke_access_family(db_conn, fam)
    db_conn.commit()
    assert deleted == 1
    assert access.load_access(db_conn, in_fam) is None
    assert access.load_access(db_conn, other_fam) is not None
    assert access.load_access(db_conn, no_fam) is not None


def test_revoke_access_family_absent_returns_zero(db_conn):
    assert access.revoke_access_family(db_conn, uuid.uuid4()) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_access_bridge.py -q`
Expected: new tests FAIL — `mint_access() got an unexpected keyword argument 'family_id'` and `module 'access' has no attribute 'revoke_access_family'`.

- [ ] **Step 3: Implement the changes**

In `src/localmail/mcp/oauth/access.py`, add `import uuid` near the top imports, then replace `mint_access` and add `revoke_access_family`:

```python
def mint_access(
    conn: psycopg.Connection,
    *,
    user_id: int,
    client_id: str,
    ttl_s: int,
    family_id: uuid.UUID | None = None,
) -> str:
    """Mint an access token into api_tokens; return the raw token. Caller commits.

    ``family_id`` ties the token to a refresh family so reuse detection can purge
    it (see ``revoke_access_family``); ``None`` (login/non-OAuth) leaves it NULL.
    """
    raw = generate_token()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_tokens "
            "(token_sha256, user_id, expires_at, oauth_client_id, "
            " oauth_refresh_family_id) "
            "VALUES (%s, %s, now() + make_interval(secs => %s), %s, %s)",
            (hash_token(raw), user_id, ttl_s, client_id, family_id),
        )
    return raw


def revoke_access_family(conn: psycopg.Connection, family_id: uuid.UUID) -> int:
    """Delete every access token in a refresh family. Returns the deleted count.

    Called from the provider's reuse branch so a detected refresh-token reuse
    contains the access window immediately. Caller commits.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM api_tokens WHERE oauth_refresh_family_id = %s",
            (family_id,),
        )
        return cur.rowcount
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_access_bridge.py -q`
Expected: all PASS (existing tests still green — `mint_access` keeps its keyword-only call shape; `family_id` defaults to None).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/oauth/access.py tests/test_oauth_access_bridge.py
git commit -m "feat(mcp): access store — tag family on mint, revoke_access_family"
```

---

### Task 3: `RotateResult.family_id` populated on reuse

**Files:**
- Modify: `src/localmail/mcp/oauth/refresh.py`
- Test: `tests/test_oauth_refresh_store.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_oauth_refresh_store.py` (it already imports
`from localmail.mcp.oauth import clients, refresh` and defines `_seed`):

```python
def test_reuse_result_carries_family_id(db_conn):
    uid = _seed(db_conn)
    old = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    fam = refresh.load_refresh(db_conn, old).family_id
    refresh.rotate_refresh(db_conn, old, ttl_s=100)  # tombstones old
    db_conn.commit()
    replay = refresh.rotate_refresh(db_conn, old, ttl_s=100)  # reuse
    db_conn.commit()
    assert replay.outcome == "reuse"
    assert replay.family_id == fam


def test_rotated_and_unknown_results_have_no_family_id(db_conn):
    uid = _seed(db_conn)
    raw = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    rotated = refresh.rotate_refresh(db_conn, raw, ttl_s=100)
    db_conn.commit()
    assert rotated.outcome == "rotated" and rotated.family_id is None
    unknown = refresh.rotate_refresh(db_conn, "bogus", ttl_s=100)
    assert unknown.outcome == "unknown" and unknown.family_id is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_refresh_store.py -q`
Expected: `test_reuse_result_carries_family_id` FAILS (`replay.family_id` is None — the field defaults None and isn't set on the reuse branches yet). `test_rotated_and_unknown_results_have_no_family_id` passes immediately (field already defaults None) — that's fine, it pins the invariant.

- [ ] **Step 3: Implement the changes**

In `src/localmail/mcp/oauth/refresh.py`:

(a) Add the field to `RotateResult` (and document it):

```python
@dataclass(frozen=True)
class RotateResult:
    """Outcome of a rotation attempt.

    - ``rotated``: presented token was live; ``new_token`` holds the successor.
    - ``reuse``: presented token was an already-consumed tombstone; its family
      has been deleted. ``new_token`` is None; ``family_id`` names the deleted
      family so the caller can also purge the family's access tokens.
    - ``unknown``: presented token was absent, expired, or its user disabled
      (no theft signal). ``new_token`` and ``family_id`` are None.
    """
    outcome: Literal["rotated", "reuse", "unknown"]
    new_token: str | None = None
    family_id: _uuid.UUID | None = None
```

(b) In `rotate_refresh`, set `family_id` on **both** reuse returns:

The early consumed-tombstone branch:

```python
    family_id, is_consumed = state
    if is_consumed:
        _delete_family(conn, family_id)
        return RotateResult("reuse", family_id=family_id)
```

The claim-lost concurrency branch:

```python
    if not claimed:
        _delete_family(conn, row.family_id)
        return RotateResult("reuse", family_id=row.family_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_refresh_store.py -q`
Expected: all PASS (existing reuse tests unaffected — they don't assert on `family_id`).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/oauth/refresh.py tests/test_oauth_refresh_store.py
git commit -m "feat(mcp): refresh store — RotateResult carries reuse family_id"
```

---

### Task 4: Provider wiring — tag access tokens + purge family on reuse

**Files:**
- Modify: `src/localmail/mcp/oauth/provider.py`
- Test: `tests/test_oauth_provider.py` (append)

- [ ] **Step 1: Write the failing acceptance tests**

Append to `tests/test_oauth_provider.py` (uses the file's existing `_provider`,
`_client`, `db_pool` helpers and `api_auth`, `codes`, `anyio` imports):

```python
def _full_flow_tokens(p, db_pool, username):
    """Run code-exchange once; return (access_token, refresh_token, uid)."""
    anyio.run(p.register_client, _client())
    with db_pool.connection() as conn:
        uid = api_auth.create_user(conn, username, "pw")
        raw_code = codes.mint_code(
            conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
            redirect_uri_provided_explicitly=True, code_challenge="chal",
            scopes=[], ttl_s=60,
        )
        conn.commit()
    loaded = anyio.run(p.load_authorization_code, _client(), raw_code)
    token = anyio.run(p.exchange_authorization_code, _client(), loaded)
    return token.access_token, token.refresh_token, uid


def test_code_exchange_access_token_is_tagged_with_family(db_conn, db_pool):
    p = _provider(db_pool)
    access_tok, _refresh, _uid = _full_flow_tokens(p, db_pool, "fam-tag-user")
    with db_pool.connection() as conn:
        cur = conn.execute(
            "SELECT oauth_refresh_family_id FROM api_tokens WHERE token_sha256 = %s",
            (api_auth.hash_token(access_tok),),
        )
        row = cur.fetchone()
    assert row is not None and row[0] is not None


def test_refresh_reuse_purges_family_access_tokens(db_conn, db_pool):
    p = _provider(db_pool)
    access_tok, refresh_tok, _uid = _full_flow_tokens(p, db_pool, "reuse-user")
    # access token works before reuse
    assert anyio.run(p.load_access_token, access_tok) is not None
    # rotate once (consumes refresh_tok), then replay it -> reuse
    rt = anyio.run(p.load_refresh_token, _client(), refresh_tok)
    anyio.run(p.exchange_refresh_token, _client(), rt, [])
    with pytest.raises(TokenError):
        # replay the now-consumed original refresh token
        rt2 = RefreshToken(
            token=refresh_tok, client_id="cid", scopes=[], expires_at=None
        )
        anyio.run(p.exchange_refresh_token, _client(), rt2, [])
    # the access token minted in that family is gone
    assert anyio.run(p.load_access_token, access_tok) is None


def test_reuse_purge_spares_login_token_of_same_user(db_conn, db_pool):
    p = _provider(db_pool)
    access_tok, refresh_tok, uid = _full_flow_tokens(p, db_pool, "spare-user")
    with db_pool.connection() as conn:
        login_tok, _exp = api_auth.issue_token(conn, uid)  # NULL family
        conn.commit()
    rt = anyio.run(p.load_refresh_token, _client(), refresh_tok)
    anyio.run(p.exchange_refresh_token, _client(), rt, [])
    with pytest.raises(TokenError):
        rt2 = RefreshToken(
            token=refresh_tok, client_id="cid", scopes=[], expires_at=None
        )
        anyio.run(p.exchange_refresh_token, _client(), rt2, [])
    # OAuth access token purged, but the user's login token (NULL family) survives
    assert anyio.run(p.load_access_token, access_tok) is None
    with db_pool.connection() as conn:
        assert api_auth.verify_token(conn, login_tok) is not None
```

> **Import note:** add `from mcp.server.auth.provider import RefreshToken` to the
> file's existing SDK import line (it currently imports `AuthorizationParams,
> TokenError`). `api_auth.issue_token(conn, uid)` returns `(raw_token,
> expires_at)`; the login token row has a NULL `oauth_refresh_family_id`, so it
> is immune to the family purge.

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_provider.py -q`
Expected: `test_code_exchange_access_token_is_tagged_with_family` FAILS (`row[0]` is None — code-exchange doesn't tag yet); `test_refresh_reuse_purges_family_access_tokens` FAILS (access token still loads after reuse — provider doesn't purge yet).

- [ ] **Step 3: Implement the provider changes**

In `src/localmail/mcp/oauth/provider.py`, `_exchange_code_sync`, replace the
mint block (currently mints access then refresh) with refresh-first + tag:

```python
            else:
                refresh_raw = refresh.mint_refresh(
                    conn, client_id=client_id, user_id=user_id,
                    scopes=auth_code.scopes,
                    ttl_s=self._cfg.oauth_refresh_token_ttl_s,
                )
                new_row = refresh.load_refresh(conn, refresh_raw)
                assert new_row is not None
                access_raw = access.mint_access(
                    conn, user_id=user_id, client_id=client_id,
                    ttl_s=self._cfg.oauth_access_token_ttl_s,
                    family_id=new_row.family_id,
                )
                clients.touch_last_used(conn, client_id)
                conn.commit()
```

In `_exchange_refresh_sync`, the `rotated` branch — pass the family into
`mint_access` (it already loads `row`):

```python
            if result.outcome == "rotated":
                assert result.new_token is not None
                row = refresh.load_refresh(conn, result.new_token)
                assert row is not None
                access_raw = access.mint_access(
                    conn, user_id=row.user_id, client_id=client_id,
                    ttl_s=self._cfg.oauth_access_token_ttl_s,
                    family_id=row.family_id,
                )
                clients.touch_last_used(conn, client_id)
                conn.commit()
```

Add a purged-count holder and the purge in the `reuse` branch. Declare
`purged = 0` next to `access_raw: str | None = None` at the top of the method,
then:

```python
            elif result.outcome == "reuse":
                assert result.family_id is not None
                purged = access.revoke_access_family(conn, result.family_id)
                # The family DELETE (refresh) + access purge must persist.
                conn.commit()
```

Finally, fold the purged count into the existing reuse WARNING:

```python
        if result.outcome == "reuse":
            logger.warning(
                "refresh-token reuse detected; revoked family for client_id=%s "
                "(access tokens purged=%d)",
                client_id, purged,
            )
            raise TokenError("invalid_grant", "refresh token reuse detected")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_oauth_provider.py -q`
Expected: all PASS, including the existing
`test_exchange_authorization_code_mints_tokens_and_consumes_code` and the reuse
tests from #186.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/oauth/provider.py tests/test_oauth_provider.py
git commit -m "feat(mcp): purge access-token family on refresh reuse (closes #186 limitation)"
```

---

### Task 5: Full suite + types + docs

**Files:**
- Modify: `CLAUDE.md` (refresh-family bullet — note the limitation is now closed)

- [ ] **Step 1: Run the full Python suite**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/ --deselect tests/test_daemon_control_socket.py`
Expected: all pass (baseline 1658 + the new tests).

- [ ] **Step 2: Run mypy**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: clean (121 files). If `uuid` import or `family_id` typing flags
anything, fix inline (e.g. ensure `revoke_access_family` returns `int` and
`mint_access`'s `family_id` is `uuid.UUID | None`).

- [ ] **Step 3: Update CLAUDE.md**

In `CLAUDE.md`, find the #183/#185 refresh-family bullet that ends with the
"Accepted limitation: the family DELETE only revokes refresh tokens; access
tokens … stay valid at `/mcp` until their ≤1h TTL …" sentence. Replace that
**Accepted limitation** sentence with a note that it is now **closed**:

> **Access-token family containment (closes the #186 limitation):** migration
> `0030_api_tokens_refresh_family.sql` adds nullable
> `api_tokens.oauth_refresh_family_id` (UUID, partial index). OAuth-minted
> access tokens are tagged with their refresh family (`access.mint_access`
> `family_id=`); on reuse detection the provider's reuse branch calls
> `access.revoke_access_family(family_id)` inside the same transaction as the
> refresh-family DELETE, so the access window is contained immediately rather
> than living out its ≤1h TTL. Login tokens (`/v1/auth/login`,
> `oauth_refresh_family_id IS NULL`) are structurally immune. Reuse-only;
> normal rotation predecessors still expire by TTL.

Also bump the "next free slot" line and the migrations list:
- migrations range mention → `… 0030_api_tokens_refresh_family.sql`
- "Latest is `0029_…`; next free slot `0030_*.sql`." → "Latest is
  `0030_api_tokens_refresh_family.sql`; next free slot `0031_*.sql`."

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md — access-token family containment closes #186 limitation"
```

---

## Self-Review notes (for the implementer)

- **Spec coverage:** Task 1 = schema; Task 2 = `mint_access` family + `revoke_access_family`; Task 3 = `RotateResult.family_id`; Task 4 = provider code-exchange tag, rotated-branch tag, reuse-branch purge + WARNING; Task 5 = verification + docs. All spec sections covered.
- **No mint_refresh signature change** (per refined spec) — the code-exchange path reads family via `load_refresh`. Do NOT change `mint_refresh`'s return type; the existing store tests rely on `raw = mint_refresh(...)`.
- **Type consistency:** `family_id` is `uuid.UUID | None` everywhere (`mint_access` param, `RotateResult.family_id`, `revoke_access_family` arg); psycopg adapts `uuid.UUID` to a UUID bind param natively. `revoke_access_family` returns `int`.
- **Provider transaction:** the reuse branch must `commit()` after `revoke_access_family` so BOTH the refresh-family DELETE (already done inside `rotate_refresh`) and the access purge persist atomically.
- **Login-token helper** in Task 4's third test is `api_auth.issue_token(conn, uid) -> (raw, expires_at)`; verify with `api_auth.verify_token(conn, raw)`. Both confirmed against `src/localmail/api/auth.py`.
