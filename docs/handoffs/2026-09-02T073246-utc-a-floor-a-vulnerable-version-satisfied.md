# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-09-02 (session 37).** `main` was **`ad69279`** at the
> start — PR #338 had already been merged by the operator, and **#321
> auto-closed correctly** (risk 2 discharged for a second consecutive round).
> This session opened **one PR** on `fix/dep-security-and-337-harness-lock`,
> closing **#337** and clearing four of the five Dependabot alerts. **CI green
> on both legs** after one round of it catching a real defect.
>
> **The previous handoff was accurate on every fact this session checked**,
> with one exception it could not have known and one it should have: `main` at
> `ad69279`, the branch deleted, 22 open issues after the merge exactly as
> predicted. The exception it could not know: **Dependabot went 0 → 5** between
> the sessions. The one it repeated from CLAUDE.md: there are **five**
> acceptance harnesses, not six.
>
> **Open issue count is 22, dropping to 21 on merge. Dependabot is 5, dropping
> to 1** — and that last one is a decision, not an oversight (see risk 1).
>
> **Two headline lessons.** Risk 5: a dependency floor that a *vulnerable*
> version satisfies is not a floor — `icalendar>=6.0` read as unaffected
> against a `>= 7.1.0, < 7.1.3` advisory while the lock sat at 7.1.0, squarely
> inside it. Read the range against `uv.lock`, never the declared floor.
>
> And risk 31, which is last session's risk 5 paying out a second time: **the
> first push was green on this laptop and RED on both CI legs.** The
> end-to-end pin rebuilt its DSN from a live connection
> (`conn.info.dsn`), which silently omits the password; macOS `pg_hba` did not
> demand one, CI did. Fixed in `74b8573`; CI is green now. A local pass is
> still not evidence.

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

Two commits, one PR. The dependency work was **not** on the previous handoff's
list — it came from reconciling with `gh` before acting (risk 3), which is the
whole reason that rule exists.

### `afc8c5f` — security floors for `pypdf` and `icalendar`

Dependabot went 0 → 5 between sessions (2 HIGH, 3 MEDIUM, all in `uv.lock`,
all filed 2026-08-25 → 2026-09-02). Four of them land on packages that parse
**attacker-controlled email attachments**: `extractor._extract_pdf` calls
`PdfReader`/`page.extract_text()`, `_extract_ics` calls `Calendar.from_ical`.
That is the case `pyproject.toml`'s sqlparse comment explicitly contrasts
against ("nothing attacker-controlled reaches it — the floor is hygiene").

- `pypdf 6.15.0 → 6.16.2`, floor `>=6.12.0 → >=6.16.1`. Three DoS advisories
  (#67/#68/#69): an infinite loop in `TreeObject.insert_child`, unbounded
  runtime/memory retrieving outlines and **extracting XForm objects** — the
  last on our own `extract_text()` hot path.
- `icalendar 7.1.0 → 7.3.0`, floor `>=6.0 → >=7.1.3`. HIGH algorithmic
  complexity in `Component.__eq__`.
- **Still a floor, not an incident.** A hostile attachment costs one extraction
  slot and then poison-pills under #153's transient budget, so it cannot stall
  the archive. That bound is what makes this a bump rather than a rollback.
- **Verified rather than assumed**, the way sqlparse was: extraction over six
  PDF/ICS shapes (native, multipage, outlined, scanned, a two-event calendar,
  an empty one) is **byte-identical** across the bump, and the 99 extraction
  tests pass. The snapshot script is deliberately **not kept** — it builds its
  fixtures with reportlab/PIL exactly as `test_extractor.py` does, which is
  where the permanent regression gate already lives.
- **`transformers` (#70, HIGH) is deliberately NOT bumped** — see risk 1.

### `f282016` — #337: the acceptance harnesses take the test-database lock

#336 made "one run at a time per test database" hold for pytest. It covered
pytest, **not the database**: the standalone harnesses truncate the same tables
against the same `LOCALMAIL_TEST_DSN` and took no lock, so starting one beside
a suite reproduced #329's corruption in both directions and with the same
silence — a `TRUNCATE` that deletes another run's rows *succeeds*.

Each entry point now wraps its database work — from `apply_migrations` onward —
in the new [tests/acceptance/_harness_lock.py](tests/acceptance/_harness_lock.py)`::harness_db_lock`.
**17 tests** in [tests/test_acceptance_harness_lock.py](tests/test_acceptance_harness_lock.py)
(16 there + 1 added to `test_db_session_lock.py`).

- **The helper and the rule requiring it live in one module** — the
  `blob_temps.py` minting-beside-matching call — and here the reason is
  sharper than usual: **nothing collects these files.** They match no
  `python_files` pattern, so no conftest fixture can arm them and the call has
  to be written into each `main()` by hand, which is exactly the obligation
  that gets forgotten. `harness_lock_error` walks each entry point's **AST**
  and reports any `DB_ENTRY_CALLS` member (`apply_migrations`, `open_pool`,
  `connect`) outside the lock; entry points are enumerated **from the
  filesystem**, so a sixth harness is in scope the day it lands.
- **"Somewhere in `main`" is not the rule; "before the first touch" is.** A
  harness that migrates and *then* locks has taken no lock at all.
- **The AST, not the text**, for the `_mentions_version_option` reason: every
  harness names the helper in prose while explaining why it calls it.
- **A contended database announces one line and exits `3`.**
  `SystemExit("some message")` prints the string and exits **1** — which is
  also what an eval returns when it fails its own acceptance gates, so a shell
  loop could not tell the two apart. It shipped that way first.
- **`busy_message`/`waiting_message` now say "test run", not "pytest
  session".** The holder may be a harness; naming pytest sends an operator
  hunting a process that need not exist.

**Correction carried into CLAUDE.md, README and the commit: there are FIVE
harnesses, not six.** `browse_explain_lib.py` touches the database but is
imported by `run_browse_explain.py` and never started, so its work already ran
inside that harness's lock. CLAUDE.md's prose said six while its own Layout
list said five; #337's issue text says six too.

### The two mutations that survived, and why both were *test* defects

Worth reading before writing the next AST guard — neither was a bug in the
code under test.

- **`_is_lock_call` forced to `True` left all 13 tests green.** The only
  non-compliant fixture had no `with` statement at all, so the function was
  never reached; the test that was supposed to prove "AST, not text" passed
  for an unrelated reason. Fixed by fixtures that wrap the DB work in *some
  other* context manager — and it needed **two**, because
  `psycopg.connect(...)` is an `ast.Attribute` and `ExitStack()` an
  `ast.Name`, and each branch survived until its own fixture existed.
- **The assertion meant to catch it was satisfied by the remedy sentence.**
  `"apply_migrations" in problem` passes on any message, because the remedy
  reads "from apply_migrations onward". That is the `__version__ in output`
  trap from #289, one module over. The assertions anchor on
  `"session lock: apply_migrations"` now.
- **A third defect was caught only by the end-to-end test**: the unit test's
  `caught.value.code != 0` passed *vacuously*, because `SystemExit`'s `code`
  was the message string. That is what produced the exit-1 bug above.

### Verification (this Mac, all extras)

- **Both refs measured in this session** (risk 9), same shell, no DB needed
  for the collect:

  | ref | collected | full run |
  |---|---|---|
  | `main` @ `ad69279` | **3025** | not re-run (last session measured 3025) |
  | branch @ `f282016` | **3042** | **3042 passed, 0 failed, 0 skipped, 2 warnings, 206.47 s** |

  The 2 warnings are the pre-existing `websockets` deprecations (**#25**) —
  i.e. **#321's acceptance signal still holds**: no third warning, and
  `pyproject`'s `error::PytestUnraisableExceptionWarning` would have made one
  a failure.
- **CI, both legs, after `74b8573`: `3041 passed, 1 skipped, 2 warnings`** —
  matching risk 29's pre-existing Linux skip exactly, with no third warning on
  either. Run `33603599768`.
- **The first push was green here and RED on both CI legs**, and the defect was
  real. The end-to-end pin passed `db_session_lock.info.dsn` to the harness
  subprocess. `ConnectionInfo.dsn` is libpq's *report* of a connection, not a
  round-trippable connection string: **it omits the password.** This Mac's
  `pg_hba.conf` does not demand one on that path, so the subprocess connected
  and the test passed; CI's does, so the harness died with
  `fe_sendauth: no password supplied` — a traceback, not a refusal, which is a
  *different* outcome that happens to also be non-zero. The DSN comes from
  `db_dsn` now. The pre-existing `"Traceback" not in stderr` assertion is what
  made the failure legible rather than a silent pass, and is documented as
  load-bearing.
- **Mutation battery, 10 mutations** (restored from a scratchpad copy every
  time, never `git checkout` — risk 13):

  | mutation | caught by |
  |---|---|
  | a harness stops taking the lock | 1 test |
  | the lock is never released | 2 tests |
  | `_is_lock_call` always True | 2 tests *(0 before the fixtures were added)* |
  | Name branch ignores the helper | 1 test *(0 before)* |
  | Attribute branch ignores the helper | 1 test |
  | the glob widens to every module | 3 tests |
  | a source with no `main` passes silently | 1 test |
  | `apply_migrations` dropped from `DB_ENTRY_CALLS` | 3 tests |
  | the error text drops the call list | 3 tests |
  | `run_browse_explain` stops taking the lock | the end-to-end pin |

  One mutation (the lock-`with`'s own context expression counted as covered)
  is **semantically null** — the helper's argument is never a DB call — and is
  recorded as such rather than as a surviving defect.
- **The end-to-end pin was run ALONE** (last session's lesson): 1 passed.
- **Both harnesses smoke-run for real after the re-indent** — nothing else
  proved they still *work*, the AST rule only proves the lock is taken.
  `run_chunk_insert_bench.py --messages 12` and
  `run_browse_explain.py --total-rows 400 --accounts 2` both exit 0.
- `mypy src/localmail` → Success, **152** files. `mypy tests/acceptance/_harness_lock.py`
  → Success.
- `ruff check src/localmail/` → **10**, the unchanged #285 baseline (no `src/`
  file was touched). One F841 in `run_recall_eval.py` is **pre-existing** —
  confirmed against a pre-fix copy — and left alone as #285 territory.
- GUI untouched.
- **README and CLAUDE.md both updated.** There is still **no ROADMAP.md** —
  that `/nextsession` step remains a no-op (risk 29, re-confirmed a fourth
  time).

## What's next

### 0. **Merge the PR, then check the issue actually closed**
   **You merge** (project convention). The body uses `Closes #337`. Risk 2 says
   verify with `gh issue list` afterwards rather than assume — it has worked
   two rounds running, which is not the same as it always working.
   - **The Mac wants a `uv sync --all-extras` after merging**, unlike last
     session: this diff changes `uv.lock`. The daemon runs an editable install
     (risk 18) and the tree is on the branch, so `git checkout main` too.
   - **The DGX wants a pull and a sync as well** — same reason, and it is the
     host where an unpatched `pypdf` matters most (it does the bulk of the
     extraction). Use `~/.local/bin/uv sync --extra mcp --extra extraction`.

### 1. **Decide `transformers` (Dependabot #70)** *(new — a decision, not a task)*
   It arrives via **docling** under the `[extraction]` extra, `grep -rn
   transformers src/` is **empty**, and the advisory is a path traversal in
   `save_pretrained` — a *write* path localmail never calls. Measured:
   `uv lock --upgrade-package transformers` moves it **5.8.1 → 5.15.1** for the
   cost of one `safetensors` bump (0.7.0 → 0.8.0), and nothing else moves.
   **Acceptance if you take it:** the extraction tests stay green and a real
   scanned PDF still OCRs — it is a seven-minor jump in the package docling
   runs its layout models through, which is the only reason it was not done
   here. **Until then Dependabot reads 1, not 0** — do not treat that as an
   unnoticed alert.

### 2. **#299 — split it, or close half of it** *(carried; last measured session 36)*
   Session 36 could not reproduce either half (0/20 targeted, 3/3 full runs).
   Its diagnosis stands: `test_second_lifecycle_op_while_busy_is_409` is a
   wall-clock window (a test-design issue, not a product race), while
   `test_route_driven_login_failures_persist_audit_rows` has no concurrency at
   all and is **textbook #335**, so #336 may already have fixed it.
   **Acceptance for the first:** the test no longer depends on a round trip
   beating a 3 s timer. **For the second:** either a reproduction on current
   `main`, or close it citing #336.

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
   (`SortOrder`/`SortMode` restated in three wire layers) · **#331**
   (`SortOrderNotApplicable`'s stated audience is wrong).

### 6. **#305 — `--version` dies on a missing third-party dependency** *(carried)*
   `cli.py` imports the daemon at module scope. **Acceptance:** blocking
   `sqlparse` on `sys.meta_path` leaves `localmail --version` printing its line
   and exiting 0. **Do it with the `cli.py` refactor, not before** — `cli.py` is
   **2177 lines**.

### 7. **#285 — ruff, repo-wide** *(carried)*
   **10** pre-existing errors in `src/` (two are the `math` import/redefinition
   pair in `searcher.py`), plus **1 F841 in `tests/acceptance/run_recall_eval.py`**
   that this session confirmed pre-existing. 9 dead `# noqa: S608`, no
   `[tool.ruff]`, no CI step. **Decide the config and the CI step together** —
   that decision is the operator's.

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
   - **165 docling failures on the Mac**, of 182 `blobs_gave_up`. Worth a fresh
     look **after** the pypdf bump lands on the host — three of the four
     advisories fixed are unbounded-runtime bugs on the extraction path, so a
     few of those give-ups may simply stop happening. **Re-measure before
     claiming it; that is a hypothesis, not a finding.**
   - **Residual implausible language labels dominated by `ja`** (~0.24%); the
     confidence-floor lever was measured useless. **Sample `ja` first.**
   - **The DGX drops remain uninvestigated** (risk 24).
   - **A session-22 stash is still on the stack** (`stash@{0}: On
     docs/session-22-handoff: review-fixes`); its content is on `main`, so
     `git stash drop` it if you want the tree tidy.
   - **A stray SDD workspace** at `.superpowers/sdd/`. Git-ignored scratch.

## Open decisions & risks

1. **One PR is open and yours to merge.** `fix/dep-security-and-337-harness-lock`,
   based on `main` (`ad69279`), closing **#337**. **22 open issues**, dropping
   to **21**. **Dependabot 5 → 1**, and the remaining one (`transformers`,
   #70) is item 1 above — **a deliberate deferral, not a miss.**
2. **A merge does NOT close issues its subject merely names** *(carried)*. Use
   `Closes #N` in the **PR body** and **check `gh issue list` after the merge**.
   Two rounds running now; that is two data points, not a guarantee.
3. **THIS FILE IS NOT THE AUTHORITY — `git` and `gh` are** *(carried,
   six-times earned, and it paid out again this session)*. Opening with `gh api
   …/dependabot/alerts` is the only reason the pypdf/icalendar work happened at
   all: the previous handoff said "expect 0" and the truth was 5. Open every
   session with `git fetch --prune && git log --oneline -1 origin/main`,
   `gh pr list`, `gh issue list`, the Dependabot query, and reconcile
   **before acting**.
4. **Base every branch on `main`; put a session's code and its handoff in ONE
   PR** *(carried)*. Session 25's stacked handoff PR merged into an
   already-merged branch 13 seconds after it landed and was lost silently.
   - The stranded-branch check is **NOISY**: `git log --oneline main..origin/<b>`
     is non-empty for every squash-merged branch. Signal = non-empty on a branch
     whose PR merged *recently*, confirmed by a **content** diff
     (`git diff --stat main origin/<branch>` empty ⇒ it landed).
5. **A dependency floor that a vulnerable version satisfies is not a floor**
   *(new)*. `icalendar>=6.0` looked unaffected by an advisory whose range is
   `>= 7.1.0, < 7.1.3` — the window sits **above** the floor, and a resolver
   taking the newest compatible release lands inside it, which is where the
   lock was. **Read `vulnerable_version_range` against `uv.lock`, never against
   the declared floor.** Corollary: this repo's floors carry their *reason* in
   a comment (sqlparse, cryptography, click, and now these two) — keep that up,
   because "why is this floor here" is otherwise unrecoverable.
6. **NEVER run two pytest sessions against one test database** *(carried —
   enforced since #336, and since #337 the acceptance harnesses are inside the
   same guard)*. The second **waits**. To run both, give one its own
   `LOCALMAIL_TEST_DSN`. A harness refused by the lock exits **3**, distinct
   from the **1** an eval returns when it fails its own gates.
7. **A test module must import `localmail.serve.app` at MODULE scope**
   *(carried — #321)*. The autouse pool-closing fixture reads `sys.modules` at
   test-setup time, so a function-local import arrives too late and that file's
   pools leak silently. An AST scan plus a teardown re-check enforce it. If it
   fires, hoist the import — do not relax the rule.
8. **When a guard is an AST rule, mutate BOTH branches of every predicate**
   *(new, and it cost two rounds here)*. `_is_lock_call` has an `ast.Name` arm
   and an `ast.Attribute` arm; each survived its mutation until a fixture
   existed for its shape, and the fixtures look almost identical
   (`ExitStack()` vs `psycopg.connect(...)`). A guard with an untested arm is a
   guard that is half off. Related: **never assert a substring that the
   message's own remedy text contains** — anchor on the part that varies.
9. **Verify host revisions; do not infer them** *(carried, earned twice)*.
   `ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'` plus
   `/v1/version`'s `build_hash` settles it. **Both hosts were left untouched
   this session, and BOTH need a sync after this merge** — unlike last session,
   the lock changed.
10. **Test-count baselines: measure both refs IN THE SAME SESSION** *(carried)*.
    `main` **3025**, branch **3042**. A number quoted from a previous handoff is
    not a baseline.
11. **A same-process assertion cannot pin a cross-process property** *(carried)*.
12. **A keyset predicate must be a row comparison, in BOTH directions**
    *(carried — #75, #322, #323)*. The OR-form is semantically identical, keeps
    the index scan, adds no Sort, and plans as a per-tuple `Filter` that rescans
    from the index head on every page. Written by mistake **twice**.
    `tests/test_searcher_sort_order_plan.py` keeps three negative controls
    (`NULLS LAST`, the ascending OR-form, the descending OR-form) — **do not
    "tidy" them away**.
13. **The undated block is where the two directions genuinely differ**
    *(carried)*. Descending must *admit* it (hence the top-up query); ascending
    meets it at the head of its own walk. **Do not "restore symmetry"**, and
    **do not "fix" a short page by restoring `OR expr IS NULL`**.
14. **A cursor identifies a position, not a query — with exactly one enforced
    exception** *(carried — #326)*. Dropping the query a **text** cursor was
    minted from is a 400; archive-walk cursors page with or without one.
15. **Never state a `sort` on a request that carries a cursor** *(carried —
    #308, #311)*. A paging client must treat **409 as recoverable** and **400
    as permanent for that cursor**.
16. **When reverting a mutation, restore from a file copy — never `git
    checkout`** *(carried, sessions 23–37)*. Used ~12 times this session. One
    restore had to be done by targeted edit instead, because the scratchpad copy
    predated the fix — **snapshot again after each GREEN**, not once at the
    start. Treat **empty** pytest output as a failed mutation, not a pass.
17. **The stale NOTIFY queue recurs, and gate 1 lies** *(carried)*. Exactly
    three LISTEN/NOTIFY failures usually means the stale queue — but session 35
    had that exact failure set with **both gates clean** (it was contention).
    **Check both gates before reaching for the runbook.**
18. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `uv sync --all-extras` (Mac) / `--extra mcp --extra extraction` (DGX).
    **`uv` is not on the DGX's non-interactive PATH** — use `~/.local/bin/uv`.
    Note also that **a branch checkout re-resolves the venv**: switching to
    `main` and back silently downgraded `pypdf`/`icalendar` mid-session.
    Re-sync after any checkout you intend to test against.
19. **The Mac's launchd daemon runs an EDITABLE install** *(carried)*. It
    executes whatever the tree is checked out to. **Currently on
    `fix/dep-security-and-337-harness-lock`** — check out `main` after merging.
    This diff touches `uv.lock`, so unlike last session the daemon's own
    dependencies change: sync deliberately, not incidentally.
20. **`search-status` is sub-second** *(carried)*. If it runs long that is a
    **regression of #280** — check `EXPLAIN (FORMAT JSON)` for a `Seq Scan on
    messages` under a `SubPlan` first.
21. **`blobs_claimable 0` alongside `blobs_pending 0` is the SETTLED shape**
    *(carried)*. Mac `9543 = 9255 + 106 + 182 + 0`; DGX `4405 = 4187 + 91 + 127
    + 0`. A steady non-zero `blobs_no_text` is **normal** (#277);
    **`blobs_gave_up` is the one to act on**. *(Not re-measured this session —
    no archive work was done.)*
22. **`uv run pytest -q` with NO arguments is the right command** *(carried)*.
    **Do not run the suite while a backfill is draining.**
23. **An empty `daemon_heartbeats` right after a restart is normal for minutes**
    *(carried — #269/#271)*: the startup blob-temp sweep. Grep for
    **`blob-temp sweep done: walked=`**.
24. **`--version`'s contract is six things, all pinned** *(carried)*. **Never
    reintroduce `@click.version_option` in any spelling** — an AST pin forbids
    it, covering `daemon_cli.py` too. stderr non-empty ⟺ unresolvable.
25. **The DGX drops are STILL UNEXPLAINED — five theories refuted** *(carried)*.
    **Do not propose a sixth without a captured outage in which the host was
    demonstrably up throughout.** Triage with `journalctl --list-boots` first.
    Power is not a candidate (~5-day UPS). **Do not edit
    `/etc/wireguard/wg0.conf`.**
26. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`.
    **Run vitest from `gui/`, not the repo root.** `cargo clippy --all-targets`
    is clean but **ungated** — CI runs clippy without it.
27. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    **#266's whitespace-heal is a one-way door too.** **`--relabel` is the only
    destructive verb in the lang path**; reach for `--retry-declined` first.
28. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    kept though #245 is fixed; #246 warns on group-write; `InsecureSecretsFile`
    refuses; #239's manual tombstone retention deliberate; admin bearer tokens
    have no per-token scope (#204).
29. **CI reports `1 skipped` on Linux; macOS reports 0** *(carried)*. It is
    pre-existing and still unidentified. **Do not read it as a missing uv
    extra** without checking a `main` run first. Expect CI here to read
    **3041 passed, 1 skipped**.
30. **No ROADMAP.md** *(carried, re-confirmed a fourth time)* — that
    `/nextsession` step is a no-op. **README and CLAUDE.md were both updated**
    this session.
31. **A green local run is still not evidence — and never rebuild a DSN from a
    live connection** *(new; last session's risk 5, one layer over)*. The
    end-to-end harness pin passed `conn.info.dsn` to a subprocess.
    `ConnectionInfo.dsn` is libpq's **report** of a connection, not a
    round-trippable connection string: it keeps host, port, user and dbname and
    **drops the password**. This Mac's `pg_hba.conf` does not demand one on
    that path, so it passed here and failed on both CI legs. Use the DSN the
    fixture was given (`db_dsn`), never one reconstructed from a connection.
    - The generalisation is the part to keep: **any test whose subject is
      "this process refuses / exits non-zero" must assert *why*.** A harness
      that cannot connect also exits non-zero, so the assertion that saved this
      was `"Traceback" not in stderr`, not the exit code. When a test asserts a
      failure, pin the failure's *shape*, or a different failure passes it.
    - Corollary for this repo specifically: **the local Postgres is more
      permissive than CI's.** A test that authenticates will pass here whether
      or not it supplies credentials. Push and let CI decide.

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

# RISK 3 — THIS IS THE ONE THAT PAID OUT THIS SESSION. Do not skip it.
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '[.[] | select(.state=="open")] | length'      # expect 1 (transformers, #70)
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | "\(.security_advisory.severity)\t\(.dependency.package.name)\tfixed in \(.security_vulnerability.first_patched_version.identifier)"'
# For each alert, check the range against THE LOCK, not the declared floor (risk 5):
grep -A1 '^name = "<pkg>"' uv.lock

# RISK 4 — stranded-branch shortlist (noisy: every squash-merged branch shows).
for b in $(git branch -r --format='%(refname:short)' | grep -v 'origin/main\|HEAD'); do
  n=$(git log --oneline main.."$b" | wc -l); [ "$n" -gt 0 ] && echo "$b: $n commits not on main"
done
git diff --stat main origin/<branch>     # EMPTY = landed, not stranded

# AFTER MERGING — BOTH hosts need a sync this time; uv.lock changed (risk 18/19).
#   Mac:  git checkout main && git pull && unset VIRTUAL_ENV && uv sync --all-extras
#   DGX:  ssh 10.0.0.3 'cd ~/src/localmail && git pull && \
#           ~/.local/bin/uv sync --extra mcp --extra extraction' \
#         && ssh 10.0.0.3 'systemctl --user restart localmail-daemon localmail-serve'

# Python suite. NEVER a bare `uv sync` (risk 18).
unset VIRTUAL_ENV && uv sync --all-extras
unset VIRTUAL_ENV && uv run pytest -q
#   macOS: expect 3042 passed, 0 failed, 0 skipped, and **2 warnings**.
#   THOSE 2 ARE #321's ACCEPTANCE SIGNAL: both are the pre-existing websockets
#   DeprecationWarnings (#25). A leaked pool is now a FAILING test whose
#   message reads "cannot join current thread" (pyproject escalates
#   PytestUnraisableExceptionWarning to an error). The test it names is
#   arbitrary — the GC picks it; the message is the diagnosis. Check for an
#   import of localmail.serve.app that is not at module scope.
#   LINUX/CI: expect 3041 passed, 1 SKIPPED; pre-existing (risk 29).
#   MEASURE BOTH REFS IN THIS SESSION (risk 10) — no DB needed:
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2   # branch: 3042
unset VIRTUAL_ENV && uv run mypy src/localmail    # expect Success, 152 files
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -2   # expect 10 (#285)

# #337, verified directly:
unset VIRTUAL_ENV && uv run pytest -q tests/test_acceptance_harness_lock.py   # expect 16
# Run the end-to-end pin ALONE — intra-file ordering hid a red one in #321:
unset VIRTUAL_ENV && uv run pytest -q \
  tests/test_acceptance_harness_lock.py::test_a_harness_started_beside_this_suite_refuses_the_database
# And prove the harnesses still RUN, which the AST rule does not (both exit 0):
unset VIRTUAL_ENV && PYTHONPATH=src:. uv run python \
  tests/acceptance/run_chunk_insert_bench.py --messages 12 --body-words 40
unset VIRTUAL_ENV && PYTHONPATH=src:. uv run python \
  tests/acceptance/run_browse_explain.py --total-rows 400 --accounts 2 \
  --dsn 'postgresql://localmail:local%40%40mail@localhost:5532/localmail_test'

# THE POOL-LEAK PROBE (risk 7) — reusable; this is what found #321's second seam.
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

# RISK 6 — the test-database lock now covers harnesses too (#336 + #337).
# If a run seems to hang at startup, look for this line; it is not a fault:
#   "waiting for another test run to release the test database ..."
# To run two at once, give one its own database:
#   LOCALMAIL_TEST_DSN=postgresql://localmail:local%40%40mail@localhost:5532/localmail_test2 uv run pytest -q

# #1 NEXT — the transformers decision. Measure the blast radius first:
unset VIRTUAL_ENV && uv lock --upgrade-package transformers --dry-run 2>&1 | tail -5
#   (measured this session: transformers 5.8.1 -> 5.15.1, safetensors 0.7.0 -> 0.8.0, nothing else)
#   NOTE: `uv sync --dry-run` is NOT read-only (see CLAUDE.md's uv footguns);
#   `uv lock --dry-run` is a different command and is safe.

# #2 NEXT — #299 did NOT reproduce in session 36 (0/20 targeted, 3/3 full runs):
unset VIRTUAL_ENV && for i in $(seq 1 20); do \
  uv run pytest -q tests/test_serve_daemon_routes.py tests/test_serve_auth_routes.py 2>&1 | tail -1; \
done | sort | uniq -c

# If EXACTLY the three LISTEN/NOTIFY tests fail, CHECK BOTH GATES (risk 17):
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'
# Only if a gate errors, use runbook Option A (bootout, verify while down, bootstrap).

# Host health (Mac) — NOT re-measured this session; no archive work was done:
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 23)

unset VIRTUAL_ENV && uv run localmail search-status    # UNDER A SECOND (risk 20)
#   Check the SHAPE, not the literals: the four buckets must sum to
#   blobs_eligible, and claimable must equal pending.
#   AFTER the pypdf bump reaches the host, blobs_gave_up is worth a second look
#   (item 10) — but RE-MEASURE, do not assume the bump fixed anything.

# The DGX (risk 9 — verify, never infer):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'   # binds 10.0.0.3, NOT localhost
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — REQUIRED only if you touch gui/ (MUST run from gui/ — risk 26):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
#   expect: 0 svelte-check errors, 408 vitest, 104 cargo
```

`main` tip at session start was **`ad69279`**. This session left **one PR** open
on `fix/dep-security-and-337-harness-lock` — `afc8c5f` (the security floors),
`f282016` (#337), `c435fff` (this handoff) and `74b8573` (the CI fix) —
closing **#337**. **CI green on both legs**: `3041 passed, 1 skipped,
2 warnings`. Latest migration
**`0036_api_keys.sql`**; next free slot `0037_*.sql` (this session adds none).
**Open issues: 22**, dropping to **21** on merge. **Dependabot: 5 → 1**, the
remainder deliberate.
