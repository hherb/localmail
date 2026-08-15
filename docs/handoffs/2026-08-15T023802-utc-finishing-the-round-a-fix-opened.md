# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-15 (session 29).** `main` is **`fb48f23`** — session
> 28's #308 fix, merged as PR #309.
>
> **Session 28 shipped code but wrote no handoff.** This file was one session
> stale when session 29 opened it: it described PR #306 as unmerged and `main`
> as `5a8826a`, while `gh` showed #306 and #309 both merged and three new issues
> filed. **Believe `git` and `gh` over this file's header** — see risk 3, which
> is now twice-earned.
>
> **This session closed the whole review round #309 generated** — #311, #312 and
> #307, all filed against the ground #308 moved. **PR #313 is open, base
> `main`, unmerged.**
>
> **The one that mattered operationally is #311.** #308 made the server answer a
> contradictory paging request with a 400 instead of silently restarting at page
> 1 — but the GUI recovered from 409 only, and its `IntersectionObserver`
> re-fires `loadMore` on every scroll event while `hasMore` is true. So the fix
> converted a silent restart into a **request loop behind an error banner**. The
> client half is what closes it.
>
> **The DGX deployed itself since the last handoff** — it now runs `fb48f23`,
> not the `29f5fae` this file used to claim. Verified over SSH (risk 4).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The database is canonical for accounts.
Daemon: hot-reload account set, heartbeats, DB command queue, two-plane
supervision. Web admin UI (HTMX): account CRUD, user management, archive
imports, daemon control. Hybrid search (Phases 1+2) + an HTTPS GUI server + a
remote MCP server (optionally a full OAuth 2.1 authorization server) + the
opt-in `--smart` LLM query rewriter are all shipped. A Tauri 2 + Svelte 5 GUI
lives under `gui/` — read-only viewer plus an admin mode (Accounts + Daemon
panels shipped; Users + Imports still placeholders). Version **0.3.0**.
Licensed AGPL-3.0-or-later (per-file SPDX headers in `src/localmail/`; **not**
in `gui/`). See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we shipped this session

Branch `fix/309-review-round`, commit **`0940f98`** (code + docs) plus the
handoff commit. **Open as PR #313, base `main`, unmerged.** No migration, no
new dependency, no config change, **no wire change**. Closes **#307, #311,
#312** — the complete #309 review round.

### 1. #311 — the client half of #308 (the one with user-visible teeth)

`POST /v1/search` can now answer a paging request with **400** — a stated
`sort` the cursor cannot serve, or a keyset cursor whose query no longer feeds
its walk. `gui/src/lib/stores/search.svelte.ts` handled **409 only**; a 400 fell
to the generic error path, which leaves `cursor`/`hasMore` untouched. With
`hasMore` still true, `MessageList`'s `IntersectionObserver` re-fires
`loadMore()` on every scroll event — a permanently-failing request, forever.

The two rules are the pure
[gui/src/lib/search_paging.ts](gui/src/lib/search_paging.ts):

- **`statedSort` — a request carrying a cursor states NO sort.** Exactly what
  `docs/mcp-usage.md` already tells every other client. This is the
  **load-bearing half**: it makes the contradicting-sort 400 *unreachable* from
  the GUI rather than merely recoverable, because the store's `sort` is
  user-mutable while a cursor is live. **The 409 recovery re-runs with no
  cursor, so it must keep stating the sort** — mutation-pinned, since omitting
  it there silently flips a `sort=date` search back to rank.
- **`isCursorRejected` — any 400 from a paging request retires the cursor.**
  Keyed on the status alone: every 400 reachable there says the same
  operational thing, and no re-issue of the identical pair can succeed. The
  rows already fetched still answer the query and **stay on screen**; the
  message is the server's own problem+json `title: detail`, which `formatError`
  already renders — the wording stays server-side.

`loadMore` now also guards on **`loading`**, not just `loadingMore`. That is a
wrong-results fix, not just a wasted request: a fresh search in flight has
already bumped `#submitSeq`, so neither response discards the other and the
page's rows are appended to a **different query's** results.

### 2. #312 — `sort=None` reached the pool as itself

`Searcher.search` fell through its `== "date"` test into the hybrid branch —
the right *ordering* by accident, the wrong *record*. The raw argument is what
the pool is cached with, and since #309 `_check_pool_sort` reads that field back
to decide a 400. So a pool built by a `sort=None` caller **reported its sort as
`None`**, and the next paging request stating the sort it would actually be
served was told the cursor continues a `None`-sorted search and rejected. The
issue called this a library-caller footgun; the pool metadata is where it bites.

- Resolved **once**, at the top of `search()`, into `effective_sort`; every read
  inside the function goes through it.
- **`DEFAULT_SORT` moved to `search/searcher.py`**, beside the `SortMode` it
  ranges over. `api/search.py` imports it from there and `api/search_cursor.py`
  no longer defines it — two layers resolving "unstated" from two literals *is*
  the drift. The api layer still resolves explicitly at its own boundary, which
  is pinned by a test that mocks the Searcher and so cannot see its resolution.
- **Signature is `SortMode | None = None`, not a removed default.**
  `allowed_account_ids` has no default because no safe value exists (#234); a
  sort has one, so the fix is to make "unstated" mean it explicitly.

### 3. #307 — the version diagnostic on a help screen *(your call, option 2)*

Since #304 the group callback reports for the 36 commands that do not report
for themselves — and click resolves the subcommand *before* applying its
`--help`, so the ERROR landed ahead of the text the operator had just asked to
read. You chose **option 2: quieten it**, rather than make the other three
shapes loud. `--version` still reports, being the command whose job that is.

- The rule is the pure
  [src/localmail/cli_help_request.py](src/localmail/cli_help_request.py)`::is_help_request`
  — reads `ctx.help_option_names` rather than spelling `--help` (click lets a
  project add `-h`) and stops at a bare `--`. **Known imprecision, documented**:
  a help token consumed as an option *value* reads as a help request, because
  judging otherwise means knowing every option's arity — a second parser to keep
  in step with the first, for one suppressed diagnostic on a pathological call.
- **The callback cannot answer this itself**, hence `_HelpAwareGroup`: by the
  time click runs the group callback, the resolved subcommand's args are off the
  context (`ctx.args` is empty whether or not `--help` was typed). They are in
  place one frame out, in `Group.invoke`, so the question is asked there and the
  verdict passed via `ctx.meta` — the **report stays in the callback**, beside
  the `SELF_REPORTING_COMMANDS` skip it shares a decision with.
- **All four shapes are pinned now**, including the three that were already
  quiet: their silence is a side effect of click's parse order, so an
  `invoke_without_command=True` would flip them loud with nothing failing. A
  **positive control** (`localmail sync` still reports) guards the other
  direction — a rule matching too broadly reopens #304 for the 36 commands it
  was filed about, and every quiet-shape assertion would still pass.

### 4. Verification (this Mac, all extras)

- `uv run pytest -q` → **2535 passed, 0 failed, 0 skipped** (136 s). Run twice.
- **Counts measured on BOTH refs in this session, not subtracted** (risk 5):
  `main` **2518**, this branch **2535** — exactly the 17 added. The `main`
  worktree needed `uv sync --all-extras` first (risk 13).
- **vitest measured both ways too**: **388 / 46 files** with main's test files,
  **399 / 47** with this branch's — the 11 added. That run also proves main's
  existing store tests pass **unchanged** against the new store.
- `uv run mypy src/localmail` → **Success, 142 source files** (141 + the new
  pure module).
- `ruff check` clean on every new/changed file. Repo-wide `src/localmail/`
  **10** errors on this branch and **10** on `main` — same two in
  `searcher.py` are pre-existing (#285). ruff on PATH is **0.11.0**.
- `npm run check` → **0 errors, 323 files**; `npm run build` ok.
- **Warnings: 3** on the final run (the two #25 websockets deprecations + the
  psycopg `ConnectionPool.__del__` race). One earlier run showed 4 — GC timing
  noise, as session 27 also observed.
- **Mutation-proven, every new rule**: 3 for `is_help_request` (over-match →
  **43 failures** incl. the #304 reach pins; dropped `--` stop; hardcoded
  `--help`), 1 for the 409-recovery sort pin. #311's other three rules and
  #312's were proven by the RED phase.

### 5. Host health confirmed, not assumed

**Mac** — launchd `running`, daemon pid **53418**, 7 heartbeats, oldest 24 s.
`search-status` **0.98 s** (no #280 regression), partition holds:

```
messages_total 127855
blobs_eligible 9506 = extracted 9218 + no_text 106 + gave_up 182 + pending 0
blobs_claimable 0        body_lang_pending 0, declined 12190
```

**DGX** — all three units `active`, and `git log --oneline -1` → **`fb48f23`**,
i.e. **level with `main`** (it was three behind at the last handoff; someone
deployed it in between). Verified over SSH.

**Dependabot: 0 open alerts.** **18 open issues**, dropping to **15** on merge.

### 6. Docs

- **CLAUDE.md** — #311's client-side rules under *Browse & search pagination*
  (beside #308's server-side ones), #312's resolution + the `DEFAULT_SORT` move,
  #307 replacing the "**#307 (open)** decides which way" placeholder, the layout
  line for `cli_help_request.py`, and `search_paging.ts` in the GUI's
  pure-modules list.
- **README** — the paging section gains the 409-vs-400 distinction and what a
  client must do with each; the version-diagnostic section gains the help-screen
  exception.
- **No ROADMAP.md** (confirmed absent again; that `/nextsession` step stays a
  no-op).

## What's next

### 0. **Merge PR #313** — the only open PR
   **The operator merges** (project convention). Closes #307, #311, #312.
   - **Acceptance:** on `main` afterwards, `uv run pytest --collect-only -q`
     reports **2535** (2518 today), and `localmail sync --help` prints its help
     with no `error:` line on a corrupt-metadata tree.
   - **Then deploy:** the DGX is already level with `main`, so after merging it
     needs the usual pull. The Mac needs `git checkout main && git pull && uv
     sync --all-extras` — **checking the tree back out to `main` matters**, its
     launchd daemon runs an editable install (risk 12).

### 1. **#305 — `--version` still dies on a missing dependency** *(carried)*
   `cli.py` imports the daemon (and so `sqlparse`, `psycopg`, `keyring`) at
   module scope, so a partial `uv sync` kills the one command an operator is
   told to run to verify an install, before click parses the flag.
   **Acceptance:** blocking `sqlparse` on `sys.meta_path` leaves `localmail
   --version` printing its line and exiting 0. **Do it with the `cli.py`
   refactor, not before** — moving imports into 38 command bodies piecemeal
   trades a startup failure for a first-use one.

### 2. **#300 — an unresolvable version has no machine-readable channel** *(carried)*
   The diagnosis is legible to a **human** on every entry point (#304, and #307
   now carves out help) and to a **machine** on none: `--version` exits 0 on the
   unknown path (deliberate and pinned — risk 6) and `/v1/version` ships the
   sentinel unflagged. **Read #295's precedent first:** its wire question was
   decided *against* adding a field, citing #278 as the cautionary case. Needs a
   decision, not an implementation. `__version_source__` is retained for it.

### 3. **#299 — two pre-existing flaky tests** *(carried)*
   Daemon lifecycle busy-guard and login audit rows; confirmed flaky on `main`.
   Neither appeared in this session's runs, which is consistent with flakiness
   and is **not** evidence they are fixed.

### 4. **#278 — the version surface's other half** *(carried; needs YOUR decision)*
   The GUI About tab renders a `build_hash` that `/v1/version` has **never**
   emitted, so "Server build" always shows `?`, while five test files mock the
   field and make it look covered. **Two options, product call before any code:**
   emit a real build hash (from git, at build time — but the version is stamped
   at *install* time, so this needs a story for `uv tool install` from a
   tarball), or delete the field end-to-end including the five mocks.

### 5. **#285 — ruff, repo-wide** *(carried)*
   **10** pre-existing `ruff check src/localmail/` errors (two of them the
   `math` import/redefinition pair in `searcher.py`), 9 dead `# noqa: S608`
   directives across 5 files, no `[tool.ruff]` config and no CI step.
   `version_report.py`'s broad catch still carries no `BLE001` suppression, and
   the comment there records that 14 of 79 sibling catches do — i.e. no
   convention in either direction. **Decide both together.**

### 6. **Admin GUI phase 5 — Users & ACL panel** *(carried, still the design's next slice)*
   `/v1/admin/users` is already `require_admin()` (bearer-capable) — **no backend
   work needed.** Service layer:
   [src/localmail/api/admin/users.py](src/localmail/api/admin/users.py).
   **Acceptance:** a `UsersPanel.svelte` replacing the placeholder tab: list,
   create, delete, per-account ACL grant/revoke (a checklist over every account),
   `is_admin` toggle, password reset, enable/disable. Surface the **two lock-out
   guards as 409s** — the count-based last-admin rule (`LastAdminError`) and the
   identity-based self-action rule. Mirror
   [serve/admin/users_panel_router.py](src/localmail/serve/admin/users_panel_router.py).
   Follow the Daemon-panel shape, and **stub the new API module in both
   `AdminView.test.ts` and `MainView.test.ts`** (see risk 11).

### 7. **Remaining robustness backlog** *(carried)*
   **#218** (GUI download commands buffer the full body before enforcing the size
   ceiling) · **#226** (self-signed cert misses the reachable IP when `--bind
   0.0.0.0`) · **#225 / #227** (`/v1/changes` subscription lifecycle gaps) ·
   **#200 / #211 / #208** (admin panels silently swallow 4xx) · **#206** (GUI
   AccountForm: folder filters not editable) · **#204** (admin bearer-token
   scope) · **#25** (websockets DeprecationWarning — two of the three warnings
   every full run prints).

### 8. **Smaller, deliberately not done** *(carried)*
   - **`cli.py` is now 2058 lines**, `daemon.py` 580, `version_report.py` 593.
     The `cli.py` refactor session 21 deferred is still owed in full, and
     **#305 depends on it**. `_HelpAwareGroup` (this session, ~15 lines) went in
     there rather than into a new module because it is click plumbing bound to
     `main`; the *rule* it consults is the separate pure module.
   - `version_report.py` is over the 500-line guideline but is **152 lines of
     code and 441 of rationale**. Splitting was considered and rejected: the
     reporting half cannot leave, because `_SEVERITY_PREFIX` derives from
     `_REPORT_LEVEL` and separating them breaks #302's one-authority property.
   - **165 docling failures on the Mac** (31 `File format not allowed`, 134
     `Input document … is not valid`), of 182 `blobs_gave_up`.
   - **Residual implausible language labels are dominated by `ja`.** ~0.24% of
     labels; the confidence-floor lever was measured useless. **Sample the `ja`
     rows first** if ever chased.
   - **The DGX drops remain uninvestigated and unexplained** (risk 7).
   - **#269's suggestion 2** (blob-temp sweep off the critical path) — deferred
     in session 19; reopen only if cold-cache startups grow past tolerable.
   - **`git stash drop` the session-22 leftover** if you want it tidy: session 26
     verified its content is already on `main` (`bc5b556`). Left in place because
     dropping is destructive and is your call.

## Open decisions & risks

1. **One PR is open and yours to merge.** `main` is `fb48f23`; **#313**
   (`fix/309-review-round`) closes #307/#311/#312. **18 open issues**, dropping
   to **15** on merge. **Dependabot: 0 open alerts.**
2. **Base every branch on `main`; put a session's code and its handoff in ONE
   PR** *(carried — session 26's whole reason for existing)*. Session 25 based
   its handoff PR on a fix branch; the fix PR merged, the handoff PR merged into
   the now-dead fix branch **13 seconds later**, and everything in it was lost
   with **no failing check, no open PR, nothing to notice**. This session put
   code and handoff on one branch, one PR, based on `main`.
   - **The documented stranded-branch check is NOISY** *(carried)*. `git log
     --oneline main..origin/<branch>` is non-empty for **every** squash-merged
     branch, because the branch's own commits never appear on `main`. The signal
     is not "non-empty" — it is **non-empty on a branch whose PR merged
     recently**, cross-checked with `git diff main origin/<branch>`. Treat the
     loop in the resume block as a *shortlist generator*, not a verdict. (Used
     that way this session: `origin/fix/search-cursor-mode-vs-default-sort`
     showed 2 commits not on `main` and a **content diff of nothing** — landed,
     not stranded.)
3. **`gh` and `git` are the authority on state, NOT this file** *(carried, and
   now twice-earned)*. Session 27's handoff described PR #306 as unmerged and
   `main` as `5a8826a`; by the time session 29 read it, #306 **and** #309 had
   merged, three issues had been filed, and **session 28 had left no handoff at
   all**. Always open with `git fetch --prune && git log --oneline -1 origin/main`
   and `gh pr list` / `gh issue list` before believing the header.
4. **Verify host revisions; do not infer them** *(carried)*. The DGX is at
   **`fb48f23`** — checked over SSH this session, and it had moved since the
   last handoff without a session doing it. One
   `ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'` settles it.
5. **Test-count baselines: measure both refs IN THE SAME SESSION** *(carried)*.
   `main` → **2518**, this branch → **2535**. Session 27 measured `main` at 2443;
   it collects 2518 today. **A number quoted from a previous handoff is not a
   baseline.** A fresh worktree gets a bare `uv sync` (risk 13), which
   under-collects by ~500 — sync it `--all-extras`. The same applies to vitest:
   measured 388 → 399 here by running the suite with `main`'s test files.
6. **`--version`'s contract is six things, all pinned** *(carried)*. It must
   (a) read no config and touch no database; (b) keep **stdout** to the single
   machine-readable line; (c) put any diagnostic on **stderr**; (d) **exit 0**
   even when the version is unknown; (e) still work on a tree whose METADATA
   cannot be read; and (f) **not** be a `log_version_diagnostic` caller — the
   group callback must never run for it. **Do not reintroduce
   `@click.version_option` in any spelling** — the AST pin forbids it, and
   covers `daemon_cli.py` too.
7. **The DGX drops are STILL UNEXPLAINED — five theories refuted** *(carried;
   not investigated this session)*. **Do not propose a sixth without a captured
   outage in which the host was demonstrably up throughout.** Triage with
   `journalctl --list-boots` first. **Power is not a candidate** (~5-day UPS).
   **Do not edit `/etc/wireguard/wg0.conf`.** `10.0.0.3` worked first try again.
8. **When reverting a mutation, restore from a file copy — never `git checkout`**
   *(carried, sessions 23–29)*. An uncommitted fix lives only in the working
   tree. Used again this session for 4 mutations; treat **empty** pytest output
   as a failed mutation, not a pass.
9. **An absence assertion needs the constant, not a literal** *(carried)*.
   Assert against the module's own constant and put a positive control beside
   it. #307's quiet-shape tests are the current example: three assertions that
   nothing was logged, guarded by one that a real invocation still logs.
10. **`blobs_claimable 0` alongside `blobs_pending 0` is the SETTLED archive
    shape** *(carried)*. **A steady non-zero `blobs_no_text` is NORMAL** (#277)
    — terminal by design, read it like `body_lang_declined`. **`blobs_gave_up`
    is the one to act on.** `QueueCounts.__post_init__` raises on two distinct
    conditions (#284); **`DISTINCT` in `EXTENSION_MATCH_JOIN_SQL` is
    load-bearing with no runtime guard** — its only symptom is `pending`
    diverging from `claimable`.
11. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`, or an
    unhandled promise rejection leaks while vitest still reports "passed".
12. **The Mac's launchd daemon runs an EDITABLE install** *(carried)*.
    `/Users/hherb/src/localmail/.venv/bin/localmail` resolves through `src/`, so
    the running service executes **whatever the working tree is checked out to**.
    Check the tree back out to `main` after merging.
13. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `--all-extras` on the Mac and `--extra mcp --extra extraction` on the DGX.
    **`uv` is not on the DGX's default non-interactive PATH** — use
    `~/.local/bin/uv` over SSH. **A non-zero `skipped` count means an extra went
    missing**; the name to look for is
    `tests/test_extractor.py::test_pdf_pipeline_options_reflect_the_configured_engine`.
    The Mac ran **0 skipped** this session.
14. **Do not run the test suite while a backfill is draining** *(carried)*, and
    **do not run two full suites in parallel** against the same Postgres. The
    `main` baseline this session was `--collect-only` (no DB) precisely so it
    could overlap the GUI build safely.
15. **`search-status` is sub-second on BOTH hosts — stop budgeting minutes**
    *(carried)*. Mac **0.98 s** this session. If it ever runs long that is a
    **regression** of #280: check `EXPLAIN (FORMAT JSON)` for a `Seq Scan on
    messages` under a `SubPlan` before looking anywhere else.
16. **The macOS socket deselect is GONE — stop using it** *(carried)*. #276
    fixed it; `uv run pytest -q` with **no arguments** is the right command.
17. **An empty `daemon_heartbeats` right after a daemon restart is normal for
    minutes** *(carried — #269/#271)*. It is the startup blob-temp sweep. Grep
    for **`blob-temp sweep done: walked=`**.
18. **The stale NOTIFY queue did not recur this session** *(carried)*. Both
    gates read healthy on the first check and the suite passed twice. Session 26
    found it in an **asymmetric** form: `pg_notification_queue_usage()` healthy
    while `LISTEN daemon_commands` errored. **Verify both gates, never one.**
19. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    Clear deliberately: `DELETE FROM attachment_text WHERE extractor =
    'type-skipped'`. **#266's whitespace-heal is a one-way door too** — what
    makes it safe is the `is_blank` gate, not the nature of the data.
20. **`--relabel` is the only destructive verb in the lang path** *(carried)*.
    Discards **every** label. Reach for `--retry-declined` first. Budget ~45 min
    for a 100k-row archive, and note that **`reopen_all`'s bulk UPDATE shows no
    progress in `pg_stat_activity` until it commits** — do **not** cancel it.
21. **`ExtractorConfigurationError` subclasses `TransientExtractorError`, and that
    subclassing *is* the #248 fix — do not "clean it up"** *(carried)*.
22. **`run_embed_worker_once`'s `failure_log` defaults to the process-wide
    `embed_worker._FAILURE_LOG`, and that default is the fix** *(carried — #267)*.
23. **No ROADMAP.md** *(carried, re-confirmed)* — the `/nextsession` ROADMAP step
    is a no-op; slice status lives here + `docs/handoffs/` + the specs. **README
    was updated** this session.
24. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    kept though #245 is fixed; #246 warns on group-write; `InsecureSecretsFile`
    refuses; #239's manual tombstone retention deliberate; admin bearer tokens
    have no per-token scope (#204).
25. **Run vitest from `gui/`, not the repo root** *(carried)*. **`cargo clippy
    --all-targets` is clean but ungated** — CI runs clippy without
    `--all-targets`, so `#[cfg(test)]` modules are never linted. No Rust changed
    this session, so cargo was not re-run.
26. **Do not "tidy up" `_PRE280_CORRELATED_ALLOWLIST_SQL`** *(carried)*.
    [tests/test_extract_queue_sql.py:306](tests/test_extract_queue_sql.py#L306)
    holds the pre-#280 correlated predicate **on purpose**, as the negative
    control that proves the plan assertions can fail.
27. **A paging client must treat 409 and 400 differently** *(new — #311)*. 409 is
    recoverable (re-run without a cursor); 400 is **permanent for that cursor**
    and must retire it. And **never state a `sort` on a request that carries a
    cursor** — that is what makes the contradiction unreachable rather than
    merely handled, and it is what `docs/mcp-usage.md` tells every client.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status                               # expect clean
git branch --show-current
git log --oneline -1 origin/main         # THE authority, not this file's header
git log --oneline main..origin/main      # NON-empty means a session landed since

# RISK 2 — the stranded-branch SHORTLIST. Noisy by construction: every
# squash-merged branch shows up. Only act on a branch whose PR merged recently,
# and confirm with a CONTENT diff before believing anything is lost:
for b in $(git branch -r --format='%(refname:short)' | grep -v 'origin/main\|HEAD'); do
  n=$(git log --oneline main.."$b" | wc -l); [ "$n" -gt 0 ] && echo "$b: $n commits not on main"
done
git diff --stat main origin/<branch>     # EMPTY = landed, not stranded

# ONE PR is open, awaiting your merge (What's next, 0).
gh pr list
gh pr checks 313
gh pr view 313 --json baseRefName --jq .baseRefName   # MUST be "main"
gh issue list --limit 30                 # 18 open; the merge closes #307/#311/#312
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '[.[] | select(.state=="open")] | length'          # expect 0

# AFTER MERGING:
#   Mac:  git checkout main && git pull && unset VIRTUAL_ENV && uv sync --all-extras
#         (checking the tree back out to main MATTERS — risk 12)
#   DGX:  ssh 10.0.0.3 'cd ~/src/localmail && git pull && ~/.local/bin/uv sync --extra mcp --extra extraction && ~/.local/bin/uv run localmail init-db && systemctl --user restart localmail-daemon localmail-serve'
# Acceptance — the version line, exit 0, nothing on stderr:
unset VIRTUAL_ENV && uv run localmail --version 2>/tmp/v.err; echo "exit=$?"; wc -c < /tmp/v.err   # expect 0
# And #307's — help must be quiet even on a BROKEN install:
unset VIRTUAL_ENV && uv run localmail sync --help 2>&1 | grep -c "could not be determined"        # expect 0

# Python test suite. No --deselect (risk 16).
# Do NOT run while a backfill is draining, and never two suites at once (risk 14).
unset VIRTUAL_ENV && uv sync --all-extras   # NOT a bare `uv sync` — risk 13
unset VIRTUAL_ENV && uv run pytest -q
#   expect: 2535 passed, 0 skipped on this branch; 2518 on main.
#   MEASURE BOTH REFS IN THIS SESSION, don't quote these (risk 5) — no DB needed:
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2

unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 142 source files

# If EXACTLY the three LISTEN/NOTIFY tests fail, it is the stale queue (risk 18).
# CHECK BOTH GATES — session 26's recurrence had gate 1 reading healthy:
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'
# Remedy (runbook Option A) — verify the gates WHILE the daemon is down:
#   launchctl bootout gui/$UID/com.localmail.daemon
#   until ! launchctl print gui/$UID/com.localmail.daemon >/dev/null 2>&1; do sleep 2; done
#   <re-run both gates>
#   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.localmail.daemon.plist

# Host health checks (Mac):
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect: 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 17)

# The attachment counters — UNDER A SECOND on both hosts (risk 15):
unset VIRTUAL_ENV && uv run localmail search-status
#   These MOVE as the archive grows, so check the SHAPE, not the literals: the
#   four buckets must sum to blobs_eligible, and claimable must equal pending.
#   Mac  approx: blobs_eligible ~9.5k = ~9.2k + 106 + 182 + 0, claimable 0
#   DGX  approx: blobs_eligible ~4.4k = ~4.1k +  91 + 127 + 0, claimable 0

# The DGX — level with main at fb48f23 as of this session (risk 4).
# Use the WireGuard address; uv is not on its non-interactive PATH (risks 7, 13):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'
ssh 10.0.0.3 'PGPASSWORD="local@@mail" psql -h 127.0.0.1 -p 5532 -U localmail -d localmail \
  -tAc "SELECT worker_kind, state, round(extract(epoch from now()-last_heartbeat_at)) FROM daemon_heartbeats"'
#   expect 5 rows (one account), ages under ~60 s
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — REQUIRED this session (gui/ changed). MUST run from gui/ (risk 25):
cd gui && npm run check && npm test && npm run build && cd ..
#   expect: 0 errors / 399 tests, 47 files
# Rust unchanged this session; run these only if you touch src-tauri:
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

`main` tip is **`fb48f23`** (PR #309). This session's work is **`0940f98`** plus
this handoff on `fix/309-review-round`, **open as PR #313, base `main`, not
merged**. Latest migration **`0035_messages_body_lang_attempted_at.sql`**; next
free slot `0036_*.sql` (this session adds none). **Open issues: 18**, dropping to
**15** on merge (#307, #311, #312). **Dependabot: 0 open alerts.** Both hosts run
`fb48f23` — **verified, not assumed**.
