# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-09-02 (session 36).** `main` was **`5dbaea0`** at the
> start — PR #336 had already been merged by the operator, and **#335 and #329
> auto-closed correctly** (the `Closes #N` body worked; risk 2 discharged for
> this round). This session opened **one PR** on
> `fix/321-testclient-pool-leak`, closing **#321**.
>
> **The previous handoff was accurate on every fact this session checked** —
> `main` at `5dbaea0`, the branch deleted, Dependabot 0, and the
> `ConnectionPool.__del__` warning reproducible on the first full run. Its one
> imprecision was cosmetic: it predicted 22 open issues after the merge; the
> real number is **23**, because #337 was filed inside the same session and
> already counted.
>
> **Open issue count is now 23, dropping to 22 on merge.**
>
> **The headline lesson is risk 5**: the first push looked complete on this
> laptop — three green full runs, warning gone — and CI's 3.13 leg proved it
> half-done. A `ConnectionPool.__del__` warning is a GC-timing artefact, so a
> quiet platform is not evidence. Instrument the seam.

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

### #321 — close the pools a test leaks, in one place

`create_app` opens its pool eagerly (`open=True`) and closes it **only** in the
FastAPI lifespan's `finally`. So a bare `create_app(...)`, or a
`TestClient(app)` used without `with`, leaks it: the pool holds its connections
until the GC reaches it, and `ConnectionPool.__del__` then joins the pool's own
worker thread *from inside that thread* → `RuntimeError: cannot join current
thread`, surfaced as a `PytestUnraisableExceptionWarning`.

**Pytest attributes that warning to whichever unrelated test was running when
the collection fired.** That is why the previous handoff named five files and
this session's baseline named four, overlapping in exactly one
(`test_serve_version_route.py`) — **the warning never names the leak site**,
and neither list was a list of leaking files. Worth knowing before chasing the
next such warning.

**There were TWO seams, and the second was only found because CI disagreed
with the laptop.** The first push closed the `create_app` half and read
2 warnings across three green macOS full runs — but CI's **3.13** leg still
reported `cannot join current thread` (3 warnings; the 3.12 leg read 2). The GC
decides when `__del__` runs, so a platform can hide the whole thing. The
residual was `localmail.db.open_pool`: **`Daemon.stop()`/`join()` never close
`self.pool`**, so 13 daemon tests across four files plus one `create_searcher`
test leaked one each.

**It was found by instrumenting, not by reading the warning.** A temporary
`pytest_sessionstart` plugin wrapping `localmail.db.ConnectionPool` and
reporting unclosed pools at `pytest_sessionfinish` named **all 14 sites with
their creation stacks in a single run**. Reading the warning would never have
— it names the wrong file by construction. That probe is reproduced in the
commands section below; it is worth keeping in the toolkit.

New pure module [tests/_pool_leaks.py](tests/_pool_leaks.py); autouse fixture
`conftest.close_leaked_pools`; **26 tests** in
[tests/test_pool_leaks.py](tests/test_pool_leaks.py).

- **The per-file sweep #321 proposes was measured and rejected — with the
  operator's explicit sign-off on the alternative.** It is **34 files, 162 call
  sites**; and as worded (wrap each in `with TestClient(...)`) it *breaks* the
  tests that exist to assert `create_app` alone is side-effect-free, because
  running the lifespan is exactly what binds the daemon control socket
  (`test_creating_app_does_not_bind_control_socket`). It also buys discipline
  where the seam buys construction: a new inline `create_app(...)` written
  tomorrow cannot reintroduce the leak.
- **A seam is the `ConnectionPool` name in the module that builds the pool** —
  `localmail.serve.app` and `localmail.db`, listed in `POOL_SEAMS`. Each is
  resolved from that module's globals on every call, so patching it reaches
  every caller. **Patching `create_app` itself would reach none of them** —
  each test module binds it into its own namespace at import time.
- **`missing_seam_error` reports rather than skips.** An aliased import
  (`from psycopg_pool import ConnectionPool as Pool`) leaves the attribute
  absent, nothing patches, every pool leaks again — and *no test fails*, because
  closing a pool that was never recorded is a no-op. It deliberately does **not**
  check the seam's *identity*: swapping a different pool class in under the same
  name is legitimate and the wrapper handles it correctly.
- **The fixture skips a seam whose module is not in `sys.modules`.**
  That keeps ~0.5 s of FastAPI import off every unit-only run — `pytest
  tests/test_pgtext.py` is 0.27 s in-pytest, so importing it unconditionally
  would have more than doubled it. The inference (module absent ⟹ no collected
  test can call `create_app`) holds only because pytest imports every collected
  module before running any test — **sound for a module-level import, false for
  a function-local one**. Six such imports existed across three files
  (`test_api_auth_rate_limiter.py`, `test_mcp_cli_wiring.py`,
  `test_mcp_discovery.py`); they are hoisted, and the pure
  `function_local_serve_app_imports` scans the whole suite to keep it that way.
  It reads the **AST**, not the text, because the rationale necessarily quotes
  the import it forbids — the `_mentions_version_option` call, mutation-proven
  in both directions. **The `localmail.db` seam needs no such rule**:
  `conftest.py` imports that module itself for `apply_migrations`, so it is
  always loaded.
- **`unclosed` filters before closing**, so the count `close_pools` returns is
  the number of pools that genuinely leaked. `close()` is idempotent, so the
  filter is about keeping the claim true, not about safety.
- **`Daemon` not closing its own pool is unchanged production behaviour.**
  `run_forever` owns the process, so the pool dies with it. This fixture is a
  *test* backstop, not a claim that `Daemon` should close it — #321 is not the
  place to change that.

### Verification (this Mac, all extras)

- **Both refs measured in this session** (risk 7):

  | ref | result | warnings |
  |---|---|---|
  | `main` @ `5dbaea0` | 2988 passed, **0 skipped**, 201.70 s | **6** |
  | branch, first push (`b286887`) | 3009 passed, 178.84 s | 2 on macOS, **3 on CI 3.13** |
  | branch, final | 3014 passed, **0 skipped**, 184.26 s | **2** |

  The 6 are 2 pre-existing `websockets` deprecations (**#25**) plus **4**
  `cannot join current thread`. The final 2 are the websockets pair alone —
  **and CI confirms it on both legs**: `3013 passed, 1 skipped, 2 warnings` on
  3.12 *and* 3.13, with no `cannot join current thread` on either (run
  `33581065558`). That last check is the one the first push failed, so make it
  the habit (risk 5). The
  instrumented run reports **0 of 131** `localmail.db` pools unclosed, against
  **14** before; the `couldn't stop thread …` spam psycopg printed at
  interpreter shutdown is gone with them.
- **The fix is not a slowdown.** An intermediate run read 247 s, which looked
  like a 23 % regression; it was machine noise. Timed directly on a serve-heavy
  pair (`test_serve_attachments_routes.py` + `test_serve_acl_routes.py`):
  **6.84 s with closing, 6.99 s without**. The final full run is *faster* than
  the baseline.
- **Eight mutations, each caught by its intended test** (restored from a
  scratchpad copy every time, never `git checkout` — risk 13):

  | mutation | caught by |
  |---|---|
  | teardown does not close | 1 test (the fixture-driving pin) |
  | fixture never patches the seam | 3 tests |
  | a hoisted import put back inside a function | the AST pin |
  | `unclosed` returns every pool | 4 tests |
  | seam guard always says "intact" | 1 test |
  | the `localmail.db` seam dropped from `POOL_SEAMS` | 2 tests |
  | `loaded_seams` ignores whether the module is loaded | 26 errors |
  | **prose quoting the forbidden import** | **correctly NOT flagged** |
- `mypy src/localmail` → Success, **152** files. `mypy tests/_pool_leaks.py`
  → Success.
- `ruff check src/localmail/` → **10**, the unchanged #285 baseline (no `src/`
  file was touched). The two new test modules and all four changed ones: clean.
- GUI untouched (`git diff --name-only main...HEAD | grep ^gui/` empty).
- **README and CLAUDE.md both updated.** There is still **no ROADMAP.md** — that
  `/nextsession` step remains a no-op (risk 30, re-confirmed a third time).

### #299 re-measured, and the previous attribution looks wrong

The handoff asked for this explicitly ("Do not assume #335's fix covered them;
re-measure first"). Measured on this branch:

- **0/20** targeted runs of `test_serve_daemon_routes.py` +
  `test_serve_auth_routes.py` failed.
- **3/3** full-suite runs today were green (2988, 3009, 3009).

Neither reproduced. Two separate mechanisms, and they should stop being
carried as one issue:

- **`test_second_lifecycle_op_while_busy_is_409` is a wall-clock window.** It
  spawns a real SIGTERM-deaf child, gives the supervisor `grace_seconds=3.0`,
  and needs a poll + POST round trip to land inside that window. Under load the
  window closes first, the second POST is *legitimately* admitted (200), and the
  test fails. That is a test-design issue, not a product race — the busy-guard
  behaved correctly.
- **`test_route_driven_login_failures_persist_audit_rows` has no concurrency at
  all.** It drives three sequential HTTP logins and asserts
  `count(*) == 3` on `api_login_attempts` for `alice`. **That is textbook
  #335**: a second pytest session's `alice` failures land in the same table
  between this test's TRUNCATE and its count. The previous handoff's claim that
  both are "genuine intra-process races" is unexplained for this one — **#336
  may already have fixed it.** Stated as a hypothesis: the original failure was
  not reproduced here, so this is not proven.

## What's next

### 0. **Merge the #321 PR, then check the issue actually closed**
   **You merge** (project convention). The body uses `Closes #321`. Risk 2 says
   verify with `gh issue list` afterwards rather than assume — it worked this
   round, which is not the same as it always working.
   - **No host action needed**: test-only diff, no migration, no dependency
     change. Neither host needs a pull.
   - The Mac tree is on `fix/321-testclient-pool-leak` and its launchd daemon
     runs an editable install (risk 16), so `git checkout main` after merging.
     Test-only, so the daemon is unaffected meanwhile.

### 1. **#337 — the acceptance harnesses bypass the test-database lock** *(the closest neighbour, and now the only known hole in test isolation)*
   The six `tests/acceptance/run_*.py` truncate the same tables against the same
   `LOCALMAIL_TEST_DSN` and take no lock, so running one beside a suite
   reproduces #329's corruption in both directions with the same silence.
   **Acceptance:** each harness entry point calls
   `tests._db_session_lock.acquire_exclusive` and holds it for the run;
   starting a harness while a suite is in flight prints the waiting notice and
   then proceeds, rather than corrupting both. README's "covers pytest, not the
   database" paragraph gets updated. Note the harnesses are **not** collected by
   pytest (they match no `python_files` pattern), so they get no conftest
   fixture — the call has to be explicit.

### 2. **#299 — split it, or close half of it** *(re-measured above; carried)*
   Given the diagnosis above, the cheap next step is to **split #299 into two**:
   the busy-guard window (a test-design fix — make the window not wall-clock
   bounded, e.g. a barrier the child releases rather than `grace_seconds=3.0`)
   and the login-audit count (likely already fixed by #336). **Acceptance for
   the first:** the test no longer depends on a round trip beating a 3 s timer.
   **For the second:** either a reproduction on current `main`, or close it
   citing #336.

### 3. **#324 — the blank-query/`sort="rank"` cursor wart** *(carried)*
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
   (risk 25).

### 9. **Remaining robustness backlog** *(carried)*
   **#218** · **#226** · **#225 / #227** · **#200 / #211 / #208** · **#206** ·
   **#204** · **#25**.

### 10. **Smaller, deliberately not done** *(carried)*
   - **165 docling failures on the Mac**, of 182 `blobs_gave_up`.
   - **Residual implausible language labels dominated by `ja`** (~0.24%); the
     confidence-floor lever was measured useless. **Sample `ja` first.**
   - **The DGX drops remain uninvestigated** (risk 24).
   - **A session-22 stash is still on the stack** (`stash@{0}: On
     docs/session-22-handoff: review-fixes`); its content is on `main`, so
     `git stash drop` it if you want the tree tidy.
   - **A stray SDD workspace** at `.superpowers/sdd/`. Git-ignored scratch.

## Open decisions & risks

1. **One PR is open and yours to merge.** `fix/321-testclient-pool-leak`, based
   on `main` (`5dbaea0`), closing **#321**. **23 open issues**, dropping to
   **22**. Dependabot **0**.
2. **A merge does NOT close issues its subject merely names** *(carried)*. Use
   `Closes #N` in the **PR body** and **check `gh issue list` after the merge**.
   It worked this round (#335/#329 both auto-closed); that is one data point,
   not a guarantee.
3. **THIS FILE IS NOT THE AUTHORITY — `git` and `gh` are** *(carried,
   five-times earned)*. Open every session with `git fetch --prune && git log
   --oneline -1 origin/main`, `gh pr list`, `gh issue list`, `gh api
   …/dependabot/alerts`, and reconcile **before acting**.
4. **Base every branch on `main`; put a session's code and its handoff in ONE
   PR** *(carried)*. Session 25's stacked handoff PR merged into an
   already-merged branch 13 seconds after it landed and was lost silently.
   - The stranded-branch check is **NOISY**: `git log --oneline main..origin/<b>`
     is non-empty for every squash-merged branch. Signal = non-empty on a branch
     whose PR merged *recently*, confirmed by a **content** diff
     (`git diff --stat main origin/<branch>` empty ⇒ it landed).
5. **A `PytestUnraisableExceptionWarning` names the test GC ran during, NOT the
   leak site** *(new, and it cost two handoffs a wrong list of files)*. The
   previous handoff's five files and this session's four overlap in one. Do not
   treat such a list as evidence about which code leaks — **instrument the
   seam** (the probe is in the commands section) and read the creation stacks.
   Corollary, earned the hard way this session: **a clean local run does not
   mean the leak is gone.** Three green macOS full runs read 2 warnings while
   CI's 3.13 leg read 3, because the GC decides when `__del__` fires. When the
   evidence is a GC-timing artefact, **believe the instrumentation, not the
   platform that happens to be quiet.**
6. **NEVER run two pytest sessions against one test database** *(carried —
   enforced since #336)*. The second now **waits**. To run both, give one its
   own `LOCALMAIL_TEST_DSN`. The guard covers **pytest only** — the acceptance
   harnesses still bypass it (**#337**, item 1 above).
7. **A test module must import `localmail.serve.app` at MODULE scope** *(new)*.
   The autouse pool-closing fixture reads `sys.modules` at test-setup time, so a
   function-local import arrives too late and that file's pools leak silently.
   `tests/test_pool_leaks.py::test_no_collected_test_module_imports_serve_app_below_module_scope`
   enforces it over the whole suite. If it fires, hoist the import — do not
   relax the rule; it is what makes the ~0.5 s import saving sound. The
   `localmail.db` seam is exempt because `conftest.py` imports that module
   itself.
8. **Verify host revisions; do not infer them** *(carried, earned twice)*.
   `ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'` plus
   `/v1/version`'s `build_hash` settles it. **Both hosts were left untouched
   this session** — the diff is test-only, so neither needs a pull.
9. **Test-count baselines: measure both refs IN THE SAME SESSION** *(carried)*.
   `main` **2988**, branch **3014**. A number quoted from a previous handoff is
   not a baseline.
10. **A same-process assertion cannot pin a cross-process property** *(carried)*.
11. **A keyset predicate must be a row comparison, in BOTH directions**
    *(carried — #75, #322, #323)*. The OR-form is semantically identical, keeps
    the index scan, adds no Sort, and plans as a per-tuple `Filter` that rescans
    from the index head on every page. Written by mistake **twice**.
    `tests/test_searcher_sort_order_plan.py` keeps three negative controls
    (`NULLS LAST`, the ascending OR-form, the descending OR-form) — **do not
    "tidy" them away**.
12. **The undated block is where the two directions genuinely differ**
    *(carried)*. Descending must *admit* it (hence the top-up query); ascending
    meets it at the head of its own walk. **Do not "restore symmetry"**, and
    **do not "fix" a short page by restoring `OR expr IS NULL`**.
13. **A cursor identifies a position, not a query — with exactly one enforced
    exception** *(carried — #326)*. Dropping the query a **text** cursor was
    minted from is a 400; archive-walk cursors page with or without one.
14. **Never state a `sort` on a request that carries a cursor** *(carried —
    #308, #311)*. A paging client must treat **409 as recoverable** and **400
    as permanent for that cursor**.
15. **When reverting a mutation, restore from a file copy — never `git
    checkout`** *(carried, sessions 23–36)*. Used 6 times this session.
    Treat **empty** pytest output as a failed mutation, not a pass.
16. **The stale NOTIFY queue recurs, and gate 1 lies** *(carried)*. Exactly
    three LISTEN/NOTIFY failures usually means the stale queue — but session 35
    had that exact failure set with **both gates clean** (it was contention).
    **Check both gates before reaching for the runbook.**
17. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `uv sync --all-extras` (Mac) / `--extra mcp --extra extraction` (DGX).
    **`uv` is not on the DGX's non-interactive PATH** — use `~/.local/bin/uv`.
18. **The Mac's launchd daemon runs an EDITABLE install** *(carried)*. It
    executes whatever the tree is checked out to. **Currently on
    `fix/321-testclient-pool-leak`** — check out `main` after merging.
    (Test-only diff this time, so nothing is at risk meanwhile.)
19. **`search-status` is sub-second** *(carried)*. If it runs long that is a
    **regression of #280** — check `EXPLAIN (FORMAT JSON)` for a `Seq Scan on
    messages` under a `SubPlan` first.
20. **`blobs_claimable 0` alongside `blobs_pending 0` is the SETTLED shape**
    *(carried)*. Mac `9543 = 9255 + 106 + 182 + 0`; DGX `4405 = 4187 + 91 + 127
    + 0`. A steady non-zero `blobs_no_text` is **normal** (#277);
    **`blobs_gave_up` is the one to act on**. *(Not re-measured this session —
    no archive work was done.)*
21. **`uv run pytest -q` with NO arguments is the right command** *(carried)*.
    **Do not run the suite while a backfill is draining.**
22. **An empty `daemon_heartbeats` right after a restart is normal for minutes**
    *(carried — #269/#271)*: the startup blob-temp sweep. Grep for
    **`blob-temp sweep done: walked=`**.
23. **`--version`'s contract is six things, all pinned** *(carried)*. **Never
    reintroduce `@click.version_option` in any spelling** — an AST pin forbids
    it, covering `daemon_cli.py` too. stderr non-empty ⟺ unresolvable.
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
27. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    kept though #245 is fixed; #246 warns on group-write; `InsecureSecretsFile`
    refuses; #239's manual tombstone retention deliberate; admin bearer tokens
    have no per-token scope (#204).
28. **CI reports `1 skipped` on Linux; macOS reports 0** *(carried)*. It is
    pre-existing and still unidentified. **Do not read it as a missing uv
    extra** without checking a `main` run first.
29. **No ROADMAP.md** *(carried, re-confirmed)* — that `/nextsession` step is a
    no-op. **README and CLAUDE.md were both updated** this session.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status && git branch --show-current
git log --oneline -1 origin/main         # THE authority, not this file's header
git log --oneline main..origin/main      # non-empty = a session landed since

# RISK 2 — after any merge, CHECK THE ISSUE ACTUALLY CLOSED.
gh pr list
gh issue list --limit 40                 # 23 open; the #321 PR should take it to 22
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '[.[] | select(.state=="open")] | length'      # expect 0

# RISK 4 — stranded-branch shortlist (noisy: every squash-merged branch shows).
for b in $(git branch -r --format='%(refname:short)' | grep -v 'origin/main\|HEAD'); do
  n=$(git log --oneline main.."$b" | wc -l); [ "$n" -gt 0 ] && echo "$b: $n commits not on main"
done
git diff --stat main origin/<branch>     # EMPTY = landed, not stranded

# AFTER MERGING — the tree is on fix/321-testclient-pool-leak (risk 18):
#   Mac:  git checkout main && git pull && unset VIRTUAL_ENV && uv sync --all-extras
#   NEITHER HOST needs a pull — test-only, no migration, no dep change.

# Python suite. NEVER a bare `uv sync` (risk 17).
unset VIRTUAL_ENV && uv sync --all-extras
unset VIRTUAL_ENV && uv run pytest -q
#   macOS: expect 3014 passed, 0 failed, 0 skipped, and **2 warnings**.
#   THOSE 2 ARE THE ACCEPTANCE SIGNAL FOR #321: both are the pre-existing
#   `websockets` DeprecationWarnings (#25). A third warning mentioning
#   "ConnectionPool.__del__" or "cannot join current thread" means the pool
#   seam went inert — see risk 7, and check for a function-local import of
#   localmail.serve.app.
#   LINUX/CI: expect 1 SKIPPED as well; pre-existing (risk 28).
#   MEASURE BOTH REFS IN THIS SESSION (risk 9) — no DB needed:
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2   # branch: 3014
unset VIRTUAL_ENV && uv run mypy src/localmail    # expect Success, 152 files
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -2   # expect 10 (#285)

# The #321 fix, verified directly (should print nothing at all):
unset VIRTUAL_ENV && uv run pytest -q 2>&1 | grep -c "cannot join current thread"   # expect 0
unset VIRTUAL_ENV && uv run pytest -q tests/test_pool_leaks.py                      # expect 26 passed

# THE POOL-LEAK PROBE (risk 5) — reusable; this is what found the second seam.
# Drop it anywhere on PYTHONPATH and load with -p. Reports every unclosed pool
# with the stack that built it, which the warning itself never tells you.
cat > /tmp/pool_leak_probe.py <<'PROBE'
import traceback
_RECORDS = []
def pytest_sessionstart(session):
    import localmail.db as db          # or localmail.serve.app
    real = db.ConnectionPool
    def factory(*a, **k):
        pool = real(*a, **k)
        _RECORDS.append((pool, traceback.extract_stack()[:-1]))
        return pool
    db.ConnectionPool = factory
def pytest_sessionfinish(session, exitstatus):
    leaked = [(p, st) for p, st in _RECORDS if not p.closed]
    print(f"\n=== {len(_RECORDS)} pools, {len(leaked)} UNCLOSED ===")
    for _p, st in leaked:
        frames = [f for f in st if "/tests/" in f.filename or "/localmail/" in f.filename]
        print("  " + " <- ".join(f"{f.filename.split('/')[-1]}:{f.lineno} {f.name}" for f in frames[-4:]))
PROBE
unset VIRTUAL_ENV && PYTHONPATH=/tmp uv run pytest -q -p pool_leak_probe 2>&1 | grep -A 20 "UNCLOSED"
#   expect: "131 pools, 0 UNCLOSED"

# RISK 6 — the test-database session lock (#336). A second pytest session WAITS.
# If a run seems to hang at startup, look for this line; it is not a fault:
#   "waiting for another pytest session to release the test database ..."
# To run two suites at once, give one its own database:
#   LOCALMAIL_TEST_DSN=postgresql://localmail:local%40%40mail@localhost:5532/localmail_test2 uv run pytest -q

# #1 NEXT — #337: the acceptance harnesses take no lock. Reproduce the gap:
ls tests/acceptance/run_*.py             # six harnesses, none calls acquire_exclusive
grep -rn "acquire_exclusive" tests/acceptance/ || echo "confirmed: none of them takes the lock"

# #2 NEXT — #299 did NOT reproduce this session (0/20 targeted, 3/3 full runs):
unset VIRTUAL_ENV && for i in $(seq 1 20); do \
  uv run pytest -q tests/test_serve_daemon_routes.py tests/test_serve_auth_routes.py 2>&1 | tail -1; \
done | sort | uniq -c

# If EXACTLY the three LISTEN/NOTIFY tests fail, CHECK BOTH GATES (risk 16):
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'
# Only if a gate errors, use runbook Option A (bootout, verify while down, bootstrap).

# Host health (Mac) — NOT re-measured this session; no archive work was done:
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 22)

unset VIRTUAL_ENV && uv run localmail search-status    # UNDER A SECOND (risk 19)
#   Check the SHAPE, not the literals: the four buckets must sum to
#   blobs_eligible, and claimable must equal pending.

# The DGX (risk 8 — verify, never infer):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'   # binds 10.0.0.3, NOT localhost
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — REQUIRED only if you touch gui/ (MUST run from gui/ — risk 25):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
#   expect: 0 svelte-check errors, 408 vitest, 104 cargo
```

`main` tip at session start was **`5dbaea0`**. This session left **one PR** open
on `fix/321-testclient-pool-leak` — `b286887` (the `create_app` seam), the
second-seam commit, and this handoff — closing **#321**. Latest migration **`0036_api_keys.sql`**; next free slot
`0037_*.sql` (this session adds none). **Open issues: 23**, dropping to **22**
on merge. **Dependabot: 0.**
