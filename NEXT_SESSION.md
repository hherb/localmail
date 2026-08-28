# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-28 (session 35).** `main` was **`815e74b`** at the
> start — PR #333 had already been merged by the operator. This session opened
> **one PR (#336)** on `fix/test-db-concurrent-session-guard` — 4 commits,
> `d4412dd` (the fix), `56fb3df` / `83d2053` / `2fac3ae` (handoff + docs) —
> closing **#335** and **#329**, and closed **#323** and **#326** by hand.
>
> **The previous handoff was accurate on substance and wrong on two facts**,
> both caught by opening with `git`/`gh` (risk 2, now four-times earned):
> #333 had merged; **#323/#326 were still OPEN**, because the merge commit
> *named* them (`(#323, #326)`) without a closing keyword. GitHub only
> auto-closes on `Closes/Fixes #N`. **Check that the issues actually closed
> after a merge — naming them in the subject does nothing.**
>
> **The DGX had already moved itself to `815e74b`** before this session pulled
> — the second time it has done so between handoffs. Risk 4 stands: verify,
> never infer.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The database is canonical for accounts.
Daemon: hot-reload account set, heartbeats, DB command queue, two-plane
supervision. Web admin UI (HTMX): account CRUD, user management, archive
imports, daemon control, API keys. Hybrid search (Phases 1+2) + an HTTPS GUI
server + a remote MCP server (optionally a full OAuth 2.1 authorization server)
+ the opt-in `--smart` LLM query rewriter are all shipped. A Tauri 2 + Svelte 5
GUI lives under `gui/` — read-only viewer plus an admin mode (Accounts + Daemon
panels shipped; Users + Imports still placeholders). Version **0.3.0**, Python
**3.13** pinned (CI matrixes 3.12 + 3.13). Licensed AGPL-3.0-or-later (per-file
SPDX headers in `src/localmail/`; **not** in `gui/`).

## What we shipped this session

### Housekeeping first (the previous handoff's item 0)

- **Closed #323 and #326 by hand** — verified the code was on `main` first
  (`date_keyset.py`, `keyset_walk.py`, `sort_axes.py`, `KeysetCursor.walk`,
  the four-prefix table), then closed each with the measurement/rule summary.
- **#323 acceptance verified on the live 128k archive.** A mid-walk descending
  keyset page at offset 64,000 EXPLAINs as
  `Index Cond: (ROW(COALESCE(internal_date, date_sent), id) < ROW(...))` —
  **0.296 ms, 46 buffers**, no `Filter`.
- **Both hosts current at `815e74b`.** DGX was already there; synced extras and
  restarted anyway (the sqlparse bump). `/v1/version` → `build_hash 815e74b`,
  `build_source git_checkout`, no `-dirty`. All three units active.
- **Dependabot: 0 open alerts.** **Open issues: 24.**

### `d4412dd` — #335/#329: one pytest session at a time per test database

**The issue named the wrong mechanism, and correcting it is most of the
value.** #335 attributed the corruption to `TRUNCATE` *blocking* on a
connection left open by a previous test's `open_pool`, and proposed a
per-worker database.

Measured instead, with a `lock_timeout` armed on the truncate and a
`pg_stat_activity` dump on any block:

| probe | result |
|---|---|
| 3 full-suite runs + 7 targeted runs, truncate instrumented | **zero** blocked truncates |
| `test_api_search_cursor_walk.py` + `test_searcher_sort_order_walk.py`, 3 runs alone | **0 failures** |
| the 14-file search set, 4 runs alone | **0 failures** |
| **one concurrent pytest process**, same database | **failed on the first attempt** — 3 foreground + 2 background, and the foreground three are *exactly* the tests #335 names |

So the truncate that hurts is the one that **succeeds**: two sessions delete
each other's seeded rows and seed into each other's queries. Nothing errors,
which is why it reads as a product bug ("48 rows where 9 were seeded").

The per-worker database it proposed is also **not buildable by the test role**:
that role has `CREATEDB` but not superuser, and migration `0001` needs
`CREATE EXTENSION vector`. Verified, not assumed.

**The fix** is a session-level Postgres advisory lock keyed on the database
name, taken in `db_dsn` *before* `apply_migrations` (so two sessions cannot
race the migration runner either). New pure module
[tests/_db_session_lock.py](tests/_db_session_lock.py); 23 tests in
[tests/test_db_session_lock.py](tests/test_db_session_lock.py).

- **Advisory lock, not a row in a table** — it dies with its backend, so a run
  killed with SIGKILL releases it instead of wedging every later run.
- **Keyed per database**, so a session pointed at its own `LOCALMAIL_TEST_DSN`
  never blocks. That is the escape hatch for genuinely wanting two suites at
  once, and it is why serialising is not a parallelism regression: two sessions
  sharing this database were never running concurrently in any useful sense.
- **The key is a blake2b digest, never `hash()`.** `hash()` is salted per
  process, so two sessions would derive *different* keys, both acquire, and the
  guard would exclude nothing — with every unit test still green. That is the
  one silent failure mode, so it is pinned **across processes**
  (`test_the_key_is_stable_ACROSS_processes` spawns a subprocess). **The
  same-process assertion beside it is satisfied by the broken version** — the
  mutation proves only the cross-process test catches it.
- **The wait is announced**, once, through pytest's terminal reporter:
  fixture-setup output is captured, so a plain `print` is invisible for exactly
  as long as the wait lasts, which is the window where silence reads as a hang.
  Timeout `DEFAULT_LOCK_TIMEOUT_S` 600 s, overridable via
  `LOCALMAIL_TEST_DB_LOCK_TIMEOUT_S`.
- **The exclusion tests run against the `postgres` maintenance database**, not
  `localmail_test`: the live session holds that lock for the whole run — that
  *is* the fix — so a test cannot acquire the same key to prove anything about
  it. Verified the `localmail` role can reach `postgres` on the exact
  `pgvector/pgvector:pg18` image CI uses, so the fixture's skip branch does not
  fire there and the `0 skipped` invariant holds.
- `DatabaseSessionBusy`, **not** `TestDatabaseBusy` — pytest collects any
  module-level `Test*` class and warns it cannot. Same call as
  `probe_connection`.
- **Adding `pytest-xdist` needs this module changed first** (recorded in both
  the module docstring and CLAUDE.md). It is not a dependency today; each
  worker is its own process, so under one shared DSN exactly one would acquire
  and the rest would block then fail — which reads as the guard being broken.
  Per-worker DSNs are the answer, and they need a database the test role can
  **create**, which it cannot for a fresh one (no superuser, `CREATE EXTENSION
  vector`).

### Verification (this Mac, all extras)

- **Both refs measured in this session** (risk 5): `main` **2951** collected →
  branch **2974** (+23, the new test file).
- `uv run pytest -q` → **2974 passed, 0 failed, 0 skipped** (181 s).
- **The acceptance experiment**: the concurrent-session run that previously
  produced 5 failures is now green — the second session prints its waiting
  notice, takes **9.9 s** where an uncontended run takes ~2 s (proving it
  actually serialised), and both sessions pass (38 + 155).
- **20 consecutive runs** of the #329 file combination → **0/20 with failures**.
- `mypy src/localmail` → Success, **152** files. `mypy tests/_db_session_lock.py`
  → Success.
- `ruff check src/localmail/` → **10**, the unchanged #285 baseline. New files
  clean.
- **Five mutations, each caught by its intended test**: salted `hash()` → only
  the cross-process pin; conftest takes no lock → the wiring pin; `xact` lock
  instead of session lock → 5 tests; one key for every database → the
  per-database pin; `on_wait` never called → the waiting pin.
- GUI untouched (`git diff --name-only main...HEAD | grep ^gui/` empty).

### Found, filed not fixed — the `ConnectionPool.__del__` warning IS #321

Every full-suite run emits `PytestUnraisableExceptionWarning: Exception ignored
in ConnectionPool.__del__ … RuntimeError: cannot join current thread`. That is
the GC finalising a pool **that was never closed** (psycopg's `__del__` tries
to join the pool's own worker thread from inside it). Pytest names the five
culprits exactly:

`test_serve_admin_csp.py`, `test_serve_admin_login.py`,
`test_serve_daemon_wiring.py`, `test_serve_version_route.py`,
`test_session_cookie_scope.py` — each builds `create_app(db_dsn=…)` inline and
never closes its pool. **This is the observable evidence for #321**, and it is
a much better starting point than that issue currently has. It is **not** the
cause of the corruption above (the instrumented runs showed zero contention).

## What's next

### 0. **Merge PR #336, then check the issues actually closed**
   **You merge** (project convention). The body uses `Closes #335` /
   `Closes #329`, so they should auto-close — **verify, because #333 did not**.
   - **No host action needed**: the change is test-only. No migration, no
     dependency change, so neither host needs a pull to keep working.
   - The Mac tree is on `fix/test-db-concurrent-session-guard` and its launchd
     daemon runs an editable install (risk 14), so `git checkout main` after
     merging. Test-only diff, so the daemon is unaffected either way.

### 1. **#321 — the five leaked TestClient pools** *(now evidence-backed, and the closest neighbour)*
   The warning above names the files. **Acceptance:** a full-suite run emits no
   `ConnectionPool.__del__` / "cannot join current thread" warning, and the
   suite still reports `0 skipped`. Likely shape: a shared fixture that builds
   the app and closes its pool on teardown, replacing five inline
   `create_app(db_dsn=…)` calls. Check whether `create_app` exposes the pool
   for closing before designing the fixture — it may need a seam.

### 2. **#299 — the other two flaky tests** *(carried, still open)*
   `test_serve_daemon_routes.py::test_second_lifecycle_op_while_busy_is_409`
   and `test_serve_auth_routes.py::test_route_driven_login_failures_persist_audit_rows`.
   **This session's guard does not address them** — they were confirmed flaky
   on a clean `main` in a *single* session, so they are genuine intra-process
   races (the busy-guard is keyed on `_lifecycle_thread.is_alive()`, which can
   finish before the second op is issued). Do not assume #335's fix covered
   them; re-measure first.

### 3. **#324 — the blank-query/`sort="rank"` cursor wart** *(carried; was ranked 1 last session)*
   A blank query is served by the date branch whatever the `sort`, so
   `sort="rank"` is accepted on page 1 and its own cursor rejected on page 2.
   Its surface is wider than "blank query" — the branch predicate runs *after*
   `parse_query`, so `subject:invoice` takes the same path. **Acceptance:** the
   stated `rank` is refused on **page 1**, wire-visibly, with README updated.
   **Do not** "fix" it by having the cursor record the sort the caller *stated*
   rather than the one that ran — a cursor claiming an ordering it did not walk
   is #308 itself.

### 4. **The #317 API-keys review round** *(carried)*
   **#319** (`ApiKeyNotFound` carries three meanings; the panel reports a
   security refusal as success) · **#318** (the `api_key_name` ⟺ `is_service`
   pairing is unenforced in the schema — sketch with a generated column + FK is
   in the issue; **next free migration slot is `0037_*.sql`**) · **#320**
   (admin panel routes do blocking DB IO on the event loop).

### 5. **The #322/#332 review round leftovers** *(carried)*
   **#327** (`CursorPlan` carries two fields its pool-mode consumer must
   ignore) · **#328** (the page-cache entry is an untyped dict) · **#330**
   (`SortOrder`/`SortMode` restated in three wire layers — `sort_axes.py` is
   the single definition site now, but the MCP and HTTP schemas still restate
   the literals) · **#331** (`SortOrderNotApplicable`'s stated audience is
   wrong).

### 6. **#305 — `--version` dies on a missing third-party dependency** *(carried)*
   `cli.py` imports the daemon at module scope. **Acceptance:** blocking
   `sqlparse` on `sys.meta_path` leaves `localmail --version` printing its line
   and exiting 0. **Do it with the `cli.py` refactor, not before** — `cli.py` is
   **2177 lines**.

### 7. **#285 — ruff, repo-wide** *(carried)*
   **10** pre-existing errors (two are the `math` import/redefinition pair in
   `searcher.py`), 9 dead `# noqa: S608`, no `[tool.ruff]`, no CI step.
   **Decide the config and the CI step together** — that decision is the
   operator's.

### 8. **Admin GUI phase 5 — Users & ACL panel** *(carried)*
   `/v1/admin/users` is already `require_admin()` — **no backend work.**
   Mirror [serve/admin/users_panel_router.py](src/localmail/serve/admin/users_panel_router.py),
   follow the Daemon-panel shape, surface the two lock-out guards as 409s, and
   **stub the new API module in both `AdminView.test.ts` and `MainView.test.ts`**
   (risk 11).

### 9. **Remaining robustness backlog** *(carried)*
   **#218** · **#226** · **#225 / #227** · **#200 / #211 / #208** · **#206** ·
   **#204** · **#25**.

### 10. **Smaller, deliberately not done** *(carried)*
   - **165 docling failures on the Mac**, of 182 `blobs_gave_up`.
   - **Residual implausible language labels dominated by `ja`** (~0.24%); the
     confidence-floor lever was measured useless. **Sample `ja` first.**
   - **The DGX drops remain uninvestigated** (risk 7).
   - **`resetpwd.py` is GONE** — the previous handoff carried it as an
     untracked working-tree file to decide about; it no longer exists, so that
     item is closed. (Carried items are worth re-checking, not re-copying.)
   - **A session-22 stash is still on the stack** (`stash@{0}: On
     docs/session-22-handoff: review-fixes`); its content is on `main`, so
     `git stash drop` it if you want the tree tidy.
   - **A stray SDD workspace** at `.superpowers/sdd/`. Git-ignored scratch.

## Open decisions & risks

1. **One PR is open and yours to merge.** #336 on
   `fix/test-db-concurrent-session-guard`, based on `main` (`815e74b`), closing
   **#335** and **#329**. **24 open issues**, dropping to **22**. Dependabot
   **0**.
2. **A merge does NOT close issues its subject merely names** *(new, and it bit
   the last session)*. #333's subject read `(#323, #326)`; both stayed open
   until this session closed them by hand. Use `Closes #N` in the **PR body**,
   and **check `gh issue list` after the merge** rather than assuming.
3. **THIS FILE IS NOT THE AUTHORITY — `git` and `gh` are** *(carried,
   four-times earned)*. Open every session with `git fetch --prune && git log
   --oneline -1 origin/main`, `gh pr list`, `gh issue list`, `gh api
   …/dependabot/alerts`, and reconcile **before acting**.
4. **Base every branch on `main`; put a session's code and its handoff in ONE
   PR** *(carried)*. Session 25's stacked handoff PR merged into an
   already-merged branch 13 seconds after it landed and was lost silently.
   - The stranded-branch check is **NOISY**: `git log --oneline main..origin/<b>`
     is non-empty for every squash-merged branch. Signal = non-empty on a branch
     whose PR merged *recently*, confirmed by a **content** diff
     (`git diff --stat main origin/<b>` empty ⇒ it landed).
5. **NEVER run two pytest sessions against one test database** *(new — and now
   enforced, not merely advised)*. This is what #335 actually was. The suite now
   takes an advisory lock and the second session **waits**; if you want both to
   run, give one its own `LOCALMAIL_TEST_DSN`. **I reproduced the corruption
   myself this session** by starting a second run while a full suite was in
   flight — that is how easy it is, and the failures look like product bugs.
6. **Verify host revisions; do not infer them** *(carried, earned twice)*. The
   DGX moved itself to `815e74b` between handoffs, again with no session doing
   it. `ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'` plus
   `/v1/version`'s `build_hash` settles it.
7. **Test-count baselines: measure both refs IN THE SAME SESSION** *(carried)*.
   `main` **2951**, branch **2974**. A number quoted from a previous handoff is
   not a baseline — last session's handoff said 2850, which was already stale
   by the time it merged (the PR's own review round took it to 2951).
8. **A same-process assertion cannot pin a cross-process property** *(new)*.
   `advisory_lock_key` stability was tested inside one process, where a salted
   `hash()` is stable too — so the test its own docstring justified would have
   passed against the one implementation that fails silently. The fix is a
   subprocess. Look for this shape wherever a value must agree between runs.
9. **A keyset predicate must be a row comparison, in BOTH directions**
   *(carried — #75, #322, #323)*. The OR-form is semantically identical, keeps
   the index scan, adds no Sort, and plans as a per-tuple `Filter` that rescans
   from the index head on every page. Written by mistake **twice**.
   `tests/test_searcher_sort_order_plan.py` keeps three negative controls
   (`NULLS LAST`, the ascending OR-form, the descending OR-form) — **do not
   "tidy" them away**.
10. **The undated block is where the two directions genuinely differ**
    *(carried)*. Descending must *admit* it (hence the top-up query); ascending
    meets it at the head of its own walk. **Do not "restore symmetry"**, and
    **do not "fix" a short page by restoring `OR expr IS NULL`**.
11. **A cursor identifies a position, not a query — with exactly one enforced
    exception** *(carried — #326)*. Dropping the query a **text** cursor was
    minted from is a 400; archive-walk cursors page with or without one.
12. **Never state a `sort` on a request that carries a cursor** *(carried —
    #308, #311)*. A paging client must treat **409 as recoverable** and **400
    as permanent for that cursor**.
13. **When reverting a mutation, restore from a file copy — never `git
    checkout`** *(carried, sessions 23–35)*. Used 5 times this session.
    Treat **empty** pytest output as a failed mutation, not a pass.
14. **The stale NOTIFY queue recurs, and gate 1 lies** *(carried — and this
    session shows the signature is AMBIGUOUS)*. Exactly three LISTEN/NOTIFY
    failures usually means the stale queue. **This session had that exact
    failure set with BOTH gates clean**, and the tests passed in isolation — so
    it was cross-session contention, not the queue. **Check both gates before
    reaching for the runbook**; if they are clean, suspect a second pytest
    session (now impossible) or ordinary flakiness.
15. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `uv sync --all-extras` (Mac) / `--extra mcp --extra extraction` (DGX).
    **`uv` is not on the DGX's non-interactive PATH** — use `~/.local/bin/uv`.
16. **The Mac's launchd daemon runs an EDITABLE install** *(carried)*. It
    executes whatever the tree is checked out to. **Currently on
    `fix/test-db-concurrent-session-guard`** — check out `main` after merging.
    (Test-only diff this time, so nothing is at risk meanwhile.)
17. **`search-status` is sub-second** *(carried)*. Mac **1.26 s** wall this
    session including `uv run` startup. If it runs long that is a **regression
    of #280** — check `EXPLAIN (FORMAT JSON)` for a `Seq Scan on messages`
    under a `SubPlan` first.
18. **`blobs_claimable 0` alongside `blobs_pending 0` is the SETTLED shape**
    *(carried)*. Mac `9543 = 9255 + 106 + 182 + 0`; DGX `4405 = 4187 + 91 + 127
    + 0`. A steady non-zero `blobs_no_text` is **normal** (#277);
    **`blobs_gave_up` is the one to act on**.
19. **`uv run pytest -q` with NO arguments is the right command** *(carried)*.
    **Do not run the suite while a backfill is draining.**
20. **An empty `daemon_heartbeats` right after a restart is normal for minutes**
    *(carried — #269/#271)*: the startup blob-temp sweep. Grep for
    **`blob-temp sweep done: walked=`**.
21. **`--version`'s contract is six things, all pinned** *(carried)*. **Never
    reintroduce `@click.version_option` in any spelling** — an AST pin forbids
    it, covering `daemon_cli.py` too. stderr non-empty ⟺ unresolvable.
22. **`/v1/version` is unauthenticated, so identifiers only** *(carried)*. The
    diagnostic **text** carries errno values and paths and must stay off the
    wire. The exact-key-set assertion in `test_serve_version_route.py` is what
    stops a *future* key leaking — do not relax it to a subset check.
23. **A negative assertion needs the module's own constant and a positive
    control** *(carried)*.
24. **The DGX drops are STILL UNEXPLAINED — five theories refuted** *(carried)*.
    **Do not propose a sixth without a captured outage in which the host was
    demonstrably up throughout.** Triage with `journalctl --list-boots` first.
    Power is not a candidate (~5-day UPS). **Do not edit
    `/etc/wireguard/wg0.conf`.**
25. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`.
    **Run vitest from `gui/`, not the repo root.** `cargo clippy --all-targets`
    is clean but **ungated** — CI runs clippy without it.
26. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    **#266's whitespace-heal is a one-way door too.** **`--relabel` is the only
    destructive verb in the lang path**; reach for `--retry-declined` first.
27. **`ExtractorConfigurationError` subclasses `TransientExtractorError`, and
    that subclassing *is* the #248 fix — do not "clean it up"** *(carried)*.
28. **`run_embed_worker_once`'s `failure_log` defaults to the process-wide
    `embed_worker._FAILURE_LOG`, and that default is the fix** *(carried —
    #267)*.
29. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    kept though #245 is fixed; #246 warns on group-write; `InsecureSecretsFile`
    refuses; #239's manual tombstone retention deliberate; admin bearer tokens
    have no per-token scope (#204).
30. **No ROADMAP.md** *(carried, re-confirmed)* — that `/nextsession` step is a
    no-op. **README and CLAUDE.md were both updated** this session.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status && git branch --show-current
git log --oneline -1 origin/main         # THE authority, not this file's header
git log --oneline main..origin/main      # non-empty = a session landed since

# RISK 2 — after any merge, CHECK THE ISSUES ACTUALLY CLOSED.
gh pr list
gh issue list --limit 40                 # 24 open; #336 should take it to 22
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '[.[] | select(.state=="open")] | length'      # expect 0

# RISK 4 — stranded-branch shortlist (noisy: every squash-merged branch shows).
for b in $(git branch -r --format='%(refname:short)' | grep -v 'origin/main\|HEAD'); do
  n=$(git log --oneline main.."$b" | wc -l); [ "$n" -gt 0 ] && echo "$b: $n commits not on main"
done
git diff --stat main origin/<branch>     # EMPTY = landed, not stranded

# AFTER MERGING — the tree is on fix/test-db-concurrent-session-guard (risk 16):
#   Mac:  git checkout main && git pull && unset VIRTUAL_ENV && uv sync --all-extras
#   NEITHER HOST needs a pull for #336 — it is test-only, no migration, no dep change.

# Python suite. NEVER a bare `uv sync` (risk 15).
unset VIRTUAL_ENV && uv sync --all-extras
unset VIRTUAL_ENV && uv run pytest -q
#   expect 2974 passed, 0 failed, 0 SKIPPED on this branch; 2951 collected on main.
#   MEASURE BOTH REFS IN THIS SESSION (risk 7) — no DB needed:
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2
unset VIRTUAL_ENV && uv run mypy src/localmail    # expect Success, 152 files
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -2   # expect 10 (#285)

# RISK 5 — the test-database session lock (new this session).
# A second pytest session now WAITS instead of corrupting. If a run seems to
# hang at startup, look for this line; it is not a fault:
#   "waiting for another pytest session to release the test database ..."
# To run two suites at once, give one its own database:
#   LOCALMAIL_TEST_DSN=postgresql://localmail:local%40%40mail@localhost:5532/localmail_test2 uv run pytest -q
# Confirm the running session holds its lock (should print `f`, i.e. NOT free):
psql -h localhost -p 5532 -U localmail -d localmail_test -tAc \
  "SELECT count(*) FROM pg_locks WHERE locktype='advisory'"

# #1 NEXT — #321: the five leaked TestClient pools. Reproduce the evidence:
unset VIRTUAL_ENV && uv run pytest -q 2>&1 | grep -A3 "ConnectionPool.__del__"
#   names test_serve_admin_csp / _admin_login / _daemon_wiring / _version_route /
#   test_session_cookie_scope. Acceptance: that warning is gone and skipped stays 0.

# If EXACTLY the three LISTEN/NOTIFY tests fail, CHECK BOTH GATES (risk 14) —
# this session had that signature with both gates CLEAN (it was contention):
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'
# Only if a gate errors, use runbook Option A (bootout, verify while down, bootstrap).

# Host health (Mac):
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 20)

unset VIRTUAL_ENV && uv run localmail search-status    # UNDER A SECOND (risk 17)
#   Check the SHAPE, not the literals: the four buckets must sum to
#   blobs_eligible, and claimable must equal pending.
#   Mac 9543 = 9255 + 106 + 182 + 0, claimable 0

# The DGX (risk 6 — verify, never infer):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'   # binds 10.0.0.3, NOT localhost
ssh 10.0.0.3 'cd ~/src/localmail && ~/.local/bin/uv run localmail search-status'
#   expect under a second; 4405 = 4187 + 91 + 127 + 0, claimable 0
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — REQUIRED only if you touch gui/ (MUST run from gui/ — risk 25):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
#   expect: 0 svelte-check errors, 408 vitest, 104 cargo
```

`main` tip at session start was **`815e74b`**. This session left **one PR
(#336)** open on `fix/test-db-concurrent-session-guard` (4 commits, head
`2fac3ae`), closing **#335** and **#329**;
**#323** and **#326** were closed by hand. Latest migration
**`0036_api_keys.sql`**; next free slot `0037_*.sql` (this session adds none).
**Open issues: 24**, dropping to **22** on merge. **Dependabot: 0.**
