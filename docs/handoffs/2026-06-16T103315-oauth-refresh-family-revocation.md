# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-06-16 (OAuth refresh-token family revocation — #183/#185).**
> Last session's PR #184 (OAuth AS hardening M1/M2/M3) is **merged** — `origin/main`
> is at `ed74ea7`. This session implemented **#183** (RFC 9700 §4.14.2 refresh-token
> family revocation on detected reuse), folding in **#185** (an index), as **PR #186**
> (open, branch pushed). Full suite **1638 passed, 0 failures** (was 1626 baseline;
> +12 tests); `mypy` clean (120 files). One migration (`0029`), no new dependency,
> AS-off path unchanged.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The database is canonical for accounts.
Daemon: hot-reload account set, heartbeats, DB command queue, two-plane
supervision, non-blocking lifecycle + admin panel. Admin UI: account CRUD, user
management, archive imports, daemon control. Hybrid search (Phases 1+2) + an
HTTPS GUI server + a remote MCP server + the opt-in `--smart` LLM query rewriter
are all shipped. The MCP server can act as an **OAuth 2.1 authorization server**
(opt-in) with sliding refresh-token rotation **and now family revocation on
reuse**. A Tauri + Svelte GUI lives under `gui/`. See [CLAUDE.md](CLAUDE.md),
[README.md](README.md).

## What we did this session

### OAuth refresh-token family revocation — #183 + #185 (PR #186, pushed)

Rotation no longer hard-deletes the presented refresh token. It **tombstones**
the token (`consumed_at`) and mints a successor in the **same `family_id`**.
Replaying an already-consumed token is treated as reuse (a stolen-copy signal,
RFC 9700 §4.14.2) → the **whole family** is `DELETE`d and the exchange returns
`invalid_grant`. An absent/expired/disabled-user token is `unknown` (never nukes
the family — preserves the M1 disabled-user containment). One migration; no new
dependency; default-off AS path byte-for-byte unchanged.

- **Schema (`0029_oauth_refresh_token_family.sql`):** `oauth_refresh_tokens`
  gains `family_id UUID NOT NULL DEFAULT gen_random_uuid()` (existing rows become
  singleton families) + `consumed_at TIMESTAMPTZ` (NULL = live, set = tombstone);
  indexes on `family_id` and `client_id` (the latter is **#185**, serving
  `cleanup_unused`'s correlated `NOT EXISTS`).
- **Store (`refresh.py`):** `rotate_refresh` returns a `RotateResult(outcome,
  new_token)` enum (`rotated` / `reuse` / `unknown`). `load_refresh` filters
  `consumed_at IS NULL`; new `sweep_consumed` GCs tombstones past their own
  `expires_at` (opportunistic, on the rotation path). The tombstone UPDATE is
  guarded `AND consumed_at IS NULL` + `rowcount == 1` so two concurrent rotations
  can't both mint a successor (the loser → reuse → family revoked).
- **Clients (`clients.py`):** `cleanup_unused` live-token guard gained
  `AND r.consumed_at IS NULL` so a not-yet-expired tombstone can't keep an
  abandoned client alive (the M2 interaction).
- **Provider (`provider.py`):** `_exchange_refresh_sync` switches on the outcome
  — `reuse` commits the family DELETE, logs a WARNING (no token leakage), raises
  `invalid_grant`; `unknown` rolls back and raises.

Commits on `feat/oauth-refresh-family-revocation` (pushed; PR #186):

| SHA | what |
|---|---|
| `3bc2958` | docs: design spec |
| `524ec27` | docs: implementation plan |
| `545a965` | feat: migration 0029 — family_id + consumed_at + indexes (#183, #185) |
| `eab3236` | feat: family-aware refresh rotation with reuse detection (#183) |
| `583d9f8` | fix: cleanup_unused ignores consumed refresh tombstones (#183) |
| `18e1563` | feat: provider revokes refresh family on reuse (#183) |
| `4aae328` | docs: record refresh-token family revocation shipped (#183, #185) |
| `8461a31` | fix: guard rotation UPDATE against concurrent double-successor (#183) |

**Verification:** `unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/
--deselect tests/test_daemon_control_socket.py` → **1638 passed, 14 deselected,
0 failures**; `uv run mypy src/localmail` clean (120 files). Built with
subagent-driven TDD (5 tasks, per-task spec + code-quality review). An Opus
**final holistic review** found one substantive issue — the rotation UPDATE
lacked a `consumed_at IS NULL` guard, so two concurrent legitimate rotations
under READ COMMITTED could mint **two** live successors — fixed in `8461a31`
(the loser now detects the concurrent consume as reuse and revokes the family).

## What's next

### 0. **Merge PR #186** *(immediate)*
   `gh pr checks 186` → squash-merge once CI is green, then advance `origin/main`.
   At push time the `pytest (PG pg18, Python 3.12)` check was **pending**.
   **Acceptance:** CI green; PR #186 squash-merged (closes #183 and #185);
   `origin/main` past `ed74ea7`. After merge the OAuth security backlog is clear
   except the two items below.

### 1. **(Optional follow-up) Access-token family containment**
   Documented accepted limitation: the family DELETE revokes refresh tokens only;
   access tokens already minted along the chain live in `api_tokens` with no
   `family_id` correlation, so they stay valid at `/mcp` until their ≤1h TTL
   (`oauth_access_token_ttl_s`). Reuse contains the 30-day refresh window at once;
   the ≤1h access window is bounded by expiry, not revoked. If instant access
   containment is ever wanted: add a `family_id` (or `oauth_client_id`-scoped
   correlation) to `api_tokens` and join it into the reuse DELETE — a schema
   change (migration `0030_*.sql`). Needs its own brainstorm → spec → plan; no
   issue filed yet. **Low priority** (1h bound is standard AS behaviour).

### 2. **Rewriter-backend follow-up §2b (low priority)**
   Cloud / non-Ollama `rewriter_backend` (still hard-`"ollama"`). Own brainstorm →
   spec → plan (backend abstraction, config surface, credentials). Defer.

### 3. **Upstream-blocked (not actionable)** — **#90** (glib/Tauri bump),
   **#25** (websockets/uvicorn depwarn).

## Open decisions & risks
1. **PR #186 is open, not merged.** First action next session is §0 (merge).
   Working tree otherwise clean (only the untracked `.claude/scheduled_tasks.lock`).
2. **Concurrent-rotation regression is not unit-tested deterministically.** The
   `8461a31` guard is correct (verified by reasoning + the row-lock semantics
   note in `refresh.py`), but the true mid-flight race (two txns passing the
   pre-check before either UPDATEs) can only be reached under real concurrency;
   a deterministic test would need a transaction seam to pause one rotation
   between its pre-check and its UPDATE. Single-threaded tests confirm the happy
   path still claims the row (`rowcount == 1`). If you want a regression test,
   add the seam or accept a (potentially flaky) threaded test asserting the
   "≤1 live successor per family after concurrent rotation" invariant.
3. **Access-token TTL gap** — see §1 above; documented in the spec + CLAUDE.md as
   an accepted limitation, not a defect.
4. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails locally
   on macOS (`AF_UNIX path too long`); deselect locally, Linux CI is the real
   signal. Also carried: psycopg_pool teardown `ResourceWarning`s, the websockets
   `DeprecationWarning` (#25), the Starlette TestClient httpx `DeprecationWarning`.
5. **No ROADMAP.md** in this repo *(carried)* — the `/nextsession` ROADMAP step is
   a no-op; slice status lives in NEXT_SESSION/handoffs + the specs.
6. **`.claude/` local files** stay untracked (incl. `.claude/scheduled_tasks.lock`).

## Exact commands to resume
```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # expect clean (only the untracked .claude lock)
git --no-pager log --oneline -5
gh pr list --state open                  # expect PR #186 (merge it — §0)
gh pr view 186
gh issue list --state open --limit 40    # #185 closes with #186; #90, #25 upstream-blocked

# §0 — merge the open feature PR once CI is green:
gh pr checks 186
gh pr merge 186 --squash --delete-branch
git checkout main && git fetch --prune origin && git pull

# Suite + types (use --extra mcp so the MCP/OAuth tests actually run):
unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/ \
  --deselect tests/test_daemon_control_socket.py            # expect ~1638 passed
unset VIRTUAL_ENV && uv run mypy src/localmail              # expect clean, 120 files
```

`origin/main` at `ed74ea7`; feature branch `feat/oauth-refresh-family-revocation`
is PR #186. Latest migration `0029_oauth_refresh_token_family.sql`; next free slot
`0030_*.sql`.
