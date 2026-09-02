# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-09-03 (session 38).** `main` was **`b35df86`** at the
> start — PR #339 merged by the operator, **#337 closed correctly**, and
> Dependabot down 5 → 1 exactly as the previous handoff predicted. This session
> opened **one PR** on `fix/299-flaky-lifecycle-pins-and-transformers`, closing
> **#299** and clearing the **last** Dependabot alert.
>
> **The previous handoff was accurate on every fact this session checked** —
> `main` tip, 22 open issues, Dependabot at 1 and it being `transformers`, and
> the transformers blast radius *in the shape it measured it*. One number it
> gave has since moved; see the second lesson below.
>
> **The headline finding: #299's stated cause was wrong, and measuring it was
> the whole job.** The issue said the route-level busy-guard pin "races the
> busy-guard — under load the first lifecycle op can finish before the second is
> issued". An instrumented copy of that test used **6.7 ms of its 3000 ms
> budget** — a **450× margin**, which cannot explain a test the issue reports
> failing in **3 of 3** runs. Near-deterministic failure is not a wall-clock
> race. The actual mechanism reproduced on the first attempt: run the pair
> beside a non-pytest process doing the per-test `TRUNCATE` a second pytest
> session would, and **8 of 8** runs fail, 2 of them with both tests failing —
> the issue's pattern exactly. The failure is not timing at all: `api_users` is
> truncated, the admin session's principal vanishes, the route 303s to the
> login page, and `_poll_state` decodes HTML as JSON. **That is #329/#335,
> closed by #336.** Control on current `main`: **20 of 20** clean.
>
> **Two lessons worth carrying.**
>
> **(1) A stated cause is a hypothesis, and an issue's own evidence can refute
> it.** The "3 of 3 runs failed" line in #299 was sitting in the issue the whole
> time and is flatly incompatible with the timing theory printed two paragraphs
> below it. Reconcile a proposed mechanism against the reported *frequency*
> before you go looking for it.
>
> **(2) `uv lock --dry-run` measures the resolution at the moment it runs; it is
> not a promise about the one that ships.** The previous handoff measured
> transformers 5.8.1 → 5.15.1 plus one `safetensors` bump and "nothing else
> moves". The real re-lock moved **three** packages — transformers **5.16.1**,
> safetensors 0.8.0, **tokenizers 0.23.1** — partly because declaring a floor
> changes the resolution, partly because a release landed in between. The
> dry-run was still worth running; quote it as a measurement with a date, not as
> a plan.
>
> **Open issue count is 22, dropping to 21 on merge. Dependabot is 1, dropping
> to 0** — for the first time in this project's recorded sessions.

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

Two code commits plus this handoff, one PR. Both items were chosen by the
operator from the previous handoff's list.

### `891e798` — #299: the busy-guard pins hold a window open instead of racing a timer

#299 filed two flaky tests. **Neither is flaky for the reason the issue gives,
and only one of them needed a change.**

- **`test_route_driven_login_failures_persist_audit_rows` gets NO change.** It
  has no concurrency of its own, its exact-count assertion (`== 3`) is correct,
  and #336 is what fixed it. **Do not "harden" it** — a retry or a tolerance
  there would hide the next real #335.
- **The busy-guard pins were rewritten anyway**, because a pin that must win a
  wall-clock race is one a loaded runner eventually breaks and the next session
  then learns to ignore. The window *was* the child's grace period, so every
  assertion had to land inside it — two HTTP round trips and a DB-backed status
  poll inside three seconds.
- The rule is the new
  [tests/_gated_supervisor.py](tests/_gated_supervisor.py)`::GatedStopSupervisor`,
  which parks `stop()` on an event so the second request is issued while the
  first is **provably** in flight. Only `stop()` is overridden — `request_stop`
  and the guard it consults are the production ones, and the guard reads *the
  thread*, not what the thread runs.
- **The parked thread holds no lock**, and that is load-bearing: it waits
  *before* delegating to `super().stop()`, the call that takes `_lock`. Parking
  under the lock would block the very `request_stop` whose refusal is being
  asserted, and the test would hang instead of failing.
- **Both pins start the child synchronously**, not through the route: a routed
  start spawns a lifecycle thread of its own, which would still be in flight
  when the first stop lands and answer it with the very 409 the test attributes
  to the stop.
- **`gate_timed_out` keeps the residual bound honest.** The wait cannot be
  unbounded (a test failing before its `finally` would hang the suite), but an
  expired park lets the lifecycle thread finish and the guard then *correctly*
  returns 202 — which reads as a broken guard. Asserting the flag reports the
  window rather than a verdict.
- **The three `time.sleep()` calls in `test_daemon_extract_thread.py` are
  deleted, not lengthened.** `start_workers()` calls `Thread.start()` for every
  worker synchronously and `Thread.start()` returns only once the thread is
  registered, so they waited for something that had already happened.
- 3 new tests in [tests/test_gated_supervisor.py](tests/test_gated_supervisor.py)
  pin the double itself, both branches of the park.

### `78c2b4b` — Dependabot #70: floor `transformers` at 5.10.0 and take the bump

CVE-2026-9856 (HIGH) covers `< 5.10.0`; the lock sat at 5.8.1, inside it.

- **This is the sqlparse case, not the pypdf/icalendar one.** The advisory is a
  path traversal in `save_pretrained` via chat-template names — a *write* path
  localmail never calls — and `grep -rn transformers src/` is empty. Hygiene,
  not an incident.
- **The floor is declared even though nothing imports it**, which is the
  corollary of what `icalendar` taught last session: the lock is the state, the
  floor is the constraint. Here there was **no floor to read against at all** —
  `transformers` appeared in neither `[project.dependencies]` nor any extra —
  which is precisely why the alert had to be read against `uv.lock`. Listing a
  package we do not import follows `ocrmac` beside it.
- **Verified end to end, because the suite structurally cannot see this.** Every
  docling test mocks the converter, so none of the 144 extraction tests loads
  the layout models a transformers bump moves. A one-off probe built an
  image-only PDF (PIL + reportlab, as `test_extractor.py` does), confirmed
  `LightweightExtractor` finds nothing in it, and ran a **real** OCR pass
  before and after. Extracted text byte-identical
  (`"Invoice 4711 total 250 EUR"`), and both runs report `Loading weights: 770`
  — which is what proves the bumped package is **on the path** rather than
  merely installed. Probe not kept, for the reason the pypdf/icalendar one was
  not.

### The mutation battery, and the two results that are recorded rather than fixed

Restored from a scratchpad copy every time, never `git checkout` (risk 13).

| mutation | caught by |
|---|---|
| `_lifecycle_in_flight` always False | route pin + unit pin |
| `request_stop` skips the guard | route pin + unit pin |
| the gate never parks | route pin + 2 double tests |
| `gate_timed_out` never set | 1 double test |
| `release()` does nothing | 1 double test |
| `extract_worker` never spawns | 3 extract tests |
| idle/poll threads never spawn | 1 extract test |

Two results are **recorded rather than smoothed over**, and both are the
"mutate both branches" lesson (risk 8) paying out again:

- **The *unit* busy-guard pin survives removing the gate.** Without it the
  window is milliseconds against a microsecond assertion path, so no mutation
  can demonstrate the gate there. It removes a small race, not an observable
  one. Stated honestly rather than dressed up as a proof.
- **`test_a_gated_stop_parks_instead_of_finishing` was written weak and caught
  by its own battery.** It asserted only that the state was still STOPPING,
  which survived the no-park mutation **by luck** — the identical lucky-win the
  gate exists to remove. It joins the thread now, and its timeout is one-sided
  by construction: it bounds only how long a *broken* gate is given to reveal
  itself.

### Verification (this Mac, all extras)

- **Both refs measured in this session** (risk 10), same shell:

  | ref | collected | full run |
  |---|---|---|
  | `main` @ `b35df86` | **3060** | **3060 passed, 0 failed, 0 skipped, 2 warnings, 253.64 s** |
  | branch @ `891e798` | 3063 | **3063 passed, 0 skipped, 2 warnings, 237.39 s** |
  | branch @ `78c2b4b` | **3063** | **3063 passed, 0 skipped, 2 warnings, 232.39 s** |

  **Note the previous handoff's 3042 was stale** — its review round added tests
  after the measurement it published. Measure both refs yourself; that is
  risk 10 and it caught this.
- The 2 warnings are the pre-existing `websockets` deprecations (**#25**), so
  **#321's acceptance signal still holds**: no third warning, and `pyproject`'s
  `error::PytestUnraisableExceptionWarning` would have made one a failure.
- Suite is **21 s faster** (253.6 → 232.4 s): the removed grace waits and sleeps.
- `mypy src/localmail` → Success, **152** files. `mypy tests/_gated_supervisor.py
  tests/test_gated_supervisor.py` → Success.
- `ruff check src/localmail/` → **10**, the unchanged #285 baseline (no `src/`
  file was touched this session).
- **#299 reproduction evidence**, all measured here: instrumented margin
  **6.7 ms / 3000 ms**; control **20/20 clean**; interference **8/8 failed**
  (2 with both tests failing).
- GUI untouched — no vitest/cargo run needed.
- **README needs no update** (checked): it documents no dependency floors, and
  the gated supervisor is a test-internal convention, which is CLAUDE.md
  territory. **CLAUDE.md was updated** — the transformers paragraph is rewritten
  from "deliberately NOT bumped" to the shipped floor, plus a Testing-notes
  entry for #299 and the Layout line for the new helper.
- **There is still no ROADMAP.md** — that `/nextsession` step remains a no-op
  (risk 30, re-confirmed a **fifth** time).

## What's next

### 0. **Merge the PR, then check the issue and the alert actually closed**
   **You merge** (project convention). The body uses `Closes #299`. Risk 2 says
   verify with `gh issue list` afterwards rather than assume.
   - **Also check Dependabot goes to 0** — this is the first session that can
     reach zero, so it is worth confirming rather than assuming; the alert is
     on `uv.lock`, which this diff changes.
   - **Both hosts want a sync after merging**: this diff changes `uv.lock`
     (transformers/safetensors/tokenizers). The Mac daemon runs an editable
     install (risk 19) and the tree is on the branch, so `git checkout main`
     too. DGX: `~/.local/bin/uv sync --extra mcp --extra extraction`.

### 1. **#324 — the blank-query/`sort="rank"` cursor wart** *(carried)*
   A blank query is served by the date branch whatever the `sort`, so
   `sort="rank"` is accepted on page 1 and its own cursor rejected on page 2.
   Its surface is wider than "blank query" — the branch predicate runs *after*
   `parse_query`, so `subject:invoice` takes the same path. **Acceptance:** the
   stated `rank` is refused on **page 1**, wire-visibly, with README updated.
   **Do not** "fix" it by having the cursor record the sort the caller *stated*
   rather than the one that ran — a cursor claiming an ordering it did not walk
   is #308 itself.

### 2. **The #317 API-keys review round** *(carried)*
   **#319** (`ApiKeyNotFound` carries three meanings; the panel reports a
   security refusal as success) · **#318** (the `api_key_name` ⟺ `is_service`
   pairing is unenforced in the schema — sketch with a generated column + FK is
   in the issue; **next free migration slot is `0037_*.sql`**) · **#320**
   (admin panel routes do blocking DB IO on the event loop).

### 3. **The #322/#332 review round leftovers** *(carried)*
   **#327** (`CursorPlan` carries two fields its pool-mode consumer must
   ignore) · **#328** (the page-cache entry is an untyped dict) · **#330**
   (`SortOrder`/`SortMode` restated in three wire layers) · **#331**
   (`SortOrderNotApplicable`'s stated audience is wrong).

### 4. **#340 — the harness lock proves a lock was taken, not which database**
   *(carried, filed last session)*. The AST rule compares call *positions* and
   never arguments, so locking one database while working against another
   passes. Latent — all five harnesses use one `dsn`. A correct check needs
   parameter-flow analysis across the helper walk.

### 5. **#305 — `--version` dies on a missing third-party dependency** *(carried)*
   `cli.py` imports the daemon at module scope. **Acceptance:** blocking
   `sqlparse` on `sys.meta_path` leaves `localmail --version` printing its line
   and exiting 0. **Do it with the `cli.py` refactor, not before** — `cli.py` is
   **2177 lines**.

### 6. **#285 — ruff, repo-wide** *(carried)*
   **10** pre-existing errors in `src/` (two are the `math` import/redefinition
   pair in `searcher.py`), plus **1 F841 in `tests/acceptance/run_recall_eval.py`**.
   9 dead `# noqa: S608`, no `[tool.ruff]`, no CI step. **Decide the config and
   the CI step together** — that decision is the operator's.

### 7. **Admin GUI phase 5 — Users & ACL panel** *(carried)*
   `/v1/admin/users` is already `require_admin()` — **no backend work.**
   Mirror [serve/admin/users_panel_router.py](src/localmail/serve/admin/users_panel_router.py),
   follow the Daemon-panel shape, surface the two lock-out guards as 409s, and
   **stub the new API module in both `AdminView.test.ts` and `MainView.test.ts`**
   (risk 25).

### 8. **Remaining robustness backlog** *(carried)*
   **#218** · **#226** · **#225 / #227** · **#200 / #211 / #208** · **#206** ·
   **#204** · **#25**.

### 9. **Smaller, deliberately not done** *(carried)*
   - **165 docling failures on the Mac**, of 182 `blobs_gave_up`. Worth a fresh
     look **now that the pypdf bump has landed on the host** — three of the four
     advisories fixed are unbounded-runtime bugs on the extraction path.
     **Re-measure before claiming it; that is a hypothesis, not a finding.**
   - **Residual implausible language labels dominated by `ja`** (~0.24%); the
     confidence-floor lever was measured useless. **Sample `ja` first.**
   - **The DGX drops remain uninvestigated** (risk 24).
   - **A session-22 stash is still on the stack** (`stash@{0}: On
     docs/session-22-handoff: review-fixes`); its content is on `main`, so
     `git stash drop` it if you want the tree tidy.
   - **A stray SDD workspace** at `.superpowers/sdd/`. Git-ignored scratch.

## Open decisions & risks

1. **One PR is open and yours to merge.** `fix/299-flaky-lifecycle-pins-and-transformers`,
   based on `main` (`b35df86`), closing **#299**. **22 open issues**, dropping
   to **21**. **Dependabot 1 → 0.**
2. **A merge does NOT close issues its subject merely names** *(carried)*. Use
   `Closes #N` in the **PR body** and **check `gh issue list` after the merge**.
   Three rounds running now.
3. **THIS FILE IS NOT THE AUTHORITY — `git` and `gh` are** *(carried,
   seven-times earned)*. Open every session with `git fetch --prune && git log
   --oneline -1 origin/main`, `gh pr list`, `gh issue list`, the Dependabot
   query, and reconcile **before acting**. It paid out again in a small way:
   this file's own predecessor quoted a stale test count (3042 vs the real
   3060), which only re-measuring caught.
4. **Base every branch on `main`; put a session's code and its handoff in ONE
   PR** *(carried)*. Session 25's stacked handoff PR merged into an
   already-merged branch 13 seconds after it landed and was lost silently.
   - The stranded-branch check is **NOISY**: `git log --oneline main..origin/<b>`
     is non-empty for every squash-merged branch. Signal = non-empty on a branch
     whose PR merged *recently*, confirmed by a **content** diff
     (`git diff --stat main origin/<branch>` empty ⇒ it landed).
5. **An issue's stated cause is a hypothesis; check it against the issue's own
   reported frequency first** *(new — this is #299's whole lesson)*. #299 blamed
   a wall-clock race while reporting the test failing in 3 of 3 runs. A 450×
   timing margin and a near-deterministic failure cannot both be true. **The
   cheapest experiment is usually instrumenting the margin, not re-running the
   test** — session 36 ran it 20 times, got 0 failures, and could conclude
   nothing; one instrumented run refuted the theory outright.
6. **To reproduce a concurrency bug the guard now prevents, interfere from
   OUTSIDE pytest** *(new)*. #336's lock makes a second pytest session wait, so
   it cannot be used to reproduce #329/#335 any more. A plain psycopg script
   performing the same per-test `TRUNCATE` takes no lock and reproduces it
   faithfully — that is also exactly the #337 scenario. Point it at
   `localmail_test`, **never** the live `localmail`.
7. **A dependency floor that a vulnerable version satisfies is not a floor —
   and a package with NO floor is worse** *(carried, extended)*. `icalendar>=6.0`
   read as unaffected by a `>= 7.1.0, < 7.1.3` advisory. `transformers` had no
   declaration at all, so there was nothing to misread and nothing to constrain
   a re-resolution. **Read `vulnerable_version_range` against `uv.lock`, always.**
   Corollary: this repo's floors carry their *reason* in a comment — keep that
   up.
8. **`uv lock --dry-run` is read-only but not predictive** *(new)*. Verified
   read-only again this session (`uv.lock` byte-identical across it). But the
   shipped re-lock moved three packages where the dry-run had reported two,
   because declaring a floor changes the resolution and releases land in
   between. Quote a dry-run as a dated measurement, never as the plan.
9. **NEVER run two pytest sessions against one test database** *(carried —
   enforced since #336, and since #337 the acceptance harnesses are inside the
   same guard)*. The second **waits**. To run both, give one its own
   `LOCALMAIL_TEST_DSN`. A harness refused by the lock exits **3**, distinct
   from the **1** an eval returns when it fails its own gates.
10. **A test module must import `localmail.serve.app` at MODULE scope**
    *(carried — #321)*. An AST scan plus a teardown re-check enforce it. If it
    fires, hoist the import — do not relax the rule.
11. **When a guard is an AST rule, mutate BOTH branches of every predicate**
    *(carried — and it paid out again here, on my own new test)*. A guard, or a
    pin, with an untested arm is half off. Related: **never assert a substring
    the message's own remedy text contains**, and **never compare a constant
    against itself**.
12. **Verify host revisions; do not infer them** *(carried, earned twice)*.
    `ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'` plus
    `/v1/version`'s `build_hash` settles it. **Both hosts were left untouched
    this session, and BOTH need a sync after this merge** — `uv.lock` changed.
13. **When reverting a mutation, restore from a file copy — never `git
    checkout`** *(carried, sessions 23–38)*. Used ~10 times this session.
    **Re-snapshot after each GREEN**, not once at the start. Treat **empty**
    pytest output as a failed mutation, not a pass.
14. **Test-count baselines: measure both refs IN THE SAME SESSION** *(carried,
    and it caught a stale number this time)*. `main` **3060**, branch **3063**.
15. **A keyset predicate must be a row comparison, in BOTH directions**
    *(carried — #75, #322, #323)*. The OR-form is semantically identical, keeps
    the index scan, adds no Sort, and plans as a per-tuple `Filter` that rescans
    from the index head on every page. Written by mistake **twice**.
    `tests/test_searcher_sort_order_plan.py` keeps three negative controls —
    **do not "tidy" them away**.
16. **The undated block is where the two directions genuinely differ**
    *(carried)*. Descending must *admit* it (hence the top-up query); ascending
    meets it at the head of its own walk. **Do not "restore symmetry"**, and
    **do not "fix" a short page by restoring `OR expr IS NULL`**.
17. **A cursor identifies a position, not a query — with exactly one enforced
    exception** *(carried — #326)*. Dropping the query a **text** cursor was
    minted from is a 400; archive-walk cursors page with or without one.
18. **Never state a `sort` on a request that carries a cursor** *(carried —
    #308, #311)*. A paging client must treat **409 as recoverable** and **400
    as permanent for that cursor**.
19. **The stale NOTIFY queue recurs, and gate 1 lies** *(carried)*. Exactly
    three LISTEN/NOTIFY failures usually means the stale queue — but session 35
    had that exact failure set with **both gates clean** (it was contention).
    **Check both gates before reaching for the runbook.**
20. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `uv sync --all-extras` (Mac) / `--extra mcp --extra extraction` (DGX).
    **`uv` is not on the DGX's non-interactive PATH** — use `~/.local/bin/uv`.
    **A branch checkout re-resolves the venv** — re-sync after any checkout you
    intend to measure against.
21. **The Mac's launchd daemon runs an EDITABLE install** *(carried)*. It
    executes whatever the tree is checked out to. **Currently on
    `fix/299-flaky-lifecycle-pins-and-transformers`** — check out `main` after
    merging, and sync deliberately: `uv.lock` changed.
22. **`search-status` is sub-second** *(carried)*. If it runs long that is a
    **regression of #280** — check `EXPLAIN (FORMAT JSON)` for a `Seq Scan on
    messages` under a `SubPlan` first.
23. **`blobs_claimable 0` alongside `blobs_pending 0` is the SETTLED shape**
    *(carried)*. Mac `9543 = 9255 + 106 + 182 + 0`; DGX `4405 = 4187 + 91 + 127
    + 0`. A steady non-zero `blobs_no_text` is **normal** (#277);
    **`blobs_gave_up` is the one to act on**. *(Not re-measured this session —
    no archive work was done.)*
24. **`uv run pytest -q` with NO arguments is the right command** *(carried)*.
    **Do not run the suite while a backfill is draining.**
25. **An empty `daemon_heartbeats` right after a restart is normal for minutes**
    *(carried — #269/#271)*: the startup blob-temp sweep. Grep for
    **`blob-temp sweep done: walked=`**.
26. **`--version`'s contract is six things, all pinned** *(carried)*. **Never
    reintroduce `@click.version_option` in any spelling** — an AST pin forbids
    it, covering `daemon_cli.py` too. stderr non-empty ⟺ unresolvable.
27. **The DGX drops are STILL UNEXPLAINED — five theories refuted** *(carried)*.
    **Do not propose a sixth without a captured outage in which the host was
    demonstrably up throughout.** Triage with `journalctl --list-boots` first.
    Power is not a candidate (~5-day UPS). **Do not edit
    `/etc/wireguard/wg0.conf`.**
28. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`.
    **Run vitest from `gui/`, not the repo root.** `cargo clippy --all-targets`
    is clean but **ungated** — CI runs clippy without it.
29. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    **#266's whitespace-heal is a one-way door too.** **`--relabel` is the only
    destructive verb in the lang path**; reach for `--retry-declined` first.
30. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    kept though #245 is fixed; #246 warns on group-write; `InsecureSecretsFile`
    refuses; #239's manual tombstone retention deliberate; admin bearer tokens
    have no per-token scope (#204).
31. **CI reports `1 skipped` on Linux; macOS reports 0** *(carried)*. It is
    pre-existing and still unidentified. **Do not read it as a missing uv
    extra** without checking a `main` run first. Expect CI here to read
    **3062 passed, 1 skipped**.
32. **No ROADMAP.md** *(carried, re-confirmed a fifth time)* — that
    `/nextsession` step is a no-op. **CLAUDE.md was updated; README was checked
    and needs nothing** this session.
33. **A green local run is still not evidence** *(carried)*. The local Postgres
    is more permissive than CI's. **Any test whose subject is "this process
    refuses / exits non-zero" must assert *why*** — a different failure is also
    non-zero. Push and let CI decide.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status && git branch --show-current
git log --oneline -1 origin/main         # THE authority, not this file's header
git log --oneline main..origin/main      # non-empty = a session landed since

# RISK 2 — after any merge, CHECK THE ISSUE ACTUALLY CLOSED.
gh pr list
gh issue list --limit 40                 # 22 open; the PR should take it to 21

# RISK 7 — expect ZERO for the first time. Confirm, don't assume.
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '[.[] | select(.state=="open")] | length'      # expect 0
# If a new one appears, read its range against THE LOCK, never the floor:
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | "\(.security_advisory.severity)\t\(.dependency.package.name)\t\(.security_vulnerability.vulnerable_version_range)\tfixed in \(.security_vulnerability.first_patched_version.identifier)"'
grep -A1 '^name = "<pkg>"' uv.lock

# RISK 4 — stranded-branch shortlist (noisy: every squash-merged branch shows).
for b in $(git branch -r --format='%(refname:short)' | grep -v 'origin/main\|HEAD'); do
  n=$(git log --oneline main.."$b" | wc -l); [ "$n" -gt 0 ] && echo "$b: $n commits not on main"
done
git diff --stat main origin/<branch>     # EMPTY = landed, not stranded

# AFTER MERGING — BOTH hosts need a sync; uv.lock changed (risk 20/21).
#   Mac:  git checkout main && git pull && unset VIRTUAL_ENV && uv sync --all-extras
#   DGX:  ssh 10.0.0.3 'cd ~/src/localmail && git pull && \
#           ~/.local/bin/uv sync --extra mcp --extra extraction' \
#         && ssh 10.0.0.3 'systemctl --user restart localmail-daemon localmail-serve'

# Python suite. NEVER a bare `uv sync` (risk 20).
unset VIRTUAL_ENV && uv sync --all-extras
unset VIRTUAL_ENV && uv run pytest -q
#   macOS: expect 3063 passed, 0 failed, 0 skipped, and **2 warnings**, ~232s.
#   THOSE 2 ARE #321's ACCEPTANCE SIGNAL: both are the pre-existing websockets
#   DeprecationWarnings (#25). A leaked pool is now a FAILING test whose
#   message reads "cannot join current thread". The test it names is arbitrary
#   — the GC picks it; the message is the diagnosis. Check for an import of
#   localmail.serve.app that is not at module scope.
#   LINUX/CI: expect 3062 passed, 1 SKIPPED; pre-existing (risk 31).
#   MEASURE BOTH REFS IN THIS SESSION (risk 14) — no DB needed:
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2   # 3063 here
unset VIRTUAL_ENV && uv run mypy src/localmail    # expect Success, 152 files
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -2   # expect 10 (#285)

# #299, verified directly — the two rewritten pins and the double's own tests:
unset VIRTUAL_ENV && uv run pytest -q tests/test_gated_supervisor.py \
  tests/test_serve_daemon_routes.py tests/test_daemon_supervisor.py \
  tests/test_daemon_extract_thread.py tests/test_daemon_control_socket.py
#   expect 63 passed in ~11s

# REPRODUCING A #329/#335-CLASS BUG NOW THAT #336 PREVENTS IT (risk 6).
# A second pytest session just waits, so interfere from OUTSIDE pytest.
# Point it at localmail_test — NEVER the live localmail.
cat > /tmp/interferer.py <<'INT'
import sys, time, psycopg
DSN = "postgresql://localmail:local%40%40mail@localhost:5532/localmail_test"
TABLES = ("accounts, mailboxes, messages, message_labels, attachment_blobs, "
  "failed_messages, message_chunks, failed_embeddings, embedding_models, "
  "failed_chunkings, attachment_text, attachment_chunks, failed_extractions, "
  "api_users, api_tokens, user_accounts, api_login_attempts, daemon_commands, "
  "daemon_heartbeats, import_jobs, oauth_clients, oauth_registration_attempts, "
  "channel_subscriptions, transient_fetches")
deadline = time.monotonic() + float(sys.argv[1])
while time.monotonic() < deadline:
    with psycopg.connect(DSN) as c:
        c.cursor().execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"); c.commit()
    time.sleep(0.15)
INT
unset VIRTUAL_ENV && uv run python /tmp/interferer.py 60 &
#   then run the suspect tests in the foreground; 8/8 failed for #299's pair.

# THE POOL-LEAK PROBE (risk 10) — reusable; this is what found #321's second seam.
# Reports every unclosed pool WITH THE STACK THAT BUILT IT, which the warning never does.
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
#   expect: "0 UNCLOSED"

# RISK 9 — the test-database lock covers harnesses too (#336 + #337).
# If a run seems to hang at startup, look for this line; it is not a fault:
#   "waiting for another test run to release the test database ..."
# To run two at once, give one its own database:
#   LOCALMAIL_TEST_DSN=postgresql://localmail:local%40%40mail@localhost:5532/localmail_test2 uv run pytest -q

# If EXACTLY the three LISTEN/NOTIFY tests fail, CHECK BOTH GATES (risk 19):
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'
# Only if a gate errors, use runbook Option A (bootout, verify while down, bootstrap).

# Host health (Mac) — NOT re-measured this session; no archive work was done:
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 25)

unset VIRTUAL_ENV && uv run localmail search-status    # UNDER A SECOND (risk 22)
#   Check the SHAPE, not the literals: the four buckets must sum to
#   blobs_eligible, and claimable must equal pending.
#   The pypdf bump has now reached the host, so blobs_gave_up is worth a second
#   look (item 9) — but RE-MEASURE; do not assume the bump fixed anything.

# The DGX (risk 12 — verify, never infer):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'   # binds 10.0.0.3, NOT localhost
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — REQUIRED only if you touch gui/ (MUST run from gui/ — risk 28):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
#   expect: 0 svelte-check errors, 408 vitest, 104 cargo
```

`main` tip at session start was **`b35df86`**. This session left **one PR** open
on `fix/299-flaky-lifecycle-pins-and-transformers` — `891e798` (#299),
`78c2b4b` (the transformers floor) and the handoff commit — closing **#299**.
Latest migration **`0036_api_keys.sql`**; next free slot `0037_*.sql` (this
session adds none). **Open issues: 22**, dropping to **21** on merge.
**Dependabot: 1 → 0.**
