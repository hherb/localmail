# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-15 (session 29).** `main` is **`eec8e09`**. This
> session shipped **two** PRs: **#313 merged** (the #308 review round — #307,
> #311, #312) and **#315 open, CI green** (build provenance — #278, #300, 19
> commits).
>
> **Read `git`/`gh` before this file, always.** This session opened a handoff
> that was a whole session stale — it described PR #306 as unmerged when #306
> *and* #309 had both landed, and session 28 had shipped #308 with no handoff at
> all. Then `main` moved again *during* the session (#314). Risk 3 is now
> twice-earned.
>
> **#314 changed the interpreter pin to 3.13, and that has a live hazard.** A
> bare `uv run` on a host that has not re-synced rebuilds the venv for the new
> pin **without extras** — stripping docling, mcp and the extraction stack from
> the environment the daemon executes. It happened on this Mac this session and
> was repaired. See risk 13, which is no longer hypothetical.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The database is canonical for accounts.
Daemon: hot-reload account set, heartbeats, DB command queue, two-plane
supervision. Web admin UI (HTMX): account CRUD, user management, archive
imports, daemon control. Hybrid search (Phases 1+2) + an HTTPS GUI server + a
remote MCP server (optionally a full OAuth 2.1 authorization server) + the
opt-in `--smart` LLM query rewriter are all shipped. A Tauri 2 + Svelte 5 GUI
lives under `gui/` — read-only viewer plus an admin mode (Accounts + Daemon
panels shipped; Users + Imports still placeholders). Version **0.3.0**, Python
**3.13** pinned (CI matrixes 3.12 + 3.13). Licensed AGPL-3.0-or-later (per-file
SPDX headers in `src/localmail/`; **not** in `gui/`).

## What we shipped this session

### PR #313 — merged as `82513a0`, closing #307, #311, #312

The review round session 28's #308 generated. No migration, no wire change.

- **#311 was the one with user-visible teeth.** Since #308 the server can answer
  a paging request with **400**, but the GUI recovered from **409 only** — and
  `MessageList`'s `IntersectionObserver` re-fires `loadMore()` on every scroll
  event while `hasMore` is true. So the fix turned a silent restart into a
  **permanently-failing request loop behind an error banner**. The rules are the
  pure [gui/src/lib/search_paging.ts](gui/src/lib/search_paging.ts): a request
  carrying a cursor states **no** sort (making the contradiction unreachable,
  not merely recoverable, since the store's sort is user-mutable while a cursor
  is live), and any 400 retires the cursor. `loadMore` also guards on `loading`
  — a wrong-results fix, since both requests share `#submitSeq`.
- **#312 was sharper than filed.** `sort=None` fell through into the hybrid
  branch — right ordering by accident, wrong *record*: the raw argument was what
  the pool got cached with, and `_check_pool_sort` reads that back to decide a
  400. `DEFAULT_SORT` moved to `search/searcher.py` beside the `SortMode` it
  ranges over.
- **#307 was your product call** (option 2): help requests stay quiet, via the
  pure [src/localmail/cli_help_request.py](src/localmail/cli_help_request.py).
  All four shapes pinned, including the three already quiet, plus a positive
  control so a rule matching too broadly cannot silently reopen #304.

### PR #315 — OPEN, base `main`, 19 commits, CI green, closing #278 and #300

Designed and implemented this session via spec → plan → subagent-driven
execution. **Design:**
[docs/superpowers/specs/2026-08-15-build-provenance-design.md](docs/superpowers/specs/2026-08-15-build-provenance-design.md).
**Plan:**
[docs/superpowers/plans/2026-08-15-build-provenance.md](docs/superpowers/plans/2026-08-15-build-provenance.md).

**Three verified facts changed the mechanism**, and they are the part worth
remembering: **there is no build** (both CI workflows test-only, no tags,
nothing publishes), and **neither deployment installs an artifact** — the Mac's
launchd daemon and the DGX both run editable installs from a git checkout. So a
hash stamped at wheel-build time would be absent on the only two machines the
row is ever read on. Runtime resolution from the checkout is the mechanism;
`BuildSource.STAMPED` is a **declared seam** — an enum member with no branch
behind it — for the day a release pipeline exists.

`/v1/version` grows from three keys to six. `build_source` and `version_source`
are **always present and never null**; only `build_hash` is nullable.

- `src/localmail/build_report.py` — lazy, process-cached, never raises. Not at
  import: that path is taken by all 38 CLI commands and a `git` subprocess there
  can hang on a stale mount (#296's scenario). Caching also gives the row the
  semantics it wants — pinned for the process, so it reports what the daemon is
  *running*, not what the tree says now.
- The **identity guard** requires `<toplevel>/src/localmail/__init__.py` to
  resolve to the imported file. Containment is not enough: a virtualenv inside a
  dotfiles repo *is* contained, and would report that project's SHA as ours.
- **`-dirty` measures tracked files only.** A marker always on carries no
  information.
- **Wire strings declared, never derived** — `VersionSource` gained a
  `wire_name` because its own values are hyphenated debugging aids while this
  API's wire enums are underscored.
- **The diagnostic text stays off the wire** (`/v1/version` is unauthenticated
  and that string carries errno values and paths since #303), enforced by an
  exact-key-set assertion so no *future* key can leak either.
- **#300's CLI half needed no flag** — stderr is non-empty iff the version is
  unresolvable; now stated in README and pinned.

**Two defects the review caught mid-flight, both of which would have shipped:**

1. **`.split()` on `git rev-parse --show-toplevel --short HEAD`** splits on any
   whitespace, but that output is two *lines* whose first is a path — so a
   checkout under a directory containing a space reported a healthy tree as
   `GIT_FAILED`. Reproduced against a real spaced path; fixed in **both** places
   the plan had duplicated it.
2. **The second git guard was unverified.** Every failure-injection test
   monkeypatches `subprocess.run` globally, so the *first* call fails and the
   function returns before the dirty probe is reached — leaving a guard unpinned
   in a module whose first rule is that it never raises.

### Verification (this Mac, all extras)

- `uv run pytest -q` → **2586 passed, 0 skipped** (171 s), 3 warnings (all
  pre-existing).
- **Both refs measured in this session** (risk 5): `main` **2538** → branch
  **2580** collected (+42; 2586 after the final fix wave's extra parametrize).
- `mypy src/localmail` → Success, **143** source files.
- `ruff check` clean on changed files; repo-wide `src/localmail/` unchanged at
  **10** pre-existing (#285).
- `npm run check` 0 errors / 325 files · `npm test` **408** · `npm run build` ok
  · `cargo test` **104** · both clippy invocations clean.
- End-to-end: `resolve_build_info()` → `63eb7e2`, matching
  `git rev-parse --short HEAD`.
- **PR #315 CI: all five checks pass** — pytest on **both** interpreter legs
  (3.12 and 3.13, #314's new matrix), svelte-check + vitest, and cargo on both
  runners.

### Host health

**Mac** — launchd `running`, pid 53418, 7 heartbeats. `search-status` **0.98 s**,
partition holds: `blobs_eligible 9506 = 9218 + 106 + 182 + 0`, claimable 0.
**DGX** — all three units `active`, and it had **deployed itself to `fb48f23`**
between handoffs; it is now behind by #314 and #313.

**Dependabot: 0 open alerts. 15 open issues** (13 after #315 merges).

## What's next

### 0. **Merge PR #315** — the only open PR, CI green
   **You merge** (project convention). Closes #278 and #300.
   - **Acceptance:** on `main` afterwards, `GET /v1/version` returns six keys,
     and the GUI's About tab shows a real short SHA instead of `?`.
   - **Then deploy both hosts** — the DGX is two merges behind. Recipe in the
     resume block. **Use `--all-extras` / `--extra mcp --extra extraction`**,
     never a bare `uv sync`, and see risk 13 — the 3.13 pin makes this sharper
     than it was.
   - The Mac needs `git checkout main` — its launchd daemon runs an editable
     install off whatever the tree is checked out to (risk 12), and it is
     currently on `feat/build-provenance`.

### 1. **#305 — `--version` dies on a missing third-party dependency** *(carried)*
   `cli.py` imports the daemon (and so `sqlparse`, `psycopg`, `keyring`) at
   module scope, so a partial `uv sync` kills the one command an operator is told
   to run to verify an install, before click parses the flag. **Acceptance:**
   blocking `sqlparse` on `sys.meta_path` leaves `localmail --version` printing
   its line and exiting 0. **Do it with the `cli.py` refactor, not before.**

### 2. **#299 — two pre-existing flaky tests** *(carried)*
   Daemon lifecycle busy-guard and login audit rows. Neither appeared in this
   session's many full runs, which is consistent with flakiness and is **not**
   evidence they are fixed.

### 3. **#285 — ruff, repo-wide** *(carried)*
   **10** pre-existing `ruff check src/localmail/` errors (two are the `math`
   import/redefinition pair in `searcher.py`), 9 dead `# noqa: S608` directives,
   no `[tool.ruff]` config and no CI step. This session added a data point: an
   implementer ran `ruff format` on one new test file, and with no config it
   applied its own defaults — 60 of 152 test files use the resulting style, 92
   do not. **Decide the config and the CI step together.**

### 4. **Admin GUI phase 5 — Users & ACL panel** *(carried, still the design's next slice)*
   `/v1/admin/users` is already `require_admin()` (bearer-capable) — **no backend
   work needed.** Service layer:
   [src/localmail/api/admin/users.py](src/localmail/api/admin/users.py).
   **Acceptance:** a `UsersPanel.svelte` replacing the placeholder tab: list,
   create, delete, per-account ACL grant/revoke, `is_admin` toggle, password
   reset, enable/disable. Surface the **two lock-out guards as 409s**. Mirror
   [serve/admin/users_panel_router.py](src/localmail/serve/admin/users_panel_router.py),
   follow the Daemon-panel shape, and **stub the new API module in both
   `AdminView.test.ts` and `MainView.test.ts`** (risk 11).

### 5. **Remaining robustness backlog** *(carried)*
   **#218** (GUI download commands buffer the full body before enforcing the size
   ceiling) · **#226** (self-signed cert misses the reachable IP when `--bind
   0.0.0.0`) · **#225 / #227** (`/v1/changes` subscription lifecycle gaps) ·
   **#200 / #211 / #208** (admin panels silently swallow 4xx) · **#206** (GUI
   AccountForm: folder filters not editable) · **#204** (admin bearer-token
   scope) · **#25** (websockets DeprecationWarning).

### 6. **Smaller, deliberately not done** *(carried)*
   - **`cli.py` is ~2075 lines**; the refactor session 21 deferred is still owed
     in full, and **#305 depends on it**.
   - **165 docling failures on the Mac**, of 182 `blobs_gave_up`.
   - **Residual implausible language labels are dominated by `ja`** (~0.24%);
     the confidence-floor lever was measured useless. **Sample `ja` first.**
   - **The DGX drops remain uninvestigated** (risk 7).
   - **`git stash drop` the session-22 leftover** if you want it tidy — session
     26 verified its content is on `main`. Left because dropping is destructive.
   - **A stray SDD workspace** sits at `.superpowers/sdd/` (flat path, from an
     earlier session — task-10/11 briefs, unrelated SHAs). Git-ignored scratch;
     left alone deliberately, since another plan's workspace is never ours to
     delete.

## Open decisions & risks

1. **One PR is open and yours to merge.** `main` is `eec8e09`; **#315**
   (`feat/build-provenance`) closes #278 and #300, CI green. **15 open issues**,
   dropping to **13**. **Dependabot: 0.**
2. **Base every branch on `main`; put a session's code and its handoff in ONE
   PR** *(carried)*. Session 25's stacked handoff PR merged into an
   already-merged branch 13 seconds after it landed, and everything in it was
   lost with no failing check and nothing to notice.
   - **The stranded-branch check is NOISY**: `git log --oneline main..origin/<b>`
     is non-empty for **every** squash-merged branch. The signal is non-empty on
     a branch whose PR merged *recently*, confirmed by a **content** diff
     (`git diff --stat main origin/<b>` empty ⇒ it landed).
3. **`git` and `gh` are the authority on state, NOT this file** *(carried, now
   twice-earned)*. This session found the file a whole session stale, and `main`
   moved again *during* the session (#314 landed mid-flight). Open every session
   with `git fetch --prune && git log --oneline -1 origin/main`, `gh pr list`,
   `gh issue list`, and reconcile before acting.
4. **Verify host revisions; do not infer them** *(carried)*. The DGX moved to
   `fb48f23` between handoffs with no session doing it. One
   `ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'` settles it.
5. **Test-count baselines: measure both refs IN THE SAME SESSION** *(carried)*.
   `main` **2538**, branch **2580 → 2586**. A number quoted from a previous
   handoff is not a baseline. A fresh worktree needs `uv sync --all-extras`
   before it collects correctly.
6. **`--version`'s contract is six things, all pinned** *(carried)*: reads no
   config, touches no DB; stdout is the single machine-readable line; the
   diagnostic goes to stderr; exit stays 0; it survives an unreadable METADATA;
   and it is **not** a `log_version_diagnostic` caller. **Never reintroduce
   `@click.version_option` in any spelling** — an AST pin forbids it, covering
   `daemon_cli.py` too. **New:** stderr non-empty ⟺ unresolvable is now a
   *documented, pinned* contract, so do not move that line to stdout.
7. **The DGX drops are STILL UNEXPLAINED — five theories refuted** *(carried)*.
   **Do not propose a sixth without a captured outage in which the host was
   demonstrably up throughout.** Triage with `journalctl --list-boots` first.
   Power is not a candidate (~5-day UPS). **Do not edit `/etc/wireguard/wg0.conf`.**
8. **When reverting a mutation, restore from a file copy — never `git checkout`**
   *(carried, sessions 23–29)*. Used repeatedly this session, including by
   subagents who were told so explicitly. Treat **empty** pytest output as a
   failed mutation, not a pass.
9. **A negative assertion needs the module's own constant and a positive control**
   *(carried)*. `assert "cause:" not in body` cannot fail once the prefix is
   renamed — and can pass because the request 500'd. Both halves are now pinned
   in `test_serve_version_route.py`; copy that shape.
10. **`blobs_claimable 0` alongside `blobs_pending 0` is the SETTLED shape**
    *(carried)*. A steady non-zero `blobs_no_text` is **normal** (#277);
    **`blobs_gave_up` is the one to act on**. `DISTINCT` in
    `EXTENSION_MATCH_JOIN_SQL` is load-bearing with no runtime guard.
11. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`, or an
    unhandled promise rejection leaks while vitest still reports "passed".
12. **The Mac's launchd daemon runs an EDITABLE install** *(carried)*. It
    executes whatever the tree is checked out to. **The tree is currently on
    `feat/build-provenance`** — check it back out to `main` after merging.
13. **`uv sync` without extras silently downgrades a host — and #314's 3.13 pin
    made this LIVE** *(carried, sharpened)*. A bare `uv run` on a host whose
    venv predates the pin **rebuilds it for 3.13 without extras**, stripping
    docling/mcp/extraction from the environment the daemon executes on its next
    restart. Observed and repaired on this Mac this session. Use
    `uv sync --all-extras` (Mac) / `--extra mcp --extra extraction` (DGX). **`uv`
    is not on the DGX's non-interactive PATH** — use `~/.local/bin/uv`. A
    non-zero `skipped` count means an extra went missing; the name to look for is
    `test_extractor.py::test_pdf_pipeline_options_reflect_the_configured_engine`.
    `rapidocr` missing on darwin is **correct** — `ocrmac` is the macOS engine.
14. **Do not run the test suite while a backfill is draining**, and never two
    suites in parallel against the same Postgres *(carried)*.
15. **`search-status` is sub-second on BOTH hosts** *(carried)*. Mac 0.98 s. If
    it runs long that is a **regression** of #280 — check `EXPLAIN (FORMAT JSON)`
    for a `Seq Scan on messages` under a `SubPlan` first.
16. **The macOS socket deselect is GONE** *(carried)*. `uv run pytest -q` with
    **no arguments** is the right command.
17. **An empty `daemon_heartbeats` right after a restart is normal for minutes**
    *(carried — #269/#271)*: the startup blob-temp sweep. Grep for
    **`blob-temp sweep done: walked=`**.
18. **The stale NOTIFY queue did not recur this session** *(carried)*. Both gates
    read healthy. Session 26 found it **asymmetric** —
    `pg_notification_queue_usage()` healthy while `LISTEN daemon_commands`
    errored. **Verify both gates, never one.**
19. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    Clear deliberately. **#266's whitespace-heal is a one-way door too** — what
    makes it safe is the `is_blank` gate, not the nature of the data.
20. **`--relabel` is the only destructive verb in the lang path** *(carried)*.
    Reach for `--retry-declined` first. `reopen_all`'s bulk UPDATE shows no
    progress in `pg_stat_activity` until it commits — do **not** cancel it.
21. **`ExtractorConfigurationError` subclasses `TransientExtractorError`, and that
    subclassing *is* the #248 fix — do not "clean it up"** *(carried)*.
22. **`run_embed_worker_once`'s `failure_log` defaults to the process-wide
    `embed_worker._FAILURE_LOG`, and that default is the fix** *(carried — #267)*.
23. **No ROADMAP.md** *(carried, re-confirmed)* — that `/nextsession` step is a
    no-op. **README and CLAUDE.md were both updated** this session.
24. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    kept though #245 is fixed; #246 warns on group-write; `InsecureSecretsFile`
    refuses; #239's manual tombstone retention deliberate; admin bearer tokens
    have no per-token scope (#204).
25. **Run vitest from `gui/`, not the repo root** *(carried)*. `cargo clippy
    --all-targets` is clean but **ungated** — CI runs clippy without it, so
    `#[cfg(test)]` modules are never linted.
26. **Do not "tidy up" `_PRE280_CORRELATED_ALLOWLIST_SQL`** *(carried)* — it is
    the negative control proving the plan assertions can fail.
27. **A paging client must treat 409 and 400 differently** *(carried — #311)*.
    409 is recoverable; 400 is **permanent for that cursor** and must retire it.
    **Never state a `sort` on a request that carries a cursor.**
28. **`/v1/version` is unauthenticated, so identifiers only** *(new — #278/#300)*.
    `build_hash` and both source enums are safe; the diagnostic **text** is not
    (errno values and filesystem paths since #303). The exact-key-set assertion
    in `test_serve_version_route.py` is what stops a *future* key leaking too —
    do not relax it to a subset check. The SHA being public is an **accepted
    risk**, recorded in the design doc with its revisit trigger.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status && git branch --show-current
git log --oneline -1 origin/main         # THE authority, not this file's header
git log --oneline main..origin/main      # non-empty = a session landed since

# RISK 2 — stranded-branch SHORTLIST (noisy: every squash-merged branch appears).
# Only act on a branch whose PR merged recently, and confirm with a CONTENT diff:
for b in $(git branch -r --format='%(refname:short)' | grep -v 'origin/main\|HEAD'); do
  n=$(git log --oneline main.."$b" | wc -l); [ "$n" -gt 0 ] && echo "$b: $n commits not on main"
done
git diff --stat main origin/<branch>     # EMPTY = landed, not stranded

# ONE PR is open, CI green, awaiting your merge (What's next, 0).
gh pr list
gh pr checks 315
gh pr view 315 --json baseRefName --jq .baseRefName   # MUST be "main"
gh issue list --limit 30                 # 15 open; the merge closes #278, #300
gh api repos/hherb/localmail/dependabot/alerts \
  --jq '[.[] | select(.state=="open")] | length'      # expect 0

# AFTER MERGING — note the tree is currently on feat/build-provenance (risk 12):
#   Mac:  git checkout main && git pull && unset VIRTUAL_ENV && uv sync --all-extras
#   DGX:  ssh 10.0.0.3 'cd ~/src/localmail && git pull && ~/.local/bin/uv sync --extra mcp --extra extraction && ~/.local/bin/uv run localmail init-db && systemctl --user restart localmail-daemon localmail-serve'
# Acceptance — a real SHA, matching the checkout:
unset VIRTUAL_ENV && uv run python -c "
from localmail.build_report import resolve_build_info; print(resolve_build_info())"
git rev-parse --short HEAD               # must match the hash above
# And the #307 acceptance — help stays quiet even on a broken install:
unset VIRTUAL_ENV && uv run localmail sync --help 2>&1 | grep -c "could not be determined"   # 0

# Python suite. No --deselect (risk 16). NEVER a bare `uv sync` (risk 13).
unset VIRTUAL_ENV && uv sync --all-extras
unset VIRTUAL_ENV && uv run pytest -q
#   expect 2586 passed, 0 skipped on this branch; 2538 collected on main.
#   MEASURE BOTH REFS IN THIS SESSION (risk 5) — no DB needed:
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2
unset VIRTUAL_ENV && uv run mypy src/localmail    # expect Success, 143 files

# If EXACTLY the three LISTEN/NOTIFY tests fail, it is the stale queue (risk 18).
# CHECK BOTH GATES — session 26's recurrence had gate 1 reading healthy:
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'
# Remedy (runbook Option A) — verify the gates WHILE the daemon is down:
#   launchctl bootout gui/$UID/com.localmail.daemon
#   until ! launchctl print gui/$UID/com.localmail.daemon >/dev/null 2>&1; do sleep 2; done
#   <re-run both gates>
#   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.localmail.daemon.plist

# Host health (Mac):
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 17)

unset VIRTUAL_ENV && uv run localmail search-status    # UNDER A SECOND (risk 15)
#   Check the SHAPE, not the literals: the four buckets must sum to
#   blobs_eligible, and claimable must equal pending.
#   Mac ~9.5k = ~9.2k + 106 + 182 + 0, claimable 0

# The DGX — behind by #314 and #313 as of this session (risk 4):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — REQUIRED if you touch gui/ (MUST run from gui/ — risk 25):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
#   expect: 0 svelte-check errors, 408 vitest, 104 cargo
```

`main` tip is **`eec8e09`**. This session merged **#313** (`82513a0`) and left
**#315** open on `feat/build-provenance` (19 commits, head `63eb7e2`, CI green).
Latest migration **`0035_messages_body_lang_attempted_at.sql`**; next free slot
`0036_*.sql` (this session adds none). **Open issues: 15**, dropping to **13** on
merge. **Dependabot: 0 open alerts.**
