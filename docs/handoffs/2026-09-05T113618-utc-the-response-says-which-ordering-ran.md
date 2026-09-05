# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-09-05 (session 42).** `main` was **`6a8ba7a`** at the
> start — PR #351 merged by the operator, **#347, #348, #349 and #350 all
> closed**, **Dependabot still 0**. This session opened **one PR** on
> `fix/345-sort-applied`, closing **#345** — the last open issue of the
> search-sort cluster.
>
> **The previous handoff's predictions were exactly right, for the first time
> in eleven sessions.** It predicted 20 open issues and 3301 branch tests;
> both were correct. Risk 3 still stands (reconcile with `gh` first), but the
> reason it kept paying was that the operator filed review issues *after* each
> handoff was written — and this time #351 merged without a review round.
>
> **The issue's preferred fix was measured and rejected, and you approved the
> alternative.** #345 preferred option 1 (infer the ordering client-side from
> the returned cursor's prefix). Probed against the live 129,009-message
> archive first: `subject:zzzqqqnope` — textless, no matches — comes back
> `next_cursor: None`, so an inference has **no signal at all** on exactly the
> narrow-filter case a user reaches deliberately, and the empty-ACL
> short-circuit is the same. Option 2 (`sort_applied` on the wire) shipped
> instead, landing **with** its renderer, which is what the #278/#295
> objection to a new wire key actually asks for.
>
> **Three findings this session, all from running things rather than reading
> them.**
>
> **(1) The route was already emitting garbage, invisibly.**
> `test_serve_search_route.py`'s fake page is a `MagicMock`, and
> `jsonable_encoder` renders an auto-attribute as `{}` rather than raising —
> so the moment `run_search` read `page.sort_applied` the wire carried
> `"sort_applied": {}` with all 34 of that file's tests green. Found by asking
> what the response body actually contained, not by a failing test.
>
> **(2) Three of my own wire tests passed vacuously.** `run_search` treats
> **both** `None` and `[]` as a grant-nothing ACL and short-circuits before
> the Searcher — so tests written with `allowed_account_ids=None` asserted the
> short-circuit's own value and never touched `page.sort_applied`. Caught only
> because a fourth test asserted `next_cursor is not None` and failed.
>
> **(3) One mutation survives and is recorded rather than smoothed over.**
> Reverting the *Relevance* radio's `checked` binding alone is masked by
> radio-group exclusivity: the rendered state is identical, so no assertion
> can see it. Reverting the Date binding, or both, is caught, and
> `relevance.disabled` carries the one-sided case.
>
> **Open issue count is 20, dropping to 19 on merge. Dependabot stays 0.**

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

Four commits plus this handoff, one PR. This is the first session in a while
to touch `gui/`, so **all five frontend gates were run** (risk 32).

### `2473b7c` — the response says which ordering actually ran

`SearchPage.sort_applied`, stamped by the branch that produced the rows — the
same derive-don't-restate call `next_keyset` makes, so no layer above can
report an ordering the walk did not use. **Defaultless**, for the reason
`KeysetCursor.order` has none: a page that could claim `rank` by omission is
#345 itself.

- `run_search` puts it on the wire on every branch. The empty-ACL
  short-circuit reports `plan.sort` — exact on the fresh and keyset modes; on
  **pool** mode it is the caller's claim about a pool that branch never
  consults, accepted only because no rows come back. `_empty_grown_page` takes
  `meta.sort`, not a default.
- HTTP and MCP return `run_search` directly with no response model, so the key
  reaches both transports unchanged. The MCP tool docstring and
  `docs/mcp-usage.md` tell agents to read it — they are the audience the
  "omit `sort`" advice is written for.
- **Seven mutations, all caught**, each verified to land in the intended
  function. The pool pins needed a **date-built pool put straight into the
  cache**: every end-to-end pool page is rank-ordered, so a hardcoded `"rank"`
  agreed with all of them (the technique
  `test_searcher_pool_metadata.py` already uses, for the same reason).

### `9544895` — the sort selector shows the ordering that ran

The pure `gui/src/lib/sort_display.ts` (`displayedSort` /
`relevanceUnavailable`), co-located for the reason `search_paging.ts` gives.
**Neither rule inspects the query** — reading the answer the server already
sent is not reproducing `parse_query`.

- **Relevance is disabled with a reason**, not merely re-labelled: the
  `action_flags` precedent, removing the inert control rather than quietening
  it.
- **Disabled only on proof.** `statedSort` never sends `rank`, so a rank
  preference answered `date` means the server found nothing to rank. A `date`
  *request* proves nothing either way, so an explicit Date selection on a
  textless query leaves Relevance enabled until clicked once — a **deliberate
  imprecision**, documented, because judging it earlier means a second parser.
- **`sortApplied` keeps describing the rows on screen** and is not cleared on
  a query edit; clearing would flip the selector back to the request while
  date-ordered rows are still displayed.
- The Rust struct has `#[serde(default)]` (the `is_admin` precedent) **and its
  own test**: every frontend test mocks `runSearch`, so a field dropped from
  `SearchResponse` would be discarded at the Tauri hop with all 450 vitest
  tests green — #278's shape.
- **Ten mutations: eight caught, one refused to apply (ambiguous anchor), one
  survives and is recorded** (the radio-group masking above).

### `09e6666` — the mock pages set it, and the route pins it

The `jsonable_encoder` finding. Every mock page sets the field explicitly, and
`test_search_returns_results` asserts it **through the real HTTP route** —
mutation-checked, dropping the key fails it with a `KeyError`. The defaultless
type stops a *real* construction omitting it; a mock bypasses that by
definition, which is why the route assertion is the one that carries it.

### `bd72c77` — README + CLAUDE.md

README gained the read-back half beside its existing "omit `sort`" advice, and
a line saying the GUI's selector renders `sort_applied`. CLAUDE.md records the
entry in place, including the rejected cursor-prefix inference **with the
measurement**, and the `{}`-on-the-wire finding.

### Verification (this Mac, all extras)

- **Both refs measured in this session** (risk 7), clean tree at each:

  | gate | `main` @ `6a8ba7a` | branch @ `bd72c77` |
  |---|---|---|
  | pytest collected | **3301** | **3318** |
  | pytest run | — | **3318 passed, 2 warnings, 214 s** |
  | vitest | **431** (48 files) | **450** (49 files) |

  The +17 python is exactly `tests/test_search_sort_applied.py`. The +19
  vitest is 8 (`sort_display.test.ts`) + 5 (SearchBar) + 6 (store).
- The 2 warnings are the pre-existing `websockets` deprecations (**#25**), so
  **#321's acceptance signal still holds**.
- `cargo test` → **108 passed** on the branch. `main` was **not** re-run
  (a cold rebuild for one number); the delta is a **static** `#[test]` count,
  39 → 40. Treat 107 for `main` as derived, not measured.
- `cargo clippy --locked -- -D warnings` and `cargo clippy --all-targets
  -- -D warnings` both clean. `npm run check` → **0 errors**, 327 files.
  `npm run build` → ok.
- `mypy src/localmail` → Success, **153** files (unchanged — the new module is
  under `tests/`). `mypy` on the new test module → Success.
- `ruff check src/localmail/` → **10**, the unchanged #285 baseline.
- **Live end-to-end against the 129,009-message archive**, five shapes, all
  correct — including `subject:zzzqqqnope`, which returns **no cursor** and
  still reports `date`. That is the case option 1 could not have served.
- **Live signal beyond pytest**: the Mac launchd daemon runs an editable
  install and was executing this branch throughout. All **7**
  `daemon_heartbeats` rows stayed under 30 s old. `com.localmail.serve` is
  running but was **not restarted**, so it is still serving pre-#345 code —
  restart it after merging if you want the field live.
- **There is still no ROADMAP.md** — that `/nextsession` step remains a no-op
  (risk 35, re-confirmed a **ninth** time).
- Machine was **not** carrying orphaned burners (risk 5 checked first), but
  load was **15** during the final run against ~5 earlier, so 214 s is not
  comparable to a quiet-machine figure.

## What's next

### 0. **Merge the PR, then check the issue actually closed**
   **You merge** (project convention). The body uses `Closes #345`. Risk 2 says
   verify with `gh issue list` afterwards — **20 → 19**.
   - **No `uv.lock` change**, so neither host needs a dependency sync. The
     Mac's editable install follows the tree, so `git checkout main` there
     after merging (risk 26), and **restart `com.localmail.serve`** if you want
     `sort_applied` on the live wire.
   - **No migration.** Latest is `0036_api_keys.sql`; next free slot
     `0037_*.sql`.
   - **The search-sort cluster is now closed** (#308, #311, #312, #322, #323,
     #324, #326, #331, #333, #342, #344, #347–#350, #345). What remains of it
     is refactoring: #327, #328, #330.

### 1. **The #322/#332 review-round leftovers** *(carried, and #330 is cheapest)*
   **#330** (`SortOrder`/`SortMode` restated in three wire layers) · **#327**
   (`CursorPlan` carries two fields its pool-mode consumer must ignore) ·
   **#328** (the page-cache entry is an untyped dict).
   - **#330 is cheaper again**: `sort_axes.py` is the one authority for both
     axes *and* their membership rule, so the wire layers have a single import
     target. Note `SearchResponse` now has a **fourth** restatement of the
     vocabulary — `sort_applied?: "rank" | "date"` in
     `gui/src/lib/api/search.ts` plus `Option<String>` in the Rust struct —
     so fold those in rather than treating them as out of scope.
   - **#328 has a concrete new consumer**: `_check_pool_sort` and
     `_empty_grown_page` both now read `meta.sort` off that dict, and the
     latter is a defaultless argument, so a typed entry would make the read
     checkable.

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
   - **#345 adds a fourth**: `cli.py`'s search output shows no ordering at all,
     so it now has a `page.sort_applied` to print if the refactor wants one.

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
   (risk 31).

### 7. **Remaining robustness backlog** *(carried)*
   **#218** · **#226** · **#225 / #227** · **#200 / #211 / #208** · **#206** ·
   **#204** · **#25**.

### 8. **Smaller, deliberately not done** *(carried)*
   - **165 docling failures on the Mac**, of 182 `blobs_gave_up`. Worth a fresh
     look now the pypdf bump has been on the host for five sessions.
     **Re-measure before claiming it; that is a hypothesis, not a finding.**
     *(Not re-measured this session — no archive work was done.)*
   - **Residual implausible language labels dominated by `ja`** (~0.24%); the
     confidence-floor lever was measured useless. **Sample `ja` first.**
   - **The DGX drops remain uninvestigated** (risk 30).
   - **A session-22 stash is still on the stack** (`stash@{0}`); its content is
     on `main`, so `git stash drop` it if you want the tree tidy.
   - **A stray SDD workspace** at `.superpowers/sdd/`. Git-ignored scratch.

## Open decisions & risks

1. **One PR is open and yours to merge.** `fix/345-sort-applied`, based on
   `main` (`6a8ba7a`), closing **#345**. **20 open issues, dropping to 19.
   Dependabot stays 0.**
2. **A merge does NOT close issues its subject merely names** *(carried)*. Use
   `Closes #N` in the **PR body** and **check `gh issue list` after the merge**.
3. **THIS FILE IS NOT THE AUTHORITY — `git` and `gh` are** *(carried)*. It was
   accurate this time, which is the first time in eleven sessions; that is a
   consequence of #351 merging without a review round, not of the file becoming
   trustworthy. Open every session with
   `git fetch --prune && git log --oneline -1 origin/main`, `gh pr list`,
   `gh issue list`, the Dependabot query — and reconcile **before acting**.
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
   concluding a change made things slow. Clean this session — but **load was
   15** at the end against ~5 at the start, so **do not compare 214 s to a
   quiet-machine figure**; re-measure on an idle box before calling anything a
   regression.
6. **In zsh an unquoted `$VAR` does not word-split** *(carried)*. `uv run
   pytest -q $T` runs **zero** tests and prints `no tests ran in 0.00s`, which
   reads as benign. Use `${=T}`, and **always run the unmutated baseline
   through the same harness first**.
7. **Measure both refs in the session that reports them** *(carried)*. Held
   this time. Note the *derived* numbers in the table above are labelled as
   such — `main`'s cargo count is a static `#[test]` count, not a run.
8. **A MUTATION THAT DOES NOT APPLY LOOKS EXACTLY LIKE ONE THAT IS CAUGHT**
   *(carried, and it fired twice this session)*. Two mutations matched an
   ambiguous anchor; the harness **refused to write** and said so, and the
   test run that followed reported a clean pass. Without the refusal both would
   have been recorded as caught. **The helper is worth keeping**: it exits
   non-zero on a non-unique anchor and prints the file, line and enclosing
   function of every mutation it does apply. It is in the resume commands below.
9. **"It's only a backstop" / "it's unreachable" / "verified" is a claim to
   test** *(carried)*.
10. **Write the justification down, then try to refute it** *(carried, and it
    paid immediately)*. #345's own preferred fix was refuted by one probe
    against the live archive before any code was written, and the refutation is
    recorded **in the code and in CLAUDE.md**, not only here.
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
    checkout`** *(carried, sessions 23–42)*. Used 17 times this session.
    **Re-snapshot after each GREEN**, not once at the start.
14. **A MagicMock attribute reaches the wire as `{}`, not as an error** *(new
    — #345)*. `jsonable_encoder` renders one silently, so adding a field that
    a route reads off a mocked page ships garbage with every test green. **When
    you add a wire key, assert it through the real transport**, not only at the
    service function — and set it explicitly on every mock page.
15. **`run_search` treats `allowed_account_ids=None` AND `[]` as
    grant-nothing** *(new — #345)*. Both short-circuit before the Searcher, so
    a test written with `None` asserts the short-circuit's own value and never
    reaches the page. Three of this session's tests passed vacuously that way.
    **A wire test that expects rows must pass a real grant**, and should assert
    `results` is non-empty so it fails loudly when the fixture stops seeding.
16. **A test module must import `localmail.serve.app` at MODULE scope**
    *(carried — #321)*. An AST scan plus a teardown re-check enforce it.
17. **When a guard is an AST rule, mutate BOTH branches of every predicate**
    *(carried)*. Related: **never assert a substring the message's own remedy
    text contains**, and **never compare a constant against itself**.
18. **Some DOM mutations are masked and cannot be pinned** *(new — #345)*.
    Radio-group exclusivity resolves a one-sided `checked` binding revert, so
    the rendered state is identical and no assertion sees it. **Record such a
    gap in the test** rather than adding an assertion that pins something a
    user cannot observe; pin the independent property instead (here,
    `disabled`).
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
    #324)*. **And never render one it did not run** *(new — #345)*: the
    selector reads `sort_applied`, and a client that omits `sort` (as every
    paging client should) has no other way to know what it got.
26. **`sort` and `sort_order` resolve from the query, not from a constant**
    *(carried — #324)*. `sort_axes.resolve_sort` is the one authority. **Do
    not** have a cursor record the sort the caller *stated* rather than the one
    that ran — that is #308 itself.
27. **The stale NOTIFY queue recurs, and gate 1 lies** *(carried)*. **Check
    both gates before reaching for the runbook.**
28. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `uv sync --all-extras` (Mac) / `--extra mcp --extra extraction` (DGX).
    **`uv` is not on the DGX's non-interactive PATH** — use `~/.local/bin/uv`.
    **A branch checkout re-resolves the venv** — re-sync after any checkout you
    intend to measure against.
29. **The Mac's launchd daemon runs an EDITABLE install** *(carried)*. It
    executes whatever the tree is checked out to. **Currently on
    `fix/345-sort-applied`** — check out `main` after merging. Its 7 heartbeats
    were verified healthy under this branch. **`com.localmail.serve` was NOT
    restarted**, so it is still serving pre-#345 code.
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
    **3317 passed, 1 skipped**.
39. **No ROADMAP.md** *(carried, re-confirmed a ninth time)* — that
    `/nextsession` step is a no-op. **README and CLAUDE.md were both updated**
    this session (README genuinely needed it — the wire gained a key).
40. **A green local run is still not evidence** *(carried)*. Push and let CI
    decide.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status && git branch --show-current
git log --oneline -1 origin/main         # THE authority, not this file's header
git log --oneline main..origin/main      # non-empty = a session landed since

# RISK 2 — after any merge, CHECK THE ISSUE ACTUALLY CLOSED.
gh pr list
gh issue list --limit 40                 # 20 open; the PR should take it to 19
gh issue view 345 --json state --jq .state

# RISK 11 — expect ZERO, still. Confirm, don't assume.
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '[.[] | select(.state=="open")] | length'      # expect 0
# If a new one appears, read its range against THE LOCK, never the floor:
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | "\(.security_advisory.severity)\t\(.dependency.package.name)\t\(.security_vulnerability.vulnerable_version_range)\tfixed in \(.security_vulnerability.first_patched_version.identifier)"'
grep -A1 '^name = "<pkg>"' uv.lock

# RISK 5 — BEFORE trusting any timing, check for orphaned CPU burners
#          AND read the load. This session ended at load 15.
ps -eo pid,ppid,etime,time,command | grep "[w]hile :; do :; done"
uptime

# RISK 4 — stranded-branch shortlist (noisy: every squash-merged branch shows).
for b in $(git branch -r --format='%(refname:short)' | grep -v 'origin/main\|HEAD'); do
  n=$(git log --oneline main.."$b" | wc -l); [ "$n" -gt 0 ] && echo "$b: $n commits not on main"
done
git diff --stat main origin/<branch>     # EMPTY = landed, not stranded

# AFTER MERGING — the Mac tree only; uv.lock did NOT change this session.
#   git checkout main && git pull        # editable install follows the tree (risk 29)
#   launchctl kickstart -k gui/$UID/com.localmail.serve   # to serve sort_applied live

# Python suite. NEVER a bare `uv sync` (risk 28).
unset VIRTUAL_ENV && uv sync --all-extras
unset VIRTUAL_ENV && uv run pytest -q
#   macOS: expect 3318 passed, 0 failed, 0 skipped, and **2 warnings**.
#   THOSE 2 ARE #321's ACCEPTANCE SIGNAL: both are the pre-existing websockets
#   DeprecationWarnings (#25). A leaked pool is now a FAILING test whose message
#   reads "cannot join current thread"; the test it names is arbitrary (the GC
#   picks it), the message is the diagnosis.
#   LINUX/CI: expect 3317 passed, 1 SKIPPED; pre-existing (risk 38).
#   MEASURE BOTH REFS IN THIS SESSION (risk 7) — no DB needed:
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2   # 3318 here
git checkout main && unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2
#   main @ 6a8ba7a = 3301.  Assert `git status --short` is EMPTY at each.
unset VIRTUAL_ENV && uv run mypy src/localmail    # expect Success, 153 files
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -2   # expect 10 (#285)

# #345, verified directly — server and client:
unset VIRTUAL_ENV && uv run pytest -q tests/test_search_sort_applied.py
#   expect 17 passed in ~2s
cd gui && npx vitest run src/lib/sort_display.test.ts src/components/SearchBar.test.ts \
                        src/lib/stores/search.test.ts && cd ..
#   expect 53 passed

# #345 END TO END against the LIVE archive — this is what decided the design.
# `subject:zzzqqqnope` returns NO cursor and still reports its ordering, which
# is the case a client-side inference from the cursor prefix cannot serve.
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
    print(f"{label:34} rows={len(out['results']):2}  cursor={kind:6}  sort_applied={out['sort_applied']!r}")
probe("textless, no filter", free_text="", filters={})
probe("has:attachment only", free_text="has:attachment", filters={})
probe("subject:zzzqqqnope (no cursor!)", free_text="subject:zzzqqqnope", filters={})
probe("text 'invoice'", free_text="invoice", filters={})
probe("text + sort=date", free_text="invoice", filters={}, sort="date")
PY
#   expect date/date/date/rank/date, with the third showing cursor=none.

# RISK 6 — the mutation harness MUST word-split. zsh does not do it for you:
T="tests/test_search_sort_applied.py"
uv run pytest -q ${=T}       # ${=T}, NOT $T. $T runs ZERO tests and says
                             # "no tests ran in 0.00s", which reads as benign.

# RISK 8 — the mutation helper. It REFUSES a non-unique anchor (which is how
# two silent no-ops were caught this session) and prints where the edit landed.
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

# Host health (Mac) — verified healthy under this branch this session:
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
#   expect 0 errors / 327 files; 450 passed (49 files); built ok
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
#   expect 108 passed; both clippy runs clean
```

`main` tip at session start was **`6a8ba7a`**. This session left **one PR** open
on `fix/345-sort-applied` — `2473b7c` (server), `9544895` (client), `09e6666`
(the mock/route pin), `bd72c77` (docs) and the handoff commit — closing
**#345**. Latest migration **`0036_api_keys.sql`**; next free slot
`0037_*.sql` (this session adds none). **Open issues: 20**, dropping to **19**
on merge. **Dependabot: 0.**
