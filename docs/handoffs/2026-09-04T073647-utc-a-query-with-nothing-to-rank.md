# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-09-04 (session 39).** `main` was **`732d97c`** at the
> start — PR from session 38 merged by the operator, **#299 closed**, **#341
> closed**, and **Dependabot at 0 for the first time in this project's recorded
> sessions**, exactly as the previous handoff predicted. This session opened
> **one PR** on `fix/324-blank-query-stated-rank`, closing **#324**.
>
> **The previous handoff was accurate on every fact this session checked** —
> `main` tip, 22→21 open issues, Dependabot 1→0 — **except one measurement**,
> and it is the same class of error as last time: it published **408** as the
> vitest baseline. The real number on `main` is **423**. Two sessions running,
> a published test count has been stale. Risk 14 holds, and its scope should
> now be read as *every* count, not just pytest's.
>
> **The work: #324, option (2).** A query with no free text — blank, *or made
> only of filter operators*, since the branch predicate runs after
> `parse_query` lifts them out — has always been served by the date walk,
> because the lexical arms early-return with no terms and the vector arms
> would rank by distance to the embedding of the empty string. The stated
> `sort` was dropped in silence. #322 gave that walk a cursor recording the
> ordering that *ran*, so the silent drop became a contradiction one page
> later: `{"query": "", "sort": "rank"}` accepted on page 1, its own cursor
> refused on page 2. It is reported at page 1 now.
>
> **Three lessons worth carrying.**
>
> **(1) In zsh, an unquoted `$VAR` does not word-split — and a mutation battery
> that runs zero tests prints `no tests ran`, which is easy to read as a
> pass.** The first battery this session ran `uv run pytest -q $T` with `T` a
> space-separated list of paths; zsh passed it as one word, pytest matched
> nothing, and all four "mutations" reported the same benign line. Use
> `${=T}`, an array, or literal paths. This is risk 13's *"treat empty pytest
> output as a failed mutation"* with a new cause: the output is not empty, it
> is reassuring.
>
> **(2) `Write` to a path you believe is new can silently clobber 346 lines,
> and the tool tells you which it did.** `gui/src/lib/stores/search.test.ts`
> already existed; a truncated `ls` was misread as showing it did not. The
> tell was in the tool result — *"has been updated successfully"* for that
> file, against *"File created successfully at"* for the genuinely new ones.
> Recovery was `git show main:<path> > <path>` and re-appending. **Run `git
> ls-files <dir>` before creating a "new" test file**, and read the Write
> result's verb.
>
> **(3) A baseline measured against a dirty tree is not a baseline.** The
> first re-measurement of vitest gave **406** — wrong, because the clobbered
> file had been deleted for the run. The correct procedure is to restore
> *every* changed file from a scratchpad copy, assert `git status --short
> <dir>` prints **nothing**, and only then measure. Both numbers below were
> taken that way.
>
> **Open issue count is 21, dropping to 20 on merge. Dependabot stays at 0.**

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

One code commit plus this handoff, one PR. The item and its direction were both
chosen by the operator.

### `eb9bffc` — #324: a query with nothing to rank refuses `sort='rank'` on page 1

- **The rule is the pure
  [src/localmail/search/sort_axes.py](src/localmail/search/sort_axes.py)** —
  `resolve_sort` (what will run) beside `sort_applicability_error` (whether the
  caller was told), co-located for the reason `keyset_walk.py` gives: split,
  they are two predicates for one question, which is the shape of the #308
  follow-up defect. The error half returns **a message or `None`**, so the api
  boundary (`ValidationFailed`) and the Searcher (the new named
  `SortNotApplicable`) cannot word one refusal two ways.
- **`DEFAULT_SORT` had to move with the rule.** Resolution is no longer a
  default but a function of the query, so a layer resolving from the constant
  alone now disagrees with the branch that serves the request — #312's rule one
  level up, and what makes the co-location load-bearing rather than tidy.
- **The classification is `keyset_walk.walk_for_text`**, gaining a third caller
  beside the branch and the cursor stamp. That is what let the retrieval branch
  **drop its `or walk_for_text(...)` arm**: `effective_sort` is resolved from
  the same string, so the predicate is one reading, not two.
- **`Searcher.search` resolves `sort` after `parse_query`, not before**, and
  the rank+asc refusal moved with it. Safe because `apply_rewrite` leaves
  `free_text` untouched and `_clamp_account_ids_to_acl` touches only `filters`
  — verified in the source, not assumed. Both guards still precede every
  connection, which their tests assert with a pool that raises if touched.
- **The membership check now reads `sort` as *stated*, not as resolved.** A
  textless query resolves to `TEXTLESS_SORT` whatever arrived, so checking the
  resolved value would swallow `sort="Date"` on exactly the branch #333 found
  swallowing it. `test_an_unknown_sort_is_refused_on_a_textless_query_too`
  closes that; every pre-existing case in that file uses a query with free text
  and so reaches none of it.
- **The inverse face was fixed with it, and had to be.** `{"query": "",
  "sort_order": "asc"}` used to be a 400 naming `sort='rank'`, a path the
  request would never take. The guard reads the *resolved* sort now, so it is
  **honoured**: oldest-first over the whole archive or any filter, with a `KA|`
  cursor.
- **`run_search`'s catch of `SortNotApplicable` is live, not a backstop —
  measured, not argued.** The gate parses the raw request field, the Searcher
  the ACL-composed query, and `parse_query` is **not compositional across an
  unbalanced quote**: `parse_query('from:"').free_text` is `'from:'`, while
  `parse_query('from:" account_id:1').free_text` is `''`. So the gate reads
  that query as rankable and the branch reads it as textless; without the catch
  the caller's error escapes as a 500 (`serve.app` handles `APIError` only).
- **The GUI states no sort it knows will be refused.** `statedSort` takes the
  query and drops a `rank` for a blank box — an empty search box with a filter
  chip set ("everything from this account") is an ordinary **shipped** flow that
  would otherwise have become an error banner. Omitting is not a fallback: the
  server resolves an unstated sort to the branch that serves it, which is what
  that flow already received. `date` is still stated, or the sort selector goes
  inert for the blank-box case.
  - **Known imprecision, deliberate**: the client uses `query.trim()`, not a
    reproduction of `parse_query`, so `subject:invoice` typed into the box still
    earns the 400. Keeping a second parser in step with the first is not worth
    one loud, actionable refusal. The store's own `hasNoScope()` uses the same
    notion.
  - **The wiring is pinned separately** in `gui/src/lib/stores/search.test.ts`,
    including the **409-recovery** call site. `statedSort`'s own tests stay green
    if a call site hands it a constant — mutation-proven both ways.
- Docs: README (the refusal, the remedy, and the now-honoured `asc`),
  `docs/mcp-usage.md` (both the tool table and the paging rules), **both MCP
  tool parameter descriptions and the tool docstring** — agents read those
  directly, so they are the contract for the audience this cluster is written
  for.

### The mutation battery — 14 mutations, all caught

Restored from a scratchpad copy every time, never `git checkout` (risk 13).

| mutation | caught by |
|---|---|
| `sort_applicability_error` always `None` | 28 tests |
| `sort_applicability_error` always refuses | 72 tests |
| `resolve_sort` drops the textless arm | 22 tests |
| `resolve_sort` always `TEXTLESS_SORT` | 14 tests |
| the "stated date is fine" arm dropped | 21 tests |
| api gate skips the applicability call | 13 tests |
| api gate resolves from `DEFAULT_SORT` again | 11 tests |
| Searcher skips its applicability guard | 9 tests |
| Searcher resolves from `DEFAULT_SORT` again | 7 tests |
| `run_search` drops the `SortNotApplicable` catch | **1** test (the one written for it) |
| membership check reads the resolved sort again | 8 tests |
| `statedSort` drops the textless-rank arm | 3 vitest |
| store passes a constant instead of the query | 2 vitest |
| store forgets the query **only on the 409 recovery** | **1** vitest |

### Verification (this Mac, all extras)

- **Both refs measured in this session** (risk 14), same shell, clean tree at
  each baseline:

  | gate | `main` @ `732d97c` | branch @ `eb9bffc` |
  |---|---|---|
  | pytest | **3063 passed, 0 skipped, 2 warnings** | **3153 passed, 0 skipped, 2 warnings** |
  | vitest | **423** | **431** |

  The +90 pytest is 24 (`test_sort_axes`) + 32 (`test_searcher_rank_without_text`)
  + 30 (`test_api_search_rank_without_text`) + 1 (cursor-direction) + 3 (axis
  validation). The +8 vitest is 4 `statedSort` + 4 store.
- The 2 warnings are the pre-existing `websockets` deprecations (**#25**), so
  **#321's acceptance signal still holds**.
- `mypy src/localmail` → Success, **152** files. `mypy` on the three new test
  files → Success.
- `ruff check src/localmail/` → **10**, the unchanged #285 baseline.
- `svelte-check` → **0 errors, 325 files**. `npm run build` → ok.
  `cargo test` → **107**. `cargo clippy --locked` and `--all-targets` → clean.
  **No Rust changed** — `sort` is already `Option<String>` with
  skip-serializing-if-none, so `undefined` from TS omits the field.
- **README updated** (checked and needed it — it documented the old behaviour
  and pointed at #324 as open). **CLAUDE.md updated** — the #324 entry is
  rewritten from "known consequence, filed" to the shipped rule, the #312
  bullet is corrected in place, and the Layout line names the two new
  functions.
- **There is still no ROADMAP.md** — that `/nextsession` step remains a no-op
  (risk 32, re-confirmed a **sixth** time).

## What's next

### 0. **Merge the PR, then check the issue actually closed**
   **You merge** (project convention). The body uses `Closes #324`. Risk 2 says
   verify with `gh issue list` afterwards rather than assume — **21 → 20**.
   - **No `uv.lock` change this session**, so neither host needs a dependency
     sync. The Mac's editable install still runs whatever the tree is checked
     out to, so `git checkout main` there after merging (risk 21).
   - **This is a wire-visible behaviour change.** If anything outside this repo
     calls `/v1/search` or the MCP `search` tool with a stated `sort="rank"` and
     a blank/operator-only query, it now gets a 400 naming the remedy. Nothing
     in-tree does — the GUI was fixed in the same commit — but it is worth
     knowing before the DGX serve is restarted.

### 1. **The #317 API-keys review round** *(carried)*
   **#319** (`ApiKeyNotFound` carries three meanings; the panel reports a
   security refusal as success) · **#318** (the `api_key_name` ⟺ `is_service`
   pairing is unenforced in the schema — sketch with a generated column + FK is
   in the issue; **next free migration slot is `0037_*.sql`**) · **#320**
   (admin panel routes do blocking DB IO on the event loop).

### 2. **The #322/#332 review round leftovers** *(carried, and one is now
   cheaper)*
   **#327** (`CursorPlan` carries two fields its pool-mode consumer must
   ignore) · **#328** (the page-cache entry is an untyped dict) · **#330**
   (`SortOrder`/`SortMode` restated in three wire layers) · **#331**
   (`SortOrderNotApplicable`'s stated audience is wrong).
   - **#331 is partly stale now and should be re-read before starting**: its
     complaint is that the exception's docstring calls its audience "library
     callers" while `run_search` refuses rank+asc first. That is still true of
     `SortOrderNotApplicable`, but the *new* `SortNotApplicable` beside it
     documents a genuinely live api-layer catch, so the two now say different
     things about the same shape. Resolve them together.
   - **#330 is also cheaper now**: `sort_axes.py` is unambiguously the one
     authority for both axes after this change, so the three wire restatements
     have somewhere to import from.

### 3. **#340 — the harness lock proves a lock was taken, not which database**
   *(carried)*. The AST rule compares call *positions* and never arguments, so
   locking one database while working against another passes. Latent — all five
   harnesses use one `dsn`. A correct check needs parameter-flow analysis
   across the helper walk.

### 4. **#305 — `--version` dies on a missing third-party dependency** *(carried)*
   `cli.py` imports the daemon at module scope. **Acceptance:** blocking
   `sqlparse` on `sys.meta_path` leaves `localmail --version` printing its line
   and exiting 0. **Do it with the `cli.py` refactor, not before** — `cli.py` is
   **2177 lines**.

### 5. **#285 — ruff, repo-wide** *(carried)*
   **10** pre-existing errors in `src/` (two are the `math` import/redefinition
   pair in `searcher.py`), plus **1 F841 in `tests/acceptance/run_recall_eval.py`**.
   9 dead `# noqa: S608`, no `[tool.ruff]`, no CI step. **Decide the config and
   the CI step together** — that decision is the operator's.

### 6. **Admin GUI phase 5 — Users & ACL panel** *(carried)*
   `/v1/admin/users` is already `require_admin()` — **no backend work.**
   Mirror [serve/admin/users_panel_router.py](src/localmail/serve/admin/users_panel_router.py),
   follow the Daemon-panel shape, surface the two lock-out guards as 409s, and
   **stub the new API module in both `AdminView.test.ts` and `MainView.test.ts`**
   (risk 25/28).

### 7. **Remaining robustness backlog** *(carried)*
   **#218** · **#226** · **#225 / #227** · **#200 / #211 / #208** · **#206** ·
   **#204** · **#25**.

### 8. **Smaller, deliberately not done** *(carried)*
   - **165 docling failures on the Mac**, of 182 `blobs_gave_up`. Worth a fresh
     look now the pypdf bump has been on the host for two sessions — three of
     the four advisories fixed are unbounded-runtime bugs on the extraction
     path. **Re-measure before claiming it; that is a hypothesis, not a
     finding.** *(Not re-measured this session — no archive work was done.)*
   - **Residual implausible language labels dominated by `ja`** (~0.24%); the
     confidence-floor lever was measured useless. **Sample `ja` first.**
   - **The DGX drops remain uninvestigated** (risk 27).
   - **A session-22 stash is still on the stack** (`stash@{0}: On
     docs/session-22-handoff: review-fixes`); its content is on `main`, so
     `git stash drop` it if you want the tree tidy.
   - **A stray SDD workspace** at `.superpowers/sdd/`. Git-ignored scratch.

## Open decisions & risks

1. **One PR is open and yours to merge.** `fix/324-blank-query-stated-rank`,
   based on `main` (`732d97c`), closing **#324**. **21 open issues**, dropping
   to **20**. **Dependabot stays 0.**
2. **A merge does NOT close issues its subject merely names** *(carried)*. Use
   `Closes #N` in the **PR body** and **check `gh issue list` after the merge**.
   Four rounds running now.
3. **THIS FILE IS NOT THE AUTHORITY — `git` and `gh` are** *(carried,
   eight-times earned)*. Open every session with `git fetch --prune && git log
   --oneline -1 origin/main`, `gh pr list`, `gh issue list`, the Dependabot
   query, and reconcile **before acting**.
4. **Base every branch on `main`; put a session's code and its handoff in ONE
   PR** *(carried)*. Session 25's stacked handoff PR merged into an
   already-merged branch 13 seconds after it landed and was lost silently.
   - The stranded-branch check is **NOISY**: `git log --oneline main..origin/<b>`
     is non-empty for every squash-merged branch. Signal = non-empty on a branch
     whose PR merged *recently*, confirmed by a **content** diff
     (`git diff --stat main origin/<branch>` empty ⇒ it landed).
5. **In zsh an unquoted `$VAR` does not word-split** *(new)*. A mutation battery
   built as `uv run pytest -q $T` ran **zero** tests four times and printed
   `no tests ran in 0.00s` each time — which reads as benign, not as a failure.
   Use `${=T}`, an array, or literal paths, and **always run the unmutated
   baseline through the same harness first** so a zero-test harness is caught
   before it certifies anything.
6. **Check `git ls-files <dir>` before Write-ing a "new" file, and read the
   tool's verb** *(new)*. `Write` reported *"has been updated successfully"*
   where it clobbered an existing 346-line test file, and *"File created
   successfully at"* for the genuinely new ones. A truncated `ls` was what
   misled me. Recovery: `git show main:<path> > <path>`.
7. **Measure a baseline against a provably clean tree** *(new, sharpening risk
   14)*. The first vitest re-measurement read **406** because a file had been
   deleted for the run; the true number is **423**. Restore *every* changed file
   from a scratchpad copy, assert `git status --short <dir>` prints nothing, and
   only then measure. **Both a published pytest count and a published vitest
   count have now been stale in consecutive handoffs** — treat every number in
   this file as needing re-measurement, not just pytest's.
8. **"It's only a backstop" is a claim to test, not to assume** *(new)*. The
   catch of `SortNotApplicable` in `run_search` looked redundant with the api
   gate. One 30-second probe of `parse_query` showed the gate and the branch
   read **different strings** and disagree on `from:"` — a live 500. The same
   probe shape is worth running any time two guards are said to be equivalent;
   `test_api_search_cursor_walk.py` had already made this argument for #326 and
   it generalises.
9. **A dependency floor that a vulnerable version satisfies is not a floor —
   and a package with NO floor is worse** *(carried)*. **Read
   `vulnerable_version_range` against `uv.lock`, always.** `uv lock --dry-run`
   is read-only but **not predictive**; `uv sync --dry-run` is **not**
   read-only.
10. **NEVER run two pytest sessions against one test database** *(carried —
    enforced since #336, and since #337 the acceptance harnesses are inside the
    same guard)*. The second **waits**. To run both, give one its own
    `LOCALMAIL_TEST_DSN`. A harness refused by the lock exits **3**, distinct
    from the **1** an eval returns when it fails its own gates.
    - Corollary seen this session: a background full run plus a foreground one
      does not fail, it **serialises** — a 190 s suite reported 412 s. That is
      the lock working, not a regression.
11. **A test module must import `localmail.serve.app` at MODULE scope**
    *(carried — #321)*. An AST scan plus a teardown re-check enforce it.
12. **When a guard is an AST rule, mutate BOTH branches of every predicate**
    *(carried)*. Related: **never assert a substring the message's own remedy
    text contains**, and **never compare a constant against itself**.
13. **When reverting a mutation, restore from a file copy — never `git
    checkout`** *(carried, sessions 23–39)*. Used 14 times this session.
    **Re-snapshot after each GREEN**, not once at the start.
14. **Verify host revisions; do not infer them** *(carried)*.
    `ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'` plus
    `/v1/version`'s `build_hash` settles it. **Both hosts were left untouched
    this session and neither needs a dependency sync** — `uv.lock` is unchanged.
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
18. **Never state a `sort` on a request the server cannot serve it for**
    *(carried and extended — #308, #311, #324)*. Two shapes now: a request
    carrying a **cursor**, and a `rank` on a query with **no free text**. A
    paging client must treat **409 as recoverable** and **400 as permanent for
    that cursor**. `gui/src/lib/search_paging.ts::statedSort` makes both
    unreachable from the GUI by construction.
19. **`sort` and `sort_order` resolve from the query, not from a constant**
    *(new — #324)*. `sort_axes.resolve_sort` is the one authority; a layer that
    resolves an unstated axis from `DEFAULT_SORT` alone disagrees with the
    branch that serves the request, and that disagreement is what #324 was.
    **Do not** "fix" a related wart by having a cursor record the sort the
    caller *stated* rather than the one that ran — a cursor claiming an
    ordering it did not walk is #308 itself.
20. **The stale NOTIFY queue recurs, and gate 1 lies** *(carried)*. Exactly
    three LISTEN/NOTIFY failures usually means the stale queue — but session 35
    had that exact failure set with **both gates clean** (it was contention).
    **Check both gates before reaching for the runbook.**
21. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `uv sync --all-extras` (Mac) / `--extra mcp --extra extraction` (DGX).
    **`uv` is not on the DGX's non-interactive PATH** — use `~/.local/bin/uv`.
    **A branch checkout re-resolves the venv** — re-sync after any checkout you
    intend to measure against.
22. **The Mac's launchd daemon runs an EDITABLE install** *(carried)*. It
    executes whatever the tree is checked out to. **Currently on
    `fix/324-blank-query-stated-rank`** — check out `main` after merging. No
    dependency sync needed this time.
23. **`search-status` is sub-second** *(carried)*. If it runs long that is a
    **regression of #280** — check `EXPLAIN (FORMAT JSON)` for a `Seq Scan on
    messages` under a `SubPlan` first.
24. **`blobs_claimable 0` alongside `blobs_pending 0` is the SETTLED shape**
    *(carried)*. Mac `9543 = 9255 + 106 + 182 + 0`; DGX `4405 = 4187 + 91 + 127
    + 0`. A steady non-zero `blobs_no_text` is **normal** (#277);
    **`blobs_gave_up` is the one to act on**. *(Not re-measured this session —
    no archive work was done.)*
25. **`uv run pytest -q` with NO arguments is the right command** *(carried)*.
    **Do not run the suite while a backfill is draining.**
26. **An empty `daemon_heartbeats` right after a restart is normal for minutes**
    *(carried — #269/#271)*: the startup blob-temp sweep. Grep for
    **`blob-temp sweep done: walked=`**.
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
    **3152 passed, 1 skipped**.
32. **No ROADMAP.md** *(carried, re-confirmed a sixth time)* — that
    `/nextsession` step is a no-op. **CLAUDE.md and README were both updated**
    this session, along with `docs/mcp-usage.md` and the MCP tool docstrings.
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
gh issue list --limit 40                 # 21 open; the PR should take it to 20

# RISK 9 — expect ZERO, still. Confirm, don't assume.
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

# AFTER MERGING — the Mac tree only; uv.lock did NOT change this session.
#   git checkout main && git pull        # editable install follows the tree (risk 22)

# Python suite. NEVER a bare `uv sync` (risk 21).
unset VIRTUAL_ENV && uv sync --all-extras
unset VIRTUAL_ENV && uv run pytest -q
#   macOS: expect 3153 passed, 0 failed, 0 skipped, and **2 warnings**, ~190s
#   on an idle machine. THOSE 2 ARE #321's ACCEPTANCE SIGNAL: both are the
#   pre-existing websockets DeprecationWarnings (#25). A leaked pool is now a
#   FAILING test whose message reads "cannot join current thread"; the test it
#   names is arbitrary (the GC picks it), the message is the diagnosis.
#   LINUX/CI: expect 3152 passed, 1 SKIPPED; pre-existing (risk 31).
#   MEASURE BOTH REFS IN THIS SESSION (risk 7/14) — no DB needed:
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2   # 3153 here
unset VIRTUAL_ENV && uv run mypy src/localmail    # expect Success, 152 files
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -2   # expect 10 (#285)

# #324, verified directly — the rule, both guards, and the round trip:
unset VIRTUAL_ENV && uv run pytest -q tests/test_sort_axes.py \
  tests/test_searcher_rank_without_text.py \
  tests/test_api_search_rank_without_text.py \
  tests/test_api_search_cursor_direction.py \
  tests/test_searcher_sort_axis_validation.py \
  tests/test_searcher_sort_order_guard.py tests/test_api_search_cursor_mode.py \
  tests/test_api_search_cursor_walk.py tests/test_searcher_blank_query_paging.py
#   expect 169 passed in ~1.5s

# RISK 5 — the mutation harness MUST word-split. zsh does not do it for you:
T="tests/test_sort_axes.py tests/test_searcher_rank_without_text.py"
uv run pytest -q ${=T}       # ${=T}, NOT $T. $T runs ZERO tests and says
                             # "no tests ran in 0.00s", which reads as benign.

# The #324 divergence probe (risk 8) — why run_search's catch is not dead code:
unset VIRTUAL_ENV && uv run python -c "
from localmail.search.query import parse_query
from localmail.api.search import build_query_string
raw = 'from:\"'
print('gate  :', repr(parse_query(raw).free_text))
print('branch:', repr(parse_query(build_query_string(free_text=raw,
      filters={'account_ids': ['1']})).free_text))"
#   expect gate='from:' branch='' — the two guards read different strings.

# REPRODUCING A #329/#335-CLASS BUG NOW THAT #336 PREVENTS IT (risk 10).
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
#   then run the suspect tests in the foreground.

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

# RISK 10 — the test-database lock covers harnesses too (#336 + #337).
# If a run seems to hang at startup, look for this line; it is not a fault:
#   "waiting for another test run to release the test database ..."
# To run two at once, give one its own database:
#   LOCALMAIL_TEST_DSN=postgresql://localmail:local%40%40mail@localhost:5532/localmail_test2 uv run pytest -q

# If EXACTLY the three LISTEN/NOTIFY tests fail, CHECK BOTH GATES (risk 20):
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'
# Only if a gate errors, use runbook Option A (bootout, verify while down, bootstrap).

# Host health (Mac) — NOT re-measured this session; no archive work was done:
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 26)

unset VIRTUAL_ENV && uv run localmail search-status    # UNDER A SECOND (risk 23)
#   Check the SHAPE, not the literals: the four buckets must sum to
#   blobs_eligible, and claimable must equal pending.

# The DGX (risk 14 — verify, never infer):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'   # binds 10.0.0.3, NOT localhost
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — REQUIRED only if you touch gui/ (MUST run from gui/ — risk 28):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
#   expect: 0 svelte-check errors (325 files), 431 vitest, 107 cargo
#   RISK 7: 431 is THIS branch. `main` is 423. The previous handoff said 408,
#   which was stale — re-measure against a provably clean tree, never quote.
```

`main` tip at session start was **`732d97c`**. This session left **one PR** open
on `fix/324-blank-query-stated-rank` — `eb9bffc` (#324) and the handoff commit —
closing **#324**. Latest migration **`0036_api_keys.sql`**; next free slot
`0037_*.sql` (this session adds none). **Open issues: 21**, dropping to **20**
on merge. **Dependabot: 0.**
