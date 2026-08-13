# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-13 (session 26).** Session 25's PR #297 was **merged by
> the operator**, so `main` moved `29f5fae` → **`cb77108`** and #295/#296 closed.
>
> **This session recovered work that had been silently lost.** Session 25's
> *review round* — four closed gaps, 14 tests, the README and CLAUDE.md updates,
> and the session-25 handoff itself — never reached `main`. It merged into a
> branch that had already been merged, 13 seconds after the merge that stranded
> it. No failing check, no open PR, nothing to notice. See §1.
>
> **The mechanism is the lesson, and it is now a convention in CLAUDE.md.**
> Session 25's handoff PR (#298) was based on the **fix branch**, not on `main`.
> When #297 merged the fix branch to `main` at 10:18:30 and #298 merged into that
> same fix branch at 10:18:43, everything in #298 landed somewhere nothing would
> ever merge again. **Base every branch on `main`; put a session's code and its
> handoff in ONE PR.** See risk 2 — it is the highest-value item in this file.
>
> **The recovered code is not incidental.** It fixes two places the #295 fix did
> not reach — `serve` was still silent whenever Postgres was unreachable, and
> `run --log-level ERROR` filtered the diagnostic out entirely — plus two pins
> that were silently green. Both were re-verified end-to-end here, including a
> side-by-side against `main` proving the gap is real (§1b).
>
> **The stale NOTIFY queue was back**, in session 22's asymmetric form: gate 1
> read healthy while gate 2 errored outright. Cleared with the runbook's Option
> A. Risk 18 — **check both gates, never one.**

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

### 1. The stranded session-25 review round, recovered onto `main`

Branch `fix/session25-review-round`, commits `dcd54dd` (the recovery) + `04ff86e`
(the convention + this handoff). **Open as PR #301, base `main`, CI green
(3m23s), unmerged.** No migration, no new dependency, no config change. **This is
not new work** — it is already-reviewed, already-CI-green work that had no path
to `main`.

**How it was found.** `NEXT_SESSION.md` on `main` was session **24**'s, while
`git log` showed session 25's fix already merged. `git branch -r` then showed
`origin/fix/295-296-version-diagnostic-reach` sitting **two commits ahead of
`main`**, the second of which (`562f7c9`) carried the whole review round.

**How it was lost**, from the API rather than inference:

| PR | head | base | merged at |
|---|---|---|---|
| #297 (the fix) | `fix/295-296-version-diagnostic-reach` | **`main`** | 10:18:30 |
| #298 (review round + handoff) | `docs/session-25-handoff` | **`fix/295-296-…`** | 10:18:43 |

#298's base was the branch #297 had just consumed. Squash-merging #297 put the
*pre-review* fix on `main`; #298 then merged into the dead branch. `gh pr list`
was empty, CI was green, and the branch read "merged" — the loss had no symptom.

**Recovery.** `git cherry-pick 562f7c9` onto `main` applied **cleanly** (`main`
already held the fix commit's content via the squash, so the cherry-pick is
exactly the review round's delta: 12 files, +1387/−534).

### 1a. What the recovered round actually fixes — two gaps, two dead pins

Worth reading before assuming this is docs-only. Each was mutation-proven by
session 25 and re-verified here.

- **`serve` was still silent whenever Postgres was unreachable.** `create_app`
  really is the first thing inside `create_app`, but `serve_cmd` raises out of
  `pending_migrations` **long before reaching it** — which is the half of #295 a
  headless host is most likely to hit. It reports before its schema check now,
  deduped per process (`_REPORTED`, the `embed_worker._FAILURE_LOG` shape) so
  the two layers never print the same line twice.
- **`run --log-level ERROR` filtered the diagnostic out entirely.** That level is
  an offered `click.Choice`, and `run_cmd` calls `basicConfig` with it *before*
  constructing the daemon — and that root handler also removes the
  `logging.lastResort` escape that was saving the serve path. Reported at ERROR
  now, pinned by a test that reads the choices **off `run_cmd` itself**, so
  adding a quieter one fails there rather than silently reopening the hole.
- **The remedy could vanish and nothing noticed.** Both assertions on the
  rendering were containment checks, so returning the `cause:` line *alone*
  passed all 49 tests — and since `detail` is always set on that path, that is
  the only string a #296-affected operator ever sees. Pinned as a relation now.
- **Neither startup reader was proven to read the package's diagnostic.** Both
  reach-tests rebind the reader's own module global, which a module-local `None`
  satisfies, so both could ship permanently silent with the suite green — the
  #278 shape. Closed with an AST pin, since an identity check passes on a healthy
  install where both sides are `None`.

Two correctness fixes fall out of the same review:

- **The unreadable remedy asserted a faulty filesystem for everything the broad
  catch sees.** `MemoryError`, `RecursionError` and third-party `sys.meta_path`
  failures all reach it — that wording sends an OOMing host to `fsck` a healthy
  volume, the module's own "the remedies differ" principle inverted at the point
  #296 added a cause. It defers to the cause line now, which carries
  **`format_exception_only`** rather than `type(exc).__name__`: a bare `OSError`
  cannot separate EIO from ESTALE from EACCES, and the type name also lost the
  filename and the decode offset #296's own reproduction turns on. It retains
  **no frames**, which matters for a value that becomes a module global.
- **`ResolvedVersion` gains the guard `VersionSource` already had one layer up.**
  `unresolvable(METADATA_UNREADABLE)` was reachable and rendered a remedy with no
  cause. The rendering rule moves onto `.unreadable(exc)` so a second catch
  cannot re-decide it.

### 1b. Re-verified end-to-end here, including the negative control

The corrupt-metadata repro (one latin-1 byte in a
`localmail-9.9.9.dist-info/METADATA`, placed ahead on `sys.path`):

```
$ PYTHONPATH=repro uv run localmail --version
localmail, version 0.0.0+unknown
warning: the localmail version could not be determined — reading its distribution metadata raised.
  remedy: read the cause below first. … For an OSError, check the filesystem under site-packages before anything else …
  cause: UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe9 in position 65: invalid continuation byte
exit=0
```

The `cause:` line now carries the **decode offset**; before the review round it
was the bare string `UnicodeDecodeError`.

Then the `serve`-before-schema-check half, run **on both trees** against an
unreachable Postgres on the same corrupt tree:

- **this branch** — emits the three-line warning, *then* `Error: could not check
  schema: …`.
- **`main`, in a throwaway worktree** — emits `Error: could not check schema: …`
  and **nothing else**. Silent, exactly as §1a describes.

That is the negative control. The gap is real on `main` today, not hypothetical.

### 2. The stale NOTIFY queue, cleared (host maintenance, no code)

The first full-suite run failed **exactly the three** documented LISTEN/NOTIFY
tests with `could not access status of transaction 3080374996`. This is the
condition in [docs/operations/postgres-maintenance-runbook.md](docs/operations/postgres-maintenance-runbook.md),
**in session 22's asymmetric form**:

```
pg_notification_queue_usage()  -> 9.5367431640625e-07   # reads healthy
LISTEN daemon_commands         -> ERROR: could not access status of transaction
                                  DETAIL: Could not open file "pg_xact/0B79"
```

Runbook Option A: `bootout` → **wait until `launchctl print` says the service is
gone** → verify **both** gates (`0` and `LISTEN`) → `bootstrap`. The three tests
then passed in 1.65 s and the full suite came back clean. Daemon is pid
**53418**; 7 fresh heartbeats after the sweep.

### 3. Verification (this Mac, all extras)

- `unset VIRTUAL_ENV && uv run pytest -q` → **2419 passed, 0 skipped, 0 failed**
  in 152 s. `main` collects **2405**. **+14 measured**, not subtracted
  (`--collect-only` on both refs).
- `unset VIRTUAL_ENV && uv run mypy src/localmail` → **Success, 141 source
  files**.
- `ruff check` clean on all 8 changed files. Repo-wide `src/localmail/` still
  **10** pre-existing errors (#285); `noqa: S608` still **9**. No new ones.
- `uv run localmail --version` → `localmail, version 0.3.0`, exit 0, **stderr 0
  bytes**.
- `search-status` **0.967 s**; partition sums (below).

### 4. Host health confirmed, not assumed

**Mac** — both launchd agents `running`/`active`, 7 fresh heartbeats, all under
30 s. `search-status` 0.967 s:

```
messages_total 127771
blobs_eligible 9499 = extracted 9211 + no_text 106 + gave_up 182 + pending 0
blobs_claimable 0        body_lang_pending 0, declined 12188
```

**DGX** — all three units `active`; `git log --oneline -1` → **`29f5fae`**,
i.e. **two commits behind `main`** (verified over SSH, not inferred — risk 3).
It does **not** have #296's fix. Deploy recipe in the resume block.

**Dependabot: 0 open alerts.** **14 open issues.**

### 5. A leftover stash, checked and harmless

`git stash list` carries `stash@{0}: On docs/session-22-handoff: review-fixes`.
Checked rather than assumed: `git diff --stat stash@{0} main -- <its 3 code
files>` is **empty**, so its content is already on `main` (it is the #280/#284
review round, landed as `bc5b556`). Safe to `git stash drop`; left in place
because dropping is destructive and is the operator's call.

### 6. Docs

- **CLAUDE.md** — the recovered review round's `version_report` corrections (the
  broad catch's wording, `format_exception_only`, the `ResolvedVersion` guard,
  the three-attribute export), **plus a new Conventions rule this session added**
  recording the stranding mechanism and the structural check for it (risk 2).
- **README** — rides along with the recovery: the third cause now tells the
  operator to read the `cause:` line first and why, and the startup-log paragraph
  says **ERROR** and why (`run --log-level ERROR` is a supported choice, and a
  report you can be configured out of is not a report).
- **No ROADMAP.md** (confirmed absent again; that `/nextsession` step stays a
  no-op).

## What's next

### 0. **Merge PR #301** — the only open PR
   Green and unmerged; **the operator merges** (project convention). It closes no
   issue; it restores work `main` silently lost.
   - **Acceptance:** on `main` afterwards, `uv run pytest --collect-only -q`
     reports **2419** (it reports 2405 today), and the §1b negative control
     inverts — `serve` against an unreachable Postgres on a corrupt-metadata tree
     prints the warning *before* the schema error instead of nothing.
   - **Then deploy the DGX** — it is at `29f5fae`, two commits behind before this
     merge and three after. Recipe in the resume block.
   - The Mac needs `git checkout main && git pull && uv sync --all-extras`. Note
     its launchd daemon runs `/Users/hherb/src/localmail/.venv/bin/localmail`, an
     **editable** install — so it executes whatever the working tree holds. It
     was restarted this session while the tree was on the branch; harmless (the
     branch and `main` behave identically on a healthy install), but it is why
     checking the tree back out to `main` matters.

### 1. **#300 — an unresolvable version has no machine-readable channel** *(new, filed by session 25's review)*
   The version cluster's remaining live thread and the natural successor to
   #295/#296: the diagnosis is now legible to a **human** on every entry point
   and to a **machine** on none. `--version` exits 0 on the unknown path (a
   deliberate, pinned decision — risk 7) and `/v1/version` ships the sentinel
   unflagged. This is also the standing reason `__version_source__` is retained
   despite having no production reader. **Read #295's precedent first:** its wire
   question was decided *against* adding a field, citing #278 as the cautionary
   case, so this needs a decision rather than an implementation.

### 2. **#299 — two pre-existing flaky tests** *(new, filed by session 25's review)*
   Daemon lifecycle busy-guard and login audit rows; **confirmed flaky on `main`**,
   so not caused by any recent change. Neither appeared in this session's two full
   runs, which is consistent with flakiness and is not evidence they are fixed.

### 3. **#278 — the version surface's other half** *(carried; needs YOUR decision first)*
   The GUI About tab renders a `build_hash` that `/v1/version` has **never**
   emitted, so "Server build" always shows `?`, while five test files mock the
   field and make it look covered. **Two options, product call before any code:**
   emit a real build hash (from git, at build time — but the version is stamped at
   *install* time, so this needs a story for `uv tool install` from a tarball), or
   delete the field end-to-end including the five mocks. Deleting is cheaper and
   honest; emitting is more useful where `--version` alone cannot distinguish two
   builds of `0.3.0`.

### 4. **#285 — ruff, repo-wide** *(carried)*
   Still **9** dead `# noqa: S608` directives across 5 files, and **10**
   pre-existing `ruff check src/localmail/` errors; no `[tool.ruff]` config and no
   CI step. The recovered round leaves a **live divergence worth resolving with
   it**: `version_report.py`'s broad catch deliberately carries no `BLE001`
   suppression while fourteen sibling catches in `src/` do. The comment there now
   says explicitly that this is a divergence, not a settled convention — the rule
   is inert under the pinned ruff and live under newer ones. **Decide both
   together.**

### 5. **Admin GUI phase 5 — Users & ACL panel** *(carried, still the design's next slice)*
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
   `AdminView.test.ts` and `MainView.test.ts`** (see risk 13).

### 6. **Remaining robustness backlog** *(carried)*
   **#218** (GUI download commands buffer the full body before enforcing the size
   ceiling) · **#226** (self-signed cert misses the reachable IP when `--bind
   0.0.0.0`) · **#225 / #227** (`/v1/changes` subscription lifecycle gaps) ·
   **#200 / #211 / #208** (admin panels silently swallow 4xx) · **#206** (GUI
   AccountForm: folder filters not editable) · **#204** (admin bearer-token
   scope) · **#25** (websockets DeprecationWarning).

### 7. **Smaller, deliberately not done** *(carried)*
   - **`cli.py` is 1981 lines**, **`daemon.py` 580**, **`version_report.py` 343**
     — measured, not inferred (`main`: 1972 / 580 / 251). `cli.py` and `daemon.py`
     are both well over the 500-line guideline; the refactor session 21 deferred
     is still owed in full.
   - **165 docling failures on the Mac** (31 `File format not allowed`, 134
     `Input document … is not valid`), of 182 `blobs_gave_up`; **127** on the DGX.
   - **Residual implausible language labels are dominated by `ja`.** ~0.24% of
     labels; the confidence-floor lever was measured useless. If ever chased,
     **sample the `ja` rows first**.
   - **The DGX drops remain uninvestigated and unexplained** (risk 5).
   - **#269's suggestion 2** (blob-temp sweep off the critical path) — deferred in
     session 19; reopen only if cold-cache startups grow past tolerable.
   - **`git stash drop` the session-22 leftover** if you want it tidy (§5).

## Open decisions & risks

1. **One PR is open and yours to merge.** `main` is `cb77108`; **#301**
   (`fix/session25-review-round`, two commits) restores session 25's review
   round. **14 open issues**, unchanged by the merge (it closes none).
   **Dependabot: 0 open alerts.** `origin/fix/295-296-version-diagnostic-reach`
   can be deleted once #301 merges — until then it is the only other copy of
   this work.
2. **Base every branch on `main`; put a session's code and its handoff in ONE
   PR** *(new, and the reason this session exists)*. Session 25 based its handoff
   PR on the fix branch. The fix PR merged to `main`; the handoff PR merged into
   the now-dead fix branch **13 seconds later**; everything in it was lost. **The
   failure is silent by construction** — `gh pr list` empty, CI green, branch
   reads "merged". The structural check is
   **`git log --oneline main..origin/<branch>` after a merge**: a non-empty
   result on a branch whose PR is already merged *is* this bug. Now recorded in
   CLAUDE.md's Conventions. Related tell that caught it here: **`NEXT_SESSION.md`
   naming an older session than `git log` does.**
3. **Verify host revisions; do not infer them** *(carried, session 25)*. The DGX
   is at `29f5fae` — checked over SSH this session, and it is **two** commits
   behind, not the "one" a plan-shaped guess would give. One `ssh 10.0.0.3 'cd
   ~/src/localmail && git log --oneline -1'` settles it. Same family as risk 6.
4. **`--version`'s contract is five things, all pinned** *(carried)*. It must
   (a) read no config and touch no database; (b) keep **stdout** to the single
   machine-readable line; (c) put any diagnostic on **stderr**; (d) **exit 0**
   even when the version is unknown; and (e) **still work on a tree whose
   METADATA cannot be read at all** — #296, the case where it matters most. **Do
   not reintroduce `@click.version_option` in any spelling** — the AST pin forbids
   it, and that pin covers `daemon_cli.py` too. The startup readers log at
   **ERROR**, not WARNING, because `run --log-level ERROR` is a supported choice.
5. **The DGX drops are STILL UNEXPLAINED — five theories refuted** *(carried; not
   investigated this session)*. **Do not propose a sixth without a captured
   outage in which the host was demonstrably up throughout.** Triage with
   `journalctl --list-boots` first. **Power is not a candidate** (~5-day UPS).
   **Do not edit `/etc/wireguard/wg0.conf`.** `10.0.0.3` worked first try again
   this session. Session 19 established that the LAN address answers ping and
   refuses SSH, so a green `lan=` probe line is *not* evidence it is the DGX.
   **Try `10.0.0.3` first.** A single `tunnel=FAIL` probe sample is not an outage.
6. **Test-count baselines: measure, don't subtract** *(carried)*. `main` →
   **2405**, this branch → **2419**. Cheap and DB-free:
   `uv run pytest --collect-only -q | tail -2`, run on **both** refs.
7. **A merged PR is not proof its content reached `main`** *(new, the general
   form of risk 2)*. `gh pr view N --json state` says `MERGED` for a PR merged
   into *any* base. **Check `baseRefName`.** In this repo the only correct base
   for a session branch is `main`.
8. **`search-status` is sub-second on BOTH hosts — stop budgeting minutes**
   *(carried)*. Mac **0.967 s** this session. If it ever runs long that is a
   **regression** of #280: check `EXPLAIN (FORMAT JSON)` for a `Seq Scan on
   messages` under a `SubPlan` before looking anywhere else.
9. **When reverting a mutation, restore from a file copy — never `git checkout`**
   *(carried, sessions 23–25)*. An uncommitted fix lives only in the working tree.
   Treat **empty** pytest output as a failed mutation, not a pass.
10. **An absence assertion needs the constant, not a literal** *(carried)*.
    `assert "cause:" not in rendered` cannot fail once the prefix is renamed.
    Assert against the module's own constant and put a positive control beside it.
11. **`blobs_claimable 0` alongside `blobs_pending 0` is the SETTLED archive
    shape, on both hosts** *(carried)*. **A steady non-zero `blobs_no_text` is
    NORMAL** (#277) — terminal by design, read it like `body_lang_declined`.
    **`blobs_gave_up` is the one to act on** — `list-failed-extractions` says why
    (poison-pill half only), `retry-failed-extractions` re-queues.
    **`QueueCounts.__post_init__` raises on two distinct conditions** (#284):
    `misfiled` is checked **before** the sum, deliberately. A fifth disposition
    goes in `BUCKET_WHERE_SQL`; everything else derives. **`DISTINCT` in
    `EXTENSION_MATCH_JOIN_SQL` is load-bearing with no runtime guard** — its only
    symptom is `pending` diverging from `claimable`.
12. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    Clear deliberately: `DELETE FROM attachment_text WHERE extractor =
    'type-skipped'`. **#266's whitespace-heal is a one-way door too** — what makes
    it safe is the `is_blank` gate, not the nature of the data.
13. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`, or an
    unhandled promise rejection leaks while vitest still reports "passed".
14. **`--relabel` is the only destructive verb in the lang path** *(carried)*.
    Discards **every** label; archive unsearchable by `lang:` until the drain
    completes. Prompts unless `--yes`. Reach for `--retry-declined` first. Budget
    ~45 min for a 100k-row archive, and note that **`reopen_all`'s bulk UPDATE
    shows no progress in `pg_stat_activity` until it commits** — tens of minutes
    of apparent hang is expected; do **not** cancel it. `body_lang_pending` means
    claimable work only; a steady non-zero `declined` is **normal** (12,188 Mac,
    ~1,974 DGX). **Do not add normalisation steps to `lang_text.py` without a
    measurement** — every candidate beyond URL-stripping measured zero.
15. **Do not run the test suite while a backfill is draining** *(carried)*.
    Shared-cluster contention produces dozens of false failures. `search-status`
    does not qualify (risk 8).
16. **The macOS socket deselect is GONE — stop using it** *(carried)*. #276 fixed
    it; `uv run pytest -q` with **no arguments** is the right command.
17. **An empty `daemon_heartbeats` right after a daemon restart is normal for
    minutes** *(carried — #269/#271)*. It is the startup blob-temp sweep. Grep for
    **`blob-temp sweep done: walked=`**. This session's restart repopulated all 7
    rows well inside a minute, so a long wait is not guaranteed either way.
18. **The stale NOTIFY queue RECURRED this session — and asymmetrically**
    *(carried, and re-confirmed the hard way)*. Treat "clear at the last handoff"
    as worth nothing. **Verify both gates, not one:**
    `pg_notification_queue_usage()` read `9.5e-07` — healthy — while `LISTEN
    daemon_commands` errored outright. Fix is the runbook's Option A: `launchctl
    bootout gui/$UID/com.localmail.daemon`, **wait until `launchctl print` says
    the service is gone**, verify both gates read `0` and `LISTEN`, then
    `bootstrap` back. Gate the pytest re-run on the probes, not on a fixed wait.
19. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `--all-extras` on the Mac and `--extra mcp --extra extraction` on the DGX.
    **`uv` is not on the DGX's default non-interactive PATH** — use
    `~/.local/bin/uv` over SSH. **A non-zero `skipped` count means an extra went
    missing.** CI installs only `--extra mcp`, so its count differs by design.
    **The skip to look for by name** is
    `tests/test_extractor.py::test_pdf_pipeline_options_reflect_the_configured_engine`
    (the #248 OCR-engine pin). The Mac ran **0 skipped** this session.
20. **`ExtractorConfigurationError` subclasses `TransientExtractorError`, and that
    subclassing *is* the #248 fix — do not "clean it up"** *(carried)*.
21. **`run_embed_worker_once`'s `failure_log` defaults to the process-wide
    `embed_worker._FAILURE_LOG`, and that default is the fix** *(carried — #267)*.
    **A new looping caller should pass nothing.**
22. **No ROADMAP.md** *(carried, re-confirmed)* — the `/nextsession` ROADMAP step
    is a no-op; slice status lives here + `docs/handoffs/` + the specs. **README
    was updated** this session, via the recovered round.
23. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    kept though #245 is fixed; #246 warns on group-write; `InsecureSecretsFile`
    refuses; #239's manual tombstone retention deliberate; admin bearer tokens
    have no per-token scope (#204).
24. **Run vitest from `gui/`, not the repo root** *(carried)*. **`cargo clippy
    --all-targets` is clean but ungated** — CI runs clippy without
    `--all-targets`, so `#[cfg(test)]` modules are never linted.
25. **Do not "tidy up" `_PRE280_CORRELATED_ALLOWLIST_SQL`** *(carried; #292 closed
    in session 24 confirming the note is durable)*.
    [tests/test_extract_queue_sql.py:306](tests/test_extract_queue_sql.py#L306)
    holds the pre-#280 correlated predicate **on purpose**, as the negative
    control that proves the plan assertions can fail. It is consumed by two named
    tests, so deleting it is a `NameError`, not a silent loss.
26. **The Mac's launchd daemon runs an EDITABLE install** *(new)*.
    `/Users/hherb/src/localmail/.venv/bin/localmail` resolves through `src/`, so
    the running service executes **whatever the working tree is checked out to**.
    Leaving the tree on a feature branch silently runs that branch in production.
    Check the branch out back to `main` after merging.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status                               # expect clean
git branch --show-current
git log --oneline origin/main..main      # expect 0
git log --oneline main..origin/main      # expect 0 — NON-empty means a session
                                         #   landed after this handoff was written

# RISK 2 — the stranded-branch check. For every branch whose PR shows MERGED,
# a non-empty result here means its content did NOT reach main:
for b in $(git branch -r --format='%(refname:short)' | grep -v 'origin/main\|HEAD'); do
  n=$(git log --oneline main.."$b" | wc -l); [ "$n" -gt 0 ] && echo "$b: $n commits not on main"
done
# And confirm a merged PR's base was main, not another branch:
gh pr view <N> --json state,baseRefName --jq '{state,base:.baseRefName}'

# ONE PR is open, awaiting your merge (What's next, 0).
gh pr list
gh pr checks 301
gh pr view 301 --json baseRefName --jq .baseRefName   # MUST be "main" (risk 7)
gh issue list --limit 20                 # 14 open; the merge closes none

gh api repos/hherb/localmail/dependabot/alerts \
  --jq '[.[] | select(.state=="open")] | length'          # expect 0

# AFTER MERGING:
#   Mac:  git checkout main && git pull && unset VIRTUAL_ENV && uv sync --all-extras
#         (checking the tree back out to main MATTERS — risk 26)
#   DGX:  ssh 10.0.0.3 'cd ~/src/localmail && git pull && ~/.local/bin/uv sync --extra mcp --extra extraction && ~/.local/bin/uv run localmail init-db && systemctl --user restart localmail-daemon localmail-serve'
# Acceptance on BOTH hosts — version line, exit 0, and NOTHING on stderr:
unset VIRTUAL_ENV && uv run localmail --version 2>/tmp/v.err; echo "exit=$?"; wc -c < /tmp/v.err   # expect 0

# Python test suite. No --deselect (risk 16).
# Do NOT run while a backfill is draining (risk 15).
unset VIRTUAL_ENV && uv sync --all-extras   # NOT a bare `uv sync` — risk 19
unset VIRTUAL_ENV && uv run pytest -q
#   expect: 2419 passed, 0 skipped on fix/session25-review-round; 2405 on main.
#   MEASURE, don't subtract (risk 6) — no DB needed:
unset VIRTUAL_ENV && uv run pytest --collect-only -q | tail -2

unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 141 source files

# If EXACTLY the three LISTEN/NOTIFY tests fail, it is the stale queue (risk 18).
# CHECK BOTH GATES — this session's recurrence had gate 1 reading healthy:
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'  # 0
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'            # LISTEN
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

# The attachment counters — UNDER A SECOND on both hosts (risk 8):
unset VIRTUAL_ENV && uv run localmail search-status
#   These MOVE as the archive grows, so check the SHAPE, not the literals: the
#   four buckets must sum to blobs_eligible, and claimable must equal pending.
#   Mac  approx: blobs_eligible ~9.5k = ~9.2k + 106 + 182 + 0, claimable 0
#   DGX  approx: blobs_eligible ~4.4k = ~4.1k +  91 + 127 + 0, claimable 0
# If it takes minutes again that is a REGRESSION of #280:
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT now()-query_start, left(query,60) FROM pg_stat_activity
    WHERE datname='localmail' AND state='active' AND pid <> pg_backend_pid()"

# The DGX — at 29f5fae, TWO commits behind main before this merge (risk 3).
# Use the WireGuard address; uv is not on its non-interactive PATH (risks 5, 19):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'   # expect 29f5fae until deployed
# Its Postgres is on 5532 too, but needs PGPASSWORD (it is not in ~/.pgpass):
ssh 10.0.0.3 'PGPASSWORD="local@@mail" psql -h 127.0.0.1 -p 5532 -U localmail -d localmail \
  -tAc "SELECT worker_kind, state, round(extract(epoch from now()-last_heartbeat_at)) FROM daemon_heartbeats"'
#   expect 5 rows (one account), ages under ~60 s
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — only if you touch gui/ (MUST run from gui/ — risk 24):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

`main` tip is **`cb77108`** (PR #297). This session's work is **`dcd54dd`** +
**`04ff86e`** on `fix/session25-review-round`, **open as PR #301, base `main`, CI
green (3m23s), not merged** — the cherry-pick of the stranded `562f7c9`, plus a
CLAUDE.md Conventions rule and this handoff. Latest migration
**`0035_messages_body_lang_attempted_at.sql`**; next free slot `0036_*.sql` (this
session adds none). **Open issues: 14**, unchanged by the merge. **Dependabot: 0
open alerts.** The DGX runs `29f5fae` — **verified over SSH, not assumed**.
