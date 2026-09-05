# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-09-05 (session 41).** `main` was **`94c0aaa`** at the
> start — PR #346 merged by the operator, **#344 and #331 both closed**,
> **Dependabot still 0**. This session opened **one PR** on
> `fix/346-review-round`, closing all four issues of #346's review round:
> **#347, #348, #349, #350**.
>
> **The previous handoff predicted 20 open issues; the real count was 24.**
> Not its fault, and the same shape as last session: the operator filed the
> four review issues *after* it was written. Risk 3 paid for the tenth time —
> reconcile with `gh` before acting, always.
>
> **The published test count was 3180; `main` measured 3188.** Again not
> stale: session 40 measured at its first commit `2a5599b`, and PR #346 gained
> a review round before merging. This is the *second consecutive* session
> where that happened, so treat "measure the ref you are about to push, in the
> session that reports it" as the rule rather than the caution (risk 7).
>
> **Three findings this session, each from writing a test rather than reading
> code.** All three are corrections to the issues themselves, and all three
> are recorded *as corrections* in the code so nobody re-derives them.
>
> **(1) #349's contract was already broken, and its own verification missed
> it.** The issue states all five raise sites were verified pre-IO. That
> verification used a `pool.connection` tripwire **alone** — and the rewriter
> is a second IO route. The #308 hybrid-branch guard sat *below* the smart
> rewrite, so a caller on the smart path paid a full LLM round trip to be told
> their cursor was unusable. Found by writing the pin, which is the argument
> for deriving pins from the type instead of hand-writing them per member.
>
> **(2) #347's title and body describe different defects, and only the body is
> #344.** A member that inherits the base but lives in `searcher.py` is mapped
> to **400** correctly — measured — so it does not "reproduce #344"; what it
> costs is the *derived pins*, since `_family()`'s `__module__` filter
> excludes it. The shape that really is #344 is `class Foo(ValueError)`, and
> the acceptance #347 states would not have caught it. Hence **two** rules.
>
> **(3) #350's premise is overstated.** It says the missing-space wire label
> is caught by nothing; measured against the pre-#350 tree, dropping the space
> from a *shipped* member fails **three** tests. The real exposure is a **new**
> member, where the right and wrong spellings fail the *same two* enumeration
> tests — which fail for the member being new and say nothing about spelling.
>
> **Open issue count is 24, dropping to 20 on merge. Dependabot stays 0.**

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

Five commits plus this handoff, one PR — the whole review round of #346, taken
together because all four touch `argument_errors.py`, `api/search.py` and one
docstring, and #348's decision feeds #347's scope and #350's wording.

### `8eccd2f` — #348: a misspelled sort is refused at the boundary

`Searcher.search` membership-checks both axes and raises a plain `ValueError`;
`serve/app.py` handles `APIError` only, so `run_search` — the transport-free
library MCP imports directly — passed it out as an **unhandled 500**. HTTP and
MCP both declare `Literal`s (verified by driving `SearchRequest` and reading
`mcp/server.py`), so neither can reach it — which made the property an
**obligation on every transport** rather than one the boundary holds.

- **One half was wire-visible already.** The empty-ACL short-circuit returns
  *before* the Searcher validates, so `sort="Date"` from a grant-nothing caller
  was answered **200 with an empty page**, byte-identical to "you have reached
  the end". Every other gate in `run_search` is ahead of that branch for
  exactly this reason; this axis was the one that was not.
- The rule is the pure `sort_axes.sort_membership_error`, shared by both
  layers — the `sort_applicability_error` / `keyset_walk_error` shape.
- **Ordered ahead of every other guard, at BOTH layers.** A value that is not
  a value cannot contradict a cursor: `resolve_cursor_plan` and
  `KeysetOrderMismatch` both interpolated the typo into a sentence asserting
  what the *cursor* continues, in wording that is a coincidence of which
  cursor was in hand.
- **The membership checks stay outside the family** (your decision, recorded
  in `argument_errors`' docstring as the issue asked).

### `d99af6e` — #350: the wire label's separator belongs to the type

`label` names the subject (`"cursor"`), `_SEPARATOR` is owned by the base, and
`wire_message()` does the join — so both boundaries read
`raise ValidationFailed(exc.wire_message()) from exc` and a fourth cannot
re-decide it. A blank message renders the label alone, tested on `.strip()`
rather than truthiness. `test_the_keyset_branch_keeps_naming_the_cursor` is
**unchanged**, as #350 asks.

### `925fa98` — #349: pre-IO is a contract, pinned — and it was false

The pin provokes every raise **site** for real and asserts the pool, the
embedding backend **and** the rewriter are untouched. Three tripwires, because
a guard below the rewrite still leaves the pool clean — which is exactly how
this went unnoticed. The #308 guard is hoisted above the rewrite; the move is
**exactly** equivalent (reaching the old site meant `effective_sort != "date"`,
and `effective_sort` is never reassigned). **No wire behaviour changes** — same
exception, same message, one fewer LLM call.

`test_every_member_has_a_provocation` is the reverse cross-check that makes a
hand-written table safe: a member with no recipe fails rather than being
skipped.

### `4505e78` — #347: the family is enforced against `src/`

Two rules in the pure [tests/_search_family_rules.py](tests/_search_family_rules.py):
`misplaced_member_error` (a member must be declared in the family module —
what #347 asks, keeping `_family()` honest) and `foreign_refusal_error` (a
**named** exception raised from `Searcher.search` must be a member — the
"stronger variant", and the one that closes the 500).

- **The raise rule judges spelling, never a decision**, which is how it avoids
  becoming the "second authority" #347 warns against: a bare builtin is out of
  scope by construction, a named class must be a member.
- Both rules **report rather than pass** when they would inspect nothing.
- AST, never text — every forbidden shape is named in prose in four places.

### `4c05a49` — CLAUDE.md

The stale "filed rather than fixed" bullet replaced with what each turned out
to be, both overstatements recorded as corrections, two stale
`exc.wire_prefix` references fixed, Layout block updated.

### The mutation battery — 19 mutations, all caught

Restored from scratchpad copies every time, never `git checkout` (risk 13).
Baseline run through the harness *before* it certified anything (risk 5).

| mutation | caught by |
|---|---|
| `sort_membership_error` always returns None | 47 |
| `run_search`'s gate deleted | 27 |
| gate moved below the empty-ACL short-circuit | 18 |
| gate moved after `resolve_cursor_plan` | 9 |
| Searcher's check put back below its cursor block | 2 |
| the rule always refuses (positive controls) | 14 |
| a `label` spelling the separator itself | 6 |
| the blank-message branch deleted | 3 |
| that branch testing truthiness, not `.strip()` | 2 |
| a boundary re-writing the join by hand | 2 |
| **the #308 guard back below the rewrite** | **1** |
| a fifth member with no provocation | 1 |
| a fifth member declared in `searcher.py` | 1 |
| **`class Foo(ValueError)` raised from `search` (the #344 shape)** | **1** |
| `ValueError` dropped from the allowlist (proves the real tree is read) | 1 |
| `_base_names` ignoring qualified bases | 1 |
| the location closure running one pass, not a fixpoint | 1 |
| `_search_method` matching any method | 1 |
| `_raised_name` treating a bare re-raise as a declaration | 1 |

**Two of those found test defects rather than code defects**, and both are
fixed: the out-of-scope fixture listed `search` first, so deleting
`item.name == SEARCH_METHOD` left the file green; and the bare-re-raise guard
was redundant with the unnameable-callee guard, so no mutation could tell them
apart — collapsed into `_raised_name`, since an arm no test can reach is one to
remove rather than document.

**Three mutation attempts were themselves broken and produced vacuous "caught"
results before being redone** (a bad anchor string; a mutation landing in
`_search_with_parsed` instead of `search`, twice). Each was noticed only by
checking *which* test failed and *why*. A mutation that does not apply reports
the same green as a mutation that is caught.

### Verification (this Mac, all extras)

- **Both refs measured in this session** (risk 7), clean tree at each:

  | gate | `main` @ `94c0aaa` | branch @ `4c05a49` |
  |---|---|---|
  | pytest collected | **3188** | **3262** |
  | pytest run | — | **3262 passed, 0 skipped, 2 warnings, 207s** |

  The +74 is exactly 33 (`test_api_search_sort_membership`) + 21
  (`test_search_family_rules`) + 7 (`test_searcher_guards_precede_io`) + 7
  (`test_search_argument_errors`, 17→24) + 6 (`test_searcher_guard_precedence`,
  7→13).
- The 2 warnings are the pre-existing `websockets` deprecations (**#25**), so
  **#321's acceptance signal still holds**.
- `mypy src/localmail` → Success, **153** files (unchanged — the new module is
  under `tests/`). `mypy` on the four new/changed test modules → Success.
- `ruff check src/localmail/` → **10**, the unchanged #285 baseline. (My change
  removed the last use of `get_args` in `searcher.py`; the now-unused import
  was dropped, which is why the count did not move to 11.)
- **No GUI gates run, because no `gui/` file changed** (risk 28 scopes them to
  `gui/` edits). **Do not quote a vitest number from this file.**
- **Live signal beyond pytest**: the Mac launchd daemon runs an editable
  install and was executing this branch throughout. All **7**
  `daemon_heartbeats` rows stayed under 25 s old — the guard hoist and the
  module changes work in a running process, not only under test.
- **README needs no update — verified, not assumed.** Driving `SearchRequest`
  directly shows pydantic answers **422** for `sort="Date"` / `sort_order="ASC"`
  *before* `run_search`, and MCP declares the same `Literal`s, so #348's new
  400 is **unreachable from either shipped transport**. #349 and #350 change no
  message and no status. **CLAUDE.md updated.**
- **There is still no ROADMAP.md** — that `/nextsession` step remains a no-op
  (risk 35, re-confirmed an **eighth** time).
- Machine was **not** carrying orphaned burners this session (risk 5 checked
  first); load was ordinary macOS indexing, so 207 s is comparable to last
  session's 212 s.

## What's next

### 0. **Merge the PR, then check the issues actually closed**
   **You merge** (project convention). The body uses `Closes #347`, `#348`,
   `#349`, `#350`. Risk 2 says verify with `gh issue list` afterwards —
   **24 → 20**.
   - **No `uv.lock` change**, so neither host needs a dependency sync. The
     Mac's editable install follows the tree, so `git checkout main` there
     after merging (risk 25).
   - **No migration.** Latest is `0036_api_keys.sql`; next free slot
     `0037_*.sql`.

### 1. **#345 — the GUI's Relevance radio is inert on a textless search** *(carried)*
   `SearchBar.svelte` binds a radio to `search.snapshot.sort` (default
   `"rank"`), but a textless query is served **date-ordered**, so clicking
   Relevance re-submits and changes nothing — the inert-control pattern
   CLAUDE.md names as a defect (#148).
   - **Acceptance:** the ordering the client *shows* is the one that ran.
   - **The issue's option 1 is preferred and needs no server change**: infer it
     from the page that came back (the date walk returns a `KA|`/`K|` cursor
     and `search_token: null`). **Do not reproduce `parse_query` in the
     client** — `search_paging.ts` rejects that explicitly, and #324's review
     already caught one regression from a client-side `query.trim()`.
   - Option 2 (a `sort_applied` wire field) is cleaner to consume but must land
     *with* its renderer (#278/#295 precedent).
   - **This is the last open issue from the search-sort cluster**, so it is the
     natural next item.

### 2. **The #317 API-keys review round** *(carried)*
   **#319** (`ApiKeyNotFound` carries three meanings; the panel reports a
   security refusal as success) · **#318** (the `api_key_name` ⟺ `is_service`
   pairing is unenforced in the schema; **next free migration slot is
   `0037_*.sql`**) · **#320** (admin panel routes do blocking DB IO on the
   event loop).

### 3. **The #322/#332 review round leftovers** *(carried, and #330 is cheaper again)*
   **#327** (`CursorPlan` carries two fields its pool-mode consumer must
   ignore) · **#328** (the page-cache entry is an untyped dict) · **#330**
   (`SortOrder`/`SortMode` restated in three wire layers).
   - **#330 is cheaper still now**: `sort_axes.py` is the one authority for
     both axes *and* now owns their membership rule, so the wire layers have a
     single import target. `argument_errors.py` and `_search_family_rules.py`
     have both established the small-module pattern.

### 4. **#340 — the harness lock proves a lock was taken, not which database**
   *(carried)*. The AST rule compares call *positions* and never arguments, so
   locking one database while working against another passes. Latent — all five
   harnesses use one `dsn`. A correct check needs parameter-flow analysis.

### 5. **#305 — `--version` dies on a missing third-party dependency** *(carried)*
   `cli.py` imports the daemon at module scope. **Acceptance:** blocking
   `sqlparse` on `sys.meta_path` leaves `localmail --version` printing its line
   and exiting 0. **Do it with the `cli.py` refactor, not before** — `cli.py` is
   **2177 lines**.
   - **#331's point 2 lives here**: widen `cli.py`'s search catch to
     `SearchArgumentRefused`, never to bare `ValueError`.
   - **#350 adds a second item to that bullet**: the CLI is the *third*
     consumer of the wire join, and since #350 it is one call —
     `exc.wire_message()` — not an f-string to restate.
   - **And #348 adds a third**: a `--sort`/`--sort-order` flag makes the
     Searcher's plain-`ValueError` membership check reachable from the CLI,
     where it would traceback. `cli.py` calls `create_searcher(...).search(...)`
     directly, so `run_search`'s new gate does **not** cover it.

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
   (risk 31).

### 8. **Remaining robustness backlog** *(carried)*
   **#218** · **#226** · **#225 / #227** · **#200 / #211 / #208** · **#206** ·
   **#204** · **#25**.

### 9. **Smaller, deliberately not done** *(carried)*
   - **165 docling failures on the Mac**, of 182 `blobs_gave_up`. Worth a fresh
     look now the pypdf bump has been on the host for four sessions.
     **Re-measure before claiming it; that is a hypothesis, not a finding.**
     *(Not re-measured this session — no archive work was done.)*
   - **Residual implausible language labels dominated by `ja`** (~0.24%); the
     confidence-floor lever was measured useless. **Sample `ja` first.**
   - **The DGX drops remain uninvestigated** (risk 30).
   - **A session-22 stash is still on the stack** (`stash@{0}`); its content is
     on `main`, so `git stash drop` it if you want the tree tidy.
   - **A stray SDD workspace** at `.superpowers/sdd/`. Git-ignored scratch.

## Open decisions & risks

1. **One PR is open and yours to merge.** `fix/346-review-round`, based on
   `main` (`94c0aaa`), closing **#347, #348, #349, #350**. **24 open issues,
   dropping to 20. Dependabot stays 0.**
2. **A merge does NOT close issues its subject merely names** *(carried)*. Use
   `Closes #N` in the **PR body** and **check `gh issue list` after the merge**.
3. **THIS FILE IS NOT THE AUTHORITY — `git` and `gh` are** *(carried, ten times
   earned; it paid again)*. The predicted count was 20 and the real one was 24,
   because four issues were filed after the handoff was written. **This is now
   the norm, not the exception: every session that ships a reviewed PR gains
   issues after its handoff is written.** Open every session with
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
   concluding a change made things slow. Clean this session; load was ordinary
   macOS indexing.
6. **In zsh an unquoted `$VAR` does not word-split** *(carried)*. `uv run
   pytest -q $T` runs **zero** tests and prints `no tests ran in 0.00s`, which
   reads as benign. Use `${=T}`, and **always run the unmutated baseline
   through the same harness first**.
7. **A count measured at your FIRST commit is not the count of what merges**
   *(carried, and now twice consecutively)*. The last handoff published 3180;
   `main` measured 3188, because PR #346 gained a review round after that
   measurement. **Re-measure both refs in the session that reports them,
   against a provably clean tree, and prefer the ref you are about to push.**
8. **A MUTATION THAT DOES NOT APPLY LOOKS EXACTLY LIKE A MUTATION THAT IS
   CAUGHT** *(new)*. Three of this session's mutation attempts silently failed
   — one on a stale anchor string, two by relocating code into
   `_search_with_parsed` instead of `Searcher.search` because the naive "first
   match" search found the wrong occurrence. Each reported a plausible pass or
   failure. **Print what the mutation did (line numbers, the enclosing
   function) and read WHICH test failed and WHY**, never just the tail count.
   `ast` is the reliable way to ask which function a line is in.
9. **"It's only a backstop" / "it's unreachable" / "verified" is a claim to
   test** *(carried and extended a third time)*. #349 said all five raise sites
   were *verified* pre-IO; the verification used one of three IO routes and the
   contract was already broken. **When an issue says "verified", ask what
   instrument was used.**
10. **Write the justification down, then try to refute it** *(carried, and it
    paid twice)*. #347's "reproduces #344" and #350's "nothing catches it" were
    both refuted by one probe each, before either shipped. Both refutations are
    recorded **in the code**, not only here.
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
    checkout`** *(carried, sessions 23–41)*. Used 19 times this session.
    **Re-snapshot after each GREEN**, not once at the start.
14. **A test module must import `localmail.serve.app` at MODULE scope**
    *(carried — #321)*. An AST scan plus a teardown re-check enforce it.
15. **When a guard is an AST rule, mutate BOTH branches of every predicate**
    *(carried, and it found two test defects this session)*. Related: **never
    assert a substring the message's own remedy text contains**, and **never
    compare a constant against itself**.
16. **Verify host revisions; do not infer them** *(carried)*.
    `ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'` plus
    `/v1/version`'s `build_hash` settles it. **Both hosts were left untouched
    and neither needs a dependency sync** — `uv.lock` is unchanged.
17. **A new Searcher guard over a stated argument must subclass
    `SearchArgumentRefused`, be declared in `argument_errors.py`, and be raised
    before any IO** *(extended — #344, #347, #349)*. All three are now enforced
    rather than documented: the family pins are derived from the type, the two
    AST rules check `src/` against the family, and the pre-IO pin provokes
    every raise site against three tripwires. **The membership checks on
    `sort`/`sort_order` are the deliberate exception** — a plain `ValueError`,
    spelled with a bare builtin, which is what the raise rule reads.
18. **Membership outranks every other guard, at both layers** *(new — #348)*.
    A value that is not a value cannot contradict a cursor or a query. Do not
    "tidy" the gate below `resolve_cursor_plan`, and do not put the Searcher's
    check back below its cursor block: both make a typo read as a paging
    problem, in wording that depends on which cursor was in hand.
19. **A keyset predicate must be a row comparison, in BOTH directions**
    *(carried — #75, #322, #323)*. The OR-form is semantically identical, keeps
    the index scan, adds no Sort, and plans as a per-tuple `Filter` that
    rescans from the index head on every page. Written by mistake **twice**.
    `tests/test_searcher_sort_order_plan.py` keeps three negative controls —
    **do not "tidy" them away**.
20. **The undated block is where the two directions genuinely differ**
    *(carried)*. Descending must *admit* it (hence the top-up query); ascending
    meets it at the head of its own walk. **Do not "restore symmetry"**, and
    **do not "fix" a short page by restoring `OR expr IS NULL`**.
21. **A cursor identifies a position, not a query — with one enforced
    exception** *(carried — #326)*. Dropping the query a **text** cursor was
    minted from is a 400; archive-walk cursors page with or without one.
22. **Never state a `sort` the server cannot serve** *(carried — #308, #311,
    #324)*. A paging client treats **409 as recoverable** and **400 as
    permanent for that cursor**. `search_paging.ts::statedSort` makes both
    unreachable from the GUI by construction.
23. **`sort` and `sort_order` resolve from the query, not from a constant**
    *(carried — #324)*. `sort_axes.resolve_sort` is the one authority. **Do
    not** have a cursor record the sort the caller *stated* rather than the one
    that ran — that is #308 itself.
24. **The stale NOTIFY queue recurs, and gate 1 lies** *(carried)*. Exactly
    three LISTEN/NOTIFY failures usually means the stale queue — but session 35
    had that failure set with **both gates clean** (it was contention).
    **Check both gates before reaching for the runbook.**
25. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `uv sync --all-extras` (Mac) / `--extra mcp --extra extraction` (DGX).
    **`uv` is not on the DGX's non-interactive PATH** — use `~/.local/bin/uv`.
    **A branch checkout re-resolves the venv** — re-sync after any checkout you
    intend to measure against.
26. **The Mac's launchd daemon runs an EDITABLE install** *(carried)*. It
    executes whatever the tree is checked out to. **Currently on
    `fix/346-review-round`** — check out `main` after merging. No dependency
    sync needed. Its 7 heartbeats were verified healthy under this branch,
    which is a genuine end-to-end signal for the guard hoist.
27. **`search-status` is sub-second** *(carried)*. If it runs long that is a
    **regression of #280** — check `EXPLAIN (FORMAT JSON)` for a `Seq Scan on
    messages` under a `SubPlan` first.
28. **`blobs_claimable 0` alongside `blobs_pending 0` is the SETTLED shape**
    *(carried)*. A steady non-zero `blobs_no_text` is **normal** (#277);
    **`blobs_gave_up` is the one to act on**. *(Not re-measured this session.)*
29. **`uv run pytest -q` with NO arguments is the right command** *(carried)*.
    **Do not run the suite while a backfill is draining.**
30. **An empty `daemon_heartbeats` right after a restart is normal for minutes**
    *(carried — #269/#271)*: the startup blob-temp sweep. Grep for
    **`blob-temp sweep done: walked=`**.
31. **The DGX drops are STILL UNEXPLAINED — five theories refuted** *(carried)*.
    **Do not propose a sixth without a captured outage in which the host was
    demonstrably up throughout.** Triage with `journalctl --list-boots` first.
    Power is not a candidate (~5-day UPS). **Do not edit
    `/etc/wireguard/wg0.conf`.**
32. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`.
    **Run vitest from `gui/`, not the repo root.** `cargo clippy --all-targets`
    is clean but **ungated** — CI runs clippy without it.
33. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    **#266's whitespace-heal is a one-way door too.** **`--relabel` is the only
    destructive verb in the lang path**; reach for `--retry-declined` first.
34. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    kept though #245 is fixed; #246 warns on group-write; `InsecureSecretsFile`
    refuses; #239's manual tombstone retention deliberate; admin bearer tokens
    have no per-token scope (#204).
35. **CI reports `1 skipped` on Linux; macOS reports 0** *(carried)*. It is
    pre-existing and still unidentified. **Do not read it as a missing uv
    extra** without checking a `main` run first. Expect CI here to read
    **3261 passed, 1 skipped**.
36. **No ROADMAP.md** *(carried, re-confirmed an eighth time)* — that
    `/nextsession` step is a no-op. **CLAUDE.md was updated; README was checked
    and correctly needed nothing** (verified by driving `SearchRequest`, not
    inferred).
37. **A green local run is still not evidence** *(carried)*. The local Postgres
    is more permissive than CI's. **Any test whose subject is "this process
    refuses / exits non-zero" must assert *why***. Push and let CI decide.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status && git branch --show-current
git log --oneline -1 origin/main         # THE authority, not this file's header
git log --oneline main..origin/main      # non-empty = a session landed since

# RISK 2 — after any merge, CHECK THE ISSUES ACTUALLY CLOSED.
gh pr list
gh issue list --limit 40                 # 24 open; the PR should take it to 20
gh issue view 347 --json state --jq .state   # and 348, 349, 350

# RISK 11 — expect ZERO, still. Confirm, don't assume.
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '[.[] | select(.state=="open")] | length'      # expect 0
# If a new one appears, read its range against THE LOCK, never the floor:
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | "\(.security_advisory.severity)\t\(.dependency.package.name)\t\(.security_vulnerability.vulnerable_version_range)\tfixed in \(.security_vulnerability.first_patched_version.identifier)"'
grep -A1 '^name = "<pkg>"' uv.lock

# RISK 5 — BEFORE trusting any timing, check for orphaned CPU burners.
ps -eo pid,ppid,etime,time,command | grep "[w]hile :; do :; done"
#   kill them with:  ps -eo pid,ppid,command | grep "[w]hile :; do :; done" \
#                      | awk '$2==1 {print $1}' | xargs -r kill
uptime      # and read the load; ordinary macOS indexing inflates a suite run

# RISK 4 — stranded-branch shortlist (noisy: every squash-merged branch shows).
for b in $(git branch -r --format='%(refname:short)' | grep -v 'origin/main\|HEAD'); do
  n=$(git log --oneline main.."$b" | wc -l); [ "$n" -gt 0 ] && echo "$b: $n commits not on main"
done
git diff --stat main origin/<branch>     # EMPTY = landed, not stranded

# AFTER MERGING — the Mac tree only; uv.lock did NOT change this session.
#   git checkout main && git pull        # editable install follows the tree (risk 26)

# Python suite. NEVER a bare `uv sync` (risk 25).
unset VIRTUAL_ENV && uv sync --all-extras
unset VIRTUAL_ENV && uv run pytest -q
#   macOS: expect 3262 passed, 0 failed, 0 skipped, and **2 warnings**, ~207s
#   on an unloaded machine. THOSE 2 ARE #321's ACCEPTANCE SIGNAL: both are the
#   pre-existing websockets DeprecationWarnings (#25). A leaked pool is now a
#   FAILING test whose message reads "cannot join current thread"; the test it
#   names is arbitrary (the GC picks it), the message is the diagnosis.
#   LINUX/CI: expect 3261 passed, 1 SKIPPED; pre-existing (risk 35).
#   MEASURE BOTH REFS IN THIS SESSION (risk 7) — no DB needed:
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2   # 3262 here
git checkout main && unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2
#   main @ 94c0aaa = 3188.  Assert `git status --short` is EMPTY at each.
unset VIRTUAL_ENV && uv run mypy src/localmail    # expect Success, 153 files
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -2   # expect 10 (#285)

# The #346 review round, verified directly — all four issues:
unset VIRTUAL_ENV && uv run pytest -q \
  tests/test_api_search_sort_membership.py tests/test_search_argument_errors.py \
  tests/test_searcher_guard_precedence.py tests/test_searcher_guards_precede_io.py \
  tests/test_search_family_rules.py
#   expect 98 passed in ~1s

# RISK 6 — the mutation harness MUST word-split. zsh does not do it for you:
T="tests/test_search_family_rules.py tests/test_searcher_guards_precede_io.py"
uv run pytest -q ${=T}       # ${=T}, NOT $T. $T runs ZERO tests and says
                             # "no tests ran in 0.00s", which reads as benign.

# RISK 8 — when mutating searcher.py, PRINT WHERE THE EDIT LANDED.
# Three attempts this session silently patched the wrong function.
unset VIRTUAL_ENV && uv run python - <<'PY'
import ast, pathlib
src = pathlib.Path("src/localmail/search/searcher.py").read_text(); tree = ast.parse(src)
def owner(ln):
    c = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
         and n.lineno <= ln <= (n.end_lineno or 0)]
    return max(c, key=lambda n: n.lineno).name if c else "<module>"
for i, l in enumerate(src.split("\n"), 1):
    if "with self._pool.connection()" in l or "raise " in l:
        print(f"{i:5} {owner(i):22} {l.strip()[:60]}")
PY

# #348 — the two defects it fixed, reproduced against `git stash` of the fix:
unset VIRTUAL_ENV && uv run python - <<'PY'
from unittest.mock import MagicMock
from localmail.api.errors import APIError
from localmail.api.search import run_search
for label, acl in (("granted", [1]), ("EMPTY ACL", [])):
    s = MagicMock(); s.smart_available = False
    try:
        out = run_search(searcher=s, free_text="hello", filters={}, limit=5,
                         allowed_account_ids=acl, user_id=1, sort="Date")
        print(f"{label:10} -> NO RAISE {out['results']!r}  <-- pre-#348 shape")
    except Exception as e:
        print(f"{label:10} -> {type(e).__name__} APIError={isinstance(e, APIError)}")
PY
#   expect BOTH to raise ValidationFailed with APIError=True.

# #349 — the pre-IO contract, with the rewriter tripwire that found the gap:
unset VIRTUAL_ENV && uv run pytest -q tests/test_searcher_guards_precede_io.py -v 2>&1 | tail -10

# #347 — the two AST rules against the real tree:
unset VIRTUAL_ENV && uv run python -c "
import pathlib
from tests._search_family_rules import (source_files, family_names,
                                        misplaced_member_error, foreign_refusal_error, FAMILY_MODULE)
src = pathlib.Path('src/localmail')
fam = family_names((src / FAMILY_MODULE).read_text())
print('family:', sorted(fam))
print('location rule:', misplaced_member_error(source_files(src)))
print('raise rule   :', foreign_refusal_error((src/'search'/'searcher.py').read_text(), family=fam))"
#   expect the five names, then None, None.

# THE POOL-LEAK PROBE — reusable; this is what found #321's second seam.
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
#   LOCALMAIL_TEST_DSN=postgresql://localmail:local%40%40mail@localhost:5532/localmail_test2 uv run pytest -q

# If EXACTLY the three LISTEN/NOTIFY tests fail, CHECK BOTH GATES (risk 24):
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'

# Host health (Mac) — verified healthy under this branch this session:
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 30)

unset VIRTUAL_ENV && uv run localmail search-status    # UNDER A SECOND (risk 27)
#   Check the SHAPE, not the literals: the four buckets must sum to
#   blobs_eligible, and claimable must equal pending.

# The DGX (risk 16 — verify, never infer):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'   # binds 10.0.0.3, NOT localhost
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — REQUIRED only if you touch gui/ (MUST run from gui/ — risk 32).
# NOT RUN THIS SESSION (no gui/ file changed), so no number is published here.
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

`main` tip at session start was **`94c0aaa`**. This session left **one PR** open
on `fix/346-review-round` — `8eccd2f` (#348), `d99af6e` (#350), `925fa98`
(#349), `4505e78` (#347), `4c05a49` (CLAUDE.md) and the handoff commit —
closing **#347, #348, #349, #350**. Latest migration **`0036_api_keys.sql`**;
next free slot `0037_*.sql` (this session adds none). **Open issues: 24**,
dropping to **20** on merge. **Dependabot: 0.**
