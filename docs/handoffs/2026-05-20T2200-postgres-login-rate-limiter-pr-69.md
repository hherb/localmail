# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-20 (post-session).** PR **#69**
> (`feat(auth): Postgres-backed login rate limiter (closes #7)`) opened
> against `main` on branch `feat/7-postgres-login-rate-limiter`. 14
> commits, ~+1000 lines across migration / api layer / config / serve
> routes / docs / tests. Full pytest suite **682 passed** (was 653 at
> session start; +29 from the new test surface). mypy clean on touched
> files; 4 pre-existing `parser.py` errors carry forward as before.
> Awaiting CI + review.
>
> The session also closed issue **#65** (factor `_lookup_blob_row`
> helper) as already addressed by PRs #66/#68 — verified by reading
> `src/localmail/api/attachments.py` and confirming
> `get_attachment_metadata`, `get_attachment_blob_info`, and
> `open_attachment_bytes` all route through `_lookup_blob_row`.
>
> Branch `feat/7-postgres-login-rate-limiter` lives locally + on origin;
> do not delete until PR #69 merges. Working tree clean (only
> `.claude/settings.local.json` untracked — local-only).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

The two production use cases drive every design decision:
1. Personal searchable mail archive (human user).
2. Backing service for AI agents that may hammer it with high
   concurrency — `uvicorn --workers N` is a near-term reality.

The rate-limiter PR was scoped against use case (2): the prior in-memory
limiter would have multiplied its effective caps by `N` the moment
workers turned on, silently breaking the security promise.

## What we shipped this session

### PR #69 — Postgres-backed login rate limiter (closes #7)

Branch: `feat/7-postgres-login-rate-limiter` (head: `05b5ead`).

| SHA | What |
|---|---|
| `3401b1f` | `docs(spec)`: design — table, single-query check, advisory-lock sweep, config schema, threat model. |
| `9d25c8c` | `docs(plan)`: 11-task TDD plan, every code block concrete. |
| `7f6fbf9` | `feat(migrations)`: `0019_api_login_attempts.sql` — `id, ts, ip, username, outcome` + 3 indexes. |
| `33edbd9` | `test(conftest)`: TRUNCATE new table between tests. |
| `d0db043` | `feat(config)`: `AuthConfig` pydantic model — 8 fields, defaults preserve prior behaviour. |
| `e370401` | `feat(api/errors)`: `RateLimited` carries keyword-only `cap` + `retry_after_s`; surfaced in `to_problem()`. |
| `1c53937` | `feat(auth)`: `_record_login_attempt` — SAVEPOINT-protected INSERT. |
| `dec5f4b` | `feat(auth)`: `_check_login_rate_limits` — single SELECT with three `FILTER (...)` aggregates; cap-label order global → ip → user. |
| `98389ce` | `feat(auth)`: `_sweep_login_attempts` — `pg_try_advisory_lock`-gated DELETE. |
| `bc231c3` | `feat(auth)`: replace in-memory limiter with DB-backed flow; `login(... *, client_ip, cfg)`; `reset_login_rate_limiter(conn)` TRUNCATEs. |
| `f868889` | `feat(serve/auth)`: pass `client_ip` from `request.client.host`; 429 carries `Retry-After` + `cap` body field; both APIError handler paths updated. |
| `080924e` | `feat(cli)`: thread `cfg.auth` from TOML into `create_app(auth_config=...)` so operator config actually takes effect. |
| `74838f2` | `docs`: CLAUDE.md `Login rate-limiting (Postgres-backed, #7)` bullet + README `[auth]` config block + proxy-gotcha note. |
| `05b5ead` | `fix(auth)`: commit audit row + sweep eagerly so the route's outer rollback on `AuthenticationFailed` doesn't discard them. Adds the route-driven regression test. |

**The critical fix in `05b5ead`** deserves a separate call-out: the
final cross-task review (after all other commits had landed) caught a
silent bug — `_record_login_attempt` deferred its commit to the route,
but the route raises `AuthenticationFailed` on bad credentials, which
triggers the `with pool.connection()` rollback and discards the audit
row. Every layer of unit tests passed because they all called
`db_conn.commit()` manually between failed-login calls, masking the
production rollback path. The fix adds `conn.commit()` after the
SAVEPOINT release in `_record_login_attempt` (and at the end of
`_maybe_sweep` for the same reason on the cleanup-DELETE side), plus a
`tests/test_serve_auth_routes.py::test_route_driven_login_failures_persist_audit_rows`
that drives 3 failed `client.post("/v1/auth/login")` calls and asserts a
fresh `psycopg.connect(db_dsn)` sees the 3 rows. Without this fix, the
per-user and per-IP failure caps would never trip from production
traffic.

### Issues closed this session

- **#7** addressed by PR #69 (open).
- **#65** closed as already done — `_lookup_blob_row` helper landed in
  PRs #66 + #68. Closed with a comment pointing at those commits.

### What CLAUDE.md gained

A new `Login rate-limiting (Postgres-backed, #7)` bullet next to the
existing per-user ACL bullet. Documents:
- The single audit table + single-query check architecture.
- `LocalmailConfig.auth` as the source of truth for thresholds.
- `_SWEEP_ADVISORY_LOCK_KEY` advisory lock for cleanup coordination.
- The **reverse-proxy gotcha** — `request.client.host` is the socket
  peer, not X-Forwarded-For; until `auth.trust_proxy_headers` lands,
  deployments behind a proxy should compensate with `login_global_max`.

CLAUDE.md's two "latest migration is 0018" references updated to
`0019_api_login_attempts.sql`.

### What README.md gained

A new "Login rate-limit config" subsection inside the GUI server
section: the `[auth]` TOML block (byte-for-byte mirror of
`config.example.toml`) plus a paragraph explaining the Postgres-backed
multi-worker promise + the proxy gotcha.

## What's next

### 1. Merge PR #69

Once CI + any review feedback is addressed, squash-merge to `main` and
delete the branch locally + remotely. Carry the squash SHA into the
next handoff.

### 2. Pick the next piece of work

Top candidates (open issues, prioritised for handoff):

- **`auth.trust_proxy_headers` config knob** (no issue yet — file one).
  The per-IP cap is currently effectively global behind a reverse
  proxy because `request.client.host` is the socket peer. A small
  config knob + a `_resolve_client_ip(request, cfg)` helper that
  honours the leftmost public IP in X-Forwarded-For when trust is
  enabled would close this. Small, scoped, follows the same shape as
  the existing config additions. Recommended next.
- **Test-file collapse / split (no-feature housekeeping)** —
  `tests/test_serve_attachments_routes.py` is still 587 lines (split
  candidate); `tests/test_api_auth_ratelimit.py` and
  `tests/test_api_auth_rate_limiter.py` cover overlapping service-layer
  surface (the final review flagged this as Important #3 — consider
  collapsing into one file with clear sections).
- **#38** `/v1/changes` semantics decision. May be premature without
  GUI-side traffic data.
- **#47** Third-party transient classes (deferred follow-up to #36).
  Still gated on real ops data.
- **#25** `websockets.legacy` deprecation. Not actionable — upstream
  uvicorn release blocker. Re-check on next uvicorn bump.
- **#28 / #27 / #24 / #22 / #18 / #17** GUI client polish & CI.
- **#10 / #12** Persist Content-ID on attachments (inline `cid:`
  rendering).
- **#4** Embedding backend — explicit model paths + verified
  query/document task prefixes.
- **#2** Migration 0006 — make GIN index builds CONCURRENT for
  production upgrades.
- **#5** Batch INSERT for chunking loop. Defer until backfill on a
  100k+ archive is measured.
- **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) still
  need triage. Independent of this session's work.

**Recommendation**: ship the `auth.trust_proxy_headers` knob next.
It's the natural follow-up to PR #69 (the proxy gotcha is documented
but not solved), well-scoped, and useful to anyone running localmail
behind nginx/Caddy/Traefik.

## Open decisions & risks (this PR specifically)

1. **Atomicity vs soft-cap (intentional).** `_check_login_rate_limits`
   (SELECT) and `_record_login_attempt` (INSERT) are not atomic.
   Concurrent in-flight logins can both pass the check before either
   records. The cap is therefore soft by up to N (in-flight requests
   per process) per worker, well under the global cap margin. Could be
   tightened with `SELECT FOR UPDATE` or an advisory lock around the
   check, but the spec explicitly preferred soft semantics.

2. **Per-IP cap not cleared on success (intentional).** A successful
   login from `alice` does NOT unlock the IP for `bob`/`carol`/…
   subsequent failures. Adding "since-last-success" semantics to
   per-IP would let an attacker chain a legit login through a shared
   workstation to reset the per-IP failure count, so the conservative
   posture wins.

3. **X-Forwarded-For not honoured.** Currently every login behind a
   reverse proxy looks like it comes from `127.0.0.1`. CLAUDE.md +
   README.md document the gotcha. See "What's next" item 1.

4. **No `api_blocked_ips` manual-ban table.** Deferred until ops needs
   it; trivial to add via a follow-up migration.

5. **No CIDR aggregation for IPv6.** A `/64`-rotating IPv6 host can
   evade the per-IP cap. Add `ip_prefix_v6` config + normalisation in
   a follow-up if observed.

6. **The eager-commit pattern in `_record_login_attempt` /
   `_maybe_sweep` is load-bearing.** Future changes to `auth.py` that
   add writes between `_record_login_attempt` and a subsequent
   `raise` must ensure those writes can survive an outer rollback — or
   wrap their own SAVEPOINT. The docstrings call this out. A future
   bug here would be very hard to spot in tests because the existing
   tests commit manually.

7. **Cleanup sweep advance-clock-then-DELETE order.** `_maybe_sweep`
   sets `_LAST_SWEEP_AT_MONOTONIC = now` BEFORE calling
   `_sweep_login_attempts`, so a sweep that fails (or short-circuits
   on a held advisory lock) still updates the in-process clock. Trade-
   off: if the sweep DELETE keeps failing, the worker stays in the
   cooldown window and the table grows. The PG advisory lock + per-
   worker hysteresis combined mean this is hard to hit in practice
   (another worker will run the sweep within the interval).

8. **`logger.warning` is the only signal for a chronically broken
   audit table.** If the table is dropped or permissions revoked
   without anyone noticing, the limiter silently stops working —
   logins still succeed, but the cap never trips. The WARNING log
   line is the only operational signal. A dashboard alert on this log
   message would be wise in production.

9. **Carried forward from prior sessions (still load-bearing):**
   - The MIME clamp list (#32) is small on purpose — only
     actively-script-executable types.
   - `parse_int_id` (#33) accepts leading zeros (`"01"` → 1).
   - `rrf_k=60` is the centre of a flat plateau (#35) — un-tuned
     against production data.
   - `websockets.legacy` DeprecationWarning (#25) still fires; uvicorn
     blocker.
   - Dependabot 12 vulnerabilities on `main` (1 high / 9 mod / 2 low)
     need triage.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch origin

# If PR #69 is still open:
git checkout feat/7-postgres-login-rate-limiter
gh pr view 69                              # check CI + review state

# After PR #69 is merged:
git checkout main
git pull
git branch -d feat/7-postgres-login-rate-limiter
git push origin :feat/7-postgres-login-rate-limiter

# Quick sanity:
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q              # expect 682 passed (on branch / post-merge)
unset VIRTUAL_ENV && uv run mypy src/localmail     # 4 pre-existing parser.py errors carry forward

# Pick next piece:
gh issue list --state open --limit 40
# Recommended: file an issue for auth.trust_proxy_headers; treat it as
# the natural follow-up to PR #69.
```

## Known gotchas (still load-bearing)

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have a stale
  `VIRTUAL_ENV` pointing at some other pyenv venv; `uv run --active`
  picks the wrong interpreter without this.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql:///localmail_test`** —
  tests must not touch the live `localmail` DB.
- **Migrations 0011–0019 are additive.** Re-running `init-db` on an
  older archive is safe (idempotent), but back up first if the archive
  is non-trivial.
- **Login rate limiter (NEW, #7)**:
  - Caps live in `LocalmailConfig.auth`; defaults preserve prior
    behaviour (5/60 per-user, 30/60 global; new 20/60 per-IP).
  - `_record_login_attempt` + `_maybe_sweep` commit eagerly. Any new
    write added to `login()` between these and a subsequent `raise`
    must wrap its own SAVEPOINT if it must NOT survive an outer
    rollback.
  - `auth.trust_proxy_headers` doesn't exist yet — bump
    `login_global_max` if running behind a reverse proxy.
  - `logger.warning("api_login_attempts insert failed: %s", exc)` is
    the only operational signal for a broken audit table.
- **TLS for `localmail serve`** — `--bind 0.0.0.0` requires `--tls-cert`
  + `--tls-key`. `--no-tls` is only honoured on `127.0.0.1`. Use
  `localmail rotate-tls` to generate a self-signed cert.
- **First-time `body_lang` install** — run `localmail lang-backfill`
  (or `embed-backfill`, which drains both queues) once after upgrade so
  `lang:` queries return rows.
- **ACL upgrade**: post-0016, new API users have **no grants**. Run
  `localmail grant-account USERNAME ACCOUNT_NAME` once per pair.
- **Probe-then-condition boundary** (#62): for any new conditional-GET
  endpoint, the order is **ACL+probe → precondition → expensive IO**.
- **Streaming WARNING contract** (#58): any new streaming endpoint
  that advertises a `Content-Length` MUST also count bytes yielded
  and call `_log_truncation()` when the source runs short.
- **ID-typing boundary** (#33): path and query parameters bearing
  entity IDs are typed `str` on the route handler signature, and
  `localmail.api.ids.parse_int_id(value, field="…")` is the ONLY
  way to cast to int.

## File map (as of branch HEAD `05b5ead`)

```
src/localmail/
  api/                               # transport-free service library
    accounts.py acl.py
    attachments.py                   # _lookup_blob_row (#65 closed)
    auth.py                          # NEW: DB-backed rate limiter (#7)
                                     #   _check_login_rate_limits (#7)
                                     #   _record_login_attempt (#7)
                                     #   _sweep_login_attempts (#7)
                                     #   _maybe_sweep + _LAST_SWEEP_AT_MONOTONIC (#7)
                                     #   _SWEEP_ADVISORY_LOCK_KEY (#7)
    conditional.py                   # #59
    errors.py                        # RateLimited.cap + .retry_after_s (#7)
    ids.py                           # #33
    range_requests.py                # #54
    messages.py sanitize.py search.py
  config.py                          # AuthConfig (#7)
  serve/
    app.py                           # auth_config kwarg + Retry-After (#7)
    middleware.py                    # Retry-After on RateLimited (#7)
    routes/
      auth.py                        # passes client_ip + cfg (#7)
      accounts.py attachments.py changes.py messages.py search.py version.py
  cli.py                             # threads cfg.auth → create_app (#7)
  daemon.py search/ ...
migrations/                          # 0001 … 0019_api_login_attempts.sql
tests/
  test_api_auth_rate_limiter.py      # NEW (#7): 19 tests — record/check/sweep/multi-worker
  test_api_auth_ratelimit.py         # PORTED (#7): 4 service-layer regression tests
  test_serve_auth_routes.py          # 429 contract + route-driven regression (#7)
  test_api_errors.py                 # RateLimited cap/retry_after assertions (#7)
  test_config.py                     # AuthConfig defaults + round-trip (#7)
  test_api_attachments.py            # #62 — get_attachment_blob_info
  test_api_conditional.py            # #59
  test_api_ids.py                    # #33
  test_api_range_requests.py         # #54
  test_serve_attachments_routes.py   # #32 + #54 + #58 (587 lines — split candidate)
  test_serve_attachments_conditional.py  # #59 + #62
  test_daemon_pool.py                # #37
  conftest.py                        # TRUNCATEs api_login_attempts (#7)
docs/superpowers/
  specs/2026-05-20-login-rate-limiter-postgres-design.md   # #7 spec
  plans/2026-05-20-login-rate-limiter-postgres.md          # #7 plan (11 tasks)
docs/handoffs/
  2026-05-20T2200-postgres-login-rate-limiter-pr-69.md     # this session's snapshot
NEXT_SESSION.md                      # this file (post-session)
```

End of postgres-login-rate-limiter session. PR #69 open against `main`
(`05b5ead`). Branch `feat/7-postgres-login-rate-limiter` alive on local
+ remote until merge. Next: merge #69, then ship the
`auth.trust_proxy_headers` follow-up so the per-IP cap is effective
behind reverse proxies.
