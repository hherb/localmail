# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-14 (session 27).** Session 26's PR #301 was **merged by
> the operator**, so `main` moved `cb77108` → **`5a8826a`** and the stranded
> review round is finally on `main`. The stranded-branch check ran clean for it.
>
> **This session closed the whole review round #301 generated** — four issues
> filed against the version diagnostic on 2026-08-14, three of them fixed and
> one verified-and-scoped. **PR #306 is open, base `main`, unmerged.**
>
> **The one that mattered operationally is #304.** 36 of 38 CLI commands
> reported an unresolvable version *nowhere*. Since #296 traded a loud crash for
> graceful degradation, that made a cron `localmail sync` on a host with a
> failing `site-packages` mount run to completion with exit 0 and say nothing —
> where before #296 it failed loudly on the first night. Confirmed by a negative
> control on `main` (§3).
>
> **`gh issue list` gained four issues without a session running.** #302–#305
> were filed at 13:11–13:12 UTC on 2026-08-14, before #301 merged at 15:22. If
> a handoff's issue count disagrees with `gh issue list`, believe `gh` — see
> risk 3.
>
> **The stale NOTIFY queue did NOT recur this session.** Both gates read healthy
> on the first check and the suite passed twice with no LISTEN/NOTIFY failures.
> That is not evidence it is fixed — risk 18 still stands.

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

Branch `fix/302-305-version-diagnostic-round`, commit **`2b2fb6d`** (code +
docs) and the handoff commit. **Open as PR #306, base `main`, unmerged.** No
migration, no new dependency, no config change, **no wire change**. Closes
**#302, #303, #304**; **#305 stays open** with its remaining half scoped.

### 1. #304 — every command reports now, not three of 38

`main.commands` has 38 entries. Exactly three surfaced an unresolvable version:
`--version`, `serve`, `run`. The other **36** caught it and reported it nowhere.

The fix is one call in the `main` group callback. **The RED test reproduced the
issue's headline exactly — 36 failures, one per command.**

- **`cli.SELF_REPORTING_COMMANDS` (`{"run", "serve"}`) is what the group
  callback steps aside for.** Both configure logging first, so their line keeps
  its level and timestamp; reporting for them in the group callback would win
  the per-process dedup with an **earlier, unformatted** line and silently
  downgrade it. Mutation-proven: forcing the group callback to report for them
  fails three existing ordering pins.
- **Only one drift direction of that set is survivable**, so it is derived from
  the code and compared, never trusted. A command *listed but not reporting*
  goes **silent** — #304 reopened for exactly the long-running processes #295
  was about. A command *reporting but not listed* merely loses its formatting.
  `test_the_skip_set_is_exactly_the_commands_that_report_themselves` reads the
  **live** `main.commands` registry (so a command added later is in scope with
  nobody updating a list) and decides by walking each callback's **AST**, not
  its text — #291 already paid for that lesson, and a third mutation proves a
  prose mention of the reporter does not count.
- `--version` is **not** a group-callback reporter and is pinned as such: its
  option is eager and exits inside its own callback, which is what keeps its
  stderr going through click while its stdout stays the machine-readable line.

### 2. #302 — the text said `warning:`, the record was an ERROR

journald showed `ERROR ... warning: ...`, so an operator told to grep for one
found the other. `_SEVERITY_PREFIX` is now
`logging.getLevelName(_REPORT_LEVEL).lower()`, so the two **cannot be changed
apart**.

- **The pin is a relation, not a literal**: it reads the word back off a record
  the module actually emitted. Asserting against `"error"` would pass against a
  remedy set and a level that agree with the literal and not with each other.
- **This is more than cosmetics.** `serve` and the group callback reach
  `logging.lastResort`, which has **no formatter** — stderr, message only, no
  level, no timestamp, no logger name. On most paths the word in the text is the
  only severity marker there is.
- **Configuring logging in the group callback was rejected**, and that is the
  answer to the issue's second half: the callback precedes all 38 commands, and
  installing a root handler for every one of them changes far more than the line
  it would format. The level still matters where it is not decoration — `run`
  after `basicConfig`, `create_app` under uvicorn's `dictConfig`, any embedder
  constructing `Daemon` directly.

### 3. #303 — the cause line dropped the `__cause__` chain

`format_exception_only` renders **one** exception, so
`raise RuntimeError("finder failed") from OSError(5, …, "/nfs/…/METADATA")`
rendered as `cause: RuntimeError: finder failed` — the errno and the filename
gone, i.e. the rendering discarding exactly what it was chosen over a bare type
name to keep, under a remedy that says *"read the cause below first"*.

The rule is the pure `version_report.render_exception_chain`.

- **Three bounds, because this runs on the import path inside a handler that may
  not fail**: `_MAX_CHAIN_LINKS` (5), an identity cycle guard, and the existing
  `_MAX_DETAIL_CHARS` applied to the **joined** result — so the ceiling is not
  silently five times looser. All four mutation-proven.
- **`__suppress_context__` is honoured** (`raise X from None` prints no chain),
  and the walk follows `__cause__` first then `__context__`: following only
  `__cause__` would miss most real wrappers, since a bare `raise` inside an
  `except` sets the context.
- **Each link renders in its own guard**, so one hostile member costs its own
  detail rather than the chain's; the outer guard remains for the *walk*, since
  reading `__cause__` off a hostile object can raise too.
- An unwrapped exception gains **no** separator and no empty link — pinned,
  because both shapes #296 reproduced are unwrapped.

### 4. #305 — verified and scoped, deliberately not fixed

`import localmail` survives a missing third-party dependency; `import
localmail.cli` does not, because `cli.py` imports the daemon (and so `sqlparse`,
`psycopg`, `keyring`) at module scope. **Reproduced** by blocking one module on
`sys.meta_path`:

```
import localmail OK, version = 0.3.0 source = VersionSource.INSTALLED
localmail.cli import -> ModuleNotFoundError: import of sqlparse halted
```

The module docstring states that scope rather than overclaiming (option 1, which
#301 had already half-shipped). Option 2 — deferring those imports into the
command bodies — is a `cli.py` change and belongs with the refactor that file
already owes. **#305 stays open.**

### 5. Verification (this Mac, all extras)

- `uv run pytest -q` → **2490 passed, 0 failed, 0 skipped** (163 s). Run twice;
  no flakes, and neither of #299's two known flaky tests appeared.
- **Counts measured on BOTH refs in the same session, not subtracted** (risk 5):
  `main` **2443**, this branch **2490** — exactly the 47 tests added. The
  worktree needed `uv sync --all-extras` first, and venv parity was confirmed on
  unchanged files (43 = 43) before trusting either number.
- `uv run mypy src/localmail` → **Success, 141 source files**.
- `ruff check` clean on all 4 changed files. Repo-wide `src/localmail/` still
  **10** pre-existing errors (#285). ruff on PATH is **0.11.0**.
- **Warnings: 3 on both refs**, all pre-existing (two are #25's websockets
  deprecations; one is a psycopg `ConnectionPool.__del__` race in
  `test_serve_messages_routes.py`). An earlier branch run showed 4 — GC timing
  noise, confirmed by re-running.
- **Mutation-proven, every new rule**: 5 for the chain, 3 for the skip-set, 1
  for the severity word, 1 for the group-callback report.
- End-to-end on a corrupt-metadata tree (one latin-1 byte in a
  `localmail-9.9.9.dist-info/METADATA` ahead on `sys.path`):

```
$ PYTHONPATH=repro uv run localmail --version
localmail, version 0.0.0+unknown
error: the localmail version could not be determined — reading its distribution metadata raised.
  remedy: read the cause below first. …
  cause: UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe9 in position 65: invalid continuation byte
exit=0
```

- **The negative control.** The same corrupt tree, a plain `localmail
  list-accounts`: on **this branch** the three-line report appears before the
  command fails; on **`main`, in a throwaway worktree**, it prints **nothing**
  about the version. The #304 gap is real on `main` today.
- The chain, end-to-end: `cause: RuntimeError: finder failed <- caused by
  OSError: [Errno 5] Input/output error:
  '/nfs/site-packages/localmail-0.3.dist-info/METADATA'`.

### 6. Host health confirmed, not assumed

**Mac** — both launchd agents `running`/`active`, daemon pid **53418**, 7
heartbeats, oldest 29 s. `search-status` **1.05 s** (no #280 regression):

```
messages_total 127818
blobs_eligible 9504 = extracted 9216 + no_text 106 + gave_up 182 + pending 0
blobs_claimable 0        body_lang_pending 0, declined 12190
```

**DGX** — all three units `active`; `git log --oneline -1` → **`29f5fae`**,
i.e. **three** commits behind `main` and four behind this PR (verified over SSH
— risk 4). It has neither #296's fix nor #301's review round.

**Dependabot: 0 open alerts.** **18 open issues.**

### 7. Docs

- **CLAUDE.md** — the `--version` commands block, the layout line, the
  `format_exception_only` bullet (now the chain), a rewritten #295/#304 reach
  section covering the skip-set and its AST pin, the #302 severity-word rule,
  and #305's scope. The "three readers" line now says three reader *modules*
  (`cli.py` reads it twice).
- **README** — the `cause:` paragraph gains the chain; the startup paragraph
  becomes "**Every command reports it**" and drops the "see issue #304" pointer.
- **No ROADMAP.md** (confirmed absent again; that `/nextsession` step stays a
  no-op).

## What's next

### 0. **Merge PR #306** — the only open PR
   **The operator merges** (project convention). Closes #302, #303, #304.
   - **Acceptance:** on `main` afterwards, `uv run pytest --collect-only -q`
     reports **2490** (2443 today), and the §5 negative control inverts — a
     plain `localmail list-accounts` on a corrupt-metadata tree prints the
     three-line report instead of nothing.
   - **Then deploy the DGX** — at `29f5fae`, four commits behind after this
     merge. Recipe in the resume block.
   - The Mac needs `git checkout main && git pull && uv sync --all-extras`.
     **Checking the tree back out to `main` matters** — its launchd daemon runs
     an editable install (risk 12).

### 1. **#305 — `--version` still dies on a missing dependency** *(carried, now scoped)*
   The remaining half of this session's round, and the only version-cluster
   issue left besides #300. `cli.py` imports the daemon at module scope, so a
   partial `uv sync` kills the one command an operator is told to run to verify
   an install. **Acceptance:** blocking `sqlparse` on `sys.meta_path` (the
   reproduction in §4) leaves `localmail --version` printing its line and
   exiting 0. **Do it with the `cli.py` refactor, not before** — moving imports
   into 38 command bodies piecemeal trades a startup failure for a first-use
   one, which the issue itself flags as sometimes worse.

### 2. **#300 — an unresolvable version has no machine-readable channel** *(carried)*
   The version cluster's other live thread. The diagnosis is now legible to a
   **human** on every entry point (that is what #304 just closed) and to a
   **machine** on none: `--version` exits 0 on the unknown path (deliberate and
   pinned — risk 6) and `/v1/version` ships the sentinel unflagged. **Read
   #295's precedent first:** its wire question was decided *against* adding a
   field, citing #278 as the cautionary case, so this needs a decision rather
   than an implementation. `__version_source__` is retained for exactly this.

### 3. **#299 — two pre-existing flaky tests** *(carried)*
   Daemon lifecycle busy-guard and login audit rows; confirmed flaky on `main`.
   Neither appeared in this session's three full runs, which is consistent with
   flakiness and is **not** evidence they are fixed.

### 4. **#278 — the version surface's other half** *(carried; needs YOUR decision first)*
   The GUI About tab renders a `build_hash` that `/v1/version` has **never**
   emitted, so "Server build" always shows `?`, while five test files mock the
   field and make it look covered. **Two options, product call before any code:**
   emit a real build hash (from git, at build time — but the version is stamped
   at *install* time, so this needs a story for `uv tool install` from a
   tarball), or delete the field end-to-end including the five mocks.

### 5. **#285 — ruff, repo-wide** *(carried)*
   Still **9** dead `# noqa: S608` directives across 5 files and **10**
   pre-existing `ruff check src/localmail/` errors; no `[tool.ruff]` config and
   no CI step. `version_report.py`'s broad catch still carries no `BLE001`
   suppression, and the comment there records that 14 of 79 sibling catches do —
   i.e. no convention in either direction. **Decide both together.**

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
   scope) · **#25** (websockets DeprecationWarning — it is two of the three
   warnings every full run prints).

### 8. **Smaller, deliberately not done** *(carried)*
   - **`cli.py` is 2020 lines**, **`daemon.py` 580**, **`version_report.py`
     593** — measured, not inferred (`main`: 1990 / 580 / 478). The `cli.py`
     refactor session 21 deferred is still owed in full, and **#305 now depends
     on it**.
     - `version_report.py` is over the 500-line guideline but is **152 lines of
       code and 441 of rationale** (measured with `ast`+`tokenize`). Splitting
       it was considered and rejected: the reporting half cannot leave, because
       `_SEVERITY_PREFIX` is derived from `_REPORT_LEVEL` and separating them
       breaks the one-authority property #302 just established. The one seam
       with a real second caller is a shared `exception_render.py` serving both
       this module and `failure_pacing.py`/`embed_worker.py` — a principled
       refactor, but it changes worker log output and needs its own decision.
   - **165 docling failures on the Mac** (31 `File format not allowed`, 134
     `Input document … is not valid`), of 182 `blobs_gave_up`; **127** on the DGX.
   - **Residual implausible language labels are dominated by `ja`.** ~0.24% of
     labels; the confidence-floor lever was measured useless. If ever chased,
     **sample the `ja` rows first**.
   - **The DGX drops remain uninvestigated and unexplained** (risk 7).
   - **#269's suggestion 2** (blob-temp sweep off the critical path) — deferred
     in session 19; reopen only if cold-cache startups grow past tolerable.
   - **`git stash drop` the session-22 leftover** if you want it tidy: session 26
     verified its content is already on `main` (`bc5b556`), so it is safe to
     drop. Left in place because dropping is destructive and is your call.

## Open decisions & risks

1. **One PR is open and yours to merge.** `main` is `5a8826a`; **#306**
   (`fix/302-305-version-diagnostic-round`) closes #302/#303/#304. **18 open
   issues**, dropping to **15** on merge. **Dependabot: 0 open alerts.**
2. **Base every branch on `main`; put a session's code and its handoff in ONE
   PR** *(carried — session 26's whole reason for existing)*. Session 25 based
   its handoff PR on a fix branch; the fix PR merged to `main`, the handoff PR
   merged into the now-dead fix branch **13 seconds later**, and everything in
   it was lost with **no failing check, no open PR, nothing to notice**. This
   session put code and handoff on one branch, one PR.
   - **The documented stranded-branch check is NOISY, and session 26's wording
     did not say so** *(new)*. `git log --oneline main..origin/<branch>` is
     non-empty for **every squash-merged branch**, because the branch's own
     commits never appear on `main`. It reported **16** branches here, all
     stale-but-landed. The signal is not "non-empty" — it is **non-empty on a
     branch whose PR merged recently**, cross-checked with
     `git diff main origin/<branch> -- <its files>`, which is how session 26
     actually cleared the leftover stash. Treat the loop in the resume block as
     a *shortlist generator*, not a verdict.
3. **`gh issue list` is the authority on issues, not this file** *(new)*. Four
   issues (#302–#305) appeared between session 26's handoff being written and
   this session starting, filed by the review of #301 with no session running.
   The handoff said "14 open issues"; `gh` said 18. **Believe `gh`.**
4. **Verify host revisions; do not infer them** *(carried)*. The DGX is at
   `29f5fae` — checked over SSH this session. One
   `ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'` settles it.
5. **Test-count baselines: measure both refs IN THE SAME SESSION** *(carried,
   and sharpened)*. `main` → **2443**, this branch → **2490**. Session 26's
   handoff said `main` was 2419; today it collects 2443 on the same tree,
   because a `uv sync` moves the baseline. **A number quoted from a previous
   handoff is not a baseline.** Also: a fresh worktree gets a bare `uv sync`
   (risk 13), which under-collects by ~500 — sync it `--all-extras` and confirm
   venv parity on an unchanged file before comparing.
6. **`--version`'s contract is five things, all pinned** *(carried)*. It must
   (a) read no config and touch no database; (b) keep **stdout** to the single
   machine-readable line; (c) put any diagnostic on **stderr**; (d) **exit 0**
   even when the version is unknown; and (e) still work on a tree whose METADATA
   cannot be read at all. **Do not reintroduce `@click.version_option` in any
   spelling** — the AST pin forbids it, and covers `daemon_cli.py` too. Sixth,
   as of #304: it is **not** a `log_version_diagnostic` caller and the group
   callback must never run for it.
7. **The DGX drops are STILL UNEXPLAINED — five theories refuted** *(carried;
   not investigated this session)*. **Do not propose a sixth without a captured
   outage in which the host was demonstrably up throughout.** Triage with
   `journalctl --list-boots` first. **Power is not a candidate** (~5-day UPS).
   **Do not edit `/etc/wireguard/wg0.conf`.** `10.0.0.3` worked first try again.
   A single `tunnel=FAIL` probe sample is not an outage.
8. **When reverting a mutation, restore from a file copy — never `git checkout`**
   *(carried, sessions 23–27)*. An uncommitted fix lives only in the working
   tree. Used again this session for 10 mutations; treat **empty** pytest output
   as a failed mutation, not a pass.
9. **An absence assertion needs the constant, not a literal** *(carried)*.
   `assert "cause:" not in rendered` cannot fail once the prefix is renamed.
   Assert against the module's own constant and put a positive control beside
   it. The #302 pin is the same idea in the other direction: it derives the
   expected word from an emitted record rather than hardcoding `"error"`.
10. **`blobs_claimable 0` alongside `blobs_pending 0` is the SETTLED archive
    shape, on both hosts** *(carried)*. **A steady non-zero `blobs_no_text` is
    NORMAL** (#277) — terminal by design, read it like `body_lang_declined`.
    **`blobs_gave_up` is the one to act on** — `list-failed-extractions` says why
    (poison-pill half only), `retry-failed-extractions` re-queues.
    **`QueueCounts.__post_init__` raises on two distinct conditions** (#284):
    `misfiled` is checked **before** the sum, deliberately. **`DISTINCT` in
    `EXTENSION_MATCH_JOIN_SQL` is load-bearing with no runtime guard** — its only
    symptom is `pending` diverging from `claimable`.
11. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`, or an
    unhandled promise rejection leaks while vitest still reports "passed".
12. **The Mac's launchd daemon runs an EDITABLE install** *(carried)*.
    `/Users/hherb/src/localmail/.venv/bin/localmail` resolves through `src/`, so
    the running service executes **whatever the working tree is checked out to**.
    This session's branch is behaviourally identical on a healthy install (the
    diagnostic is `None`, and `run` is in `SELF_REPORTING_COMMANDS` either way),
    but check the tree back out to `main` after merging.
13. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `--all-extras` on the Mac and `--extra mcp --extra extraction` on the DGX.
    **`uv` is not on the DGX's default non-interactive PATH** — use
    `~/.local/bin/uv` over SSH. **A non-zero `skipped` count means an extra went
    missing.** CI installs only `--extra mcp`, so its count differs by design.
    **The skip to look for by name** is
    `tests/test_extractor.py::test_pdf_pipeline_options_reflect_the_configured_engine`.
    The Mac ran **0 skipped** this session.
14. **Do not run the test suite while a backfill is draining** *(carried)*.
    Shared-cluster contention produces dozens of false failures. `search-status`
    does not qualify. **Do not run two full suites in parallel** against the same
    Postgres for the same reason — the main-worktree baseline and the branch run
    were deliberately sequenced this session.
15. **`search-status` is sub-second on BOTH hosts — stop budgeting minutes**
    *(carried)*. Mac **1.05 s** this session. If it ever runs long that is a
    **regression** of #280: check `EXPLAIN (FORMAT JSON)` for a `Seq Scan on
    messages` under a `SubPlan` before looking anywhere else.
16. **The macOS socket deselect is GONE — stop using it** *(carried)*. #276 fixed
    it; `uv run pytest -q` with **no arguments** is the right command.
17. **An empty `daemon_heartbeats` right after a daemon restart is normal for
    minutes** *(carried — #269/#271)*. It is the startup blob-temp sweep. Grep
    for **`blob-temp sweep done: walked=`**.
18. **The stale NOTIFY queue did NOT recur this session, which proves nothing**
    *(carried)*. Session 26 found it back in an **asymmetric** form:
    `pg_notification_queue_usage()` read healthy while `LISTEN daemon_commands`
    errored outright. **Verify both gates, never one.** Fix is the runbook's
    Option A: `launchctl bootout gui/$UID/com.localmail.daemon`, **wait until
    `launchctl print` says the service is gone**, verify both gates, then
    `bootstrap` back. Gate the pytest re-run on the probes, not a fixed wait.
19. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    Clear deliberately: `DELETE FROM attachment_text WHERE extractor =
    'type-skipped'`. **#266's whitespace-heal is a one-way door too** — what
    makes it safe is the `is_blank` gate, not the nature of the data.
20. **`--relabel` is the only destructive verb in the lang path** *(carried)*.
    Discards **every** label; archive unsearchable by `lang:` until the drain
    completes. Prompts unless `--yes`. Reach for `--retry-declined` first. Budget
    ~45 min for a 100k-row archive, and note that **`reopen_all`'s bulk UPDATE
    shows no progress in `pg_stat_activity` until it commits** — tens of minutes
    of apparent hang is expected; do **not** cancel it.
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
    `--all-targets`, so `#[cfg(test)]` modules are never linted.
26. **Do not "tidy up" `_PRE280_CORRELATED_ALLOWLIST_SQL`** *(carried)*.
    [tests/test_extract_queue_sql.py:306](tests/test_extract_queue_sql.py#L306)
    holds the pre-#280 correlated predicate **on purpose**, as the negative
    control that proves the plan assertions can fail.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status                               # expect clean
git branch --show-current
git log --oneline origin/main..main      # expect 0
git log --oneline main..origin/main      # expect 0 — NON-empty means a session
                                         #   landed after this handoff was written

# RISK 2 — the stranded-branch SHORTLIST. Noisy by construction: every
# squash-merged branch shows up here. Only act on a branch whose PR merged
# recently, and confirm with a content diff before believing anything is lost:
for b in $(git branch -r --format='%(refname:short)' | grep -v 'origin/main\|HEAD'); do
  n=$(git log --oneline main.."$b" | wc -l); [ "$n" -gt 0 ] && echo "$b: $n commits not on main"
done
gh pr view <N> --json state,baseRefName --jq '{state,base:.baseRefName}'   # base MUST be main

# ONE PR is open, awaiting your merge (What's next, 0).
gh pr list
gh pr checks 306
gh pr view 306 --json baseRefName --jq .baseRefName   # MUST be "main"
gh issue list --limit 30                 # 18 open; the merge closes #302/#303/#304
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '[.[] | select(.state=="open")] | length'          # expect 0

# AFTER MERGING:
#   Mac:  git checkout main && git pull && unset VIRTUAL_ENV && uv sync --all-extras
#         (checking the tree back out to main MATTERS — risk 12)
#   DGX:  ssh 10.0.0.3 'cd ~/src/localmail && git pull && ~/.local/bin/uv sync --extra mcp --extra extraction && ~/.local/bin/uv run localmail init-db && systemctl --user restart localmail-daemon localmail-serve'
# Acceptance on BOTH hosts — version line, exit 0, and NOTHING on stderr:
unset VIRTUAL_ENV && uv run localmail --version 2>/tmp/v.err; echo "exit=$?"; wc -c < /tmp/v.err   # expect 0
# And the #304 acceptance — a plain command must ALSO be silent on a healthy install:
unset VIRTUAL_ENV && uv run localmail list-accounts 2>/tmp/l.err >/dev/null; grep -c "could not be determined" /tmp/l.err   # expect 0

# Python test suite. No --deselect (risk 16).
# Do NOT run while a backfill is draining, and never two suites at once (risk 14).
unset VIRTUAL_ENV && uv sync --all-extras   # NOT a bare `uv sync` — risk 13
unset VIRTUAL_ENV && uv run pytest -q
#   expect: 2490 passed, 0 skipped on this branch; 2443 on main.
#   MEASURE BOTH REFS IN THIS SESSION, don't quote these (risk 5) — no DB needed:
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2

unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 141 source files

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

# The DGX — at 29f5fae, THREE commits behind main before this merge (risk 4).
# Use the WireGuard address; uv is not on its non-interactive PATH (risks 7, 13):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'   # expect 29f5fae until deployed
ssh 10.0.0.3 'PGPASSWORD="local@@mail" psql -h 127.0.0.1 -p 5532 -U localmail -d localmail \
  -tAc "SELECT worker_kind, state, round(extract(epoch from now()-last_heartbeat_at)) FROM daemon_heartbeats"'
#   expect 5 rows (one account), ages under ~60 s
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Reproducing the version diagnostics (both were used this session):
#   corrupt METADATA — one latin-1 byte in a dist-info placed ahead on sys.path
#   missing dependency (#305) — block one module on sys.meta_path and import
#     localmail (survives) then localmail.cli (does not)

# Frontend — only if you touch gui/ (MUST run from gui/ — risk 25):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

`main` tip is **`5a8826a`** (PR #301). This session's work is **`2b2fb6d`** plus
this handoff on `fix/302-305-version-diagnostic-round`, **open as PR #306, base
`main`, not merged**. Latest migration
**`0035_messages_body_lang_attempted_at.sql`**; next free slot `0036_*.sql` (this
session adds none). **Open issues: 18**, dropping to **15** on merge (#302, #303,
#304). **Dependabot: 0 open alerts.** The DGX runs `29f5fae` — **verified over
SSH, not assumed**.
