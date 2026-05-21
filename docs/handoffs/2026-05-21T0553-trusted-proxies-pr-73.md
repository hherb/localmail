# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-21 (post-session).** PR **#73**
> (`feat(auth): trusted_proxies — XFF-aware login rate limiter`)
> **open against `main`** on branch `feat/auth-trusted-proxies`.
> 6 commits (4 tasks + spec + plan), +2,190 / -10 lines, 11 changed
> files. Full pytest suite **755 passed** (was 731 at session start;
> +24 from new resolver / config-validator / e2e-route tests). mypy
> clean on touched files; 4 pre-existing `parser.py` errors carry
> forward. Awaiting review + merge.
>
> Prior session's PR **#70** (`fix(search): unbounded sort=date,
> coalesced wire date, reranker off by default`) **merged** to `main`
> on 2026-05-20 as `f803ab3` — no leftover work.
>
> Branch `feat/auth-trusted-proxies` lives locally + on origin; keep
> until PR #73 merges. Working tree clean (only
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

This session closed the proxy gotcha that landed open in PR #69's
handoff: behind a reverse proxy, `request.client.host` is the
proxy's address, so the per-IP login rate cap collapsed into a copy
of the global cap. PR #73 adds an opt-in `auth.trusted_proxies`
CIDR list that recovers the real client IP from `X-Forwarded-For`
using the canonical right-to-left peel (matches nginx
`set_real_ip_from` / Caddy `trusted_proxies` semantics). Empty
default = unchanged behaviour.

## What we shipped this session

### PR #73 — `auth.trusted_proxies` XFF-aware client IP resolution

Branch: `feat/auth-trusted-proxies` (head `d5376d9`). Shipped via the
brainstorm → spec → plan → subagent-driven-development pipeline.

#### Spec + plan

| SHA | What |
|---|---|
| `2d8debc` | `docs(auth)`: design for `trusted_proxies` + XFF client IP resolution. Threat model (A1 spoofed-direct, A2 spoofed-tail, A3 oversized, A4 malformed), eight-step algorithm, sanitisation rules, logging contract, four deployment shapes covered. |
| `64315ff` | `docs(auth)`: implementation plan — 5 tasks, ~1,100 lines, every code block verbatim so a fresh implementer needs zero project context. |

#### Implementation (Tasks 1-4)

| SHA | What |
|---|---|
| `e4eca86` | `feat(api)`: `localmail.api.client_ip.resolve_client_ip(socket_peer, xff_header, *, trusted_proxies, max_hops)`. Pure module (no FastAPI / DB / IO), eight-step right-to-left peel, transport-free so future MCP callers reuse it. 17 unit tests (T1-T16 + host-form sanity); mypy clean. |
| `8613de5` | `feat(config)`: `AuthConfig.trusted_proxies: list[str]` + `trusted_proxies_max_hops: int = Field(default=3, ge=1, le=10)`. Pydantic `field_validator` fails LOUD on bad CIDR; `model_post_init` populates `_trusted_proxies_parsed: tuple[IPv4Network \| IPv6Network, ...]` via `PrivateAttr` so per-request cost is one property read. 6 validator tests. |
| `7877308` | `feat(serve)`: `/v1/auth/login` calls `resolve_client_ip(...)` before `auth_svc.login`. Five-line wiring change; `_check_login_rate_limits`, `_record_login_attempt`, audit table all unchanged. 1 end-to-end test driving 5+3+1 login attempts via `TestClient(app, client=("127.0.0.1", 50000))` to confirm per-IP cap distinguishes XFF values. |
| `d5376d9` | `docs(auth)`: README adds "Behind a reverse proxy" subsection with three recipes (same-host, private-LAN, Cloudflare) + `uvicorn --forwarded-allow-ips` non-interaction warning. CLAUDE.md "Proxy gotcha" bullet → "Reverse-proxy support" pointing at the shipped knob. `config.example.toml` gets the three commented recipes. |

### Test deltas

```
backend pytest:    731 → 755  (+24)
mypy:              4 pre-existing parser.py errors (unchanged)
```

Test breakdown of the +24:
- `tests/test_api_client_ip.py` — 17 new tests (T1-T16 covering the eight-step algorithm + host-form sanity)
- `tests/test_config.py` — 6 new tests (defaults, host-form, CIDR-form, bad CIDR, max_hops=0, max_hops=11)
- `tests/test_api_auth_rate_limiter.py` — 1 new end-to-end test (`test_per_ip_cap_uses_xff_when_trusted`)

### Final code-review pass

Pass-by-pass per-task spec + quality reviews all approved; the final
end-to-end branch review (PR #73 scope) also approved with five
minor polish items noted for future cleanup:

1. `client_ip.py:111-123` — `xff_last=<unparseable>` log field can
   be misleading when the failure isn't actually the rightmost
   entry (it's wherever the right-to-left walk found it). Spec
   sample log uses the same string so this matches the spec; nit
   only.
2. `tests/test_config.py:243,250` — `from ipaddress import IPv4Network`
   inlined in test bodies. Move to module-top imports.
3. `config.py:100-109` — `object.__setattr__` for `PrivateAttr` is
   defensive; direct `self._trusted_proxies_parsed = ...` works on
   pydantic v2. Harmless.
4. `config.py:13` — `TrustedProxies` exported from `localmail.config`
   but never imported from there (duplicated identically in
   `client_ip.py` to keep `config.py` independent of `api/`). Either
   prefix with underscore or canonicalize in one place.
5. No explicit `load_config()` round-trip test for `trusted_proxies`
   keys (the e2e test builds `AuthConfig` directly). TOML→pydantic
   path is implicitly covered by other auth fields, so this is a
   theoretical gap.

None blocking. File for a follow-up cleanup PR if the polish is wanted.

### Docs updates this session

- **README.md** — new "Behind a reverse proxy" subsection (three
  CIDR recipes, the `uvicorn --forwarded-allow-ips` non-interaction
  warning); previously-stale "until `auth.trust_proxy_headers`
  lands" tuning blockquote replaced with a pointer to the shipped
  knob.
- **CLAUDE.md** — the "Proxy gotcha" bullet under the login
  rate-limiter section now reads "Reverse-proxy support" and
  describes the shipped design instead of the planned one. Links to
  [docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md].
- **config.example.toml** — three commented recipes appended to the
  `[auth]` block (same-host, private-LAN, Cloudflare) plus
  `trusted_proxies_max_hops`.

## What's next

### 1. Merge PR #73

CI status not yet observable at session end. After review and merge:

```bash
git checkout main
git pull
git branch -d feat/auth-trusted-proxies
git push origin :feat/auth-trusted-proxies
```

The PR's "Test plan" has one unchecked manual smoke item — stand up
nginx on `127.0.0.1` fronting `localmail serve --bind 127.0.0.1
--no-tls`, set `trusted_proxies = ["127.0.0.0/8"]`, drive 4 failed
logins via curl with distinct `X-Forwarded-For` values, confirm none
trips 429; then 4 with the same XFF, confirm the 4th trips with
`cap = "ip"`. Run on a real proxy before approving.

### 2. Pick the next piece

In order of recommendation:

- **#71** Searcher accessor refactor — small, mechanical,
  carried over from PR #70 handoff. `serve/routes/search.py`
  reaches into Searcher private attributes; expose
  `Searcher.pool_meta(token)` accessor and switch the route.
  No behaviour change.
- **#72** `EXPLAIN ANALYZE` of `messages_recent_idx` under the
  `account_id = ANY(...)` ACL filter. No code change expected;
  if the index isn't used, file a covering index instead.
- **PR-73 follow-up cleanup** — bundle the 5 minor polish items
  listed above into one small PR (move the inline imports, drop
  `object.__setattr__` defensive form, decide on `TrustedProxies`
  alias canonicalisation, optionally add a TOML round-trip test).
- **`grow_pool` deep-pagination duplicates on `sort=rank`** —
  carried over from PR #70 handoff. When the cache exhausts past
  pool size 100, `grow_pool` returns page 1 of the enlarged pool,
  surfacing already-seen top hits. `sort=date` covers "show me
  everything" so this is lower priority; revisit if rank-paginated
  duplicates become user-visible.
- **#38** `/v1/changes` semantics decision — now that the GUI's
  initial load goes through `/v1/messages` (PR #70), `/v1/changes`
  is only the delta-fetch path. Worth resolving while the change
  is fresh.
- **Dependabot — 12 vulnerabilities** (1 high / 9 mod / 2 low) on
  `main` still need triage. Independent of this session.
- **#28 / #27 / #24 / #22 / #18 / #17** GUI client polish & CI.
- **#10 / #12** Persist Content-ID on attachments (inline `cid:`).
- **#4 / #2 / #5** Search-perf follow-ups (model paths, CONCURRENT
  GIN build, batch INSERT for chunking).
- **#25** `websockets.legacy` DeprecationWarning — still blocked on
  upstream uvicorn release.
- **#47** Third-party transient classes (deferred follow-up to #36).
  Still gated on real ops data.

## Open decisions & risks

1. **`trusted_proxies = []` default is intentional.** Operators
   running localmail directly on a public IP must NOT enable
   the knob — every attacker would set their own `X-Forwarded-For`.
   If a user reports "rate limit isn't working behind my proxy",
   the diagnostic flow is: (a) is `trusted_proxies` set? (b) does
   it actually contain the proxy's CIDR? Document this in any
   support response.

2. **Same CIDR list governs both admission (socket peer trusted?)
   and peeling (which XFF entries to skip).** Tempting to split
   into two configs for "operators who want different lists" but
   the right-to-left peel is identical to nginx/Caddy convention
   and a split adds surface without solving any real deployment
   shape. Keep them unified; revisit only if a concrete need
   surfaces.

3. **`uvicorn --forwarded-allow-ips` is explicitly non-supported.**
   That flag rewrites `request.client.host` *before* our admission
   check runs, which collapses the security guarantee. README and
   CLAUDE.md both call this out. If a future operator complains
   their `auth.trusted_proxies` is being ignored, check whether
   they're also setting `--forwarded-allow-ips`.

4. **`X-Real-IP` and `Forwarded:` (RFC 7239) are NOT supported.**
   Brainstorming Q2 picked XFF-only on the grounds that all the
   operators we expect to serve (nginx, Caddy, Cloudflare, AWS
   ALB) emit XFF. If a deployment surfaces that genuinely needs
   `X-Real-IP` (e.g., a minimal nginx config), add it then —
   small extension to the resolver, same admission check.

5. **`max_hops=3` default is enough for client → CDN → ALB → app.**
   Validator caps at 10 (sanity). If anyone reports a deeper chain
   in production, bump the upper bound rather than the default;
   the spec's threat model only depends on the admission check, not
   the cap value.

6. **Carried forward from prior sessions (still load-bearing):**
   - Postgres-backed login rate limiter (#7) merged in PR #69 —
     the `trusted_proxies` opt-in (this PR) closes its
     documented proxy gap.
   - PR #70 (`sort=date` keyset, reranker off-by-default) merged;
     the documented `reranker_enabled = false` default still
     stands.
   - The MIME clamp list (#32) is small on purpose — only
     actively-script-executable types.
   - `parse_int_id` (#33) accepts leading zeros (`"01"` → 1).
   - `rrf_k=60` is the centre of a flat plateau (#35).
   - `websockets.legacy` DeprecationWarning (#25) — uvicorn blocker.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch origin

# If PR #73 is still open:
git checkout feat/auth-trusted-proxies
gh pr view 73                              # check CI + review state

# After PR #73 is merged:
git checkout main
git pull
git branch -d feat/auth-trusted-proxies
git push origin :feat/auth-trusted-proxies

# Quick sanity:
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q              # expect 755 passed
unset VIRTUAL_ENV && uv run mypy src/localmail     # 4 pre-existing parser.py errors

# Pick next piece:
gh issue list --state open --limit 40
# Recommended: ship #71 (Searcher accessor refactor) — smallest
# scoped issue carried over from PR #70 review.
```

## Known gotchas (still load-bearing)

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have a stale
  `VIRTUAL_ENV` pointing at some other pyenv venv; `uv run --active`
  picks the wrong interpreter without this.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql:///localmail_test`** —
  tests must not touch the live `localmail` DB.
- **Migrations 0011–0019 are additive.** Re-running `init-db` on an
  older archive is safe (idempotent), but back up first if the archive
  is non-trivial. Next migration would be `0020_*.sql`.
- **Wire `date` = `COALESCE(internal_date, date_sent)`** on every
  paginated list endpoint (carried from PR #70). Guard tests in
  `test_serve_browse_route.py` / `test_serve_search_route.py` /
  `test_serve_changes_route.py` enforce.
- **Two cursor flavours on `/v1/search`** — `"<token>:<page>"` (pool)
  and `"K|<base64>"` (keyset, `sort=date` + non-empty query).
  Route dispatches by prefix; never parse cursors client-side.
- **Page-cache miss → HTTP 409 `/problems/search-cursor-expired`**,
  never 500. The GUI's transparent re-run path expects this exact
  problem type.
- **`reranker_enabled` default = False.** CPU-bound cross-encoder
  rerank fanout overruns timeouts when `grow_pool` doubles the
  pool. Flip in `config.toml` only on GPU hosts.
- **NEW from this session: `auth.trusted_proxies`** must contain
  the proxy's CIDR for the per-IP login cap to read the real
  client. Empty default = unchanged behaviour. Do NOT also set
  `uvicorn --forwarded-allow-ips` — collapses the admission check.
- **NEW: `trusted_proxies` validator fails LOUD at config load**
  on a bad CIDR. `trusted_proxies_max_hops` clamps to `[1, 10]`
  via pydantic Field constraints. Misconfig surfaces at startup,
  never at request time.
- **Login rate limiter (#7, PR #69)** still load-bearing:
  - Caps live in `LocalmailConfig.auth`.
  - `_record_login_attempt` + `_maybe_sweep` commit eagerly. New
    writes added to `login()` between these and a subsequent
    `raise` must wrap their own SAVEPOINT if they must NOT survive
    an outer rollback.
- **TLS for `localmail serve`** — `--bind 0.0.0.0` requires
  `--tls-cert` + `--tls-key`. `--no-tls` is only honoured on
  `127.0.0.1`. Use `localmail rotate-tls` to generate a self-signed
  cert.
- **First-time `body_lang` install** — run `localmail lang-backfill`
  (or `embed-backfill`, which drains both queues) once after upgrade
  so `lang:` queries return rows.
- **ACL upgrade**: post-0016, new API users have **no grants**. Run
  `localmail grant-account USERNAME ACCOUNT_NAME` once per pair.
- **Probe-then-condition boundary** (#62): for any new
  conditional-GET endpoint, the order is
  **ACL+probe → precondition → expensive IO**.
- **Streaming WARNING contract** (#58): any new streaming endpoint
  that advertises a `Content-Length` MUST also count bytes yielded
  and call `_log_truncation()` when the source runs short.
- **ID-typing boundary** (#33): path and query parameters bearing
  entity IDs are typed `str` on the route handler signature, and
  `localmail.api.ids.parse_int_id(value, field="…")` is the ONLY
  way to cast to int.

## File map (as of branch HEAD `d5376d9`)

```
src/localmail/
  api/
    client_ip.py                       # NEW (PR #73): resolve_client_ip,
                                        # _normalise_xff_entry, TrustedProxies alias
    auth.py                            # unchanged — still takes client_ip arg
    acl.py attachments.py browse_cursor.py
    conditional.py errors.py ids.py messages.py
    range_requests.py sanitize.py search.py search_cursor.py
  config.py                            # PR #73: AuthConfig.trusted_proxies +
                                        # trusted_proxies_max_hops + PrivateAttr cache
  search/                              # unchanged from PR #70
  serve/
    routes/
      auth.py                          # PR #73: /login resolves client_ip via resolver
      messages.py search.py changes.py accounts.py
      attachments.py version.py
    app.py middleware.py
  cli.py daemon.py worker.py ...
migrations/                            # 0001 … 0019_api_login_attempts.sql
tests/                                 # 755 passing
  test_api_client_ip.py                # NEW (PR #73): T1-T16 resolver unit tests
  test_api_auth_rate_limiter.py        # +1 e2e: per-IP cap uses XFF when trusted
  test_config.py                       # +6: trusted_proxies validators
  test_api_browse.py test_api_browse_cursor.py
  test_api_search_cursor.py test_api_search_cursor_error.py
  test_api_search_pagination.py
  test_searcher.py
  test_serve_browse_route.py test_serve_search_route.py
  test_serve_changes_route.py
  conftest.py
docs/superpowers/
  specs/2026-05-21-trust-proxy-headers-design.md   # NEW (PR #73)
  plans/2026-05-21-trust-proxy-headers.md          # NEW (PR #73)
docs/handoffs/
  2026-05-21T0553-trusted-proxies-pr-73.md         # this session's snapshot
NEXT_SESSION.md                       # this file (post-session)
gui/                                  # 271 GUI tests passing, unchanged
  src-tauri/
  src/
```

End of `trusted_proxies` session. PR #73 open against `main`
(`d5376d9`). Branch `feat/auth-trusted-proxies` alive on local +
remote until merge. Next: merge #73, then ship #71 (Searcher
accessor refactor) as the next small-scoped follow-up.
