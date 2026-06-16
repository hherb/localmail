# OAuth refresh-token family revocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On detected reuse of an already-rotated OAuth refresh token, revoke the entire active refresh-token family for that client+user (RFC 9700 §4.14.2), and add the `oauth_refresh_tokens(client_id)` index (#185).

**Architecture:** Add a `family_id` UUID (carried across rotations) and a `consumed_at` tombstone column to `oauth_refresh_tokens`. Rotation marks the presented token consumed (instead of hard-deleting) and mints a successor in the same family. Replaying a consumed token is detected as reuse → the whole family is deleted. Consumed tombstones are swept once past their own `expires_at`. `clients.cleanup_unused` is taught to ignore tombstones.

**Tech Stack:** Python 3.12, psycopg v3 + raw SQL, numbered `.sql` migrations, pytest against `localmail_test`, the `mcp` extra for the provider tests.

---

## Reference (read before starting)

- Spec: [docs/superpowers/specs/2026-06-16-oauth-refresh-token-family-revocation-design.md](../specs/2026-06-16-oauth-refresh-token-family-revocation-design.md)
- Store: [src/localmail/mcp/oauth/refresh.py](../../../src/localmail/mcp/oauth/refresh.py)
- Clients store: [src/localmail/mcp/oauth/clients.py](../../../src/localmail/mcp/oauth/clients.py)
- Provider: [src/localmail/mcp/oauth/provider.py](../../../src/localmail/mcp/oauth/provider.py)
- Migration 0028 (table def): [migrations/0028_oauth_server.sql](../../../migrations/0028_oauth_server.sql)
- Migrations auto-apply once per session via `db_dsn` fixture (`apply_migrations(TEST_DSN)` in `tests/conftest.py:63`). Adding the `.sql` file is enough for tests to pick it up.

**Run all tests with the mcp extra and deselect the macOS-only flaky socket test:**
```bash
unset VIRTUAL_ENV && uv run --extra mcp pytest -q <paths> --deselect tests/test_daemon_control_socket.py
```

---

## Task 1: Migration 0029 — family_id, consumed_at, indexes

**Files:**
- Create: `migrations/0029_oauth_refresh_token_family.sql`
- Test: `tests/test_oauth_migration.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_oauth_migration.py`:

```python
def test_refresh_tokens_gains_family_and_consumed(db_conn):
    cols = _columns(db_conn, "oauth_refresh_tokens")
    assert {"family_id", "consumed_at"} <= cols


def test_refresh_tokens_has_client_id_and_family_indexes(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = %s",
            ("oauth_refresh_tokens",),
        )
        defs = " ".join(r[0] for r in cur.fetchall())
    assert "family_id" in defs
    assert "(client_id)" in defs.replace(" ", "")


def test_cleanup_unused_subquery_uses_client_id_index(db_conn):
    # #185 acceptance: the NOT EXISTS correlated subquery must hit the
    # client_id index, not a seq scan, once oauth_refresh_tokens is seeded.
    from localmail.api import auth as api_auth
    from localmail.mcp.oauth import clients, refresh

    clients.register_client(
        db_conn, client_id="idx-cid", client_secret_sha256=None,
        redirect_uris=["https://c/cb"], client_name=None,
        grant_types=["refresh_token"], response_types=["code"],
        token_endpoint_auth_method="none", scope=None,
    )
    uid = api_auth.create_user(db_conn, "idx-user", "pw")
    for _ in range(50):
        refresh.mint_refresh(
            db_conn, client_id="idx-cid", user_id=uid, scopes=[], ttl_s=3600
        )
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute(
            "EXPLAIN SELECT 1 FROM oauth_refresh_tokens r "
            "WHERE r.client_id = %s AND r.expires_at > now()",
            ("idx-cid",),
        )
        plan = " ".join(r[0] for r in cur.fetchall())
    assert "oauth_refresh_tokens_client_id_idx" in plan
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/test_oauth_migration.py -k "family or index or client_id" --deselect tests/test_daemon_control_socket.py`
Expected: FAIL (columns/indexes don't exist yet).

- [ ] **Step 3: Write the migration**

Create `migrations/0029_oauth_refresh_token_family.sql`:

```sql
-- Refresh-token family revocation (RFC 9700 §4.14.2). Rotation no longer
-- hard-deletes the presented token: it is tombstoned via consumed_at, and the
-- successor inherits the same family_id. Replaying a consumed token signals
-- theft, so the whole family is deleted (see mcp/oauth/refresh.py). The
-- client_id index serves clients.cleanup_unused's correlated NOT EXISTS (#185).

ALTER TABLE oauth_refresh_tokens
    ADD COLUMN family_id   UUID NOT NULL DEFAULT gen_random_uuid(),
    ADD COLUMN consumed_at TIMESTAMPTZ;

CREATE INDEX oauth_refresh_tokens_family_id_idx ON oauth_refresh_tokens (family_id);
CREATE INDEX oauth_refresh_tokens_client_id_idx ON oauth_refresh_tokens (client_id);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/test_oauth_migration.py --deselect tests/test_daemon_control_socket.py`
Expected: PASS (all migration tests, including the 3 new ones).

- [ ] **Step 5: Commit**

```bash
git add migrations/0029_oauth_refresh_token_family.sql tests/test_oauth_migration.py
git commit -m "feat(mcp): add refresh-token family_id + consumed_at + indexes (#183, #185)"
```

---

## Task 2: refresh store — family-aware mint/load + RotateResult rotation + sweep

**Files:**
- Modify: `src/localmail/mcp/oauth/refresh.py`
- Test: `tests/test_oauth_refresh_store.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_oauth_refresh_store.py` (it already has `_seed`, `_disable_user`, and imports `refresh`, `clients`, `api_auth`):

```python
def _raw_lookup(conn, raw):
    from localmail.api.auth import hash_token
    with conn.cursor() as cur:
        cur.execute(
            "SELECT family_id, consumed_at FROM oauth_refresh_tokens "
            "WHERE token_sha256 = %s",
            (hash_token(raw),),
        )
        return cur.fetchone()


def test_rotate_tombstones_old_keeps_family(db_conn):
    uid = _seed(db_conn)
    old = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    res = refresh.rotate_refresh(db_conn, old, ttl_s=100)
    db_conn.commit()
    assert res.outcome == "rotated" and res.new_token
    # old row survives as a tombstone (consumed_at set), no longer loads live
    old_row = _raw_lookup(db_conn, old)
    assert old_row is not None and old_row[1] is not None
    assert refresh.load_refresh(db_conn, old) is None
    # successor is live and shares the family
    new_live = refresh.load_refresh(db_conn, res.new_token)
    assert new_live is not None
    assert new_live.family_id == old_row[0]


def test_replay_consumed_token_revokes_family(db_conn):
    # #183 acceptance: rotate, then replay the OLD token -> reuse detected and
    # the NEW (active) token is also rejected afterward.
    uid = _seed(db_conn)
    old = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    res = refresh.rotate_refresh(db_conn, old, ttl_s=100)
    db_conn.commit()
    new = res.new_token
    assert refresh.load_refresh(db_conn, new) is not None
    replay = refresh.rotate_refresh(db_conn, old, ttl_s=100)
    db_conn.commit()
    assert replay.outcome == "reuse" and replay.new_token is None
    assert refresh.load_refresh(db_conn, new) is None  # active token nuked too
    assert _raw_lookup(db_conn, new) is None            # family hard-deleted


def test_replay_revokes_only_its_own_family(db_conn):
    uid = _seed(db_conn)
    a = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    b = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    a2 = refresh.rotate_refresh(db_conn, a, ttl_s=100).new_token
    db_conn.commit()
    refresh.rotate_refresh(db_conn, a, ttl_s=100)  # reuse on family A
    db_conn.commit()
    assert refresh.load_refresh(db_conn, a2) is None   # family A gone
    assert refresh.load_refresh(db_conn, b) is not None  # family B untouched


def test_rotate_unknown_returns_unknown(db_conn):
    res = refresh.rotate_refresh(db_conn, "bogus", ttl_s=100)
    assert res.outcome == "unknown" and res.new_token is None


def test_expired_not_consumed_is_unknown_not_reuse(db_conn):
    uid = _seed(db_conn)
    raw = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=-1)
    db_conn.commit()
    res = refresh.rotate_refresh(db_conn, raw, ttl_s=100)
    assert res.outcome == "unknown"


def test_disabled_user_not_consumed_is_unknown_not_reuse(db_conn):
    uid = _seed(db_conn)
    raw = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    _disable_user(db_conn, uid)
    res = refresh.rotate_refresh(db_conn, raw, ttl_s=100)
    assert res.outcome == "unknown"


def test_family_id_stable_across_rotations(db_conn):
    uid = _seed(db_conn)
    t = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    fam0 = refresh.load_refresh(db_conn, t).family_id
    for _ in range(3):
        t = refresh.rotate_refresh(db_conn, t, ttl_s=100).new_token
        db_conn.commit()
        assert refresh.load_refresh(db_conn, t).family_id == fam0


def test_sweep_consumed_deletes_only_expired_tombstones(db_conn):
    uid = _seed(db_conn)
    # live token: not consumed -> never swept
    live = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    # consumed-but-not-expired tombstone -> kept (reuse still detectable)
    keep = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    # consumed-and-expired tombstone -> swept
    gone = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=-1)
    db_conn.commit()
    with db_conn.cursor() as cur:
        from localmail.api.auth import hash_token
        cur.execute("UPDATE oauth_refresh_tokens SET consumed_at = now() "
                    "WHERE token_sha256 = ANY(%s)",
                    ([hash_token(keep), hash_token(gone)],))
    db_conn.commit()
    deleted = refresh.sweep_consumed(db_conn)
    db_conn.commit()
    assert deleted == 1
    assert _raw_lookup(db_conn, gone) is None
    assert _raw_lookup(db_conn, keep) is not None
    assert _raw_lookup(db_conn, live) is not None
```

Note: the existing `test_rotate_revokes_old_returns_new` and
`test_rotate_unknown_returns_none` tests assert the *old* `str | None` contract
and must be updated to the new `RotateResult` shape. Replace their bodies:

```python
def test_rotate_revokes_old_returns_new(db_conn):
    uid = _seed(db_conn)
    old = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    res = refresh.rotate_refresh(db_conn, old, ttl_s=100)
    db_conn.commit()
    assert res.outcome == "rotated" and res.new_token and res.new_token != old
    assert refresh.load_refresh(db_conn, old) is None
    assert refresh.load_refresh(db_conn, res.new_token) is not None
```

And delete the now-duplicated `test_rotate_unknown_returns_none` (superseded by
`test_rotate_unknown_returns_unknown`). Also update
`test_rotate_rejected_for_disabled_user` to the new shape:

```python
def test_rotate_rejected_for_disabled_user(db_conn):
    uid = _seed(db_conn)
    raw = refresh.mint_refresh(db_conn, client_id="cid", user_id=uid, scopes=[], ttl_s=100)
    db_conn.commit()
    _disable_user(db_conn, uid)
    assert refresh.rotate_refresh(db_conn, raw, ttl_s=100).outcome == "unknown"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/test_oauth_refresh_store.py --deselect tests/test_daemon_control_socket.py`
Expected: FAIL (`RotateResult`/`sweep_consumed`/`family_id` not defined; AttributeError on `.outcome`).

- [ ] **Step 3: Rewrite `src/localmail/mcp/oauth/refresh.py`**

Replace the file body with the family-aware version:

```python
"""Rotating refresh-token store. Tokens are SHA-256-hashed.

Rotation tombstones the presented token (sets ``consumed_at``) and mints a
successor in the same ``family_id`` with a fresh sliding expiry, so an active
client never re-authenticates. Replaying an already-consumed token is treated as
reuse (a stolen-copy signal): the entire family is deleted (RFC 9700 §4.14.2).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg

from localmail.api.auth import generate_token, hash_token


@dataclass(frozen=True)
class RefreshRow:
    client_id: str
    user_id: int
    scopes: list[str]
    expires_at: datetime
    family_id: str


@dataclass(frozen=True)
class RotateResult:
    """Outcome of a rotation attempt.

    - ``rotated``: presented token was live; ``new_token`` holds the successor.
    - ``reuse``: presented token was an already-consumed tombstone; its family
      has been deleted. ``new_token`` is None.
    - ``unknown``: presented token was absent, expired, or its user disabled
      (no theft signal). ``new_token`` is None.
    """
    outcome: Literal["rotated", "reuse", "unknown"]
    new_token: str | None = None


def mint_refresh(
    conn: psycopg.Connection,
    *,
    client_id: str,
    user_id: int,
    scopes: list[str],
    ttl_s: int,
    family_id: str | None = None,
) -> str:
    """Mint + persist a refresh token; return the raw token. Caller commits.

    ``family_id=None`` lets the DB default mint a fresh family (code-exchange);
    a supplied value joins the successor to its parent's family (rotation).
    """
    raw = generate_token()
    with conn.cursor() as cur:
        if family_id is None:
            cur.execute(
                "INSERT INTO oauth_refresh_tokens (token_sha256, client_id, "
                "user_id, scopes, expires_at) "
                "VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s))",
                (hash_token(raw), client_id, user_id, scopes, ttl_s),
            )
        else:
            cur.execute(
                "INSERT INTO oauth_refresh_tokens (token_sha256, client_id, "
                "user_id, scopes, expires_at, family_id) "
                "VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s), %s)",
                (hash_token(raw), client_id, user_id, scopes, ttl_s, family_id),
            )
    return raw


def load_refresh(conn: psycopg.Connection, raw_token: str) -> RefreshRow | None:
    """Load a *live* (not consumed, not expired, user enabled) refresh row.

    The ``consumed_at IS NULL`` filter hides tombstones; the ``api_users`` JOIN
    + ``disabled_at IS NULL`` mirrors ``api.auth.verify_token`` (RFC 9700 §4.13).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.client_id, r.user_id, r.scopes, r.expires_at, r.family_id "
            "FROM oauth_refresh_tokens r "
            "JOIN api_users u ON u.id = r.user_id "
            "WHERE r.token_sha256 = %s AND r.expires_at > now() "
            "  AND r.consumed_at IS NULL AND u.disabled_at IS NULL",
            (hash_token(raw_token),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return RefreshRow(
        client_id=row[0], user_id=row[1], scopes=row[2],
        expires_at=row[3], family_id=str(row[4]),
    )


def revoke_refresh(conn: psycopg.Connection, raw_token: str) -> bool:
    """Hard-delete a token by hash (the SDK's explicit revoke). Caller commits."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM oauth_refresh_tokens WHERE token_sha256 = %s",
            (hash_token(raw_token),),
        )
        return cur.rowcount > 0


def _raw_state(
    conn: psycopg.Connection, raw_token: str
) -> tuple[str, bool] | None:
    """Return ``(family_id, is_consumed)`` for a token regardless of expiry /
    user state, or None if no such row. Used to distinguish reuse from unknown.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT family_id, consumed_at FROM oauth_refresh_tokens "
            "WHERE token_sha256 = %s",
            (hash_token(raw_token),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return str(row[0]), row[1] is not None


def _delete_family(conn: psycopg.Connection, family_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM oauth_refresh_tokens WHERE family_id = %s", (family_id,)
        )
        return cur.rowcount


def sweep_consumed(conn: psycopg.Connection) -> int:
    """Delete consumed tombstones past their own ``expires_at``. Caller commits.

    Reuse stays detectable for the full original token lifetime; afterwards the
    whole family has expired anyway, so the tombstone is safe to drop.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM oauth_refresh_tokens "
            "WHERE consumed_at IS NOT NULL AND expires_at < now()"
        )
        return cur.rowcount


def rotate_refresh(
    conn: psycopg.Connection, raw_token: str, *, ttl_s: int
) -> RotateResult:
    """Tombstone ``raw_token`` and mint a successor in the same family, or detect
    reuse and revoke the family. Caller commits.
    """
    state = _raw_state(conn, raw_token)
    if state is None:
        return RotateResult("unknown")
    family_id, is_consumed = state
    if is_consumed:
        _delete_family(conn, family_id)
        return RotateResult("reuse")
    row = load_refresh(conn, raw_token)
    if row is None:
        # present but expired / user-disabled — natural, not theft.
        return RotateResult("unknown")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE oauth_refresh_tokens SET consumed_at = now() "
            "WHERE token_sha256 = %s",
            (hash_token(raw_token),),
        )
    new = mint_refresh(
        conn, client_id=row.client_id, user_id=row.user_id,
        scopes=row.scopes, ttl_s=ttl_s, family_id=row.family_id,
    )
    sweep_consumed(conn)
    return RotateResult("rotated", new_token=new)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/test_oauth_refresh_store.py --deselect tests/test_daemon_control_socket.py`
Expected: PASS (all store tests).

- [ ] **Step 5: Run mypy**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: clean (no new errors).

- [ ] **Step 6: Commit**

```bash
git add src/localmail/mcp/oauth/refresh.py tests/test_oauth_refresh_store.py
git commit -m "feat(mcp): family-aware refresh rotation with reuse detection (#183)"
```

---

## Task 3: clients.cleanup_unused ignores tombstones

**Files:**
- Modify: `src/localmail/mcp/oauth/clients.py:96-105` (the `cleanup_unused` SQL)
- Test: `tests/test_oauth_clients_store.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_oauth_clients_store.py`:

```python
def test_cleanup_reaps_client_whose_only_token_is_a_tombstone(db_conn):
    # A not-yet-expired *consumed* tombstone must NOT count as a live token and
    # keep an abandoned client alive (the M2 interaction with #183).
    cid = _register(db_conn, client_id="tombstoned")
    clients.touch_last_used(db_conn, cid)
    uid = api_auth.create_user(db_conn, "tomb-user", "pw")
    raw = refresh.mint_refresh(db_conn, client_id=cid, user_id=uid, scopes=[], ttl_s=3600)
    db_conn.commit()
    with db_conn.cursor() as cur:
        from localmail.api.auth import hash_token
        cur.execute("UPDATE oauth_refresh_tokens SET consumed_at = now() "
                    "WHERE token_sha256 = %s", (hash_token(raw),))
    db_conn.commit()
    assert clients.cleanup_unused(db_conn, retention_s=0) == 1
    db_conn.commit()
    assert clients.get_client(db_conn, cid) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/test_oauth_clients_store.py::test_cleanup_reaps_client_whose_only_token_is_a_tombstone --deselect tests/test_daemon_control_socket.py`
Expected: FAIL (returns 0 — tombstone wrongly counts as live).

- [ ] **Step 3: Add the `consumed_at IS NULL` guard**

In `src/localmail/mcp/oauth/clients.py`, change the `NOT EXISTS` subquery inside `cleanup_unused`:

```python
        cur.execute(
            "DELETE FROM oauth_clients c "
            "WHERE COALESCE(c.last_used_at, c.created_at) "
            "      < now() - make_interval(secs => %s) "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM oauth_refresh_tokens r "
            "    WHERE r.client_id = c.client_id AND r.expires_at > now() "
            "      AND r.consumed_at IS NULL)",
            (retention_s,),
        )
```

Also update the docstring's "no live refresh token" sentence to note that
consumed tombstones don't count as live.

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/test_oauth_clients_store.py --deselect tests/test_daemon_control_socket.py`
Expected: PASS (new test + the 3 existing cleanup tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/oauth/clients.py tests/test_oauth_clients_store.py
git commit -m "fix(mcp): cleanup_unused ignores consumed refresh tombstones (#183)"
```

---

## Task 4: provider switches on RotateResult

**Files:**
- Modify: `src/localmail/mcp/oauth/provider.py:210-245` (`_exchange_refresh_sync`)
- Test: `tests/test_oauth_provider.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_oauth_provider.py`:

```python
def test_exchange_refresh_reuse_revokes_family(db_conn, db_pool):
    # Rotate once, then replay the original refresh -> invalid_grant AND the
    # active successor token is dead afterward (RFC 9700 §4.14.2).
    p = _provider(db_pool)
    anyio.run(p.register_client, _client())
    with db_pool.connection() as conn:
        uid = api_auth.create_user(conn, "prov-reuse", "pw")
        raw_code = codes.mint_code(
            conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
            redirect_uri_provided_explicitly=True, code_challenge="chal",
            scopes=[], ttl_s=60,
        )
        conn.commit()
    loaded = anyio.run(p.load_authorization_code, _client(), raw_code)
    token = anyio.run(p.exchange_authorization_code, _client(), loaded)
    old_refresh = anyio.run(p.load_refresh_token, _client(), token.refresh_token)
    rotated = anyio.run(p.exchange_refresh_token, _client(), old_refresh, [])
    # Successor is live...
    assert anyio.run(p.load_refresh_token, _client(), rotated.refresh_token) is not None
    # ...replay the consumed original -> reuse.
    with pytest.raises(TokenError) as exc:
        anyio.run(p.exchange_refresh_token, _client(), old_refresh, [])
    assert exc.value.error == "invalid_grant"
    # ...and the active successor is now revoked too.
    assert anyio.run(p.load_refresh_token, _client(), rotated.refresh_token) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/test_oauth_provider.py::test_exchange_refresh_reuse_revokes_family --deselect tests/test_daemon_control_socket.py`
Expected: FAIL (current code raises before nuking the family / `RotateResult` not handled — likely AttributeError or successor still live).

- [ ] **Step 3: Rewrite `_exchange_refresh_sync`**

Replace the method body in `src/localmail/mcp/oauth/provider.py`. Add a module-level logger near the top of the file if absent (`import logging` + `logger = logging.getLogger("localmail.mcp.oauth")`):

```python
    def _exchange_refresh_sync(
        self, client_id: str | None, rt: RefreshToken
    ) -> OAuthToken:
        assert client_id is not None
        access_raw: str | None = None
        with self._pool.connection() as conn:
            result = refresh.rotate_refresh(
                conn, rt.token, ttl_s=self._cfg.oauth_refresh_token_ttl_s
            )
            if result.outcome == "rotated":
                assert result.new_token is not None
                row = refresh.load_refresh(conn, result.new_token)
                assert row is not None
                access_raw = access.mint_access(
                    conn, user_id=row.user_id, client_id=client_id,
                    ttl_s=self._cfg.oauth_access_token_ttl_s,
                )
                # A refresh is client activity too — keep last_used_at honest so
                # the unused-client cleanup never reaps an active client.
                clients.touch_last_used(conn, client_id)
                conn.commit()
            elif result.outcome == "reuse":
                # The family DELETE must persist; commit before raising.
                conn.commit()
            else:
                conn.rollback()
        # Raise AFTER the connection context exits — TokenError is a frozen
        # dataclass and the contextmanager's __exit__ cannot set __traceback__
        # on it (same constraint as _exchange_code_sync).
        if result.outcome == "reuse":
            logger.warning(
                "refresh-token reuse detected; revoked family for client_id=%s",
                client_id,
            )
            raise TokenError("invalid_grant", "refresh token reuse detected")
        if result.outcome != "rotated":
            raise TokenError("invalid_grant", "refresh token is no longer valid")
        assert access_raw is not None
        return OAuthToken(
            access_token=access_raw,
            token_type="Bearer",
            expires_in=self._cfg.oauth_access_token_ttl_s,
            refresh_token=result.new_token,
        )
```

- [ ] **Step 4: Run the provider tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/test_oauth_provider.py --deselect tests/test_daemon_control_socket.py`
Expected: PASS (new reuse test + existing `test_exchange_refresh_rotates`, `test_exchange_refresh_rejects_disabled_user_without_500`).

- [ ] **Step 5: Run mypy**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/mcp/oauth/provider.py tests/test_oauth_provider.py
git commit -m "feat(mcp): provider revokes refresh family on reuse (#183)"
```

---

## Task 5: Full suite + docs

**Files:**
- Modify: `CLAUDE.md` (the "AS hardening" bullet — add the #183/#185 closure)
- Modify: `README.md` (if it documents OAuth refresh behaviour)
- Modify: `docs/mcp-usage.md` (if it documents refresh rotation)

- [ ] **Step 1: Run the full suite**

Run:
```bash
unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/ --deselect tests/test_daemon_control_socket.py
```
Expected: PASS, count ≈ 1626 baseline + the new tests (~10), 0 failures.

- [ ] **Step 2: Run mypy on the whole package**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: clean (120 files).

- [ ] **Step 3: Update CLAUDE.md**

In the "AS hardening tidy-ups" section, replace the "Still open: #183 … #185" line with a new bullet recording #183/#185 as shipped: family_id + consumed_at tombstone, reuse → family DELETE, `sweep_consumed`, `cleanup_unused` tombstone guard, migration `0029_oauth_refresh_token_family.sql`. Update the "next free slot" line to `0030_*.sql`.

- [ ] **Step 4: Update README.md / docs/mcp-usage.md if needed**

Check whether either documents refresh-token rotation; if so, add a sentence that reuse of a rotated refresh token revokes the whole family. If neither mentions it, skip (note the skip).

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md README.md docs/mcp-usage.md
git commit -m "docs: record refresh-token family revocation shipped (#183, #185)"
```

---

## Self-review notes (author)

- **Spec coverage:** schema (Task 1), store mint/load/rotate/sweep + RotateResult (Task 2), cleanup_unused fix (Task 3), provider switch + WARNING (Task 4), tests across all four, docs (Task 5). #185 index + EXPLAIN acceptance in Task 1. All spec sections mapped.
- **Type consistency:** `RotateResult(outcome, new_token)`, `RefreshRow.family_id`, `mint_refresh(..., family_id=None)`, `sweep_consumed`, `_raw_state`, `_delete_family` used identically across tasks and tests.
- **Contract change:** `rotate_refresh` now returns `RotateResult`, not `str | None`. The only caller is `provider._exchange_refresh_sync` (Task 4); the existing store tests asserting the old contract are explicitly rewritten in Task 2.
- **Migration bookkeeping:** `0029_oauth_refresh_token_family.sql`; next free slot becomes `0030_*.sql` (updated in CLAUDE.md, Task 5).
