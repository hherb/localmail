# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-09-06 (session 43).** `main` was **`be678f3`** at the
> start — PR #352 merged by the operator, **#345 closed**, **Dependabot still
> 0**. This session opened **one PR** (#355) on `fix/353-354-sort-selector`,
> closing **#353** and **#354** — the review round for #352.
>
> **The previous handoff's issue predictions were wrong, and in the usual
> direction.** It predicted 20 open dropping to 19; the real count was **21**,
> because the operator filed #353 and #354 *after* the handoff was written.
> That is risk 3 exactly, and it has now paid in twelve of thirteen sessions.
> Its *test-count* predictions were also stale — it said `main` would collect
> 3318 and vitest 450; the real `main` was **3320** and **457**, because the
> operator added review commits before squash-merging (CLAUDE.md already
> carried their correction when this session opened). **Re-measure `main`;
> never quote a handoff's number for it.**
>
> **#353 was a design call and you chose 1+3** — record the click *and* put
> `rankable` on the wire. The analysis that mattered: option 2 is not an
> alternative to option 1 (no guard change reaches a handler that is never
> invoked), and option 3 fixes nothing on its own. Only the pair removes the
> defect without causing the re-enable flicker the issue predicted.
>
> **#354 turned out not to be a design call at all.** The issue posed
> `aria-describedby` versus visible text as an either/or; `AccountForm.svelte`
> and `DaemonPanel.svelte` already render server-disable reasons as markup, so
> the precedent existed and the tooltip was the outlier. Its scope note asked
> whether other controls need the same sweep — **they do not**, checked:
> `grep title= gui/src/components/admin` is empty.
>
> **Three findings this session, all from running things rather than reading
> them.**
>
> **(1) Binding both `click` and `change` double-fires.** The obvious fix for
> #353 is "bind onclick as well as onchange". It sends two searches for every
> real change of mind, because `shownSort` only moves when the response lands,
> so the second handler still sees a disagreement. `click` is a strict
> superset for a radio — pinned by a test that fires both.
>
> **(2) A derived `rankable` would be wrong at exactly one site.**
> `_empty_grown_page` builds `query=parse_query("")`, so a property computed
> from `page.query` reports an exhausted *pool* — rankable by construction —
> as unrankable. Found by reading the construction sites before writing the
> field, and it is why the field is explicit and defaultless.
>
> **(3) The MagicMock trap fired again, and the garbage is not a constant.**
> An unset `page.rankable` reached the wire as **`[]`** where `sort_applied`
> had rendered as `{}`. Do not go looking for a particular wrong value.
>
> **Open issue count is 21, dropping to 19 on merge. Dependabot stays 0.**

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

Three commits plus this handoff, one PR (#355). This session touched `gui/`,
so **all five frontend gates were run** (risk 35).

### `4220a98` — the response says whether ranking was possible at all

`sort_applied` (#345) is exact only for a caller that states nothing: a stated
`date` is honoured for every query, so `sort_applied == "date"` covers both
"nothing to rank" and "rank was available and not chosen". The GUI inferred
the first from the second, which is #353.

- `sort_axes.is_rankable` is the rule, and **`resolve_sort` asks it** rather
  than repeating the `walk_for_text` test — so a response cannot carry
  `rankable=False` beside `sort_applied="rank"`.
- `SearchPage.rankable` / `PoolMetadata.rankable`, defaultless, stamped by the
  branch that produced the rows and derived at every site from that page's own
  query. **Never a property** — see finding (2) above.
- The empty-ACL short-circuit reports it **exactly**, unlike `sort_applied`
  beside it: rankability is a property of the query alone, so the gate's own
  parse answers it without needing to agree with a branch never reached.
- MCP: both `server.py` (the published description agents read) and `tools.py`,
  with its own pin — #345's lesson.

### `dc29a94` — the selector records a preference, and says why out loud

- `sortClick` splits the one guard into the two questions it was answering
  with one field: **record** iff the click disagrees with the preference,
  **re-run** iff it disagrees with the rows on screen.
- Handler moved from `change` to `click` — see finding (1).
- `relevanceUnavailable` reads `rankable` instead of inferring. That is what
  stops the fix causing the flicker, and it retires the imprecision #345
  documented.
- `asRankable` narrows the wire value; **truthiness would be wrong in both
  directions** (`"false"` is truthy, `0` is falsy and would silently disable a
  working control).
- #354: the reason is markup + `aria-describedby`, following the
  `AccountForm`/`DaemonPanel` precedent. `title` removed.

### `948cd5f` — README + docs/mcp-usage.md + CLAUDE.md

CLAUDE.md records the entry in place, including the equivalent-mutant proof
and the `click`-vs-`change` finding, and **corrects the now-stale
`_assert_wire_sort_applied` name** in the #345 entry (renamed
`_assert_wire_ordering_fields`).

### Verification (this Mac, all extras)

- **Both refs measured in this session** (risk 7), clean tree at each:

  | gate | `main` @ `be678f3` | branch @ `948cd5f` |
  |---|---|---|
  | pytest collected | **3320** | **3372** |
  | pytest run | — | **3372 passed, 2 warnings, 223 s** |
  | vitest | **457** (49 files) | **479** (49 files) |
  | rust `#[test]` (static) | **40** | **41** |

  The +52 python is 30 (`test_sort_axes.py`) + 21 (`test_search_rankable.py`)
  + 1 (`test_mcp_server_build.py`). The +22 vitest is 9 (`sort_display`) +
  7 (SearchBar) + 6 (store). **Both breakdowns were measured per file against
  a `main` worktree**, not derived — the first draft of this line guessed
  11/5/6 for vitest and was wrong on two of the three.
- The 2 warnings are the pre-existing `websockets` deprecations (**#25**), so
  **#321's acceptance signal still holds**.
- `cargo test` → **109 passed** on the branch. `main` was **not** re-run; 108
  is derived from the static `#[test]` count above. Both clippy runs clean.
- `npm run check` → **0 errors**, 327 files. `npm run build` → ok.
- `mypy src/localmail` → Success, **153** files. `ruff check src/localmail/`
  → **10**, the unchanged #285 baseline.
- **Live end-to-end against the 129,036-message archive**, five shapes. Four
  report `sort_applied='date'`; only `rankable` separates
  `invoice + sort=date` (True) from the textless ones (False).
- **16 mutations: 14 caught, 1 refused to apply, 1 survives as an equivalent
  mutant** (recorded in the test file with its proof). See risk 8 and 41.
- **There is still no ROADMAP.md** — that `/nextsession` step remains a no-op
  (risk 39, re-confirmed a **tenth** time).
- Machine was **not** carrying orphaned burners (risk 5 checked first), but
  load rose from **5.7** to **12** during the session, so 223 s is not
  comparable to a quiet-machine figure.

## What's next

### 0. **Merge the PR, then check the issues actually closed**
   **You merge** (project convention). The body uses `Closes #353` and
   `Closes #354`. Risk 2 says verify with `gh issue list` afterwards —
   **21 → 19**.
   - **No `uv.lock` change**, so neither host needs a dependency sync. The
     Mac's editable install follows the tree, so `git checkout main` there
     after merging (risk 29).
   - **`com.localmail.serve` was NOT restarted** this session, so it is still
     serving pre-#353 code. Restart it if you want `rankable` on the live wire.
   - **No migration.** Latest is `0036_api_keys.sql`; next free slot
     `0037_*.sql`.
   - **The search-sort cluster is closed again** (#308, #311, #312, #322,
     #323, #324, #326, #331, #333, #342, #344, #345, #347–#350, #353, #354).
     What remains of it is refactoring: #327, #328, #330.

### 1. **The #322/#332 review-round leftovers** *(carried, and #330 is cheapest)*
   **#330** (`SortOrder`/`SortMode` restated in three wire layers) · **#327**
   (`CursorPlan` carries two fields its pool-mode consumer must ignore) ·
   **#328** (the page-cache entry is an untyped dict).
   - **#330 grew again this session**: `SearchResponse` now restates the
     vocabulary in `gui/src/lib/api/search.ts` *and* in the Rust struct, and
     `rankable` adds a boolean beside it in both. Fold them in.
   - **#328 gained a third consumer**: `get_pool_metadata` now derives
     `rankable` from `entry["parsed"].free_text`, so a typed entry would make
     that read checkable too.

### 2. **The #317 API-keys review round** *(carried)*
   **#319** (`ApiKeyNotFound` carries three meanings; the panel reports a
   security refusal as success) · **#318** (the `api_key_name` ⟺ `is_service`
   pairing is unenforced in the schema; **next free migration slot is
   `0037_*.sql`**) · **#320** (admin panel routes do blocking DB IO on the
   event loop).

### 3. **#340 — the harness lock proves a lock was taken, not which database**
   *(carried)*. The AST rule compares call *positions* and never arguments, so
   locking one database while working against another passes. Latent — all five
   harnesses use one `dsn`. A correct check needs parameter-flow analysis.

### 4. **#305 — `--version` dies on a missing third-party dependency** *(carried)*
   `cli.py` imports the daemon at module scope. **Acceptance:** blocking
   `sqlparse` on `sys.meta_path` leaves `localmail --version` printing its line
   and exiting 0. **Do it with the `cli.py` refactor, not before** — `cli.py` is
   **2177 lines**.
   - **#331's point 2 lives here**: widen `cli.py`'s search catch to
     `SearchArgumentRefused`, never to bare `ValueError`.
   - **#350 adds**: the CLI is the third consumer of the wire join, and since
     #350 it is one call — `exc.wire_message()` — not an f-string to restate.
   - **#348 adds**: a `--sort`/`--sort-order` flag makes the Searcher's
     plain-`ValueError` membership check reachable from the CLI, where it would
     traceback. `cli.py` calls `create_searcher(...).search(...)` directly, so
     `run_search`'s gate does **not** cover it.
   - **#345/#353 add a fourth and fifth**: `cli.py`'s search output shows no
     ordering at all, so it now has `page.sort_applied` *and* `page.rankable`
     to print if the refactor wants them.

### 5. **#285 — ruff, repo-wide** *(carried)*
   **10** pre-existing errors in `src/`, plus **1 F841** in
   `tests/acceptance/run_recall_eval.py`. 9 dead `# noqa: S608`, no
   `[tool.ruff]`, no CI step. **The config and the CI step are one decision,
   and it is the operator's.**

### 6. **Admin GUI phase 5 — Users & ACL panel** *(carried)*
   `/v1/admin/users` is already `require_admin()` — **no backend work.**
   Mirror [serve/admin/users_panel_router.py](src/localmail/serve/admin/users_panel_router.py),
   follow the Daemon-panel shape, surface the two lock-out guards as 409s, and
   **stub the new API module in both `AdminView.test.ts` and `MainView.test.ts`**
   (risk 35).

### 7. **Remaining robustness backlog** *(carried)*
   **#218** · **#226** · **#225 / #227** · **#200 / #211 / #208** · **#206** ·
   **#204** · **#25**.

### 8. **Smaller, deliberately not done** *(carried)*
   - **165 docling failures on the Mac**, of 182 `blobs_gave_up`. Worth a fresh
     look now the pypdf bump has been on the host for six sessions.
     **Re-measure before claiming it; that is a hypothesis, not a finding.**
     *(Not re-measured this session — no archive work was done.)*
   - **Residual implausible language labels dominated by `ja`** (~0.24%); the
     confidence-floor lever was measured useless. **Sample `ja` first.**
   - **The DGX drops remain uninvestigated** (risk 30).
   - **A session-22 stash is still on the stack** (`stash@{0}`); its content is
     on `main`, so `git stash drop` it if you want the tree tidy.
   - **A stray SDD workspace** at `.superpowers/sdd/`. Git-ignored scratch.

## Open decisions & risks

1. **One PR is open and yours to merge.** #355 on `fix/353-354-sort-selector`,
   based on `main` (`be678f3`), closing **#353** and **#354**. **21 open
   issues, dropping to 19. Dependabot stays 0.**
2. **A merge does NOT close issues its subject merely names** *(carried)*. Use
   `Closes #N` in the **PR body** and **check `gh issue list` after the merge**.
3. **THIS FILE IS NOT THE AUTHORITY — `git` and `gh` are** *(carried, and it
   paid again)*. The previous handoff was wrong about the issue count (20 vs
   21) *and* about both of `main`'s test counts, because the operator filed
   issues and pushed review commits after it was written. Open every session
   with `git fetch --prune && git log --oneline -1 origin/main`, `gh pr list`,
   `gh issue list`, the Dependabot query — and **re-measure `main` yourself**.
4. **Base every branch on `main`; put a session's code and its handoff in ONE
   PR** *(carried)*. Session 25's stacked handoff PR merged into an
   already-merged branch and was lost silently.
   - The stranded-branch check is **NOISY**: `git log --oneline main..origin/<b>`
     is non-empty for every squash-merged branch. Signal = non-empty on a branch
     whose PR merged *recently*, confirmed by a **content** diff
     (`git diff --stat main origin/<branch>` empty ⇒ it landed).
5. **A machine that is inexplicably slow may be carrying someone else's
   garbage** *(carried)*. Check
   `ps -eo pid,ppid,etime,time,command | grep "[w]hile :"` **before**
   concluding a change made things slow. Clean this session — but **load rose
   5.7 → 12** during it, so **do not compare 223 s to a quiet-machine figure**.
6. **In zsh an unquoted `$VAR` does not word-split** *(carried)*. `uv run
   pytest -q $T` runs **zero** tests and prints `no tests ran in 0.00s`, which
   reads as benign. Use `${=T}`, and **always run the unmutated baseline
   through the same harness first**.
7. **Measure both refs in the session that reports them** *(carried)*. Held.
   Note the *derived* number in the table above is labelled as such — `main`'s
   cargo count is a static `#[test]` count, not a run.
8. **A MUTATION THAT DOES NOT APPLY LOOKS EXACTLY LIKE ONE THAT IS CAUGHT**
   *(carried, and it fired again)*. One mutation matched an ambiguous anchor;
   the harness **refused to write** and said so, and the test run that followed
   reported a clean pass. Without the refusal it would have been recorded as
   caught. **The helper is worth keeping** — it is in the resume commands below.
9. **"It's only a backstop" / "it's unreachable" / "verified" is a claim to
   test** *(carried)*.
10. **Write the justification down, then try to refute it** *(carried)*.
11. **A dependency floor that a vulnerable version satisfies is not a floor**
    *(carried)*. **Read `vulnerable_version_range` against `uv.lock`, never the
    declared floor.** `uv lock --dry-run` is read-only; `uv sync --dry-run` is
    **not**.
12. **NEVER run two pytest sessions against one test database** *(carried —
    enforced since #336; since #337 the acceptance harnesses are in the same
    guard)*. The second **waits**. To run both, give one its own
    `LOCALMAIL_TEST_DSN`. A harness refused by the lock exits **3**, distinct
    from the **1** an eval returns when it fails its own gates.
13. **When reverting a mutation, restore from a file copy — never `git
    checkout`** *(carried, sessions 23–43)*. Used 16 times this session.
    **Re-snapshot after each GREEN**, not once at the start.
14. **A MagicMock attribute reaches the wire as garbage, and the garbage is not
    a constant** *(updated — #353)*. `jsonable_encoder` serialises an unset
    auto-attribute rather than raising: `{}` for `sort_applied` (#345), **`[]`**
    for `rankable`. **When you add a wire key, assert it through the real
    transport**, set it explicitly on every mock page, and make the shared guard
    **structural** (type/membership), which is the only half a *new* fake cannot
    get past.
15. **`run_search` treats `allowed_account_ids=None` AND `[]` as
    grant-nothing** *(carried — #345)*. Both short-circuit before the Searcher,
    so a test written with `None` asserts the short-circuit's own value and
    never reaches the page. **A wire test that expects rows must pass a real
    grant**, and should assert `results` is non-empty.
16. **A test module must import `localmail.serve.app` at MODULE scope**
    *(carried — #321)*. An AST scan plus a teardown re-check enforce it.
17. **When a guard is an AST rule, mutate BOTH branches of every predicate**
    *(carried)*. Related: **never assert a substring the message's own remedy
    text contains**, and **never compare a constant against itself**.
18. **Some DOM mutations are masked and cannot be pinned** *(carried — #345)*.
    Radio-group exclusivity resolves a one-sided `checked` binding revert.
    **Pin the independent property instead** (there, `disabled`).
19. **Verify host revisions; do not infer them** *(carried)*.
    `ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'` plus
    `/v1/version`'s `build_hash` settles it. **Both hosts were left untouched
    and neither needs a dependency sync** — `uv.lock` is unchanged.
20. **A new Searcher guard over a stated argument must subclass
    `SearchArgumentRefused`, be declared in `argument_errors.py`, and be raised
    before any IO** *(carried — #344, #347, #349)*. All three are enforced.
    **The membership checks on `sort`/`sort_order` are the deliberate
    exception** — a plain `ValueError`, spelled with a bare builtin.
21. **Membership outranks every other guard, at both layers** *(carried —
    #348)*.
22. **A keyset predicate must be a row comparison, in BOTH directions**
    *(carried — #75, #322, #323)*. Written by mistake **twice**.
    `tests/test_searcher_sort_order_plan.py` keeps three negative controls —
    **do not "tidy" them away**.
23. **The undated block is where the two directions genuinely differ**
    *(carried)*. **Do not "restore symmetry"**, and **do not "fix" a short page
    by restoring `OR expr IS NULL`**.
24. **A cursor identifies a position, not a query — with one enforced
    exception** *(carried — #326)*.
25. **Never state a `sort` the server cannot serve** *(carried — #308, #311,
    #324)*, **never render one it did not run** *(carried — #345)*, and **never
    infer availability from the ordering** *(new — #353)*. `sort_applied`
    cannot distinguish a `date` the user chose from one imposed on a textless
    query; `rankable` is the field for that, and inferring it re-enabled a
    control on queries that cannot be ranked.
26. **`sort` and `sort_order` resolve from the query, not from a constant**
    *(carried — #324)*. `sort_axes.resolve_sort` is the one authority, and
    since #353 it asks `is_rankable` rather than repeating the test. **Do not**
    have a cursor record the sort the caller *stated* rather than the one that
    ran — that is #308 itself.
27. **The stale NOTIFY queue recurs, and gate 1 lies** *(carried)*. **Check
    both gates before reaching for the runbook.**
28. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `uv sync --all-extras` (Mac) / `--extra mcp --extra extraction` (DGX).
    **`uv` is not on the DGX's non-interactive PATH** — use `~/.local/bin/uv`.
    **A branch checkout re-resolves the venv** — re-sync after any checkout you
    intend to measure against.
29. **The Mac's launchd daemon runs an EDITABLE install** *(carried)*. It
    executes whatever the tree is checked out to. **Currently on
    `fix/353-354-sort-selector`** — check out `main` after merging.
    **`com.localmail.serve` was NOT restarted**, so it is still serving
    pre-#353 code.
30. **`search-status` is sub-second** *(carried)*. If it runs long that is a
    **regression of #280**. *(Not re-measured this session.)*
31. **`blobs_claimable 0` alongside `blobs_pending 0` is the SETTLED shape**
    *(carried)*. A steady non-zero `blobs_no_text` is **normal** (#277);
    **`blobs_gave_up` is the one to act on**.
32. **`uv run pytest -q` with NO arguments is the right command** *(carried)*.
    **Do not run the suite while a backfill is draining.**
33. **An empty `daemon_heartbeats` right after a restart is normal for minutes**
    *(carried — #269/#271)*. Grep for **`blob-temp sweep done: walked=`**.
34. **The DGX drops are STILL UNEXPLAINED — five theories refuted** *(carried)*.
    **Do not propose a sixth without a captured outage in which the host was
    demonstrably up throughout.** **Do not edit `/etc/wireguard/wg0.conf`.**
35. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`.
    **Run vitest from `gui/`, not the repo root.** `cargo clippy --all-targets`
    is clean but **ungated** — CI runs clippy without it.
36. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    **#266's whitespace-heal is a one-way door too.** **`--relabel` is the only
    destructive verb in the lang path**; reach for `--retry-declined` first.
37. **Secrets/ACL invariants unchanged** *(carried)*.
38. **CI reports `1 skipped` on Linux; macOS reports 0** *(carried)*. It is
    pre-existing and still unidentified. Expect CI here to read
    **3371 passed, 1 skipped**.
39. **No ROADMAP.md** *(carried, re-confirmed a tenth time)* — that
    `/nextsession` step is a no-op. **README, docs/mcp-usage.md and CLAUDE.md
    were all updated** this session (README genuinely needed it — the wire
    gained a key).
40. **A green local run is still not evidence** *(carried)*. Push and let CI
    decide.
41. **Distinguish a surviving mutation from an EQUIVALENT one** *(new — #353)*.
    Hardcoding `rankable=True` in the pool branch of `Searcher.search` survives
    and always will: that branch runs only under `effective_sort == "rank"`,
    which `resolve_sort` returns only for a rankable query, so no input can
    separate the two. The three pool *readers* looked identical and were **real
    gaps** — their cache entry is independent input, so a textless pool put
    straight into the cache pins them. **Record the proof next to the code**; a
    reader who conflates the two deletes pins that are load-bearing.
42. **For a radio, `click` is a strict superset of `change`** *(new — #353)*.
    An already-checked radio fires **no** `change`; it fires `click` either way,
    for pointer and keyboard alike. So bind `click`. **Binding both
    double-fires** — measured — because a `$derived` read inside the handler
    does not update between the two events.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status && git branch --show-current
git log --oneline -1 origin/main         # THE authority, not this file's header
git log --oneline main..origin/main      # non-empty = a session landed since

# RISK 2 — after any merge, CHECK THE ISSUES ACTUALLY CLOSED.
gh pr list
gh issue list --limit 40                 # 21 open; the PR should take it to 19
gh issue view 353 --json state --jq .state
gh issue view 354 --json state --jq .state

# RISK 11 — expect ZERO, still. Confirm, don't assume.
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '[.[] | select(.state=="open")] | length'      # expect 0
# If a new one appears, read its range against THE LOCK, never the floor:
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | "\(.security_advisory.severity)\t\(.dependency.package.name)\t\(.security_vulnerability.vulnerable_version_range)\tfixed in \(.security_vulnerability.first_patched_version.identifier)"'
grep -A1 '^name = "<pkg>"' uv.lock

# RISK 5 — BEFORE trusting any timing, check for orphaned CPU burners
#          AND read the load. This session ended at load 12.
ps -eo pid,ppid,etime,time,command | grep "[w]hile :; do :; done"
uptime

# RISK 4 — stranded-branch shortlist (noisy: every squash-merged branch shows).
for b in $(git branch -r --format='%(refname:short)' | grep -v 'origin/main\|HEAD'); do
  n=$(git log --oneline main.."$b" | wc -l); [ "$n" -gt 0 ] && echo "$b: $n commits not on main"
done
git diff --stat main origin/<branch>     # EMPTY = landed, not stranded

# AFTER MERGING — the Mac tree only; uv.lock did NOT change this session.
#   git checkout main && git pull        # editable install follows the tree (risk 29)
#   launchctl kickstart -k gui/$UID/com.localmail.serve   # to serve `rankable` live

# Python suite. NEVER a bare `uv sync` (risk 28).
unset VIRTUAL_ENV && uv sync --all-extras
unset VIRTUAL_ENV && uv run pytest -q
#   macOS: expect 3372 passed, 0 failed, 0 skipped, and **2 warnings**.
#   THOSE 2 ARE #321's ACCEPTANCE SIGNAL: both are the pre-existing websockets
#   DeprecationWarnings (#25). A leaked pool is now a FAILING test whose message
#   reads "cannot join current thread"; the test it names is arbitrary (the GC
#   picks it), the message is the diagnosis.
#   LINUX/CI: expect 3371 passed, 1 SKIPPED; pre-existing (risk 38).
#   MEASURE BOTH REFS IN THIS SESSION (risk 7) — no DB needed:
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2   # 3372 here
git checkout main && unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2
#   main @ be678f3 = 3320.  Assert `git status --short` is EMPTY at each.
#   DO NOT trust this file's number for main — last session's was stale by 2.
unset VIRTUAL_ENV && uv run mypy src/localmail    # expect Success, 153 files
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -2   # expect 10 (#285)

# #353/#354, verified directly — server and client:
unset VIRTUAL_ENV && uv run pytest -q tests/test_search_rankable.py tests/test_sort_axes.py
#   expect 88 passed in ~2s
cd gui && npx vitest run src/lib/sort_display.test.ts src/components/SearchBar.test.ts \
                        src/lib/stores/search.test.ts && cd ..
#   expect 82 passed

# #353 END TO END against the LIVE archive. The last two rows are the point:
# both report sort_applied='date' and only `rankable` separates them.
unset VIRTUAL_ENV && uv run python - <<'PY' 2>&1 | grep -v "couldn't stop\|hint:"
import psycopg
from localmail.config import load_config
from localmail.search import create_searcher
from localmail.api.search import run_search
cfg = load_config()
with psycopg.connect(cfg.database.dsn) as c:
    ids = [r[0] for r in c.execute("SELECT id FROM accounts ORDER BY id").fetchall()]
s = create_searcher(cfg=cfg)
def probe(label, **kw):
    out = run_search(searcher=s, limit=5, allowed_account_ids=ids, user_id=1, **kw)
    cur = out["next_cursor"]
    kind = "none" if cur is None else ("keyset" if cur.split("|")[0] in ("K","KA","KT","KAT") else "pool")
    print(f"{label:36} rows={len(out['results']):2} cursor={kind:6} "
          f"sort_applied={out['sort_applied']!r:7} rankable={out['rankable']}")
probe("textless, no filter", free_text="", filters={})
probe("has:attachment only", free_text="has:attachment", filters={})
probe("subject:zzzqqqnope (no cursor!)", free_text="subject:zzzqqqnope", filters={})
probe("text 'invoice'", free_text="invoice", filters={})
probe("text 'invoice' + sort=date", free_text="invoice", filters={}, sort="date")
PY
#   expect rankable False/False/False/True/True with sort_applied
#   date/date/date/rank/date.

# RISK 6 — the mutation harness MUST word-split. zsh does not do it for you:
T="tests/test_search_rankable.py"
uv run pytest -q ${=T}       # ${=T}, NOT $T. $T runs ZERO tests and says
                             # "no tests ran in 0.00s", which reads as benign.

# RISK 8 — the mutation helper. It REFUSES a non-unique anchor (which is how a
# silent no-op was caught this session) and prints where the edit landed.
cat > /tmp/mutate.py <<'PY'
"""Apply one mutation, report WHERE it landed, never guess."""
import ast, pathlib, sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path); s = p.read_text(); n = s.count(old)
if n != 1:
    sys.exit(f"ANCHOR MATCHED {n} TIMES — mutation NOT applied: {old[:70]!r}")
line = s[:s.index(old)].count("\n") + 1
p.write_text(s.replace(old, new))
if p.suffix == ".py":
    tree = ast.parse(p.read_text())
    o = [x for x in ast.walk(tree) if isinstance(x, (ast.FunctionDef, ast.ClassDef))
         and x.lineno <= line <= (x.end_lineno or 0)]
    where = (max(o, key=lambda x: x.lineno).name + "()") if o else "<module>"
else:
    where = repr(s.split("\n")[line - 1].strip()[:60])
print(f"MUTATED {path}:{line} at {where}")
PY
#   Snapshot to a scratch dir first and restore from THERE (risk 13) —
#   `git checkout <file>` wipes your own uncommitted edits in that file.

# RISK 12 — the test-database lock covers harnesses too (#336 + #337).
# If a run seems to hang at startup, look for this line; it is not a fault:
#   "waiting for another test run to release the test database ..."
#   LOCALMAIL_TEST_DSN=postgresql://localmail:local%40%40mail@localhost:5532/localmail_test2 uv run pytest -q

# If EXACTLY the three LISTEN/NOTIFY tests fail, CHECK BOTH GATES (risk 27):
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'

# Host health (Mac):
launchctl list | grep -i localmail
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 33)

unset VIRTUAL_ENV && uv run localmail search-status    # UNDER A SECOND (risk 30)
#   Check the SHAPE, not the literals: the four buckets must sum to
#   blobs_eligible, and claimable must equal pending.

# The DGX (risk 19 — verify, never infer):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'   # binds 10.0.0.3, NOT localhost
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — REQUIRED (this session touched gui/). MUST run from gui/ (risk 35).
cd gui && npm run check && npm test && npm run build && cd ..
#   expect 0 errors / 327 files; 479 passed (49 files); built ok
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
#   expect 109 passed; both clippy runs clean
```

`main` tip at session start was **`be678f3`**. This session left **one PR**
(#355) open on `fix/353-354-sort-selector` — `4220a98` (server), `dc29a94`
(client), `948cd5f` (docs) and the handoff commit — closing **#353** and
**#354**. Latest migration **`0036_api_keys.sql`**; next free slot
`0037_*.sql` (this session adds none). **Open issues: 21**, dropping to **19**
on merge. **Dependabot: 0.**
