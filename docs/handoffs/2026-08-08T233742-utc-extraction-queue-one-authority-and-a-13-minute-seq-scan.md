# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-09 (session 21).** A **single-issue session**: the
> operator picked **#277** off the carried robustness backlog. Fixed TDD-style
> on `fix/277-blobs-pending-counts-sentinels`, commit **`85a2fed`**, opened as
> **PR #281 — CI green, NOT merged**. `main` is still **`e471f25`**. One new
> issue filed: **#280** (the eligibility query is a 13-minute per-blob seq
> scan — measured, pre-existing, deliberately not fixed here).
>
> **Two pieces of good news that change the resume commands:**
> 1. **The stale-NOTIFY fault has cleared.** Both gates pass and the full suite
>    is **0 failed**. (Risk 2/16 of every handoff since session 17.)
> 2. **The macOS `test_daemon_control_socket.py` deselect is obsolete** — #276
>    fixed it. **Run the suite with no `--deselect` at all: 2318 passed.**
>
> **Read "What we shipped" section 0 first:** session 20 shipped the 0.3.0
> release and a version/socket fix and left **no handoff**, so the session-19
> document you may have read was two commits stale.

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

### 0. First — what session 20 landed and never wrote down

Nothing to do here; this exists so the next session doesn't re-derive it.
Two PRs merged after the session-19 handoff was frozen, with no handoff of
their own:

- **`94fcbb1` (PR #275)** — first tagged release, **0.3.0**. Python had sat at
  0.1.0 for 342 commits while the GUI tracked 0.2.0; both trees now carry one
  number. All three lockfiles regenerated rather than hand-edited.
- **`e471f25` (PR #276)** — two defects found preparing that release.
  **`tauri.conf.json`'s `version` key is now absent on purpose** (tauri-codegen
  falls back to `env!("CARGO_PKG_VERSION")`, so Cargo.toml alone drives it) and
  `localmail.__version__` derives from installed distribution metadata.
  `test_version_single_source.py` pins the three literals that genuinely cannot
  be collapsed (pyproject, Cargo.toml, package.json) against each other.
  **It also fixed the macOS AF_UNIX socket-path failures** — see risk 12.

Its review filed **#278** and **#279**, both still open (see What's next).

### 1. PR #281 → commit `85a2fed` — the extraction queue has one authority (fixes #277)

Branch `fix/277-blobs-pending-counts-sentinels`. **Open, CI green, unmerged.**
No migration, no new dependency, no config change.

`search-status` derived `blobs_pending = blobs_eligible - blobs_extracted`,
where *extracted* meant an `attachment_text` row with non-empty text. But
`_claim_batch` skips a blob the moment **any** `attachment_text` row exists —
so every blob the worker had already disposed of by writing an empty-text
sentinel (`type-skipped` #216, `lightweight-empty`, `size-skipped`, a
#266-healed row) counted as outstanding work **forever**. Blobs parked at a
retry cap (#153) were the same defect wearing a different row: they carry no
`attachment_text` row *at all*, so the subtraction never reached them either.

Measured on the live Mac archive (127,473 messages), before → after:

```
blobs_eligible   9490          blobs_eligible   9490
blobs_extracted  9202          blobs_extracted  9202
blobs_pending     288    ->    blobs_no_text     106
                               blobs_gave_up     182
                               blobs_pending       0
```

The queue really was empty. The 288 that never drained were 106 sentinels plus
182 capped-out failures — and `blobs_gave_up 182` equals `failed_extractions
182` exactly, i.e. **every** failed blob on this archive is at its cap (the 165
`docling: File format not allowed` rows carried in the backlog since session
17 are most of it).

This is the drift #251 found on the language half of the same command, so the
fix has the same shape: **one authority for the predicate, composed by both
sides.** New pure module
[src/localmail/search/extract_queue.py](src/localmail/search/extract_queue.py)
owns `CLAIMABLE_WHERE_SQL`, the `QUEUE_FROM_SQL` join shape it reads,
`ALLOWLISTED_WHERE_SQL`, and the one thin read `fetch_queue_counts`.
**`_claim_batch` now composes the same constants**, so the report cannot again
describe a queue the worker disagrees with.

Four buckets partition `blobs_eligible`, disjoint and jointly exhaustive:
`blobs_extracted` (a row with text), `blobs_no_text` (a row with `''`),
`blobs_gave_up` (no row, a retry budget exhausted), `blobs_pending` (no row,
still claimable). `QueueCounts.__post_init__` **raises** when they fail to sum.

**Three judgement calls a future session would plausibly undo — don't:**

- **`gave_up` is reported separately, not folded into `no_text`.** It is
  recoverable (`retry-failed-extractions`); `no_text` is the documented one-way
  door. Collapsing them tells the operator to act on rows that are finished and
  says nothing about the 182 actually waiting on them.
- **`blobs_extracted` is now scoped to allowlisted blobs**, where it used to be
  a global `attachment_text` count. That scoping is what lets the four sum. The
  two agreed at 9202 on the live archive — only an allowlist *narrowed* after
  extraction can separate them.
- **The sentinel breakdown is deliberately NOT in the payload.** A nested
  per-extractor map breaks `--format text`'s one-number-per-line shape; the
  docstring and README carry the one-line SQL instead.

### 2. Issue #280 filed — measured, not fixed

`search-status` takes **~14 minutes** on the Mac archive, and always has. The
allowlist half is a correlated `EXISTS` over `messages.attachments`;
`messages_attachments_gin` serves the containment predicate at cost **~42**
with a *constant* operand but is abandoned for a per-blob **`Seq Scan on
messages` at cost ~36,203** once the operand correlates on `b.sha256`.

Both forms were measured so the fix could not be blamed for it: **old
eligibility query alone 13:04**, **whole new command 14:07**, matching the
planner's 452,749 → 459,756. The added LEFT JOINs are index scans over ≤16k
rows. Decorrelating (scan `messages` once into a CTE of `(sha256, extension)`
pairs) is the obvious first move and needs no schema change.

### 3. Verification (this Mac, all extras)

- `unset VIRTUAL_ENV && uv run pytest -q` → **2318 passed, 1 skipped, 0
  failed**, **with no `--deselect`**. `tests/test_daemon_control_socket.py`
  passes 16/16 on macOS now.
- `unset VIRTUAL_ENV && uv run mypy src/localmail` → **Success, 140 source
  files** (139 + `search/extract_queue.py`).
- Both NOTIFY gates pass: `pg_notification_queue_usage()` → `0` **and** `LISTEN
  daemon_commands` on `localmail_test` succeeds.
- Every new test watched fail first. The two CLI tests failed as
  **`assert 4 == 0`** and **`assert 2 == 0`** against the pre-fix command — the
  defect itself, not a missing key.
- CI green on PR #281 (pytest, PG pg18, Python 3.12, 3m17s).

## What's next

### 0. **Merge PR #281, then deploy both hosts**
   The PR is green and unmerged; **the operator merges** (project convention).
   Neither host runs it, and the DGX is three commits behind besides.
   - **Mac**: `git pull`, `uv sync --all-extras`, `launchctl kickstart -k
     gui/$UID/com.localmail.daemon` (and the serve agent).
   - **DGX**: `git pull`, `~/.local/bin/uv sync --extra mcp --extra
     extraction`, `systemctl --user restart localmail-daemon localmail-serve`.
   - **Acceptance:** `localmail search-status` reports `blobs_pending 0` on the
     Mac with `blobs_no_text 106` / `blobs_gave_up 182`, and the four buckets
     sum to `blobs_eligible`. **Budget ~14 minutes for that one command and do
     not take it for a hang — that is #280** (watch it in `pg_stat_activity` if
     unsure). A daemon restart is *not* required for the counter — it is a CLI
     read — and `_claim_batch`'s rewrite is semantically identical to what is
     already deployed, so there is no urgency on the restart either.

### 1. **#280 — decorrelate the eligibility query** *(new, and the natural follow-on)*
   Everything needed is in the issue: both plans, both wall-clock timings, three
   options, and the invariant to preserve.
   **Acceptance:** `search-status` completes in seconds on the 127k archive;
   `blobs_eligible` and the four buckets are **unchanged** (9490 / 9202 / 106 /
   182 / 0 at filing time); the SQL stays in `search/extract_queue.py`.

### 2. **#279 and #278 — the version surface** *(carried from session 20's review)*
   - **#279** is close to a one-liner: `@click.version_option(__version__,
     package_name="localmail")`. The manual's *install-verification* step tells
     users to run `localmail --version`, which currently prints a usage error —
     the worst possible place for it. Also closes a real gap: on a daemon-only
     host the version is unobtainable without starting `serve`.
   - **#278** needs a product decision first: the GUI About tab renders a
     `build_hash` that `/v1/version` has **never** emitted, so the "Server
     build" row always shows `?` — while five test files mock the field and make
     it look covered. Either emit a build hash or delete the field end-to-end.

### 3. **Admin GUI phase 5 — Users & ACL panel** *(carried, still the design's next slice)*
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

### 4. **Remaining robustness backlog** *(carried)*
   **#218** (GUI download commands buffer the full body before enforcing the
   size ceiling) · **#226** (self-signed cert misses the reachable IP when
   `--bind 0.0.0.0`) · **#225 / #227** (`/v1/changes` subscription lifecycle
   gaps) · **#200 / #211 / #208** (admin panels silently swallow 4xx) ·
   **#206** (GUI AccountForm: folder filters not editable) · **#204** (admin
   bearer-token scope) · **#25** (websockets DeprecationWarning).

### 5. **Smaller, deliberately not done** *(carried)*
   - **`cli.py` is 1901 lines**, `daemon.py` 573 — both over the 500-line
     guideline. This session took 25 lines out of `cli.py` by moving SQL into
     `extract_queue.py`; a real refactor is still owed.
   - **165 `docling: File format not allowed` failures on the Mac** (of 182).
     They are now *visible* as `blobs_gave_up 182` rather than hidden inside a
     never-draining `blobs_pending` — which makes this the cheapest it has ever
     been to act on.
   - **Residual implausible language labels are dominated by `ja`** (229 of the
     Mac's 350). 0.24% of labels; the confidence-floor lever was measured
     useless. If ever chased, **sample the `ja` rows first**.
   - **The DGX drops remain uninvestigated and unexplained** (risk 3).
   - **#269's suggestion 2** (blob-temp sweep off the critical path) — deferred
     in session 19; reopen only if cold-cache startups grow past tolerable.

## Open decisions & risks

1. **PR #281 is open, green, and yours to merge.** `main` is `e471f25`;
   the branch is `fix/277-blobs-pending-counts-sentinels` at `85a2fed`.
   **14 open issues** (13 carried + **#280** filed this session); #277 closes on
   merge, taking it back to 13. 0 Dependabot alerts.
2. **`search-status` takes ~14 minutes and that is #280, not a hang** *(new)*.
   It has always been this slow — measured 13:04 for the pre-fix eligibility
   query alone. Confirm with
   `SELECT now()-query_start FROM pg_stat_activity WHERE state='active'`
   before concluding anything is wedged. **This is also why risk 13 matters:
   never run it concurrently with the test suite.**
3. **The DGX drops are STILL UNEXPLAINED — five theories refuted, four of them
   mine** *(carried; not investigated this session)*. **Do not propose a sixth
   without a captured outage in which the host was demonstrably up throughout.**
   Triage with `journalctl --list-boots` first. **Power is not a candidate**
   (~5-day UPS; the journal holds exactly one unclean stop, a scheduled PSU
   install). **Do not edit `/etc/wireguard/wg0.conf`.**
   **Addressing, as verified this session:** `10.0.0.3` (WireGuard) worked
   first try — SSH, `git log`, `systemctl --user is-active` all fine. The probe
   currently reports `lan=ok(3/3)@192.168.68.62`, but session 19 established
   that **that address answers ping and refuses SSH**, so a green `lan=` line is
   *not* evidence it is the DGX. **Try `10.0.0.3` first.**
4. **A single `tunnel=FAIL` probe sample is not an outage** *(carried)* —
   Starlink losing three packets on a ~900 ms path. Sustained = several
   consecutive samples. Both probes running; last samples all-green.
5. **A steady non-zero `blobs_no_text` is NORMAL** *(new — #277)*. Those blobs
   are finished, just with nothing to index; the bucket is terminal by design
   (`_claim_batch` never re-opens a rowed blob). Read it like
   `body_lang_declined`. **`blobs_gave_up` is the one to act on** —
   `list-failed-extractions` says why, `retry-failed-extractions` re-queues.
6. **`QueueCounts.__post_init__` raises when the buckets don't sum** *(new)*.
   That is deliberate: a gap can only come from a predicate bug, and the number
   an operator reads is the command's whole product. If you add a fifth
   disposition, add it to the partition — do not relax the check.
7. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
   Clear deliberately: `DELETE FROM attachment_text WHERE extractor =
   'type-skipped'`. **#266's whitespace-heal is a one-way door too** — the same
   `_claim_batch` rule means nothing re-extracts a healed blob. What makes it
   safe is the `is_blank` gate, not the nature of the data: relax that gate and
   the heal becomes an archive-wide silent delete.
8. **`--relabel` is the only destructive verb in the lang path** *(carried)*.
   Discards **every** label; archive unsearchable by `lang:` until the drain
   completes. Prompts unless `--yes`. Reach for `--retry-declined` first.
   Budget ~45 min for a 100k-row archive, and note that **`reopen_all`'s bulk
   UPDATE shows no progress in `pg_stat_activity` until it commits** — tens of
   minutes of apparent hang is expected; do **not** cancel it.
9. **`body_lang_pending` means claimable work only** *(carried)*; the
   turned-away remainder is `body_lang_declined`. A steady non-zero `declined`
   is **normal** (currently 12,182 on the Mac).
10. **Do not add normalisation steps to `lang_text.py` without a measurement**
    *(carried)*. Every candidate step beyond URL-stripping measured zero.
11. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`, or an
    unhandled promise rejection leaks while vitest still reports "passed".
12. **The macOS socket deselect is GONE — stop using it** *(changed)*. #276
    replaced the `tmp_path`-derived socket dir with one under the platform temp
    dir, so `tests/test_daemon_control_socket.py` passes 16/16 on macOS.
    `uv run pytest -q` with **no arguments** is now the right command:
    **2318 passed, 1 skipped**.
13. **Do not run the test suite while a backfill is draining — or while
    `search-status` is running** *(carried, extended)*. Shared-cluster
    contention produces dozens of false failures, and #280 means `search-status`
    now qualifies as heavy DB work for ~14 minutes.
14. **The stale NOTIFY queue is CLEAR right now** *(changed — was live at the
    last two handoffs)*. If the three `LISTEN`/`NOTIFY` tests fail again, cycle
    the daemon and **verify both gates before re-running**:
    `pg_notification_queue_usage()` → `0` **and** `LISTEN daemon_commands`
    succeeding on `localmail_test`. Session 19 saw usage `0` with `LISTEN` still
    erroring — **the queue reading alone is not the gate.**
15. **An empty `daemon_heartbeats` right after a daemon restart is normal for
    minutes** *(carried — #269/#271)*. It is the startup blob-temp sweep;
    `start_workers` wipes the table *before* the sweep and workers spawn only
    after it. The log now says so explicitly — `sweeping blob temps under …`
    with no `done` line yet means in progress. Grep for
    **`blob-temp sweep done: walked=`** (the pre-#271 prefix was `scanned=`).
16. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `--all-extras` on the Mac and `--extra mcp --extra extraction` on the DGX.
    **`uv` is not on the DGX's default non-interactive PATH** — use
    `~/.local/bin/uv` over SSH. The pytest count depends on the extras: **2318**
    with all extras on the Mac; fewer without (`mcp` alone gates 3 integration
    tests). CI installs only `--extra mcp`.
17. **`ExtractorConfigurationError` subclasses `TransientExtractorError`, and
    that subclassing *is* the #248 fix — do not "clean it up"** *(carried)*.
18. **`run_embed_worker_once`'s `failure_log` defaults to the process-wide
    `embed_worker._FAILURE_LOG`, and that default is the fix** *(carried —
    #267)*. **A new looping caller should pass nothing.** Handing it a fresh
    per-call dict "to be safe" makes every sweep report a first-on-record
    failure and restores the ~24 tracebacks/min flood.
19. **No ROADMAP.md** *(carried)* — the `/nextsession` ROADMAP step is a no-op;
    slice status lives here + `docs/handoffs/` + the specs. **README *was*
    updated this session** (a new "Reading the attachment counters" section
    under "Which attachments get extracted").
20. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    kept though #245 is fixed; #246 warns on group-write; `InsecureSecretsFile`
    refuses; #239's manual tombstone retention deliberate; admin bearer tokens
    have no per-token scope (#204).
21. **Run vitest from `gui/`, not the repo root** *(carried)*.
22. **`cargo clippy --all-targets` is clean but ungated** *(carried)* — CI runs
    clippy without `--all-targets`, so `#[cfg(test)]` modules are never linted.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status                               # expect clean
git branch --show-current
git log --oneline origin/main..main      # expect 0

# PR #281 is OPEN and GREEN, awaiting your merge (What's next, 0).
# 14 open issues (#280 newest, filed this session). 0 Dependabot.
gh pr list; gh pr checks 281; gh issue list --limit 20

# AFTER MERGING #281, deploy both hosts:
#   Mac:  git pull && uv sync --all-extras && launchctl kickstart -k gui/$UID/com.localmail.daemon
#   DGX:  ssh 10.0.0.3 'cd ~/src/localmail && git pull && ~/.local/bin/uv sync --extra mcp --extra extraction && systemctl --user restart localmail-daemon localmail-serve'

# Python test suite. NO --deselect any more (risk 12).
# Do NOT run while a backfill or search-status is in flight (risk 13).
unset VIRTUAL_ENV && uv run pytest -q
#   expect: 2318 passed, 1 skipped, 0 failed (all extras installed — risk 16)

unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 140 source files

# Host health checks:
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect: 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 15)
grep -n 'blob-temp sweep done: walked=' ~/Library/Logs/localmail/daemon.err.log | tail -3

# The attachment counters (~14 MINUTES — risk 2, that is #280 not a hang):
unset VIRTUAL_ENV && uv run localmail search-status
#   expect after #281: blobs_eligible 9490 = extracted 9202 + no_text 106
#                      + gave_up 182 + pending 0
# If it feels stuck, confirm it is progressing rather than killing it:
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT now()-query_start, left(query,60) FROM pg_stat_activity
    WHERE datname='localmail' AND state='active' AND pid <> pg_backend_pid()"

# NOTIFY gates — CLEAR as of this handoff; check both only if those 3 tests fail:
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'  # 0
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'            # LISTEN

# The DGX — use the WireGuard address; the LAN candidates mislead (risk 3).
# uv is not on its non-interactive PATH (risk 16):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'   # 80c1138 until you deploy
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — only if you touch gui/ (MUST run from gui/ — risk 21):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

`main` tip is **`e471f25`** (PR #276). This session's work is **`85a2fed`** on
`fix/277-blobs-pending-counts-sentinels`, **open as PR #281, CI green, not
merged**. Latest migration **`0035_messages_body_lang_attempted_at.sql`**; next
free slot `0036_*.sql` (this session adds none). **Open issues: 14** (13 after
#281 merges closes #277). Dependabot: **0** open alerts.
