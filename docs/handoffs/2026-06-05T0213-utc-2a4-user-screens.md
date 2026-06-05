# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-06-05T0213-utc (2A.4 — /admin/users management screens, PR open).**
> This session designed, planned, and implemented **Sub-plan 2A.4**: the
> `/admin/users` admin screens + a JSON `/v1/admin/users` router for managing API
> users (CRUD, per-account ACL grants, admin toggle, session revocation, password
> reset, enable/disable) with last-admin + self-action lock-out guards. Built
> TDD via subagent-driven development (implementer + spec + code-quality review
> per task). Work is on branch `admin-ui-2a4-user-screens`, pushed and open as
> **PR #160** (https://github.com/hherb/localmail/pull/160, "Closes the
> /admin/users 404"), **CI pending at handoff time**. `main` is at `9a458eb`
> (not yet merged). **Local: 1366 passed, mypy clean (90 files), ruff clean. No
> new migration.**
>
> **Also at session start:** confirmed the prior handoff's "immediate" task was
> already done — **PR #159 (#158 test-connection errors) was already merged** into
> `main` (`9a458eb`); the two stale local branches
> (`admin-2a3-test-connection-errors`, `admin-ui-2a3-account-screens`) were
> squash-merged and have been deleted locally.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The **database is canonical for accounts**
end-to-end. Daemon: hot-reload account set (2B.1), heartbeats (2B.2), DB command
queue + LISTEN/NOTIFY (2B.3), two-plane supervision (2B.4), non-blocking
lifecycle + admin panel (2B.5). Admin UI: account CRUD (2A.3) + **user
management (2A.4, this session)**. Hybrid search (Phases 1+2) + an HTTPS GUI
server are shipped. A Tauri + Svelte GUI lives under `gui/`. See
[CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we did this session

### 2A.4 — `/admin/users` management screens (branch `admin-ui-2a4-user-screens`)

Closes the `/admin/users` 404 (the nav link existed but 404'd). Mirrors the 2A.3
account-CRUD screens: HTML panel **and** JSON router over one service layer. Full
op parity with the CLI (`add-api-user`, `grant-account`, `grant-admin`, …) plus
password reset + enable/disable.

Design → spec → plan → TDD implementation:
- Design: `docs/superpowers/specs/2026-06-05-admin-users-screens-design.md` (`d22837a`)
- Plan: `docs/superpowers/plans/2026-06-05-admin-users-screens.md` (`f7a04f5`)

**Architecture (approach B):** a transport-free service module
[`api/admin/users.py`](src/localmail/api/admin/users.py) composes the existing
primitives (`api/auth.py`, `api/acl.py`, `api/admin/auth.py`) and adds CRUD +
guards. Two thin routers share it. Pure form logic in
[`serve/admin/user_forms.py`](src/localmail/serve/admin/user_forms.py).

**Lock-out guards (the safety-critical bit):**
- **Count-based last-admin** rule in the SERVICE: pure `would_orphan_last_admin`
  predicate + an IO wrapper reading `count(*) WHERE is_admin IS TRUE AND
  disabled_at IS NULL` → `LastAdminError`.
- **Identity-based self-action** rule (no self-demote, no self-delete) in the
  ROUTERS (`uid == admin.id`). Both → **409**; validation → **400**.
- The edit screen also renders unsafe controls `disabled` via `action_flags`
  (UX only; the service/router guards are the real enforcement).

**Implementation commits (TDD, each spec- + quality-reviewed):**
- `7a5214e` service skeleton: dataclasses/errors + `list_users`/`get_user`
- `be0023d` test helper: real `disabled_at` timestamp
- `4eb7fbf` `class_row` for `get_user`; drop unused import; zero-account test
- `781ac37` `create_user` + admin `set_password`
- `6e4086f` last-admin guard + `set_admin`/`set_disabled`/`delete_user`
- `4e117cb` `set_grant`, `revoke_sessions`, `action_flags`
- `88793ee` pure `user_forms` module
- `fe58f62` JSON `/v1/admin/users` router + app wiring
- `7b50d5b` JSON router: cover 404/non-digit/password/revoke-sessions; fix docstring
- `a2af3ff` HTML `/admin/users` screens + 9 templates + static
- `31800c2` docs: CLAUDE.md 2A.4 bullet + README user-panel paragraph

**Files (1675 insertions, all under 500 lines, no migration):**
```
src/localmail/api/admin/users.py                     291  # service layer + pure guards
src/localmail/serve/admin/user_forms.py               55  # pure form parse + error map
src/localmail/serve/admin/users_router.py            214  # JSON /v1/admin/users
src/localmail/serve/admin/users_panel_router.py      286  # HTML /admin/users
src/localmail/serve/admin/templates/users/*.html       9 templates
src/localmail/serve/admin/static/users-panel.js        3  # placeholder (CSP script-src 'self')
src/localmail/serve/app.py                            +4  # 2 imports + 2 includes
tests/test_api_admin_users.py                        258  # service (real DB)
tests/test_user_forms.py                              69  # pure
tests/test_serve_admin_users.py                      228  # JSON routes
tests/test_serve_admin_user_screens.py               139  # HTML screens
```

### Verification (this session)
```bash
unset VIRTUAL_ENV && uv run pytest -q tests/      # 1366 passed (was 1295; +71 new)
unset VIRTUAL_ENV && uv run mypy src/localmail    # clean, 90 files (was 86; +4 modules)
unset VIRTUAL_ENV && uv run ruff check src/localmail tests   # clean
```
The recurring psycopg pool `__del__` ResourceWarnings at teardown are **not**
failures (carried note). The mypy `cli.py:596` `annotation-unchecked` note is
pre-existing and unrelated.

## What's next

### 0. **Merge PR #160 for 2A.4** *(immediate)*
Pushed and open as **PR #160**; CI was pending at handoff time.
```bash
gh pr checks 160                          # let CI finish
gh pr merge 160 --squash --delete-branch  # closes the /admin/users 404
git checkout main && git pull
```

### 1. **The last admin nav link — `/admin/imports`** *(after #160 — brainstorm → spec → plan FIRST)*
With 2A.4 merged, `/admin/users` resolves; the only remaining 404 nav link is:
- **`/admin/imports`** — archive/mbox import UI. **Scope is TBD** — needs real
  brainstorming. There IS an `auth_method = 'archive'` account type and the
  `extract-backfill` machinery, but no import-from-file flow yet. Acceptance
  criteria to define at design time: what file formats (mbox? maildir? .eml?),
  whether it ties to an `archive` account, progress reporting, idempotency/dedup
  (the existing `raw_sha256` + content-addressable attachments already dedup, so
  re-import should be safe), and failure handling (poison-pill rows).

### 2. **Optional follow-ups on 2A.4** *(low priority, filed-as-notes only)*
- `create_user` route does `next(r for r in list_users(...) if r.id == uid)` —
  an O(n) read-your-writes lookup. Harmless for small rosters; if a deployment
  ever has hundreds of users, add a `get_user_summary(conn, id)` single-row
  service query. (Noted inline in `users_router.py`.)
- `set_grant`'s FK→`UserFieldError` defensive branch is untested (the UI only
  ever sends existing account ids). Add a 3-line service test if desired.
- The `username.strip()` canonicalisation in `create_user` is untested.

### 3. **Remaining open issues** *(both blocked — not actionable)*
- **#90** (glib via Tauri Rust stack) — Dependabot alert dismissed; bump
  upstream-blocked by Tauri pinning `gtk=^0.18`. Close or leave parked.
- **#25** (websockets/uvicorn depwarn) — not actionable until uvicorn ships an
  upstream release on `websockets.asyncio`.

## Open decisions & risks
1. **PR #160 open, not yet merged** — `main` is at `9a458eb`. The branch is
   pushed (HEAD `31800c2`) and CI was pending at handoff. First action next
   session: confirm CI green + merge (see "What's next §0").
2. **Self-demote is blocked even when other admins exist** (`SelfActionError`,
   matching self-delete). Intentional — an admin demoting themselves is almost
   always a mistake; another admin can demote them. Self-DISABLE is NOT blocked
   (only the last-admin guard catches the dangerous case) — documented in the
   spec's risk list; revisit only if operators report friction.
3. **"Last admin" = active admins only** (`is_admin IS TRUE AND disabled_at IS
   NULL`). A *disabled* admin does not protect against lock-out; re-enabling
   needs CLI/DB access. Acceptable for v1.
4. **`is_admin` is NOT NULL** (migration `0021_api_users_admin.sql`), contrary to
   an earlier assumption that it was nullable (0022). `is_admin IS TRUE` is used
   throughout anyway — correct and safe for both nullable and non-nullable.
5. **No new migration this session.** Latest applied is **0025**
   (`transient_extractions`). Next free slot `0026_*.sql`. Re-check
   `ls migrations/` at plan-time.
6. **No ROADMAP.md** in this repo *(carried)* — slice status lives in
   NEXT_SESSION/handoffs + the specs. The `/nextsession` ROADMAP step is a no-op
   here by design.
7. **Heartbeat vocabulary still load-bearing** *(carried)* — any new heartbeat
   call site must use a `worker_kind`/`state` present in both the SQL CHECK lists
   (0023) and the `WorkerKind`/`WorkerState` Literals; all loop heartbeats go
   through `safe_heartbeat`.
8. **GUI test noise** *(carried)* — `npm test` prints `HTMLCanvasElement.getContext`
   stderr from the PDF preview (jsdom has no canvas). Pre-existing; not a failure.
9. **`.claude/` + `.superpowers/` local files** stay untracked, by design.

## Exact commands to resume
```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # on admin-ui-2a4-user-screens, clean
git branch -vv                           # main (9a458eb) + admin-ui-2a4-user-screens (31800c2)
git --no-pager log --oneline -8
gh pr list --state open                  # #160 (2A.4)
gh pr checks 160                          # CI status before merging
gh issue list --state open --limit 40    # #90, #25 (both blocked)

# Confirm the suite (the live, canonical signal this session):
unset VIRTUAL_ENV && uv run pytest -q tests/    # expect 1366 passed
unset VIRTUAL_ENV && uv run mypy src/localmail  # expect clean, 90 files
```

After PR #160 merges, pick the next work (brainstorm → spec → plan FIRST):
```bash
git checkout main && git pull
git checkout -b admin-ui-2a5-imports          # /admin/imports (scope TBD — brainstorm first)
ls migrations/    # latest is 0025; next free slot 0026_*.sql
```

`main` at `9a458eb` (== `origin/main`). Branch `admin-ui-2a4-user-screens`
pushed (HEAD `31800c2`), open as **PR #160**. Working tree clean (only
`.claude/` + `.superpowers/` local files). **No migration added this session.**
