# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-06-04T1135-utc (#158 — friendly test-connection failures, committed on a branch).**
> This session fixed **#158** (genuine IMAP connect failures surfaced as 500
> instead of a friendly inline error). TDD, 10 new parametrized tests. Work is on
> branch `admin-2a3-test-connection-errors` (1 commit `bf5b6ab`), **not yet pushed
> or PR'd** — `main` is at `50754c6`. **Local: 1295 passed, mypy clean.**
>
> **Also at session start:** confirmed the prior handoff's "immediate" task was
> already done — **PR #157 (2A.3 account admin screens) was already merged** into
> `main` (commit `50754c6`); stale branch `admin-ui-2a3-account-screens` deleted on
> the remote. The §1 follow-up the prior handoff recommended was filed as
> **issue #158** and is what this session closes (on merge).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The **database is canonical for accounts**
end-to-end. Daemon: hot-reload account set (2B.1), heartbeats (2B.2), DB command
queue + LISTEN/NOTIFY (2B.3), two-plane supervision (2B.4), non-blocking
lifecycle + admin panel (2B.5). Account CRUD admin screens (2A.3) shipped.
Hybrid search (Phases 1+2) + an HTTPS GUI server are shipped. A Tauri + Svelte
GUI lives under `gui/`. See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we did this session

### #158 — friendly test-connection failures (branch `admin-2a3-test-connection-errors`, commit `bf5b6ab`)

A *genuine* IMAP connect failure (wrong host/port/password, DNS, TLS) raises
`OSError` / `imaplib.IMAP4.error` / `imapclient.exceptions.IMAPClientError`,
which escaped both `probe_connection`'s narrow `except RuntimeError` and the
routes' `except AccountFieldError` — surfacing as a **500**, undercutting the
whole point of "Test connection".

Fix (TDD, broadened **at the transport routes**, not in the service):
- New classification tuple `accounts.CONNECT_FAILURE_EXC_TYPES` next to
  `probe_connection`, naming exactly those types. Documented that the service
  deliberately does **not** catch them (its contract is to *raise* on connect
  failure — the broadening is a transport concern).
- **HTML route** (`accounts_panel_router.py::test_connection`) renders the
  `_test_result.html` error fragment (HTTP 200, `ctx["error"]`).
- **JSON `/v1` route** (`accounts_router.py::test_connection`) — **explicit
  decision: mirror it** as a clean **400** with the error detail (uniform with
  the existing `AccountFieldError → 400` mapping; machine clients get a
  structured, actionable error rather than an opaque 500).
- `probe_connection`'s builtin transient-classification narrowness is untouched.

Tests: 10 new parametrized cases (5 exception types × 2 routes) in
`tests/test_serve_admin_account_screens.py` (HTML, asserts 200 + inline error
fragment) and `tests/test_serve_admin_accounts.py` (JSON, asserts 400 + detail).

Docs: CLAUDE.md 2A.3 "Known follow-up" note replaced with a resolved-#158
bullet; README `/admin/accounts` test-connection sentence notes inline failures.

### Verification (this session)
```bash
unset VIRTUAL_ENV && uv run pytest -q tests/      # 1295 passed (was 1285; +10 new)
unset VIRTUAL_ENV && uv run mypy src/localmail    # clean, 86 files
```
The recurring psycopg pool `__del__` ResourceWarnings at teardown are **not**
failures (carried note). The mypy `cli.py:596` `annotation-unchecked` note is
pre-existing and unrelated.

## What's next

### 0. **Push branch + open PR for #158** *(immediate)*
The fix is committed locally only — not pushed, no PR yet.
```bash
git push -u origin admin-2a3-test-connection-errors
gh pr create --fill   # body should say "Closes #158"
gh pr checks <N>      # let CI finish
gh pr merge <N> --squash --delete-branch
git checkout main && git pull
```
After merge, **open issues: 2** — #90, #25 (both blocked, see below). #158 closes with this PR.

### 1. **Next real feature** *(after #158 merges — brainstorm → spec → plan FIRST)*
The other two admin nav links still 404:
- **`/admin/users`** — API-user management + per-account ACL grants. The service
  layer partially exists (`api_users`, `api_tokens`, `user_accounts`; CLI
  `add-api-user` / `grant-account` / `revoke-account`). Natural follow-on
  sub-plan. Likely the higher-value of the two.
- **`/admin/imports`** — archive/mbox import UI. Less defined; scope TBD.

### 2. **Remaining open issues** *(both blocked — not actionable)*
- **#90** (glib via Tauri Rust stack, medium) — Dependabot alert **#3 already
  dismissed** (`not_used`, zero call sites; bump upstream-blocked by Tauri pinning
  `gtk=^0.18`). Close #90 or leave parked until a Tauri release lifts the pin.
- **#25** (websockets/uvicorn depwarn) — not actionable until uvicorn ships an
  upstream release on `websockets.asyncio`; only a `filterwarnings` band-aid now.

## Open decisions & risks
1. **#158 branch not pushed / no PR yet** — `main` is at `50754c6`. Commit
   `bf5b6ab` lives only on local branch `admin-2a3-test-connection-errors`.
   First action next session: push + PR (see "What's next §0").
2. **JSON `/v1` route mirrors the 400** — the explicit decision #158 asked for.
   Rationale: uniform contract across both transports; structured error beats an
   opaque 500. If a downstream machine consumer ever needs to distinguish a
   *config* 400 (validation) from an *operational* 400 (connect failure), that's
   a future refinement (a distinct problem-type), not a regression.
3. **Broadening is at the route, not the service** — `probe_connection` still
   raises on connect failure by contract; `CONNECT_FAILURE_EXC_TYPES` is the
   single shared classification both routers import. Do **not** move the catch
   into the service (it would swallow the very signal probe is meant to surface).
4. **No new migration this session.** Latest applied is **0025**
   (`transient_extractions`). Next free slot `0026_*.sql`. Re-check
   `ls migrations/` at plan-time.
5. **No ROADMAP.md** in this repo *(carried)* — slice status lives in
   NEXT_SESSION/handoffs + the specs. The `/nextsession` ROADMAP step is a no-op
   here by design.
6. **Heartbeat vocabulary still load-bearing** *(carried)* — any new heartbeat
   call site must use a `worker_kind`/`state` present in both the SQL CHECK lists
   (0023) and the `WorkerKind`/`WorkerState` Literals; all loop heartbeats go
   through `safe_heartbeat`.
7. **GUI test noise** *(carried)* — `npm test` prints `HTMLCanvasElement.getContext`
   stderr from the PDF preview (jsdom has no canvas). Pre-existing; not a failure.
8. **`.claude/` + `.superpowers/` local files** stay untracked, by design
   (`.superpowers/` is already gitignored).

## Exact commands to resume
```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # on admin-2a3-test-connection-errors, clean
git branch -vv                           # main (50754c6) + admin-2a3-test-connection-errors (bf5b6ab)
git --no-pager log --oneline -6
gh pr list --state open                  # none yet — push + PR #158 first
gh issue list --state open --limit 40    # #158 (this work), #90, #25

# Confirm the suite (the live, canonical signal this session):
unset VIRTUAL_ENV && uv run pytest -q tests/    # expect 1295 passed
unset VIRTUAL_ENV && uv run mypy src/localmail  # expect clean, 86 files
```

After PR #158 merges, pick the next work (brainstorm → spec → plan FIRST):
```bash
git checkout main && git pull
git checkout -b admin-ui-2a4-user-screens          # /admin/users (recommended) or /admin/imports
ls migrations/    # latest is 0025; next free slot 0026_*.sql
```

## File map (this session — all on branch `admin-2a3-test-connection-errors`)
```
NEXT_SESSION.md                                              # REPLACED this session
docs/handoffs/2026-06-04T1135-utc-158-test-connection-errors.md  # frozen snapshot of this file

# backend
src/localmail/api/admin/accounts.py                         # + CONNECT_FAILURE_EXC_TYPES
src/localmail/serve/admin/accounts_panel_router.py          # HTML route: connect failure -> 200 fragment
src/localmail/serve/admin/accounts_router.py                # JSON route: connect failure -> 400

# tests
tests/test_serve_admin_account_screens.py                   # +5 HTML hard-failure cases
tests/test_serve_admin_accounts.py                          # +5 JSON hard-failure cases

# docs
README.md                                                   # /admin/accounts test-connection sentence
CLAUDE.md                                                   # 2A.3 follow-up -> resolved-#158 bullet
```

`main` at `50754c6` (== `origin/main`). Branch `admin-2a3-test-connection-errors`
committed locally (HEAD `bf5b6ab`), **not pushed**. Working tree clean (only
`.claude/` + `.superpowers/` local files). **No migration added this session.**
