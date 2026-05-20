# Postgres-backed login rate limiter — design

> **Status:** Draft 2026-05-20. Closes #7 (Auth: IP-based / global login
> rate limiter).

## Motivation

`localmail` has two production use cases: (1) personal searchable mail
archive accessed by a human, and (2) a queryable backing service that AI
agents may hammer with high concurrency. The serve layer is therefore on
a near-term path to `uvicorn --workers N`.

The current login rate limiter in
[`src/localmail/api/auth.py`](../../../src/localmail/api/auth.py) keeps all
state in **in-process** dicts:

- `_LOGIN_FAILURES` — per-username sliding window.
- `_LOGIN_GLOBAL_ATTEMPTS` — server-wide sliding window (CPU/argon2 cap).
- No per-IP limiter at all.

Three problems, all closed by moving the state to Postgres:

1. **Cross-username brute force.** An attacker rotating usernames (`alice`
   5× → `bob` 5× → …) evades the per-username cap. The existing global
   cap (30/60s) bounds CPU but not credential discovery — a per-IP cap
   between the two is the right grain.
2. **Multi-worker drift.** With N workers the effective limits become
   N× looser because each process keeps its own dict. This already breaks
   the security promise the moment `--workers` is enabled.
3. **No durability.** Restarting the daemon resets every counter — an
   attacker can survive a graceful reload and start over.

Postgres is already a hard dependency (every login does a `SELECT` from
`api_users`), so a small new table is essentially free in deployment
complexity while solving all three.

## Scope

In:

- New migration `0019_api_login_attempts.sql`: one append-only table
  `api_login_attempts(id, ts, ip, username, outcome)` with three indexes.
- Replace `_LOGIN_FAILURES`, `_LOGIN_GLOBAL_ATTEMPTS`, both locks, and
  `LOGIN_FAILURES_MAX_USERS` LRU machinery with DB-backed equivalents in
  `localmail.api.auth`.
- Single SELECT (three `FILTER (...)` aggregates) checks all three caps
  per login attempt; single INSERT records the outcome.
- `login(conn, username, password, *, client_ip: str | None = None)` —
  add the `client_ip` keyword argument.
- New `LocalmailConfig.auth` config section (TOML), moving every threshold
  + window + retention out of module-level constants.
- Periodic best-effort cleanup of expired rows, gated by a Postgres
  advisory lock so workers don't pile up DELETEs.
- 429 response carries `Retry-After` derived from the smallest exceeded
  window.
- Route layer extracts client IP from `request.client.host`. **No
  X-Forwarded-For trust** in this PR (separate issue / config knob).
- Document the proxy-deployment gotcha in CLAUDE.md and README.md.

Out:

- **No `api_blocked_ips` manual-ban table.** Deferred until operations
  actually need it — the table can be added by a follow-up migration.
- **No X-Forwarded-For / trust-proxy support.** Separate config decision;
  a deployment behind a reverse proxy currently sees every login from
  `127.0.0.1` and effectively has no per-IP protection. CLAUDE.md and
  README.md call this out so it is not silent.
- **No CLI for inspecting / clearing attempts.** `localmail` already has
  per-account / per-user commands; if we end up needing operator
  inspection of attempts the migration is trivial. The DB table is
  inspectable with `psql` until then.
- **No IPv6 / CIDR aggregation.** IPs are stored verbatim. If IPv6 hosts
  rotate /64 prefixes to evade per-IP limits we add CIDR collapsing in
  a follow-up.

## Threat model addressed

1. **Argon2 CPU amplification from any unauthenticated caller** — bounded
   by the global cap, which counts *all* attempts including successes
   (an attacker with valid credentials can still DoS via parallel
   logins).
2. **Per-username brute force from one or many IPs** — bounded by the
   per-username failure cap.
3. **Cross-username brute force from one IP** — bounded by the per-IP
   failure cap. An attacker who fails on `alice`, `bob`, `carol`, … from
   one IP trips the IP cap regardless of the per-username caps.
4. **Multi-worker drift** — all three counters live in shared Postgres
   state, so adding `--workers N` does not loosen the limits.
5. **Restart-survives credential probing** — counters persist across
   `localmail serve` restarts up to the retention window.

Not addressed (deliberate):

- **Distributed brute force from a botnet rotating IPs.** Only the global
  cap defends against this, and it already does (CPU is the resource the
  attacker is trying to consume). Per-IP becomes meaningless under
  source-IP rotation; per-CIDR could help but is out of scope.
- **Inside-the-trust-boundary attackers** with valid credentials trying
  to hammer the API past the global cap. The global cap fires equally on
  success; legitimate operators bump the config.

## Schema

```sql
CREATE TABLE api_login_attempts (
  id        BIGSERIAL PRIMARY KEY,
  ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
  ip        TEXT,                                          -- nullable
  username  TEXT NOT NULL,
  outcome   TEXT NOT NULL CHECK (outcome IN ('success','failure'))
);

CREATE INDEX api_login_attempts_ts_idx
  ON api_login_attempts (ts DESC);
CREATE INDEX api_login_attempts_ip_ts_idx
  ON api_login_attempts (ip, ts DESC) WHERE ip IS NOT NULL;
CREATE INDEX api_login_attempts_user_ts_idx
  ON api_login_attempts (username, ts DESC);
```

- `ip` is nullable so callers without a transport-layer client IP
  (CLI-driven tests, future MCP path) can still record an attempt.
- `username` is `NOT NULL` — even on the unknown-user branch the login
  attempt records the username the caller tried, so the per-username cap
  catches enumeration on non-existent users too.
- `outcome` is a `TEXT CHECK` rather than an enum so we don't need
  `ALTER TYPE` to add states later (e.g. `'disabled'`, `'rate_limited'`)
  — see [project conventions](../../../CLAUDE.md) on additive schema
  changes.
- The three indexes are sized for the workload: `ts_idx` for global +
  cleanup, partial `ip_ts_idx` for per-IP (NULLs skipped), `user_ts_idx`
  for per-user. All three are descending on `ts` because every query is
  "in the last N seconds".

## Configuration

New `LocalmailConfig.auth` section (pydantic model):

```toml
[auth]
login_per_user_max = 5
login_per_user_window_s = 60
login_per_ip_max = 20
login_per_ip_window_s = 60
login_global_max = 30
login_global_window_s = 60

# How long to retain attempt rows. Anything older is best-effort deleted
# by the cleanup sweep; raising this lets you audit further back but does
# NOT affect the rate-limiting windows above.
login_attempt_retention_s = 86400  # 24 hours

# How often (per worker) to run the cleanup sweep. Gated by a PG
# advisory lock so concurrent workers don't pile up DELETEs.
login_cleanup_interval_s = 300
```

Defaults preserve the current per-user (5/60s) and global (30/60s)
behaviour exactly. Per-IP (20/60s) is new — looser than per-user because
shared NATs / corporate proxies put multiple legitimate users behind one
IP.

`reset_login_rate_limiter()` is repurposed: it `TRUNCATE`s the table.
Tests still call it between cases.

## Service-layer API

The transport-free public surface in `localmail.api.auth`:

```python
def login(
    conn: psycopg.Connection,
    username: str,
    password: str,
    *,
    client_ip: str | None = None,
) -> tuple[str, datetime]:
    """Verify credentials and mint a token.

    Raises RateLimited if any of the three configured caps (global,
    per-IP, per-username) is exceeded. Raises AuthenticationFailed
    for bad credentials or disabled users.

    Order: check global → per-IP → per-username → argon2 verify.
    Global stays first so an attacker can't pin CPU before we check
    the cheaper cap; per-IP before per-username so an attacker
    rotating usernames trips IP first.
    """
```

Internal helpers:

```python
def _check_login_rate_limits(
    conn: psycopg.Connection,
    username: str,
    client_ip: str | None,
    *,
    cfg: AuthConfig,
) -> None:
    """Single SELECT with FILTER aggregates. Raises RateLimited with
    a Retry-After hint matching the smallest exceeded window."""

def _record_login_attempt(
    conn: psycopg.Connection,
    username: str,
    client_ip: str | None,
    outcome: Literal["success", "failure"],
) -> None:
    """Single INSERT into api_login_attempts. Uses a nested SAVEPOINT
    so a logging failure does not abort the outer transaction."""

def _sweep_login_attempts(
    conn: psycopg.Connection,
    *,
    retention_s: int,
) -> int:
    """DELETE rows older than retention. Returns row count.

    Gated by a Postgres advisory lock (xact_lock) so concurrent workers
    don't pile up. Best-effort: skipped silently if the lock is held."""
```

The cleanup sweep runs at most once per `login_cleanup_interval_s` per
worker, gated by a per-process `_last_sweep_at` monotonic timestamp.
Combined with the advisory lock, the net effect on N workers is a sweep
roughly every `login_cleanup_interval_s / N` wall-clock seconds — fine.

## Single-query rate-limit check

```sql
SELECT
  COUNT(*) FILTER (
    WHERE ts > now() - make_interval(secs => %s)
  )                                                AS global_attempts,
  COUNT(*) FILTER (
    WHERE ip = %s
      AND outcome = 'failure'
      AND ts > now() - make_interval(secs => %s)
  )                                                AS ip_failures,
  COUNT(*) FILTER (
    WHERE username = %s
      AND outcome = 'failure'
      AND ts > now() - make_interval(secs => %s)
      AND ts > COALESCE(
        (SELECT MAX(ts) FROM api_login_attempts
          WHERE username = %s AND outcome = 'success'),
        '-infinity'::timestamptz
      )
  )                                                AS user_failures
FROM api_login_attempts
WHERE ts > now() - make_interval(secs => %s);  -- broadest window for index pruning
```

- The outer `WHERE` uses the largest of the three windows so the planner
  can use `api_login_attempts_ts_idx` to prune.
- The per-user `FILTER` includes the "since last success" clause —
  this is how "successful login clears the failure count" is preserved
  on an append-only table.
- The per-IP `FILTER` does **not** include a "since last success"
  clause: a successful login from one user on a shared NAT does not
  unlock the IP for other users. This is intentional and the bullet in
  *Open questions* below records the trade-off.

Performance: ~10s of microseconds with indexed counts even at 1M+
historical rows; the predicates lean on the same indexes that the
cleanup sweep uses to bound table growth.

## Routes

`POST /v1/auth/login` in [`src/localmail/serve/routes/auth.py`](../../../src/localmail/serve/routes/auth.py):

```python
@router.post("/v1/auth/login")
def login_route(payload: LoginPayload, request: Request, ...):
    client_ip = request.client.host if request.client else None
    try:
        token, expires_at = auth.login(
            conn, payload.username, payload.password,
            client_ip=client_ip,
        )
    except RateLimited as exc:
        # Body: standard problem+json with retry_after_s.
        # Headers: Retry-After: <seconds>
        raise HTTPException(...)
```

The error contract:

- 429 `application/problem+json` with `type: "/problems/rate-limited"`,
  `retry_after_s: <int>`, `cap: "global" | "ip" | "username"` so the
  client can tell *why* it was throttled.
- HTTP `Retry-After` header matches `retry_after_s`.

## Testing strategy (TDD)

Test surface lives in `tests/test_api_auth_rate_limiter.py` (new) and
extends `tests/test_serve_auth_routes.py` (existing) for the route
layer.

Unit tests (no FastAPI):

1. Empty table → no caps hit → login succeeds.
2. Per-user cap: N failures from one user → next attempt raises
   `RateLimited(cap="username")` even from a different IP.
3. Per-IP cap: M failures across N usernames from one IP → trips
   `RateLimited(cap="ip")`, **regardless** of any individual per-user
   count. Direct regression for issue #7's threat model.
4. Global cap: total attempts (successes included) → trips
   `RateLimited(cap="global")`.
5. Window expiry: a failure outside the window is not counted.
6. Per-user clears on success; per-IP does not.
7. NULL IP path: `client_ip=None` does not crash, does not contribute
   to per-IP cap.
8. Cleanup sweep deletes rows older than retention; returns
   deleted-row count; advisory lock prevents two workers from
   piling up.
9. **Multi-worker semantics**: two separate `psycopg.Connection`
   objects against the same DB both see each other's failures (proves
   the multi-worker promise — the in-memory implementation would fail
   this).
10. `reset_login_rate_limiter()` truncates the table cleanly.

Route tests:

11. 429 response carries `Retry-After` header.
12. 429 body shape: problem+json with `type`, `retry_after_s`, `cap`.
13. The route resolves `client_ip` from `request.client.host`.

Migration test:

14. Re-running `init-db` on a populated DB is idempotent. Existing
    rows survive. New table appears with all three indexes.

## CLI

No new subcommands in this PR. Operator inspection happens via `psql`
until we have a demonstrated need for `localmail list-login-attempts`
or `localmail clear-login-attempts`.

## Migration & upgrade story

- `0019_api_login_attempts.sql` is purely additive: a new table + three
  indexes, no schema changes to `api_users` / `api_tokens`.
- Existing in-memory limiter state is lost on the upgrade — acceptable;
  the worst case is that an in-flight attacker's counter resets to
  zero, but they will trip the new DB-backed counter within the same
  60-second window. No long-term security regression.
- On a fresh install nothing changes from the operator's perspective:
  thresholds default to today's values.

## Risks & open questions

1. **Per-IP cap and shared NATs.** 20/60s is calibrated for "one person
   behind one home router on shared WiFi", not "a corporate proxy
   serving 100 employees". If a real deployment trips this, the cap is
   in TOML config — bump and reload.
2. **Per-IP not cleared on success.** A successful login from `alice`
   does not unlock the IP for `bob`'s subsequent failures. Trade-off:
   adding "since-last-success" semantics to per-IP would let an
   attacker chain `alice` (legit) + `bob/carol/…` (brute force) from
   the same workstation. Keep the conservative posture; revisit if
   anyone hits it.
3. **X-Forwarded-For not honoured.** Documented in CLAUDE.md and
   README.md but not implemented. Anyone running localmail behind a
   reverse proxy effectively has no per-IP protection until they get
   a config knob (separate issue).
4. **No CIDR aggregation.** IPv6 hosts can rotate within a /64 to evade
   per-IP. Add `ip_prefix_v6 = 64` config + a normalisation step in a
   follow-up if this is observed.
5. **Argon2 verify on unknown-user path is still done before the
   rate-limit failure is recorded.** Same as today. The dummy verify
   is what gives timing parity; recording the failure after is fine
   because it still costs the attacker an argon2 round.
6. **Rate-limit check is not strictly atomic with record.** The SELECT
   that evaluates the caps and the INSERT that records the new attempt
   are two statements, so two concurrent logins can both pass the check
   before either records its failure. The cap is therefore soft by up
   to (concurrent in-flight logins) in the worst case — at our argon2
   verify time (~100 ms) that's a handful per process, well below the
   defense margin. Resolvable with `SELECT FOR UPDATE` on a per-user/IP
   row, or with an advisory lock around the whole check-then-insert,
   but neither is worth the complexity for a security defense whose
   point is to bound CPU and slow brute force, not to be exact. The
   prior in-memory implementation was atomic per-process but lost the
   atomicity guarantee the moment `--workers N` lands, so this is a
   wash.

7. **Cleanup sweep at scale.** At 1M attempts/day with 24h retention
   the table holds ~1M rows. `DELETE` of expired rows is cheap with
   the `ts_idx` but produces `VACUUM` debt. If anyone runs a deployment
   that hot we add `pg_partman` or daily-partitioned tables in a
   follow-up; not worth the operational complexity now.

## Done definition

- `0019_api_login_attempts.sql` lands. `init-db` applies it cleanly on
  a fresh DB and on the live archive.
- Every old in-memory rate-limiter symbol in `auth.py` is removed.
  `reset_login_rate_limiter()` survives but `TRUNCATE`s the table.
- `login()` accepts `client_ip` kwarg; route extracts it from
  `request.client.host`.
- `LocalmailConfig.auth` exists; defaults preserve current thresholds.
- The full test surface (unit + route + migration + multi-worker
  semantics) passes.
- CLAUDE.md gains a `## Rate-limiting model` block documenting:
  - Three caps + the order they fire.
  - Postgres-backed shared state across workers.
  - The X-Forwarded-For gotcha for reverse-proxy deployments.
- README.md mentions the new `[auth]` config section and the proxy
  gotcha.
- mypy clean on `localmail.api.auth`, `localmail.serve.routes.auth`,
  and the new test file.
