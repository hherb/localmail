# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-06-16.** This session shipped the **access-token family
> containment** feature (the §2 follow-up carried from #186) as **PR #193**
> (open, branch `feat/access-token-family-containment`). Full Python suite
> **1670 passed** (14 deselected), `mypy` clean (121 files). Built via the full
> brainstorm → spec → plan → subagent-driven-development pipeline; every task
> passed a two-stage (spec + code-quality) review plus a final whole-feature
> review.
>
> Prior session's security PRs **#191** (crypto/multipart/starlette) and **#192**
> (httpx2 dev dep) are **merged**; **all Dependabot alerts are now `fixed` or
> `dismissed`** (torch #17 was dismissed). `origin/main` HEAD is `4547e9a`.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The database is canonical for accounts.
Daemon: hot-reload account set, heartbeats, DB command queue, two-plane
supervision, non-blocking lifecycle + admin panel. Admin UI: account CRUD, user
management, archive imports, daemon control. Hybrid search (Phases 1+2) + an
HTTPS GUI server + a remote MCP server + the opt-in `--smart` LLM query rewriter
(Ollama / OpenAI-compat / Anthropic backends) are all shipped. The MCP server
can act as an **OAuth 2.1 authorization server** (opt-in) with sliding
refresh-token rotation, family revocation on reuse, **and (this session) family
containment of the access tokens issued along that chain**. A Tauri 2 + Svelte 5
GUI lives under `gui/`. See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we did this session

### Access-token family containment on refresh-token reuse (PR #193)

Closes the accepted limitation from the #186 refresh-family-revocation design:
when refresh-token **reuse** is detected and the refresh **family** is deleted,
the OAuth AS now **also immediately deletes the access tokens minted within that
same family** from `api_tokens` — instead of leaving them valid at `/mcp` until
their ≤1h TTL. **Family-precise, reuse-only.**

- **Migration `0030_api_tokens_refresh_family.sql`** — nullable
  `api_tokens.oauth_refresh_family_id` (UUID) + partial index
  `WHERE oauth_refresh_family_id IS NOT NULL`. No backfill; login tokens
  (`/v1/auth/login`, NULL family) are structurally immune.
- **Store boundary preserved:** `refresh.py` only touches `oauth_refresh_tokens`
  (reports the family as data via the new `RotateResult.family_id`, set on the
  `reuse` outcome); `access.py` owns `api_tokens` (`mint_access(family_id=…)` +
  new `revoke_access_family(conn, family_id) -> int`); the **provider
  orchestrates both inside one transaction** so the refresh-family DELETE and the
  access-family purge commit atomically. The reuse WARNING gains
  `(access tokens purged=%d)`.
- **No SDK-facing signature change, no wire-shape change, no new config knob.**
  `mint_refresh` deliberately kept its `str` return type (code-exchange reads the
  family via `load_refresh`) to avoid rippling ~12 store-test call sites.
- Reuse-only — normal-rotation predecessors still expire by ≤1h TTL (eager
  revocation would break in-flight requests); explicit RFC 7009 revoke unchanged.

Design: [docs/superpowers/specs/2026-06-16-access-token-family-containment-design.md](docs/superpowers/specs/2026-06-16-access-token-family-containment-design.md)
· Plan: [docs/superpowers/plans/2026-06-16-access-token-family-containment.md](docs/superpowers/plans/2026-06-16-access-token-family-containment.md)

Commits on `feat/access-token-family-containment` (pushed; PR #193):

| SHA | what |
|---|---|
| `70b49c7` | docs(mcp): design — access-token family containment on refresh reuse |
| `df17f77` | docs(mcp): implementation plan |
| `e637ffe` | feat(mcp): migration 0030 — `api_tokens.oauth_refresh_family_id` |
| `f8f631c` | feat(mcp): access store — tag family on mint, `revoke_access_family` |
| `0da3c74` | feat(mcp): refresh store — `RotateResult` carries reuse `family_id` |
| `b49950c` | feat(mcp): purge access-token family on refresh reuse |
| `c757a2a` | docs: CLAUDE.md — closes #186 limitation |
| `2beea4c` | test(mcp): pin family purge across a whole rotation chain on reuse |
| `cf6333e` | docs(readme): note access tokens purged on reuse |

**Verification:** `unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/
--deselect tests/test_daemon_control_socket.py` → **1670 passed, 14 deselected**
(+12 new tests over the 1658 baseline). `uv run mypy src/localmail` → clean,
121 files.

## What's next

### 0. **Merge PR #193** *(immediate — once CI green)*
   Check CI, squash-merge, advance `origin/main`. **Acceptance:** PR #193
   squash-merged; `origin/main` past `4547e9a`; migration `0030` is then live
   on `main` (operators apply it with `localmail init-db`).
```bash
gh pr checks 193
gh pr merge 193 --squash --delete-branch
git checkout main && git fetch --prune origin && git pull
```

### 1. **(Deferred / not filed)** — no queued engineering work remains.
   All prior Dependabot alerts are `fixed`/`dismissed`; the #186 access-token
   containment limitation is now closed by PR #193. Candidate future work, none
   blocking and none with an open issue:
   - **RFC 8707 resource indicators** for the OAuth AS — moot today (single
     resource server; audience restriction adds nothing). Only worth it if a
     second RS is ever added.
   - **httpx2 test migration** — Starlette 1.3.1 emits a TestClient
     `httpx`-deprecation warning; `#192` added `httpx2` to the dev group but the
     tests still use the `httpx` client. Cosmetic; migrate when convenient.
   - **torch #17** alert was *dismissed* (heavy CUDA/ONNX bump); revisit only if
     deliberately doing dep work.

## Open decisions & risks
1. **PR #193 is open, not merged.** First action next session is §0.
2. **Accepted residual (by design, in spec "Out of scope"):** the family DELETE
   correlates access tokens by `oauth_refresh_family_id`. An access token whose
   `api_tokens` row was already GC'd/expired before reuse is simply absent — the
   purge is a no-op for it (correct; `revoke_access_family` returns 0). Reuse
   contains the live access window immediately; nothing lingers.
3. **Concurrency** (carried from #186, unchanged): the claim-lost reuse branch
   (`if not claimed:`) is race-only and not directly unit-tested (hard to
   trigger deterministically); both family sources resolve to the same UUID, so
   risk is low. The happy-path + multi-rotation-chain reuse paths ARE covered.
4. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
   locally on macOS (`AF_UNIX path too long`); deselect locally, Linux CI is the
   real signal. Also carried: psycopg_pool teardown `ResourceWarning`s (seen at
   end of the full run, harmless), the websockets `DeprecationWarning` (GitHub
   issue #25), the Starlette TestClient `httpx` `DeprecationWarning`.
5. **No ROADMAP.md** in this repo *(carried)* — the `/nextsession` ROADMAP step
   is a no-op; slice status lives in NEXT_SESSION/handoffs + the specs.
6. **`.claude/` local files** stay untracked (incl. `.claude/scheduled_tasks.lock`).

## Exact commands to resume
```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # expect clean (only the untracked .claude lock)
git --no-pager log --oneline -5
gh pr list --state open                  # expect PR #193 (merge it — §0)
gh pr view 193
gh pr checks 193

# §0 — merge the feature PR (once CI green):
gh pr merge 193 --squash --delete-branch
git checkout main && git fetch --prune origin && git pull
unset VIRTUAL_ENV && uv run localmail init-db    # applies migration 0030

# Python suite + types (use --extra mcp so the MCP/OAuth tests actually run):
unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/ \
  --deselect tests/test_daemon_control_socket.py            # expect 1670 passed
unset VIRTUAL_ENV && uv run mypy src/localmail              # expect clean, 121 files

# gui frontend checks (run inside gui/, unchanged this session):
cd gui && npm ci && npm run check && npm test && npm run build && cd ..
```

`origin/main` at `4547e9a`; feature branch `feat/access-token-family-containment`
is PR #193. Latest migration `0030_api_tokens_refresh_family.sql`; next free
slot `0031_*.sql`.
