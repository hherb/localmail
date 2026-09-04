# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-09-04 (session 40).** `main` was **`f1a2e34`** at the
> start — PR #342 merged by the operator, **#324 closed**, **Dependabot still
> 0**. This session opened **one PR** on `fix/344-search-argument-refused-base`,
> closing **#344** and most of **#331**.
>
> **The previous handoff was accurate on every fact this session checked**
> except the issue count, and that was not its fault: it predicted 21 open,
> and the count is **22**, because the operator filed **#344** and **#345**
> from the review of #342 *after* the handoff was written. Reconcile with
> `gh`, always (risk 3) — it worked exactly as intended here.
>
> **The published test count was 3153; `main` measures 3159.** This is *not*
> the stale-number failure of the last two sessions, and calling it that
> would be the wrong lesson. The previous session measured at its **first
> commit** (`eb9bffc`); PR #342 then gained **three more commits**, including
> a review round, before it merged. A count measured mid-PR is not the count
> of what lands. See risk 7.
>
> **The work: #344, plus #331 points 1, 3 and 4.** `Searcher.search` raises
> four sibling exceptions whose entire purpose is "map me to a 400", deriving
> straight from `ValueError` with no shared base — so `api/search.py`
> enumerated them by name, in **two different tuples on two branches**, each
> arguing per-member which were unreachable there. A fifth guard added
> without widening a tuple is an operator-facing **500**. Not hypothetical:
> #342 shipped exactly that hole, safe only because `KEYSET_SORT is
> TEXTLESS_SORT` — a decision made in a different module. `#331 point 4` asked
> for the same base class in the same words, so it came along.
>
> **Three lessons worth carrying.**
>
> **(1) An orphaned CPU burner from an earlier session ran for 32 hours and
> silently tripled a test run.** Eight `while :; do :; done` spin loops
> (PPID 1, ~1905 CPU-minutes *each*) were left over from a previous session's
> `test_gated_supervisor.py` load test — its `kill $BURN` never reached them
> because the subshells had been reparented to init. The suite crawled from
> 81% for twenty minutes; killing them took it to 100% in three. **When a
> measurement is inexplicably slow, look at `ps` before blaming the change.**
>
> **(2) "It's only a category error in an unreachable branch" is exactly when
> wording rots — and widening a catch can make it reachable.** #331 point 3
> (the `cursor:` prefix applied to everything the keyset branch caught) was
> unreachable, and #344's own fix — catching the whole family there — would
> have widened the set of members that could carry the wrong label. Fixing
> the two together was cheaper than either alone.
>
> **(3) Write the justification down, then try to refute it.** The first
> draft of the precedence change claimed it also saved a smart-rewrite round
> trip. It does not: the rewriter runs only under `parsed.free_text.strip()`
> and that guard fires only when the string is blank. Refuted in one probe,
> before it shipped — the claim is now recorded *as refuted* in both the code
> comment and CLAUDE.md, so nobody re-derives it.
>
> **Open issue count is 22, dropping to 21 on merge (#331 stays, trimmed to
> its point 2). Dependabot stays at 0.**

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

One code commit plus this handoff, one PR. The item was chosen by the operator
from the two issues filed against the #342 review.

### `2a5599b` — #344 (+ #331 points 1/3/4): one refusal family, caught as one

- **The family is
  [src/localmail/search/argument_errors.py](src/localmail/search/argument_errors.py)`::SearchArgumentRefused`**,
  with the four subclasses **moved out of `searcher.py`** (1439 → 1391 lines)
  the way `sort_axes`/`keyset_walk` were. It is the contract *between* the
  Searcher and every boundary that maps it, and stating the rule a new guard
  must join needs somewhere to state it. `searcher.py` re-exports the four, so
  every existing import path resolves; the base is new and has no legacy path,
  so boundaries import it from `argument_errors`.
- **Both api boundaries catch the base**, and the per-member reachability
  arguments are retired with the enumeration.
- **The membership checks on `sort`/`sort_order` deliberately stay out of the
  family** — a plain `ValueError`, because HTTP and MCP declare those as
  `Literal`s, so a bad value cannot arrive from the wire and there is no api/
  mapping to be caught by. Admitting them would claim a wire audience they do
  not have.
- **Two pin kinds, because either alone has a hole.** *Structural*: every
  exception class in the module inherits the base, so a fifth guard written in
  the right place joins by construction. *Behavioural*: the family is
  enumerated from the **type** (`__subclasses__`, transitively) and every
  member is driven through **both** `run_search` branches — so a member added
  later is in scope without the test being edited. A `_family()` returning
  `[]` would make the parametrised half vacuous, so that has its own control.
- **Precedence (#344's fold-in): a cursor problem outranks the textless rule,
  now at both layers.** `test_api_search_rank_without_text.py` states that as
  a *rule*; it held at the api boundary and was **inverted inside the
  Searcher**, so `search("", keyset_cursor=<text-walk>, sort="rank")` was
  answered differently over HTTP than from a library call. The walk guard
  moved ahead of the two sort guards.
  - **No wire behaviour changed.** The api boundary already reported the
    cursor, and both widened catches are unreachable on the branch that gained
    them (the fresh branch passes no cursor; the keyset branch passes
    `sort="date"`, for which `sort_applicability_error` is `None`).
- **#331 point 1** — `SortOrderNotApplicable`'s docstring called its api/ catch
  a backstop. **#324 falsified that** and nobody updated it: the gate and the
  Searcher judge different strings, and `'"'` is textless to the gate and text
  once the ACL token composes in. Corrected in place.
- **#331 point 3** — `cursor:` was written into the keyset branch's f-string,
  so it labelled everything caught there.
  `SearchArgumentRefused.wire_prefix` carries it now and both boundaries
  interpolate, so the label follows the **cause**, not the branch — the same
  derive-don't-restate call as `version_report`'s severity word (#302). The
  default is **empty rather than mandatory**, the opposite of `VersionSource`'s
  forced remedy: a forgotten prefix loses a word of context, an inherited wrong
  one makes a false claim.
- **#331 point 2 stays open** — `cli.py`'s search catches `RuntimeError` only,
  so a `--sort-order` flag added there would traceback. Latent; belongs with
  the `cli.py` refactor.

### The mutation battery — 12 mutations, all caught

Restored from a scratchpad copy every time, never `git checkout` (risk 13).
The harness was proven to actually run tests *before* it certified anything
(risk 5) — baseline 103, not "no tests ran".

| mutation | caught by |
|---|---|
| `SortNotApplicable` stops inheriting the base | 5 tests |
| `KeysetCursorUnusable` stops inheriting the base | 4 tests |
| fresh branch re-enumerates the old two members | 2 tests |
| keyset branch re-enumerates without `SortNotApplicable` (**the #342 hole**) | **1** test |
| both boundaries widen to bare `ValueError` | **1** test |
| walk guard moved back after the sort guards (**the precedence**) | **1** test |
| the textless guard deleted (positive control) | 12 tests |
| `_family()` returns `[]` (vacuity control) | **1** test |
| `keyset_walk_error` always refuses (positive control) | 15 tests |
| keyset branch hardcodes `cursor:` again (**the #331 mislabel**) | **1** test |
| `KeysetCursorUnusable` loses its prefix | 3 tests |
| the base defaults to `"cursor: "` (a wrong inherited claim) | 2 tests |

### Verification (this Mac, all extras)

- **Both refs measured in this session** (risk 7), clean tree at each:

  | gate | `main` @ `f1a2e34` | branch @ `2a5599b` |
  |---|---|---|
  | pytest collected | **3159** | **3180** |
  | pytest run | — | **3180 passed, 0 skipped, 2 warnings, 212s** |

  The +21 is 17 (`test_search_argument_errors`) + 4
  (`test_searcher_guard_precedence`).
- The 2 warnings are the pre-existing `websockets` deprecations (**#25**), so
  **#321's acceptance signal still holds**.
- `mypy src/localmail` → Success, **153** files (was 152 — the new module).
  `mypy` on the two new test files → Success.
- `ruff check src/localmail/` → **10**, the unchanged #285 baseline.
- **No GUI gates run, because no `gui/` file changed** (risk 28 scopes them to
  `gui/` edits). **Do not quote a vitest number from this file** — none was
  measured this session.
- **Live signal beyond pytest**: the Mac launchd daemon runs an editable
  install, so it was executing this branch during the session. Its 7
  `daemon_heartbeats` rows stayed under 25 s old — the module move and
  re-export work in a running process, not only under test.
- **README needs no update** — verified, not assumed: it documents wire
  behaviour, this change has none, and no user-facing doc names these types
  (only frozen `docs/superpowers/` plans and past handoffs do).
  **CLAUDE.md updated** — a new #344 entry, the #331 fold-in, the now-stale
  half of the `KEYSET_SORT` alias note corrected in place, and the Layout
  block names the new module.
- **There is still no ROADMAP.md** — that `/nextsession` step remains a no-op
  (risk 32, re-confirmed a **seventh** time).

## What's next

### 0. **Merge the PR, then check the issue actually closed**
   **You merge** (project convention). The body uses `Closes #344`. Risk 2 says
   verify with `gh issue list` afterwards — **22 → 21**.
   - **No `uv.lock` change**, so neither host needs a dependency sync. The
     Mac's editable install follows the tree, so `git checkout main` there
     after merging (risk 22).
   - **Decide what to do with #331.** Points 1, 3 and 4 are done; **point 2**
     (the CLI's `RuntimeError`-only catch) is not. Either trim the issue to
     point 2 or close it and fold that line into the `cli.py` refactor
     (#305's item). The PR body does **not** say `Closes #331`, deliberately.

### 1. **#345 — the GUI's Relevance radio is inert on a textless search** *(new)*
   Filed from the same review. `SearchBar.svelte` binds a radio to
   `search.snapshot.sort` (default `"rank"`), but a textless query is served
   **date-ordered**, and clicking Relevance re-submits and changes nothing —
   the inert-control pattern CLAUDE.md names as a defect (#148). Pre-existing;
   #324 made it load-bearing by turning "textless is date-ordered" into an
   explicit documented rule.
   - **Acceptance:** the ordering the client *shows* is the one that ran.
   - **The issue's option 1 is preferred and needs no server change**: infer it
     from the page that came back (the date walk returns a `KA|`/`K|` cursor
     and `search_token: null`). **Do not reproduce `parse_query` in the
     client** — `search_paging.ts` rejects that explicitly, and #324's own
     review already caught one regression from a client-side `query.trim()`.
   - Option 2 (a `sort_applied` wire field) is cleaner to consume but must
     land *with* its renderer (#278/#295 precedent).

### 2. **The #317 API-keys review round** *(carried)*
   **#319** (`ApiKeyNotFound` carries three meanings; the panel reports a
   security refusal as success) · **#318** (the `api_key_name` ⟺ `is_service`
   pairing is unenforced in the schema; **next free migration slot is
   `0037_*.sql`**) · **#320** (admin panel routes do blocking DB IO on the
   event loop).

### 3. **The #322/#332 review round leftovers** *(carried, and one is cheaper)*
   **#327** (`CursorPlan` carries two fields its pool-mode consumer must
   ignore) · **#328** (the page-cache entry is an untyped dict) · **#330**
   (`SortOrder`/`SortMode` restated in three wire layers).
   - **#330 is cheaper now**: `sort_axes.py` is the one authority for both
     axes, and `argument_errors.py` has established the pattern of a small
     module the wire layers import from.
   - **#331 is mostly done** — see item 0.

### 4. **#340 — the harness lock proves a lock was taken, not which database**
   *(carried)*. The AST rule compares call *positions* and never arguments, so
   locking one database while working against another passes. Latent — all five
   harnesses use one `dsn`. A correct check needs parameter-flow analysis.

### 5. **#305 — `--version` dies on a missing third-party dependency** *(carried)*
   `cli.py` imports the daemon at module scope. **Acceptance:** blocking
   `sqlparse` on `sys.meta_path` leaves `localmail --version` printing its line
   and exiting 0. **Do it with the `cli.py` refactor, not before** — `cli.py` is
   **2177 lines**. #331 point 2 belongs here.

### 6. **#285 — ruff, repo-wide** *(carried)*
   **10** pre-existing errors in `src/`, plus **1 F841** in
   `tests/acceptance/run_recall_eval.py`. 9 dead `# noqa: S608`, no
   `[tool.ruff]`, no CI step. **The config and the CI step are one decision,
   and it is the operator's.**

### 7. **Admin GUI phase 5 — Users & ACL panel** *(carried)*
   `/v1/admin/users` is already `require_admin()` — **no backend work.**
   Mirror [serve/admin/users_panel_router.py](src/localmail/serve/admin/users_panel_router.py),
   follow the Daemon-panel shape, surface the two lock-out guards as 409s, and
   **stub the new API module in both `AdminView.test.ts` and `MainView.test.ts`**
   (risk 25/28).

### 8. **Remaining robustness backlog** *(carried)*
   **#218** · **#226** · **#225 / #227** · **#200 / #211 / #208** · **#206** ·
   **#204** · **#25**.

### 9. **Smaller, deliberately not done** *(carried)*
   - **165 docling failures on the Mac**, of 182 `blobs_gave_up`. Worth a fresh
     look now the pypdf bump has been on the host for three sessions.
     **Re-measure before claiming it; that is a hypothesis, not a finding.**
     *(Not re-measured this session — no archive work was done.)*
   - **Residual implausible language labels dominated by `ja`** (~0.24%); the
     confidence-floor lever was measured useless. **Sample `ja` first.**
   - **The DGX drops remain uninvestigated** (risk 27).
   - **A session-22 stash is still on the stack** (`stash@{0}`); its content is
     on `main`, so `git stash drop` it if you want the tree tidy.
   - **A stray SDD workspace** at `.superpowers/sdd/`. Git-ignored scratch.

## Open decisions & risks

1. **One PR is open and yours to merge.** `fix/344-search-argument-refused-base`,
   based on `main` (`f1a2e34`), closing **#344**. **22 open issues**, dropping
   to **21**. **Dependabot stays 0.** #331 needs a decision (item 0).
2. **A merge does NOT close issues its subject merely names** *(carried)*. Use
   `Closes #N` in the **PR body** and **check `gh issue list` after the merge**.
3. **THIS FILE IS NOT THE AUTHORITY — `git` and `gh` are** *(carried, nine
   times earned; it paid again this session)*. The predicted issue count was
   21 and the real one was 22, because two issues were filed after the handoff
   was written. Open every session with `git fetch --prune && git log
   --oneline -1 origin/main`, `gh pr list`, `gh issue list`, the Dependabot
   query — and reconcile **before acting**.
4. **Base every branch on `main`; put a session's code and its handoff in ONE
   PR** *(carried)*. Session 25's stacked handoff PR merged into an
   already-merged branch and was lost silently.
   - The stranded-branch check is **NOISY**: `git log --oneline main..origin/<b>`
     is non-empty for every squash-merged branch. Signal = non-empty on a branch
     whose PR merged *recently*, confirmed by a **content** diff
     (`git diff --stat main origin/<branch>` empty ⇒ it landed).
5. **A machine that is inexplicably slow may be carrying someone else's
   garbage** *(new)*. Eight orphaned `while :; do :; done` spin loops (PPID 1,
   ~1905 CPU-minutes each, 32 hours old) from a previous session's
   `test_gated_supervisor.py` load test were burning 8 cores. A full suite
   crawled at 81% for twenty minutes; after `kill`, it finished in **212 s**.
   **`ps -eo pid,ppid,etime,time,command | grep "while :"` before concluding a
   change made things slow** — and if you ever launch burners, verify the
   cleanup, because a backgrounded subshell reparented to init survives
   `kill $BURN`.
6. **In zsh an unquoted `$VAR` does not word-split** *(carried)*. `uv run
   pytest -q $T` runs **zero** tests and prints `no tests ran in 0.00s`, which
   reads as benign. Use `${=T}`, and **always run the unmutated baseline
   through the same harness first** so a zero-test harness is caught before it
   certifies anything. Done this session: baseline 103.
7. **A count measured at your FIRST commit is not the count of what merges**
   *(new — sharpening risk 14, and a correction to how the last two handoffs
   framed this)*. The previous handoff published **3153**; `main` measures
   **3159**. It was not stale — it was measured at `eb9bffc`, and PR #342 then
   gained **three more commits** including a review round. **Re-measure both
   refs in the session that reports them, against a provably clean tree
   (`git status --short` empty), and prefer measuring the ref you are about to
   push.** Both numbers in this file were taken that way.
8. **Check `git ls-files <dir>` before Write-ing a "new" file, and read the
   tool's verb** *(carried)*. `Write` says *"has been updated successfully"*
   when it clobbers and *"File created successfully at"* when it does not.
   Checked for all three new files this session.
9. **"It's only a backstop" / "it's unreachable" is a claim to test** *(carried
   and extended)*. #324 found `run_search`'s `SortNotApplicable` catch was
   live, not a backstop. This session found the same of
   `SortOrderNotApplicable`'s docstring — **falsified by #324 and never
   updated** — and that #331's unreachable `cursor:` mislabel would have been
   *widened* by #344's own fix. Unreachable code is where wording rots.
10. **Write the justification down, then try to refute it** *(new)*. The
    precedence change was nearly justified as "it also saves a smart-rewrite
    round trip". It does not — the rewriter runs only under
    `parsed.free_text.strip()` and that guard fires only when the string is
    blank. One probe. The refutation is now recorded in the code comment and
    CLAUDE.md so it is not re-derived.
11. **A dependency floor that a vulnerable version satisfies is not a floor**
    *(carried)*. **Read `vulnerable_version_range` against `uv.lock`, never the
    declared floor.** `uv lock --dry-run` is read-only; `uv sync --dry-run` is
    **not**.
12. **NEVER run two pytest sessions against one test database** *(carried —
    enforced since #336; since #337 the acceptance harnesses are in the same
    guard)*. The second **waits**. To run both, give one its own
    `LOCALMAIL_TEST_DSN`. A harness refused by the lock exits **3**, distinct
    from the **1** an eval returns when it fails its own gates.
13. **A test module must import `localmail.serve.app` at MODULE scope**
    *(carried — #321)*. An AST scan plus a teardown re-check enforce it.
14. **When a guard is an AST rule, mutate BOTH branches of every predicate**
    *(carried)*. Related: **never assert a substring the message's own remedy
    text contains**, and **never compare a constant against itself**.
15. **When reverting a mutation, restore from a file copy — never `git
    checkout`** *(carried, sessions 23–40)*. Used 12 times this session.
    **Re-snapshot after each GREEN**, not once at the start.
16. **Verify host revisions; do not infer them** *(carried)*.
    `ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'` plus
    `/v1/version`'s `build_hash` settles it. **Both hosts were left untouched
    and neither needs a dependency sync** — `uv.lock` is unchanged.
17. **A keyset predicate must be a row comparison, in BOTH directions**
    *(carried — #75, #322, #323)*. The OR-form is semantically identical, keeps
    the index scan, adds no Sort, and plans as a per-tuple `Filter` that
    rescans from the index head on every page. Written by mistake **twice**.
    `tests/test_searcher_sort_order_plan.py` keeps three negative controls —
    **do not "tidy" them away**.
18. **The undated block is where the two directions genuinely differ**
    *(carried)*. Descending must *admit* it (hence the top-up query); ascending
    meets it at the head of its own walk. **Do not "restore symmetry"**, and
    **do not "fix" a short page by restoring `OR expr IS NULL`**.
19. **A cursor identifies a position, not a query — with one enforced
    exception** *(carried — #326)*. Dropping the query a **text** cursor was
    minted from is a 400; archive-walk cursors page with or without one.
20. **Never state a `sort` the server cannot serve** *(carried — #308, #311,
    #324)*. Two shapes: a request carrying a **cursor**, and a `rank` on a
    query with **no free text**. A paging client treats **409 as recoverable**
    and **400 as permanent for that cursor**. `search_paging.ts::statedSort`
    makes both unreachable from the GUI by construction.
21. **`sort` and `sort_order` resolve from the query, not from a constant**
    *(carried — #324)*. `sort_axes.resolve_sort` is the one authority. **Do
    not** "fix" a related wart by having a cursor record the sort the caller
    *stated* rather than the one that ran — that is #308 itself.
22. **A new Searcher guard over a stated argument must subclass
    `SearchArgumentRefused`** *(new — #344)*. Both api boundaries catch the
    family, so inheriting is the whole of the wiring; not inheriting is a 500.
    The membership checks on `sort`/`sort_order` are the deliberate exception —
    a plain `ValueError`, because HTTP and MCP declare `Literal`s and they
    cannot arrive from the wire. **Put the wire label on the exception
    (`wire_prefix`), never in the catching branch's f-string.**
23. **The stale NOTIFY queue recurs, and gate 1 lies** *(carried)*. Exactly
    three LISTEN/NOTIFY failures usually means the stale queue — but session 35
    had that failure set with **both gates clean** (it was contention).
    **Check both gates before reaching for the runbook.**
24. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `uv sync --all-extras` (Mac) / `--extra mcp --extra extraction` (DGX).
    **`uv` is not on the DGX's non-interactive PATH** — use `~/.local/bin/uv`.
    **A branch checkout re-resolves the venv** — re-sync after any checkout you
    intend to measure against.
25. **The Mac's launchd daemon runs an EDITABLE install** *(carried)*. It
    executes whatever the tree is checked out to. **Currently on
    `fix/344-search-argument-refused-base`** — check out `main` after merging.
    No dependency sync needed. Its heartbeats were verified healthy under this
    branch, which is a genuine end-to-end signal for a module move.
26. **`search-status` is sub-second** *(carried)*. If it runs long that is a
    **regression of #280** — check `EXPLAIN (FORMAT JSON)` for a `Seq Scan on
    messages` under a `SubPlan` first.
27. **`blobs_claimable 0` alongside `blobs_pending 0` is the SETTLED shape**
    *(carried)*. A steady non-zero `blobs_no_text` is **normal** (#277);
    **`blobs_gave_up` is the one to act on**. *(Not re-measured this session.)*
28. **`uv run pytest -q` with NO arguments is the right command** *(carried)*.
    **Do not run the suite while a backfill is draining** — or, as this session
    learned, while eight orphaned spin loops are running (risk 5).
29. **An empty `daemon_heartbeats` right after a restart is normal for minutes**
    *(carried — #269/#271)*: the startup blob-temp sweep. Grep for
    **`blob-temp sweep done: walked=`**.
30. **The DGX drops are STILL UNEXPLAINED — five theories refuted** *(carried)*.
    **Do not propose a sixth without a captured outage in which the host was
    demonstrably up throughout.** Triage with `journalctl --list-boots` first.
    Power is not a candidate (~5-day UPS). **Do not edit
    `/etc/wireguard/wg0.conf`.**
31. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`.
    **Run vitest from `gui/`, not the repo root.** `cargo clippy --all-targets`
    is clean but **ungated** — CI runs clippy without it.
32. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    **#266's whitespace-heal is a one-way door too.** **`--relabel` is the only
    destructive verb in the lang path**; reach for `--retry-declined` first.
33. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    kept though #245 is fixed; #246 warns on group-write; `InsecureSecretsFile`
    refuses; #239's manual tombstone retention deliberate; admin bearer tokens
    have no per-token scope (#204).
34. **CI reports `1 skipped` on Linux; macOS reports 0** *(carried)*. It is
    pre-existing and still unidentified. **Do not read it as a missing uv
    extra** without checking a `main` run first. Expect CI here to read
    **3179 passed, 1 skipped**.
35. **No ROADMAP.md** *(carried, re-confirmed a seventh time)* — that
    `/nextsession` step is a no-op. **CLAUDE.md was updated; README was
    checked and correctly needed nothing.**
36. **A green local run is still not evidence** *(carried)*. The local Postgres
    is more permissive than CI's. **Any test whose subject is "this process
    refuses / exits non-zero" must assert *why***. Push and let CI decide.

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
gh issue view 331                         # decide: trim to point 2, or close

# RISK 11 — expect ZERO, still. Confirm, don't assume.
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '[.[] | select(.state=="open")] | length'      # expect 0
# If a new one appears, read its range against THE LOCK, never the floor:
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | "\(.security_advisory.severity)\t\(.dependency.package.name)\t\(.security_vulnerability.vulnerable_version_range)\tfixed in \(.security_vulnerability.first_patched_version.identifier)"'
grep -A1 '^name = "<pkg>"' uv.lock

# RISK 5 — BEFORE trusting any timing, check for orphaned CPU burners.
# Eight of these (PPID 1, 32h old) tripled a suite run this session.
ps -eo pid,ppid,etime,time,command | grep "[w]hile :; do :; done"
#   kill them with:  ps -eo pid,ppid,command | grep "[w]hile :; do :; done" \
#                      | awk '$2==1 {print $1}' | xargs -r kill

# RISK 4 — stranded-branch shortlist (noisy: every squash-merged branch shows).
for b in $(git branch -r --format='%(refname:short)' | grep -v 'origin/main\|HEAD'); do
  n=$(git log --oneline main.."$b" | wc -l); [ "$n" -gt 0 ] && echo "$b: $n commits not on main"
done
git diff --stat main origin/<branch>     # EMPTY = landed, not stranded

# AFTER MERGING — the Mac tree only; uv.lock did NOT change this session.
#   git checkout main && git pull        # editable install follows the tree (risk 25)

# Python suite. NEVER a bare `uv sync` (risk 24).
unset VIRTUAL_ENV && uv sync --all-extras
unset VIRTUAL_ENV && uv run pytest -q
#   macOS: expect 3180 passed, 0 failed, 0 skipped, and **2 warnings**, ~212s
#   on an UNLOADED machine. THOSE 2 ARE #321's ACCEPTANCE SIGNAL: both are the
#   pre-existing websockets DeprecationWarnings (#25). A leaked pool is now a
#   FAILING test whose message reads "cannot join current thread"; the test it
#   names is arbitrary (the GC picks it), the message is the diagnosis.
#   LINUX/CI: expect 3179 passed, 1 SKIPPED; pre-existing (risk 34).
#   MEASURE BOTH REFS IN THIS SESSION (risk 7) — no DB needed:
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2   # 3180 here
git checkout main && unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2
#   main @ f1a2e34 = 3159.  Assert `git status --short` is EMPTY at each.
unset VIRTUAL_ENV && uv run mypy src/localmail    # expect Success, 153 files
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -2   # expect 10 (#285)

# #344 + #331, verified directly — the family, both boundaries, the precedence:
unset VIRTUAL_ENV && uv run pytest -q tests/test_search_argument_errors.py \
  tests/test_searcher_guard_precedence.py tests/test_searcher_keyset_guard.py \
  tests/test_api_search_rank_without_text.py tests/test_api_search_cursor_mode.py \
  tests/test_api_search_cursor_walk.py tests/test_searcher_rank_without_text.py
#   expect 134 passed in ~1.3s

# RISK 6 — the mutation harness MUST word-split. zsh does not do it for you:
T="tests/test_search_argument_errors.py tests/test_searcher_guard_precedence.py"
uv run pytest -q ${=T}       # ${=T}, NOT $T. $T runs ZERO tests and says
                             # "no tests ran in 0.00s", which reads as benign.

# The #344 precedence probe — the two layers used to disagree here:
unset VIRTUAL_ENV && uv run python - <<'PY'
from unittest.mock import MagicMock
from localmail.config import SearchConfig
from localmail.search.searcher import Searcher, KeysetCursor
from localmail.api.search_cursor import resolve_cursor_plan, encode_keyset_cursor
from localmail.api.errors import ValidationFailed
class _E:
    name = model = "s"; dimension = 768
    def embed_documents(self, t): raise AssertionError
    def embed_query(self, t): raise AssertionError
    def health_check(self): pass
pool = MagicMock(); pool.connection.side_effect = AssertionError("no IO")
s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None, rewriter=None)
ks = KeysetCursor(ts=None, id=5, order="desc", walk="text")
try: s.search("", keyset_cursor=ks, sort="rank", allowed_account_ids=None)
except Exception as e: print("Searcher ->", type(e).__name__)
try: resolve_cursor_plan(cursor=encode_keyset_cursor(ks), requested_sort="rank",
                         requested_sort_order=None, free_text="")
except ValidationFailed as e: print("api      -> cursor diagnosis")
PY
#   expect: Searcher -> KeysetCursorUnusable  (it used to say SortNotApplicable)

# The #324 divergence probe (risk 9) — why run_search's catches are not dead:
unset VIRTUAL_ENV && uv run python -c "
from localmail.search.query import parse_query
from localmail.api.search import build_query_string
raw = 'from:\"'
print('gate  :', repr(parse_query(raw).free_text))
print('branch:', repr(parse_query(build_query_string(free_text=raw,
      filters={'account_ids': ['1']})).free_text))"
#   expect gate='from:' branch='' — the two guards read different strings.

# THE POOL-LEAK PROBE — reusable; this is what found #321's second seam.
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

# RISK 12 — the test-database lock covers harnesses too (#336 + #337).
# If a run seems to hang at startup, look for this line; it is not a fault:
#   "waiting for another test run to release the test database ..."
# To run two at once, give one its own database:
#   LOCALMAIL_TEST_DSN=postgresql://localmail:local%40%40mail@localhost:5532/localmail_test2 uv run pytest -q

# If EXACTLY the three LISTEN/NOTIFY tests fail, CHECK BOTH GATES (risk 23):
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'
# Only if a gate errors, use runbook Option A (bootout, verify while down, bootstrap).

# Host health (Mac) — verified healthy under this branch this session:
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 29)

unset VIRTUAL_ENV && uv run localmail search-status    # UNDER A SECOND (risk 26)
#   Check the SHAPE, not the literals: the four buckets must sum to
#   blobs_eligible, and claimable must equal pending.

# The DGX (risk 16 — verify, never infer):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'   # binds 10.0.0.3, NOT localhost
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — REQUIRED only if you touch gui/ (MUST run from gui/ — risk 31).
# NOT RUN THIS SESSION (no gui/ file changed), so no number is published here.
# Measure it yourself before quoting one (risk 7):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

`main` tip at session start was **`f1a2e34`**. This session left **one PR** open
on `fix/344-search-argument-refused-base` — `2a5599b` (#344 + #331 points 1/3/4)
and the handoff commit — closing **#344**. Latest migration
**`0036_api_keys.sql`**; next free slot `0037_*.sql` (this session adds none).
**Open issues: 22**, dropping to **21** on merge. **Dependabot: 0.**
