# Postgres-backed login rate limiter — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the in-memory per-username + global login rate limiter with a Postgres-backed implementation that survives `uvicorn --workers N`, adds a per-IP cap, and lifts every threshold into TOML config.

**Architecture:** One new append-only table `api_login_attempts(id, ts, ip, username, outcome)`. A single SELECT with three `FILTER (...)` aggregates evaluates global / per-IP / per-user caps; a single INSERT records the outcome. Periodic cleanup sweep gated by a Postgres advisory lock keeps the table bounded across workers. All thresholds/windows/retention/cleanup-interval move to `LocalmailConfig.auth`.

**Tech Stack:** psycopg v3 (raw SQL, no ORM), pydantic v2 (config), pytest (TDD), Postgres advisory locks (`pg_try_advisory_xact_lock`), FastAPI `Request.client.host` (route layer).

**Spec:** [2026-05-20-login-rate-limiter-postgres-design.md](../specs/2026-05-20-login-rate-limiter-postgres-design.md)

---

## File map

**Create:**
- `migrations/0019_api_login_attempts.sql` — new table + 3 indexes.
- `tests/test_api_auth_rate_limiter.py` — new test file covering the DB-backed limiter end-to-end.

**Modify:**
- `src/localmail/api/auth.py` — gut in-memory state, replace with DB-backed helpers + cleanup sweep. Add `client_ip` kwarg to `login()`. Repurpose `reset_login_rate_limiter()` to `TRUNCATE`.
- `src/localmail/api/errors.py` — extend `RateLimited` so the route layer can derive `cap` + `retry_after_s` from the raised exception.
- `src/localmail/config.py` — new `AuthConfig` pydantic model wired into `Config`.
- `src/localmail/serve/routes/auth.py` — pass `client_ip` from `request.client.host`; surface 429 with `Retry-After` + cap-aware problem detail.
- `src/localmail/serve/middleware.py` (if exception handler maps `APIError` → `problem+json`) — include `cap` and `retry_after_s` in the response body and `Retry-After` header on `RateLimited`.
- `tests/conftest.py` — add `api_login_attempts` to the TRUNCATE list in the `db_conn` fixture.
- `tests/test_api_auth_ratelimit.py` — port to the new config-driven constants / new internal helpers (rename to `test_api_auth_rate_limiter.py` and merge with the new tests, or keep both — see Task 9).
- `CLAUDE.md` — new "Rate-limiting model" block.
- `README.md` — new `[auth]` config section + proxy gotcha note.

**Untouched but worth knowing:**
- `tests/conftest.py` `api_user` fixture calls `reset_login_rate_limiter()` — that still works after the repurpose (TRUNCATE), so the fixture is unchanged.
- Every other test file that imports `reset_login_rate_limiter` from `localmail.api.auth` (per `grep -rn` from session: `test_api_auth_passwords.py`, `test_serve_auth_routes.py`, `test_serve_acl_routes.py`, `test_api_auth_service.py`, `test_e2e_serve.py`) keeps working because the symbol survives — only its body changes.

---

## Convention reminders

- **psycopg v3 + raw SQL.** No ORM, no f-string SQL — always parametrise. `conn.execute(...)` returns a cursor; prefer `with conn.cursor() as cur` so the cursor is closed.
- **One migration file per change.** Latest applied is `0018_messages_date_received_internaldate.sql`; this PR adds `0019_*.sql`. Never edit a committed migration.
- **`unset VIRTUAL_ENV && uv run ...`** for every `uv` invocation — shell may carry a stale `VIRTUAL_ENV` from pyenv.
- **`LOCALMAIL_TEST_DSN`** defaults to `postgresql:///localmail_test`. Tests never touch the live `localmail` DB.
- **`assert row is not None`** before `row[0]` — mypy is enabled and will reject `cur.fetchone()[0]`.
- **No comments unless the WHY is non-obvious.** Don't restate SQL or Python.
- **No magic numbers** — every threshold/window comes from `AuthConfig`.
- **Tests first.** Each task writes the failing test before the implementation.

---

## Task 1: Migration 0019 — `api_login_attempts` table

**Files:**
- Create: `migrations/0019_api_login_attempts.sql`
- Test: `tests/test_db_migrations.py` (existing — add one assertion if it has one for table presence; otherwise the table being usable from later tests is the implicit assertion)

- [ ] **Step 1: Write the migration**

Create `migrations/0019_api_login_attempts.sql` with the exact contents:

```sql
-- Append-only log of every /v1/auth/login attempt.
-- The three sliding-window caps (global, per-IP, per-user) read indexed
-- COUNTs over this table, so the limits survive uvicorn --workers N and
-- localmail serve restarts. Rows older than auth.login_attempt_retention_s
-- are best-effort deleted by the in-process sweep gated on a PG advisory
-- lock; see localmail.api.auth._sweep_login_attempts.

CREATE TABLE api_login_attempts (
    id          BIGSERIAL    PRIMARY KEY,
    ts          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    ip          TEXT,
    username    TEXT         NOT NULL,
    outcome     TEXT         NOT NULL
                             CHECK (outcome IN ('success','failure'))
);

CREATE INDEX api_login_attempts_ts_idx
    ON api_login_attempts (ts DESC);

CREATE INDEX api_login_attempts_ip_ts_idx
    ON api_login_attempts (ip, ts DESC)
    WHERE ip IS NOT NULL;

CREATE INDEX api_login_attempts_user_ts_idx
    ON api_login_attempts (username, ts DESC);
```

- [ ] **Step 2: Apply on the test DB**

Run:
```bash
unset VIRTUAL_ENV && uv run localmail --config /dev/null init-db 2>&1 | tail -5
```

Expected: a line like `applying 0019_api_login_attempts.sql` followed by no error. (If `/dev/null` doesn't work as a config path, use a one-line TOML pointing at the test DSN — but `init-db` does not require accounts to be configured, so `/dev/null` typically suffices.)

If it fails because no config is loadable, instead run psql directly:
```bash
psql "$LOCALMAIL_TEST_DSN" -f migrations/0019_api_login_attempts.sql
```

- [ ] **Step 3: Sanity-check the schema**

Run:
```bash
psql "$LOCALMAIL_TEST_DSN" -c "\d api_login_attempts"
```

Expected output includes the four columns (`id`, `ts`, `ip`, `username`, `outcome`) and three indexes named `api_login_attempts_ts_idx`, `api_login_attempts_ip_ts_idx`, `api_login_attempts_user_ts_idx`.

- [ ] **Step 4: Idempotency check**

Re-running migrations must not error (the migration runner tracks applied versions in `schema_migrations`). Run:
```bash
unset VIRTUAL_ENV && uv run pytest tests/test_db.py -q 2>&1 | tail -5
```

If `test_db.py` exists and has a "re-apply is idempotent" assertion, it will pass. Otherwise spot-check by hand: re-running the migration via the runner is a no-op because the version is recorded in `schema_migrations`.

- [ ] **Step 5: Commit**

```bash
git add migrations/0019_api_login_attempts.sql
git commit -m "feat(migrations): add api_login_attempts table (#7)"
```

---

## Task 2: Add `api_login_attempts` to conftest TRUNCATE

**Files:**
- Modify: `tests/conftest.py` (the `db_conn` fixture's TRUNCATE list)

- [ ] **Step 1: Find the existing TRUNCATE**

In `tests/conftest.py`, locate the `db_conn` fixture (`@pytest.fixture` named `db_conn`). It calls `TRUNCATE accounts, mailboxes, messages, ..., user_accounts RESTART IDENTITY CASCADE`.

- [ ] **Step 2: Add `api_login_attempts` to the list**

Edit the TRUNCATE statement so the table list reads:

```python
"TRUNCATE accounts, mailboxes, messages, message_labels, "
"attachment_blobs, failed_messages, message_chunks, "
"failed_embeddings, embedding_models, failed_chunkings, "
"attachment_text, attachment_chunks, failed_extractions, "
"api_users, api_tokens, user_accounts, api_login_attempts "
"RESTART IDENTITY CASCADE"
```

- [ ] **Step 3: Verify the test suite still loads**

Run:
```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_auth_passwords.py -q 2>&1 | tail -5
```

Expected: all tests pass (or skip if no DB). The TRUNCATE change should be a no-op until later tasks insert rows into the new table.

- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py
git commit -m "test(conftest): truncate api_login_attempts between tests (#7)"
```

---

## Task 3: `AuthConfig` pydantic model

**Files:**
- Modify: `src/localmail/config.py`
- Modify: `config.example.toml`
- Test: `tests/test_config.py` (if it exists — otherwise the round-trip is implicit through the live tests)

- [ ] **Step 1: Write the failing test for defaults**

If `tests/test_config.py` exists, add this test. Otherwise create the file with:

```python
"""Defaults + round-trip for LocalmailConfig.auth."""
from __future__ import annotations

import io
import tomllib  # py311+; project requires py312
from pathlib import Path

import pytest

from localmail.config import AuthConfig, Config, load_config


def test_auth_config_defaults_preserve_pre_pg_thresholds() -> None:
    cfg = AuthConfig()
    assert cfg.login_per_user_max == 5
    assert cfg.login_per_user_window_s == 60
    assert cfg.login_per_ip_max == 20
    assert cfg.login_per_ip_window_s == 60
    assert cfg.login_global_max == 30
    assert cfg.login_global_window_s == 60
    assert cfg.login_attempt_retention_s == 86400
    assert cfg.login_cleanup_interval_s == 300


def test_auth_config_round_trip_from_toml(tmp_path: Path) -> None:
    toml_text = """
[database]
dsn = "postgresql:///localmail_test"

[auth]
login_per_user_max = 3
login_per_ip_max = 7
login_global_max = 11
login_attempt_retention_s = 3600
"""
    p = tmp_path / "config.toml"
    p.write_text(toml_text)
    cfg = load_config(p)
    assert cfg.auth.login_per_user_max == 3
    assert cfg.auth.login_per_ip_max == 7
    assert cfg.auth.login_global_max == 11
    assert cfg.auth.login_attempt_retention_s == 3600
    # Defaults fill in the rest.
    assert cfg.auth.login_per_user_window_s == 60
```

- [ ] **Step 2: Run, confirm failure**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_config.py -q 2>&1 | tail -10
```

Expected: ImportError (no `AuthConfig`) or AttributeError (`Config` has no `auth`).

- [ ] **Step 3: Add `AuthConfig` to `config.py`**

In `src/localmail/config.py`, after `ServeConfig` (around line 46), add:

```python
class AuthConfig(BaseModel):
    """Tunables for the login rate limiter (Postgres-backed)."""

    login_per_user_max: int = 5
    login_per_user_window_s: int = 60

    login_per_ip_max: int = 20
    login_per_ip_window_s: int = 60

    login_global_max: int = 30
    login_global_window_s: int = 60

    # Best-effort retention: rows older than this are deleted by the
    # in-process sweep. Independent of the sliding-window caps above —
    # raise to keep audit history further back without affecting limits.
    login_attempt_retention_s: int = 86400

    # Per-worker cadence for the sweep. Gated by a PG advisory lock so
    # concurrent workers don't pile up DELETEs.
    login_cleanup_interval_s: int = 300
```

And add it to the `Config` model (around line 221):

```python
class Config(BaseModel):
    database: DatabaseConfig
    attachments: AttachmentsConfig = AttachmentsConfig()
    daemon: DaemonConfig = DaemonConfig()
    serve: ServeConfig = Field(default_factory=ServeConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    gmail_oauth: GmailOAuthConfig | None = None
    accounts: list[AccountConfig] = Field(default_factory=list)
    search: SearchConfig = Field(default_factory=SearchConfig)
```

- [ ] **Step 4: Update `config.example.toml`**

Add a section (after `[serve]`):

```toml
[auth]
# Login rate-limit thresholds (all Postgres-backed; survive
# uvicorn --workers N and serve restarts).
login_per_user_max = 5
login_per_user_window_s = 60
login_per_ip_max = 20
login_per_ip_window_s = 60
login_global_max = 30
login_global_window_s = 60
login_attempt_retention_s = 86400  # 24h
login_cleanup_interval_s = 300     # 5m
```

- [ ] **Step 5: Run, confirm pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_config.py -q 2>&1 | tail -5
```

Expected: both tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/config.py config.example.toml tests/test_config.py
git commit -m "feat(config): AuthConfig for login rate-limit tunables (#7)"
```

---

## Task 4: Extend `RateLimited` with `cap` + `retry_after_s`

**Files:**
- Modify: `src/localmail/api/errors.py`
- Test: `tests/test_api_errors.py` (create or extend)

- [ ] **Step 1: Write the failing test**

Append (or create `tests/test_api_errors.py`):

```python
"""Behavior of typed API errors."""
from localmail.api.errors import RateLimited


def test_rate_limited_carries_cap_and_retry_after() -> None:
    exc = RateLimited("too many", cap="ip", retry_after_s=42)
    assert exc.detail == "too many"
    assert exc.cap == "ip"
    assert exc.retry_after_s == 42
    payload = exc.to_problem()
    assert payload["status"] == 429
    assert payload["cap"] == "ip"
    assert payload["retry_after_s"] == 42


def test_rate_limited_backwards_compat_without_cap() -> None:
    """Existing call sites raising RateLimited(detail) still work."""
    exc = RateLimited("legacy")
    assert exc.cap is None
    assert exc.retry_after_s is None
    payload = exc.to_problem()
    # When cap is None we omit it from the body to keep the contract clean.
    assert "cap" not in payload
    assert "retry_after_s" not in payload
```

- [ ] **Step 2: Run, confirm failure**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_errors.py -q 2>&1 | tail -10
```

Expected: TypeError on the constructor — `RateLimited.__init__()` got unexpected keyword argument `cap`.

- [ ] **Step 3: Extend `RateLimited`**

In `src/localmail/api/errors.py`, replace the `RateLimited` class with:

```python
class RateLimited(APIError):
    http_status = 429
    problem_type = "/problems/rate-limited"
    title = "Too many requests"

    def __init__(
        self,
        detail: str,
        *,
        cap: str | None = None,
        retry_after_s: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.cap = cap
        self.retry_after_s = retry_after_s

    def to_problem(self) -> dict[str, object]:
        payload = super().to_problem()
        if self.cap is not None:
            payload["cap"] = self.cap
        if self.retry_after_s is not None:
            payload["retry_after_s"] = self.retry_after_s
        return payload
```

- [ ] **Step 4: Run, confirm pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_errors.py -q 2>&1 | tail -5
```

Expected: both tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/errors.py tests/test_api_errors.py
git commit -m "feat(api/errors): RateLimited carries cap + retry_after_s (#7)"
```

---

## Task 5: `_record_login_attempt` — single INSERT helper

**Files:**
- Modify: `src/localmail/api/auth.py` (add helper, do not remove the in-memory code yet)
- Test: `tests/test_api_auth_rate_limiter.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_auth_rate_limiter.py` with:

```python
"""Postgres-backed login rate limiter (#7)."""
from __future__ import annotations

import psycopg
import pytest

from localmail.api import auth as auth_mod


def _count(conn: psycopg.Connection, sql: str, *params) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def test_record_login_attempt_inserts_failure(db_conn: psycopg.Connection) -> None:
    auth_mod._record_login_attempt(db_conn, "alice", "10.0.0.1", "failure")
    db_conn.commit()
    assert _count(db_conn, "SELECT count(*) FROM api_login_attempts") == 1
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT username, ip, outcome FROM api_login_attempts"
        )
        row = cur.fetchone()
        assert row == ("alice", "10.0.0.1", "failure")


def test_record_login_attempt_null_ip(db_conn: psycopg.Connection) -> None:
    auth_mod._record_login_attempt(db_conn, "bob", None, "success")
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("SELECT ip FROM api_login_attempts WHERE username = 'bob'")
        row = cur.fetchone()
        assert row is not None
        assert row[0] is None


def test_record_login_attempt_rejects_bad_outcome(db_conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        auth_mod._record_login_attempt(db_conn, "alice", "1.1.1.1", "garbage")  # type: ignore[arg-type]
    db_conn.rollback()
```

- [ ] **Step 2: Run, confirm failure**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_auth_rate_limiter.py::test_record_login_attempt_inserts_failure -q 2>&1 | tail -10
```

Expected: AttributeError — `module 'localmail.api.auth' has no attribute '_record_login_attempt'`.

- [ ] **Step 3: Implement `_record_login_attempt`**

In `src/localmail/api/auth.py`, near the bottom of the rate-limit helpers (do NOT delete the in-memory functions yet — Task 8 cleans them up), add:

```python
from typing import Literal


def _record_login_attempt(
    conn: psycopg.Connection,
    username: str,
    client_ip: str | None,
    outcome: Literal["success", "failure"],
) -> None:
    """Append a row to api_login_attempts.

    Uses a nested SAVEPOINT so a logging failure (table missing, transient
    error) cannot abort the outer login transaction — the limiter is
    defense-in-depth, never a correctness gate for credential verification.
    """
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO api_login_attempts (username, ip, outcome) "
                    "VALUES (%s, %s, %s)",
                    (username, client_ip, outcome),
                )
    except psycopg.errors.CheckViolation:
        # Bad outcome label — only the internal callers can hit this; surface
        # so tests can verify the constraint. Outer transaction stays open
        # because the SAVEPOINT rolled back.
        raise
    except psycopg.Error:
        # Anything else (table missing during migration race, transient IO)
        # silently fails — better to issue a token without an audit row than
        # to deny a legit login because the audit table is unavailable.
        pass
```

The two-tier exception split is the WHY-comment payload: `CheckViolation` is a programmer bug we want surfaced; transient infra errors should not block authentication.

- [ ] **Step 4: Run, confirm pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_auth_rate_limiter.py -q 2>&1 | tail -10
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/auth.py tests/test_api_auth_rate_limiter.py
git commit -m "feat(auth): _record_login_attempt DB-backed audit row (#7)"
```

---

## Task 6: `_check_login_rate_limits` — single-query check

**Files:**
- Modify: `src/localmail/api/auth.py`
- Test: `tests/test_api_auth_rate_limiter.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_auth_rate_limiter.py`:

```python
from localmail.api.errors import RateLimited
from localmail.config import AuthConfig


def _record_many(
    conn: psycopg.Connection,
    *,
    username: str,
    ip: str | None,
    outcome: str,
    n: int,
) -> None:
    for _ in range(n):
        auth_mod._record_login_attempt(conn, username, ip, outcome)  # type: ignore[arg-type]
    conn.commit()


def test_check_passes_on_empty_table(db_conn: psycopg.Connection) -> None:
    cfg = AuthConfig()
    auth_mod._check_login_rate_limits(db_conn, "alice", "1.1.1.1", cfg=cfg)


def test_check_trips_user_cap(db_conn: psycopg.Connection) -> None:
    cfg = AuthConfig(login_per_user_max=3, login_per_user_window_s=60)
    _record_many(db_conn, username="alice", ip="1.1.1.1", outcome="failure", n=3)
    with pytest.raises(RateLimited) as ei:
        auth_mod._check_login_rate_limits(db_conn, "alice", "1.1.1.1", cfg=cfg)
    assert ei.value.cap == "user"
    assert ei.value.retry_after_s == 60


def test_check_trips_ip_cap_across_usernames(db_conn: psycopg.Connection) -> None:
    """The cross-username brute-force case (#7 motivation)."""
    cfg = AuthConfig(login_per_ip_max=5, login_per_ip_window_s=60)
    for u in ("alice", "bob", "carol", "dave", "eve"):
        auth_mod._record_login_attempt(db_conn, u, "1.1.1.1", "failure")
    db_conn.commit()
    with pytest.raises(RateLimited) as ei:
        auth_mod._check_login_rate_limits(db_conn, "frank", "1.1.1.1", cfg=cfg)
    assert ei.value.cap == "ip"
    assert ei.value.retry_after_s == 60


def test_check_trips_global_cap_including_successes(db_conn: psycopg.Connection) -> None:
    cfg = AuthConfig(login_global_max=4, login_global_window_s=60)
    # Mix successes + failures from different IPs — global counts both.
    auth_mod._record_login_attempt(db_conn, "alice", "1.1.1.1", "success")
    auth_mod._record_login_attempt(db_conn, "bob", "2.2.2.2", "failure")
    auth_mod._record_login_attempt(db_conn, "carol", "3.3.3.3", "success")
    auth_mod._record_login_attempt(db_conn, "dave", "4.4.4.4", "failure")
    db_conn.commit()
    with pytest.raises(RateLimited) as ei:
        auth_mod._check_login_rate_limits(db_conn, "eve", "5.5.5.5", cfg=cfg)
    assert ei.value.cap == "global"


def test_check_user_cap_clears_on_success(db_conn: psycopg.Connection) -> None:
    """A successful login clears the per-user counter (preserves prior semantics)."""
    cfg = AuthConfig(login_per_user_max=3)
    _record_many(db_conn, username="alice", ip="1.1.1.1", outcome="failure", n=2)
    auth_mod._record_login_attempt(db_conn, "alice", "1.1.1.1", "success")
    _record_many(db_conn, username="alice", ip="1.1.1.1", outcome="failure", n=2)
    # Only 2 failures *since* last success — under the cap of 3.
    auth_mod._check_login_rate_limits(db_conn, "alice", "1.1.1.1", cfg=cfg)


def test_check_ip_cap_does_not_clear_on_success(db_conn: psycopg.Connection) -> None:
    """Success from one user does NOT unlock the IP for another user's failures."""
    cfg = AuthConfig(login_per_ip_max=4)
    _record_many(db_conn, username="alice", ip="1.1.1.1", outcome="failure", n=2)
    auth_mod._record_login_attempt(db_conn, "alice", "1.1.1.1", "success")
    _record_many(db_conn, username="bob", ip="1.1.1.1", outcome="failure", n=3)
    with pytest.raises(RateLimited) as ei:
        auth_mod._check_login_rate_limits(db_conn, "carol", "1.1.1.1", cfg=cfg)
    assert ei.value.cap == "ip"


def test_check_window_expires(db_conn: psycopg.Connection) -> None:
    """Failures outside the window do not count."""
    cfg = AuthConfig(login_per_user_max=2, login_per_user_window_s=1)
    _record_many(db_conn, username="alice", ip="1.1.1.1", outcome="failure", n=2)
    # Bump those rows' ts into the past via SQL.
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE api_login_attempts SET ts = now() - interval '10 seconds'"
        )
    db_conn.commit()
    auth_mod._check_login_rate_limits(db_conn, "alice", "1.1.1.1", cfg=cfg)


def test_check_null_ip_does_not_contribute_to_ip_cap(db_conn: psycopg.Connection) -> None:
    cfg = AuthConfig(login_per_ip_max=2)
    for _ in range(5):
        auth_mod._record_login_attempt(db_conn, "alice", None, "failure")
    db_conn.commit()
    # No IP context → no IP cap evaluated for this call site.
    auth_mod._check_login_rate_limits(db_conn, "alice", None, cfg=cfg)


def test_check_order_global_first(db_conn: psycopg.Connection) -> None:
    """Global cap is checked first so RateLimited.cap == 'global' even when
    per-IP and per-user are also over."""
    cfg = AuthConfig(
        login_per_user_max=1,
        login_per_ip_max=1,
        login_global_max=1,
    )
    auth_mod._record_login_attempt(db_conn, "alice", "1.1.1.1", "failure")
    db_conn.commit()
    with pytest.raises(RateLimited) as ei:
        auth_mod._check_login_rate_limits(db_conn, "alice", "1.1.1.1", cfg=cfg)
    assert ei.value.cap == "global"
```

- [ ] **Step 2: Run, confirm failure**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_auth_rate_limiter.py -q 2>&1 | tail -10
```

Expected: AttributeError — `_check_login_rate_limits` not defined.

- [ ] **Step 3: Implement `_check_login_rate_limits`**

In `src/localmail/api/auth.py` (still alongside the old in-memory helpers; Task 8 removes those):

```python
from localmail.config import AuthConfig


def _check_login_rate_limits(
    conn: psycopg.Connection,
    username: str,
    client_ip: str | None,
    *,
    cfg: AuthConfig,
) -> None:
    """Evaluate global / per-IP / per-user caps in one round trip.

    Order is global → per-IP → per-user so a hit on the broader cap wins
    the cap label — telling the caller which knob to bump is more useful
    than reporting whichever cap was tripped first by SQL evaluation.
    """
    widest_window_s = max(
        cfg.login_global_window_s,
        cfg.login_per_ip_window_s,
        cfg.login_per_user_window_s,
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (
                WHERE ts > now() - make_interval(secs => %s)
              ) AS global_attempts,
              COUNT(*) FILTER (
                WHERE ip = %s
                  AND outcome = 'failure'
                  AND ts > now() - make_interval(secs => %s)
              ) AS ip_failures,
              COUNT(*) FILTER (
                WHERE username = %s
                  AND outcome = 'failure'
                  AND ts > now() - make_interval(secs => %s)
                  AND ts > COALESCE(
                    (SELECT MAX(ts) FROM api_login_attempts
                      WHERE username = %s AND outcome = 'success'),
                    '-infinity'::timestamptz
                  )
              ) AS user_failures
            FROM api_login_attempts
            WHERE ts > now() - make_interval(secs => %s)
            """,
            (
                cfg.login_global_window_s,
                client_ip, cfg.login_per_ip_window_s,
                username, cfg.login_per_user_window_s, username,
                widest_window_s,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        global_attempts, ip_failures, user_failures = row

    if global_attempts >= cfg.login_global_max:
        raise RateLimited(
            f"server-wide login rate limit exceeded "
            f"({cfg.login_global_max} attempts per {cfg.login_global_window_s}s)",
            cap="global",
            retry_after_s=cfg.login_global_window_s,
        )
    if client_ip is not None and ip_failures >= cfg.login_per_ip_max:
        raise RateLimited(
            f"too many failed logins from this IP "
            f"({cfg.login_per_ip_max} per {cfg.login_per_ip_window_s}s)",
            cap="ip",
            retry_after_s=cfg.login_per_ip_window_s,
        )
    if user_failures >= cfg.login_per_user_max:
        raise RateLimited(
            f"too many failed login attempts; try again in "
            f"{cfg.login_per_user_window_s} seconds",
            cap="user",
            retry_after_s=cfg.login_per_user_window_s,
        )
```

Make sure `from localmail.api.errors import RateLimited, ...` is in the imports (it already is — Task 5 added nothing new there).

- [ ] **Step 4: Run, confirm pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_auth_rate_limiter.py -q 2>&1 | tail -10
```

Expected: all 9 tests in the file pass.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/auth.py tests/test_api_auth_rate_limiter.py
git commit -m "feat(auth): _check_login_rate_limits single-query check (#7)"
```

---

## Task 7: Cleanup sweep with advisory lock

**Files:**
- Modify: `src/localmail/api/auth.py`
- Test: `tests/test_api_auth_rate_limiter.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_auth_rate_limiter.py`:

```python
def test_sweep_deletes_expired_rows(db_conn: psycopg.Connection) -> None:
    # Two recent rows, three old rows.
    for u in ("alice", "bob"):
        auth_mod._record_login_attempt(db_conn, u, "1.1.1.1", "failure")
    for u in ("carol", "dave", "eve"):
        auth_mod._record_login_attempt(db_conn, u, "2.2.2.2", "failure")
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE api_login_attempts SET ts = now() - interval '1 day' "
            "WHERE username IN ('carol','dave','eve')"
        )
    db_conn.commit()
    deleted = auth_mod._sweep_login_attempts(db_conn, retention_s=60)
    db_conn.commit()
    assert deleted == 3
    assert _count(db_conn, "SELECT count(*) FROM api_login_attempts") == 2


def test_sweep_no_op_when_lock_contended(db_conn: psycopg.Connection, db_dsn: str) -> None:
    """Second worker can't pile up DELETEs while another holds the lock."""
    auth_mod._record_login_attempt(db_conn, "alice", "1.1.1.1", "failure")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE api_login_attempts SET ts = now() - interval '1 day'")
    db_conn.commit()

    other = psycopg.connect(db_dsn, autocommit=False)
    try:
        # Acquire the sweep advisory lock on `other` for the whole test.
        with other.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (auth_mod._SWEEP_ADVISORY_LOCK_KEY,))
        # `db_conn` tries to sweep — must short-circuit.
        deleted = auth_mod._sweep_login_attempts(db_conn, retention_s=60)
        assert deleted == 0  # lock not acquired → did not run
    finally:
        with other.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (auth_mod._SWEEP_ADVISORY_LOCK_KEY,))
        other.close()
```

- [ ] **Step 2: Run, confirm failure**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_auth_rate_limiter.py::test_sweep_deletes_expired_rows -q 2>&1 | tail -10
```

Expected: AttributeError — `_sweep_login_attempts` not defined.

- [ ] **Step 3: Implement the sweep**

In `src/localmail/api/auth.py`, add:

```python
# Stable advisory-lock key for the cleanup sweep. Any nonzero int64 works;
# choose a fixed value so all workers in the cluster contend on the same
# lock. The number itself is arbitrary — chosen for "localmail" mnemonic.
_SWEEP_ADVISORY_LOCK_KEY = 0x6C_6F_63_61_6C_6D_61_69  # "localmai" in ASCII


def _sweep_login_attempts(
    conn: psycopg.Connection,
    *,
    retention_s: int,
) -> int:
    """Best-effort DELETE of expired rows. Returns deleted row count.

    Gated by ``pg_try_advisory_lock`` so concurrent workers don't pile up
    parallel DELETEs. Returns 0 if the lock is held by another worker —
    not an error; the next worker around will get to it.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_lock(%s)", (_SWEEP_ADVISORY_LOCK_KEY,)
        )
        row = cur.fetchone()
        assert row is not None
        if not row[0]:
            return 0
        try:
            cur.execute(
                "DELETE FROM api_login_attempts "
                "WHERE ts < now() - make_interval(secs => %s)",
                (retention_s,),
            )
            return cur.rowcount
        finally:
            cur.execute(
                "SELECT pg_advisory_unlock(%s)", (_SWEEP_ADVISORY_LOCK_KEY,)
            )
```

- [ ] **Step 4: Run, confirm pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_auth_rate_limiter.py -q 2>&1 | tail -10
```

Expected: all tests in the file pass (now 11).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/auth.py tests/test_api_auth_rate_limiter.py
git commit -m "feat(auth): cleanup sweep with PG advisory lock (#7)"
```

---

## Task 8: Rewire `login()` and retire the in-memory limiter

**Files:**
- Modify: `src/localmail/api/auth.py` (remove old in-memory helpers, repurpose `reset_login_rate_limiter`, wire `login()` to new helpers, accept `client_ip` kwarg)
- Modify: `tests/test_api_auth_ratelimit.py` (port the old tests to the new constants/config)

- [ ] **Step 1: Inventory call sites of the old limiter**

The session grep already enumerated them. Confirm one more time:

```bash
grep -rn "LOGIN_MAX_FAILURES\|LOGIN_LOCKOUT_SECONDS\|LOGIN_GLOBAL_MAX_PER_WINDOW\|LOGIN_GLOBAL_WINDOW_SECONDS\|LOGIN_FAILURES_MAX_USERS\|_check_login_rate_limit\b\|_record_login_failure\|_clear_login_failures\|_check_login_global_rate_limit\|_sweep_login_failures_locked\|_LOGIN_FAILURES\b\|_LOGIN_GLOBAL_ATTEMPTS" src tests
```

Only one test file references these directly: `tests/test_api_auth_ratelimit.py`. Every other test imports the public `reset_login_rate_limiter` (still surviving — see Step 4) and `login` / `create_user`. No code outside `auth.py` reads the in-memory state.

- [ ] **Step 2: Write the failing tests for the new `login()` signature + behaviour**

Append to `tests/test_api_auth_rate_limiter.py`:

```python
from localmail.api.auth import create_user, login, reset_login_rate_limiter
from localmail.api.errors import AuthenticationFailed


@pytest.fixture(autouse=True)
def _truncate_attempts(db_conn: psycopg.Connection) -> None:
    """Tests in this file rely on a clean attempts table."""
    reset_login_rate_limiter(db_conn)
    db_conn.commit()


def test_login_records_failure_with_ip(db_conn: psycopg.Connection) -> None:
    create_user(db_conn, "alice", "hunter2")
    db_conn.commit()
    with pytest.raises(AuthenticationFailed):
        login(db_conn, "alice", "wrong", client_ip="9.9.9.9")
    db_conn.commit()
    assert _count(
        db_conn,
        "SELECT count(*) FROM api_login_attempts "
        "WHERE username = 'alice' AND ip = '9.9.9.9' AND outcome = 'failure'",
    ) == 1


def test_login_records_success_with_ip(db_conn: psycopg.Connection) -> None:
    create_user(db_conn, "alice", "hunter2")
    db_conn.commit()
    tok, _exp = login(db_conn, "alice", "hunter2", client_ip="9.9.9.9")
    db_conn.commit()
    assert tok
    assert _count(
        db_conn,
        "SELECT count(*) FROM api_login_attempts "
        "WHERE username = 'alice' AND outcome = 'success'",
    ) == 1


def test_login_records_unknown_user_failure(db_conn: psycopg.Connection) -> None:
    """An attempt against a non-existent username still lands a row so the
    per-IP cap closes the enumeration vector."""
    with pytest.raises(AuthenticationFailed):
        login(db_conn, "ghost", "anything", client_ip="9.9.9.9")
    db_conn.commit()
    assert _count(
        db_conn,
        "SELECT count(*) FROM api_login_attempts "
        "WHERE username = 'ghost' AND ip = '9.9.9.9' AND outcome = 'failure'",
    ) == 1


def test_login_two_connections_see_each_others_failures(
    db_conn: psycopg.Connection, db_dsn: str,
) -> None:
    """Multi-worker semantics — two distinct connections share state via PG."""
    create_user(db_conn, "alice", "hunter2")
    db_conn.commit()
    # Drive failures on connection A.
    for _ in range(4):
        with pytest.raises(AuthenticationFailed):
            login(db_conn, "alice", "wrong", client_ip="9.9.9.9")
    db_conn.commit()

    # Connection B sees them and trips the per-user cap on attempt 5.
    other = psycopg.connect(db_dsn, autocommit=False)
    try:
        with pytest.raises(AuthenticationFailed):
            login(other, "alice", "wrong", client_ip="9.9.9.9")
        other.commit()
        with pytest.raises(RateLimited):
            login(other, "alice", "wrong", client_ip="9.9.9.9")
    finally:
        other.close()


def test_reset_login_rate_limiter_truncates(db_conn: psycopg.Connection) -> None:
    auth_mod._record_login_attempt(db_conn, "alice", "1.1.1.1", "failure")
    db_conn.commit()
    assert _count(db_conn, "SELECT count(*) FROM api_login_attempts") == 1
    reset_login_rate_limiter(db_conn)
    db_conn.commit()
    assert _count(db_conn, "SELECT count(*) FROM api_login_attempts") == 0
```

Note `reset_login_rate_limiter(db_conn)` now takes a connection argument — the signature changes. Old call sites without a `db_conn` break; Step 5 handles those.

- [ ] **Step 3: Run, confirm failure**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_auth_rate_limiter.py -q 2>&1 | tail -20
```

Expected: failures on the new tests — `login()` doesn't accept `client_ip`, `reset_login_rate_limiter()` is the no-arg in-memory version.

- [ ] **Step 4: Rewrite `auth.py` rate-limit surface**

In `src/localmail/api/auth.py`:

1. **Delete** the entire in-memory block:
   - `LOGIN_MAX_FAILURES`, `LOGIN_LOCKOUT_SECONDS`, `LOGIN_GLOBAL_MAX_PER_WINDOW`, `LOGIN_GLOBAL_WINDOW_SECONDS`, `LOGIN_FAILURES_MAX_USERS` constants.
   - `_LOGIN_FAILURES_LOCK`, `_LOGIN_FAILURES`, `_LOGIN_GLOBAL_LOCK`, `_LOGIN_GLOBAL_ATTEMPTS` globals.
   - `_check_login_global_rate_limit`, `_sweep_login_failures_locked`, `_check_login_rate_limit`, `_record_login_failure`, `_clear_login_failures` functions.
   - The `import threading`, `from collections import OrderedDict`, `import time as _time` imports if nothing else uses them (search the file).

2. **Repurpose** `reset_login_rate_limiter`:

```python
def reset_login_rate_limiter(conn: psycopg.Connection) -> None:
    """Truncate api_login_attempts. Test-only helper.

    Takes an explicit connection because in production we never wipe the
    audit trail; only test fixtures want a fast reset between cases.
    Caller commits.
    """
    with conn.cursor() as cur:
        cur.execute("TRUNCATE api_login_attempts RESTART IDENTITY")
```

3. **Rewire** `login()` to use the new helpers:

```python
import time as _monotonic_time

# Per-worker last-sweep timestamp so cleanup runs at most once per
# AuthConfig.login_cleanup_interval_s wall-clock per process. The PG
# advisory lock in _sweep_login_attempts further dedupes across workers.
_LAST_SWEEP_AT_MONOTONIC: float = 0.0


def _maybe_sweep(conn: psycopg.Connection, cfg: AuthConfig) -> None:
    """Run the cleanup sweep if it's been ≥ cfg.login_cleanup_interval_s
    since this worker's last sweep."""
    global _LAST_SWEEP_AT_MONOTONIC
    now = _monotonic_time.monotonic()
    if now - _LAST_SWEEP_AT_MONOTONIC < cfg.login_cleanup_interval_s:
        return
    _LAST_SWEEP_AT_MONOTONIC = now
    _sweep_login_attempts(conn, retention_s=cfg.login_attempt_retention_s)


def login(
    conn: psycopg.Connection,
    username: str,
    password: str,
    *,
    client_ip: str | None = None,
    cfg: AuthConfig | None = None,
) -> tuple[str, datetime]:
    """Verify credentials and mint a token.

    Raises:
      RateLimited (with .cap and .retry_after_s) if any cap is exceeded.
      AuthenticationFailed for bad credentials or disabled users.

    ``cfg`` defaults to ``AuthConfig()`` so test call sites that don't
    care about thresholds still work. Production callers should pass the
    loaded ``LocalmailConfig.auth``.
    """
    if cfg is None:
        cfg = AuthConfig()
    _check_login_rate_limits(conn, username, client_ip, cfg=cfg)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, password_hash FROM api_users "
            "WHERE username = %s AND disabled_at IS NULL",
            (username,),
        )
        row = cur.fetchone()
    if row is None:
        verify_password(password, _DUMMY_PASSWORD_HASH)
        _record_login_attempt(conn, username, client_ip, "failure")
        _maybe_sweep(conn, cfg)
        raise AuthenticationFailed("invalid username or password")
    if not verify_password(password, row[1]):
        _record_login_attempt(conn, username, client_ip, "failure")
        _maybe_sweep(conn, cfg)
        raise AuthenticationFailed("invalid username or password")
    _record_login_attempt(conn, username, client_ip, "success")
    _maybe_sweep(conn, cfg)
    return issue_token(conn, row[0])
```

4. **Add a module-level test hook** to reset the in-process sweep timestamp (some tests need this — see Step 6):

```python
def _reset_sweep_clock_for_tests() -> None:
    """Test-only: clear the per-worker last-sweep timestamp."""
    global _LAST_SWEEP_AT_MONOTONIC
    _LAST_SWEEP_AT_MONOTONIC = 0.0
```

- [ ] **Step 5: Update every test file that calls `reset_login_rate_limiter()` with no args**

Find them:

```bash
grep -rn "reset_login_rate_limiter()" tests
```

For each occurrence:
- If the surrounding scope has a `db_conn` fixture, change to `reset_login_rate_limiter(db_conn)` and follow with `db_conn.commit()` if the next assertion reads the table.
- If the surrounding scope does NOT have a DB connection (e.g. a fixture that runs before `db_conn` is available), drop the call entirely — the conftest `db_conn` fixture already TRUNCATEs `api_login_attempts` between tests (Task 2), so the explicit reset is redundant.

Expected files to touch: `tests/test_api_auth_passwords.py`, `tests/test_serve_auth_routes.py`, `tests/test_serve_acl_routes.py`, `tests/test_api_auth_service.py`, `tests/test_e2e_serve.py`, `tests/conftest.py` (the `api_user` fixture).

For the `api_user` fixture in `tests/conftest.py`, change:

```python
@pytest.fixture
def api_user(db_conn):
    from localmail.api.auth import create_user, reset_login_rate_limiter
    reset_login_rate_limiter(db_conn)
    ...
```

(`db_conn` is already TRUNCATEing the table, but the explicit reset documents intent and is harmless.)

- [ ] **Step 6: Port `tests/test_api_auth_ratelimit.py` to the new API**

Replace the file's contents. The old tests had value (per-user cap, no-leak across usernames, success resets count, LRU bound — now obsolete, global cap) — keep the first three, drop the LRU-bound test (no longer relevant — the table is bounded by retention sweep, not LRU), and update the global cap test:

```python
"""Port of the original per-username + global rate-limit tests to the
Postgres-backed limiter. Multi-worker / per-IP semantics live in
test_api_auth_rate_limiter.py."""
import psycopg
import pytest

from localmail.api import auth as auth_mod
from localmail.api.auth import create_user, login, reset_login_rate_limiter
from localmail.api.errors import AuthenticationFailed, RateLimited
from localmail.config import AuthConfig


@pytest.fixture(autouse=True)
def _reset(db_conn: psycopg.Connection):
    reset_login_rate_limiter(db_conn)
    db_conn.commit()
    yield
    reset_login_rate_limiter(db_conn)
    db_conn.commit()


def test_login_rate_limited_after_max_failures(db_conn: psycopg.Connection) -> None:
    cfg = AuthConfig(login_per_user_max=5)
    create_user(db_conn, "alice", "hunter2")
    db_conn.commit()
    for _ in range(cfg.login_per_user_max):
        with pytest.raises(AuthenticationFailed):
            login(db_conn, "alice", "wrong", cfg=cfg)
        db_conn.commit()
    with pytest.raises(RateLimited):
        login(db_conn, "alice", "wrong", cfg=cfg)


def test_rate_limit_does_not_leak_across_usernames(db_conn: psycopg.Connection) -> None:
    cfg = AuthConfig(login_per_user_max=5, login_per_ip_max=100, login_global_max=100)
    create_user(db_conn, "alice", "hunter2")
    create_user(db_conn, "bob", "correct horse")
    db_conn.commit()
    for _ in range(cfg.login_per_user_max):
        with pytest.raises(AuthenticationFailed):
            login(db_conn, "alice", "wrong", cfg=cfg)
        db_conn.commit()
    token, _ = login(db_conn, "bob", "correct horse", cfg=cfg)
    db_conn.commit()
    assert token


def test_successful_login_resets_user_failure_count(db_conn: psycopg.Connection) -> None:
    cfg = AuthConfig(login_per_user_max=5)
    create_user(db_conn, "alice", "hunter2")
    db_conn.commit()
    for _ in range(cfg.login_per_user_max - 1):
        with pytest.raises(AuthenticationFailed):
            login(db_conn, "alice", "wrong", cfg=cfg)
        db_conn.commit()
    token, _ = login(db_conn, "alice", "hunter2", cfg=cfg)
    db_conn.commit()
    assert token
    with pytest.raises(AuthenticationFailed):
        login(db_conn, "alice", "wrong", cfg=cfg)


def test_global_login_rate_limit_caps_all_usernames(db_conn: psycopg.Connection) -> None:
    """Global limiter bounds argon2 CPU work no matter which username is tried."""
    cfg = AuthConfig(login_global_max=3, login_per_ip_max=100)
    create_user(db_conn, "alice", "hunter2")
    db_conn.commit()
    for u in ("alice", "bob", "charlie"):
        with pytest.raises(AuthenticationFailed):
            login(db_conn, u, "wrong", cfg=cfg)
        db_conn.commit()
    with pytest.raises(RateLimited) as ei:
        login(db_conn, "dave", "wrong", cfg=cfg)
    assert ei.value.cap == "global"
```

- [ ] **Step 7: Run, confirm pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_auth_rate_limiter.py tests/test_api_auth_ratelimit.py tests/test_api_auth_passwords.py tests/test_api_auth_service.py tests/test_serve_auth_routes.py tests/test_serve_acl_routes.py -q 2>&1 | tail -20
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/localmail/api/auth.py tests/
git commit -m "feat(auth): replace in-memory limiter with DB-backed flow (#7)

Drops the per-username dict + global list + their locks. login() now
takes a client_ip kwarg and an optional cfg, records every attempt to
api_login_attempts, and runs an opportunistic sweep gated by a PG
advisory lock so the table stays bounded across workers.

reset_login_rate_limiter() now takes a connection and TRUNCATEs."
```

---

## Task 9: Route layer — extract IP + 429 contract

**Files:**
- Modify: `src/localmail/serve/routes/auth.py`
- Modify: `src/localmail/serve/middleware.py` (the `APIError` → `problem+json` exception handler — add `Retry-After` header on `RateLimited`)
- Test: `tests/test_serve_auth_routes.py` (extend)

- [ ] **Step 1: Inspect existing middleware**

```bash
sed -n '1,80p' src/localmail/serve/middleware.py
```

Find the exception handler that turns `APIError` into the problem+json response. The handler probably uses `JSONResponse(content=exc.to_problem(), status_code=exc.http_status)`. We need to attach a `Retry-After` header on `RateLimited`.

- [ ] **Step 2: Write the failing route test**

Append to `tests/test_serve_auth_routes.py`:

```python
def test_login_429_carries_retry_after_and_cap(client, api_user, db_conn):
    """Per-IP cap with a tight threshold trips and exposes cap='ip'."""
    # Drive failures via direct service to set the table state, then hit
    # the route once to exercise the 429 path.
    from localmail.api.auth import _record_login_attempt
    from localmail.config import AuthConfig

    # Six failures from one IP across distinct usernames.
    for u in ("a", "b", "c", "d", "e", "f"):
        _record_login_attempt(db_conn, u, "9.9.9.9", "failure")
    db_conn.commit()

    # Patch the loaded config so the per-IP cap is 5/60s for the duration
    # of this test (the rest of the suite uses production defaults).
    from localmail.serve.app import get_auth_config  # if such a getter exists
    import localmail.serve.app as serve_app
    serve_app._test_auth_config = AuthConfig(login_per_ip_max=5)
    try:
        resp = client.post(
            "/v1/auth/login",
            json={"username": "alice", "password": "hunter2"},
            headers={"X-Test-Client-IP": "9.9.9.9"},
        )
    finally:
        serve_app._test_auth_config = None

    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "60"
    body = resp.json()
    assert body["status"] == 429
    assert body["cap"] == "ip"
    assert body["retry_after_s"] == 60
```

Note this test depends on:
1. The login route reading `client_ip` from `request.client.host`. For pytest the test client uses `testclient`, which sets `client.host = "testclient"` — so to test per-IP we need either a way to override (a header? an app-state hook?) or we drive the failures directly into the DB via the table and let the route call `_check_login_rate_limits` against the seeded state. The test above takes the latter route + uses a config override.

If `localmail.serve.app` does not have a `get_auth_config()` / `_test_auth_config` hook today, we add one in Step 4.

- [ ] **Step 3: Run, confirm failure**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_serve_auth_routes.py::test_login_429_carries_retry_after_and_cap -q 2>&1 | tail -10
```

Expected: the assertion on `cap == "ip"` fails (route currently passes no `client_ip`), or an import error on `get_auth_config`.

- [ ] **Step 4: Update the login route**

In `src/localmail/serve/routes/auth.py`, replace `login`:

```python
@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, request: Request) -> TokenResponse:
    pool = request.app.state.pool
    cfg = _resolve_auth_config(request.app)
    client_ip = request.client.host if request.client else None
    with pool.connection() as conn:
        token, expires_at = auth_svc.login(
            conn, req.username, req.password,
            client_ip=client_ip,
            cfg=cfg,
        )
        conn.commit()
    return TokenResponse(token=token, expires_at=expires_at.isoformat())


def _resolve_auth_config(app):
    """Return AuthConfig from app state, falling back to defaults.

    Production: app.state.config (loaded from TOML) → its .auth attribute.
    Tests: app.state.auth_config_override can be set to a custom AuthConfig.
    """
    override = getattr(app.state, "auth_config_override", None)
    if override is not None:
        return override
    config = getattr(app.state, "config", None)
    if config is not None:
        return config.auth
    from localmail.config import AuthConfig
    return AuthConfig()
```

In `src/localmail/serve/app.py`, when the app is constructed, set `app.state.config = loaded_config`. (Likely already done — confirm with `grep -n "app.state" src/localmail/serve/app.py`.) If not, add it now.

- [ ] **Step 5: Update the middleware to emit `Retry-After`**

In `src/localmail/serve/middleware.py`, find the `APIError` exception handler. After building the response body, add the `Retry-After` header when the exception is a `RateLimited` with `retry_after_s` set:

```python
from localmail.api.errors import APIError, RateLimited

@app.exception_handler(APIError)
async def handle_api_error(request: Request, exc: APIError) -> JSONResponse:
    response = JSONResponse(
        content=exc.to_problem(),
        status_code=exc.http_status,
        media_type="application/problem+json",
    )
    if isinstance(exc, RateLimited) and exc.retry_after_s is not None:
        response.headers["Retry-After"] = str(exc.retry_after_s)
    return response
```

(The exact registration pattern depends on the existing middleware module — keep its style; the addition is the `isinstance` check + header.)

- [ ] **Step 6: Adjust the test for the actual config hook name**

Edit the test in Step 2 so it sets `client.app.state.auth_config_override = AuthConfig(login_per_ip_max=5)` and clears it in a `try/finally`. Replace the `serve_app._test_auth_config = ...` lines.

- [ ] **Step 7: Run, confirm pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_serve_auth_routes.py -q 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/localmail/serve/routes/auth.py src/localmail/serve/middleware.py src/localmail/serve/app.py tests/test_serve_auth_routes.py
git commit -m "feat(serve/auth): pass client_ip + 429 Retry-After + cap (#7)"
```

---

## Task 10: Documentation — CLAUDE.md + README.md

**Files:**
- Modify: `CLAUDE.md` (new "Rate-limiting model" section near the existing Auth bullets)
- Modify: `README.md` (mention `[auth]` config section + proxy gotcha)

- [ ] **Step 1: Update CLAUDE.md**

Open `CLAUDE.md`, find the section that lists Auth migrations (around the GUI server / migration `0014_api_users.sql` block). Add a new bullet after the per-user ACL bullet:

```markdown
- **Login rate-limiting (Postgres-backed, #7)**: migration
  `0019_api_login_attempts.sql` adds an append-only audit table read by
  three sliding-window caps — global, per-IP, per-user. Every login
  attempt (success + failure) is one INSERT; the check is a single SELECT
  with three `FILTER (...)` aggregates. Caps + windows + retention live
  in `LocalmailConfig.auth` so there are no module-level magic numbers in
  `api/auth.py`. The in-memory dicts that preceded this design were
  per-process and lost the security promise the moment `uvicorn
  --workers N` came into scope; the DB-backed table keeps the limits
  consistent across workers and across `serve` restarts. Cleanup is
  best-effort, gated by a Postgres advisory lock
  (`_SWEEP_ADVISORY_LOCK_KEY`) so concurrent workers don't pile up
  DELETEs. **Proxy gotcha**: `request.client.host` is the socket peer,
  not the X-Forwarded-For client. Behind a reverse proxy every login
  appears to come from `127.0.0.1` and the per-IP cap is effectively
  global — bump `login_global_max` accordingly or wait for the planned
  `auth.trust_proxy_headers` config knob.
```

- [ ] **Step 2: Update README.md**

Find the section that documents `config.toml` (likely under a "Configuration" or "Setup" heading). Add an `[auth]` snippet — copy the block from `config.example.toml`. Add a short paragraph below:

> The three login-rate-limit caps (global / per-IP / per-user) are
> Postgres-backed, so they survive `localmail serve` restarts and apply
> consistently across `uvicorn --workers N`. Behind a reverse proxy the
> per-IP cap is not effective until `auth.trust_proxy_headers` lands
> (see issue tracker) — bump `login_global_max` to compensate.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: postgres-backed login rate limiter (#7)"
```

---

## Task 11: Full verification

**Files:** none.

- [ ] **Step 1: Run the full suite**

```bash
unset VIRTUAL_ENV && uv run pytest -q 2>&1 | tail -20
```

Expected: every test passes. Compare the pass count to baseline before the branch (`653` at the start of the session — bumped by `Task 5/6/7/8/9` additions).

- [ ] **Step 2: Run mypy**

```bash
unset VIRTUAL_ENV && uv run mypy src/localmail 2>&1 | tail -20
```

Expected: no new errors; the 4 pre-existing `parser.py` errors are unchanged.

- [ ] **Step 3: Re-run migration on the live DB (smoke test)**

```bash
unset VIRTUAL_ENV && uv run localmail init-db 2>&1 | tail -5
```

Expected: a line for `0019_api_login_attempts.sql`, no errors. Re-running once more is a no-op (the migration is tracked in `schema_migrations`).

- [ ] **Step 4: Push the branch + open the PR**

```bash
git push -u origin feat/7-postgres-login-rate-limiter
gh pr create --title "feat(auth): Postgres-backed login rate limiter (closes #7)" --body "$(cat <<'EOF'
## Summary
- New migration `0019_api_login_attempts.sql` — append-only audit table.
- Replaces in-memory per-username + global limiter with a single SELECT (three `FILTER` aggregates) + single INSERT.
- Adds a per-IP cap between the existing two (closes #7's cross-username brute-force gap).
- All thresholds + windows + retention + cleanup interval moved to `LocalmailConfig.auth`.
- 429 responses now carry `Retry-After` and a `cap` field (`global` | `ip` | `user`) in problem+json.
- Cleanup sweep is gated by a PG advisory lock so concurrent workers don't pile up DELETEs.

## Test plan
- [x] `tests/test_api_auth_rate_limiter.py` — full TDD surface (record, check, sweep, multi-worker).
- [x] `tests/test_api_auth_ratelimit.py` — ported per-username + global tests.
- [x] `tests/test_serve_auth_routes.py` — 429 contract (Retry-After + cap).
- [x] Multi-worker semantics test (two psycopg connections see each other's failures).
- [x] `uv run pytest -q` clean.
- [x] `uv run mypy src/localmail` no new errors.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review

**Spec coverage:** every section in the spec is covered —
- Schema → Task 1
- Configuration → Task 3
- Service-layer API (`login`, `_check_login_rate_limits`, `_record_login_attempt`, `_sweep_login_attempts`) → Tasks 5, 6, 7, 8
- Single-query check → Task 6
- Routes (429 + Retry-After + cap) → Task 9
- Testing strategy items 1–14 → Tasks 5, 6, 7, 8 (multi-worker test in Task 8, migration idempotency in Task 1)
- Done definition checklist → Task 11

**Placeholder scan:** no TBDs or "appropriate error handling" wording in the plan. Every code block is concrete.

**Type consistency:** `cap` labels (`"global"` / `"ip"` / `"user"`) match between the error class, the check function, and the tests. `_record_login_attempt(conn, username, client_ip, outcome)` signature is identical across every reference. `_sweep_login_attempts(conn, *, retention_s)` returns `int` everywhere.

**Cross-file naming:** `reset_login_rate_limiter` now takes a `conn` arg — Task 8 calls out every caller that needs updating.
