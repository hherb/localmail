# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-08 (session 19).** A **three-fix robustness session** —
> the operator picked the carried robustness backlog over the Users & ACL GUI
> panel. Three issues fixed TDD-style, each on its own branch + PR; **all three
> are now merged** and `main` is **`3b9043d`**: **#271** (fixes #269 — the
> startup blob-temp sweep logs before/after, unconditionally, with elapsed time
> and entries walked), **#272** (fixes #266 — whitespace-only `extracted_text`
> collapses to the `''` sentinel at the `ExtractedText` boundary, plus a
> worker self-heal for legacy rows) and **#273** (fixes #267 — a persistently
> broken embedding backend reports at most once per interval, with the
> traceback). No migration in any of them; **#273 adds one config knob.**
> **Neither host is deployed** — the Mac daemon (pid 3300, started 2026-08-07
> 10:47) predates #271 and the DGX still reports `ef831c0`. Deploying both is
> step 0.
>
> **Read section "What we shipped" carefully even if you saw the PRs open:**
> two of the three changed shape under review after the first commit, and the
> *rejected* designs are the intuitive ones a future session would re-propose.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The database is canonical for accounts.
Daemon: hot-reload account set, heartbeats, DB command queue, two-plane
supervision. Web admin UI (HTMX): account CRUD, user management, archive
imports, daemon control. Hybrid search (Phases 1+2) + an HTTPS GUI server + a
remote MCP server (optionally a full OAuth 2.1 authorization server) + the
opt-in `--smart` LLM query rewriter are all shipped. A Tauri 2 + Svelte 5 GUI
lives under `gui/` — read-only viewer plus an admin mode (Accounts + Daemon
panels shipped; Users + Imports still placeholders). Licensed AGPL-3.0-or-later
(per-file SPDX headers in `src/localmail/`; **not** in `gui/`).
See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we shipped this session

Three PRs, all TDD (every test watched fail first), all mypy-clean, all merged.
None adds a migration; **#273 adds a config field** (below). Each PR gained
review commits *after* the first — the merged design is what follows, not the
one the PR description opened with.

### 1. PR #271 → `32ae1b8` — announce the startup blob-temp sweep (fixes #269)

Branch `fix/269-startup-sweep-logging`, commits **`ce95988`** (fix) +
**`bf5881e`** (review). `Daemon._sweep_blob_temps` logs
`sweeping blob temps under <root> ...` before the walk and an **unconditional**

```
blob-temp sweep done: walked=N scanned=N removed=N bytes=N errors=N took=Ns
```

after it — the old completion line was gated on `removed or errors`, i.e.
skipped on exactly the silent-but-slow cold-cache startups the issue is about
(~3 minutes observed on the Mac, with heartbeats freshly wiped and
`pool sizing` as the last log line).

**`walked=` is from the review commit and is the field that names the cost.**
`scanned` counts only name-matching temps (`<64 hex>.<pid>.<32 hex>.tmp`),
which on a healthy archive is 0 — so the pre-review line reported `scanned=0`
after a three-minute walk and still explained nothing. `walked` counts every
directory entry visited: the Mac's last logged sweep was
`walked=16641 scanned=0 removed=0 bytes=0 errors=0 took=1.2s` (warm cache).
**Grep for `blob-temp sweep done: walked=`** when checking the deploy; the
pre-review prefix `blob-temp sweep done: scanned=` will not match.

The issue's suggestion 2 (moving the sweep off the critical path) was
deliberately **not** done — warm-cache timing is sub-second and the thread move
needs a `wind_down_threads` (#221 A) interaction check; reopen only if
cold-cache startups grow past tolerable.

### 2. PR #272 → `b9c3558` — whitespace-only `extracted_text` becomes the sentinel (fixes #266)

Branch `fix/266-whitespace-only-attachment-text`, commits **`ffa438d`** (fix) +
**`070b674`** (CLAUDE.md) + **`e970c3d`** (review). Three parts:

- **The rule is a new pure module**,
  [src/localmail/search/text_empty.py](src/localmail/search/text_empty.py)`::is_blank`
  — `not text or text.isspace()`, **not** `not text.strip()`: `strip()` copies
  a possibly-megabyte extraction whenever there is leading/trailing
  whitespace, in a per-blob hot loop. `tests/test_text_empty.py` pins
  `is_blank(t) == (normalize_whitespace(t) == '')` over every character Python
  calls whitespace, including the `str.splitlines()` boundaries (`\x0b`,
  `\x1c`–`\x1e`, `\x85`, U+2028). **Do not reimplement this predicate inline.**
- **Boundary:** `ExtractedText.__post_init__` calls `is_blank` and collapses
  whitespace-only text to the `''` sentinel, beside the #249 NUL strip, same
  by-construction reasoning. Consequence, deliberate: `_process_blob`'s step-3
  gate is `if lw_text is not None and lw_text.text:`
  ([extract_worker.py:498](src/localmail/search/extract_worker.py#L498)), so a
  whitespace-only lightweight result now falls
  through to step 4 — the docling/OCR fallback for PDFs (usually right: pure
  space out of a lightweight extraction is a scanned page), the
  `lightweight-empty` sentinel for everything else.
- **Backstop:** `_chunk_attachments_lazily` heals a claimed row by stamping
  `extracted_text = ''` in place (same SAVEPOINT, one INFO line), so legacy
  rows drain out on first claim with no migration.

**The heal is gated on `is_blank`, NOT on the chunker's bare `[]` verdict —
this is the review commit and it inverted the original rationale.** `[]` alone
is shorter and today means exactly the same thing, which is why `ffa438d`
shipped it that way and argued the chunker's own verdict was the *point*. It
is not: the UPDATE is destructive and **one-way** (`_claim_batch` skips any
blob that already has an `attachment_text` row, so nothing re-extracts it), so
a future chunker rule returning `[]` for text with substance would silently
delete real extracted text archive-wide. Gated on `is_blank`, that case takes
the `elif not specs:` branch — a WARNING naming the character count, row left
claimable. A loud wedge, which is recoverable, over a quiet one-way door: the
same trade as `type-skipped` and the rejected `'und'` sentinel. **Do not
"simplify" the redundant-looking `is_blank` conjunct away.**

**`search-status` effect after deploy, and the issue it created:** healed rows
leave `blobs_extracted` (they count like every other sentinel) — and
`blobs_extracted` counts `extracted_text <> ''`, so they move into
`blobs_pending`, **which never drains**. Filed as **#277**. It is pre-existing
for every other sentinel; the heal adds to that bucket rather than creating it.
**Expect a non-zero, non-draining `blobs_pending` after this deploy — that is
#277, not the wedged-queue signature of #216.** Incidence can be pre-counted
with the diagnostic SQL in #266 (note its `btrim` undercounts unicode
whitespace; `is_blank` is the authoritative rule).

### 3. PR #273 → `3b9043d` — pace the broken-backend report by time (fixes #267)

Branch `fix/267-throttle-backend-failure-logs`, commits **`0460414`** (fix) +
**`c37de2a`** (review). **The merged design is time-paced, not streak-based.**
The rule is the pure
[src/localmail/search/failure_pacing.py](src/localmail/search/failure_pacing.py)`::note_failure`:

> report a failure, **with its traceback**, when it is the first on record for
> that table, when the exception type changes, or when
> `search.embed_worker_failure_report_interval_s` has elapsed since the last
> report; otherwise stay silent and count.

The next report names how many it swallowed, so nothing is lost, only
deferred. The retry *pace* is untouched — throttling it would re-introduce
#259; only log volume changes (was ~24 tracebacks/min while a language backlog
drained against a broken backend).

**Three things the first commit got wrong, each closing a way the intuitive
rule fails — do not restore any of them:**

- **Success does NOT clear the record.** `0460414` reset a streak on a
  successful batch. A backend alternating 200/503 — the "network blip" the
  batch-level handler exists for — then makes every failure the first of a
  fresh streak, so every one carries a traceback and the throttle buys
  nothing. Recovery is expressed by the interval instead.
- **The record holds the exception type, not just a count.** A count alone
  cannot tell a continuing failure from a *different* one arriving
  mid-incident; the second would be suppressed and reported as a continuation
  of the first, leaving the one traceback on record naming the wrong problem.
- **Every report carries the traceback**, periodic ones included. Logging it
  once per process leaves a long incident undiagnosable from a rotated log or
  the supervisor's `deque(maxlen)` ring buffer.

There is no `should_log_traceback` and no `failure_streaks` — grepping for
either finds nothing. The line names `type(exc).__name__` explicitly because
`str(exc)` is empty for `ConnectionError()`, `MemoryError()` and much of what a
backend raises.

**New config field:** `[search] embed_worker_failure_report_interval_s`
(default `300.0`, `ge=0`; `0` disables the throttle and restores per-sweep
reporting). [config.py:501](src/localmail/config.py#L501),
[config.example.toml:223](config.example.toml#L223).

### 4. Verification (measured after all three merged, on this Mac, all extras)

- `unset VIRTUAL_ENV && uv run pytest -q` → **2293 collected**; with
  `--deselect tests/test_daemon_control_socket.py` (risk 15) → **2279**.
- This session's run: **2276 passed, 3 failed** under the deselect — the three
  failures are the known stale-NOTIFY signature (risk 2/16), which **recurred
  and was NOT cleared this session**. `pg_notification_queue_usage()` reads
  `0` while `LISTEN daemon_commands` on `localmail_test` still errors with
  `could not access status of transaction …`. Cycle the daemon, verify both
  gates, then re-run and expect **2279 passed**.
- `unset VIRTUAL_ENV && uv run mypy src/localmail` → **Success, 139 source
  files** (137 + `search/text_empty.py` + `search/failure_pacing.py`).
- CI green on all three PRs before merge.

## What's next

### 0. **Deploy both hosts** *(nothing to merge — the PRs are in)*
   Neither host runs the fixes. Evidence: the Mac daemon's current run logs
   `pool sizing` with **no** `sweeping blob temps` line after it (only one such
   line exists in the whole log, from the 2026-08-06 17:26 run); the DGX
   reports `ef831c0`.
   - **Mac**: `git pull`, `uv sync --all-extras`, `launchctl kickstart -k
     gui/$UID/com.localmail.daemon` (and the serve agent if running).
   - **DGX**: `git pull`, `~/.local/bin/uv sync --extra mcp --extra
     extraction`, `systemctl --user restart localmail-daemon localmail-serve`.
   - **Acceptance:** the restart log shows the two new sweep lines
     (`sweeping blob temps under …` → `blob-temp sweep done: walked=… took=…s`
     — grep on `walked=`, see section 1); heartbeats fresh **after** the sweep
     completes (empty during it — risk 3); a few `healed to the '' sentinel`
     INFO lines on the Mac as the embed worker touches legacy whitespace-only
     rows. **`search-status`'s `blobs_pending` will be non-zero and will not
     drain — that is #277, not a wedge** (see section 2).

### 1. **Remaining robustness issues** *(carried; three fewer, one new)*
   - **#277** — *new, created by #272*: `search-status`'s `blobs_pending`
     counts sentinel rows the extract worker will never claim, so the counter
     never reaches zero. Cheap and worth doing before it trains the operator
     to ignore that number.
   - **#218** — GUI download commands buffer the full body before enforcing the
     size ceiling.
   - **#226** — self-signed cert misses the reachable IP when `--bind 0.0.0.0`.
   - **#225 / #227** — `/v1/changes` subscription lifecycle gaps.
   - **#200 / #211 / #208** — admin panels silently swallow 4xx; surface as a toast.
   - **#206** — GUI AccountForm: folder filters not editable.
   - (**#25** websockets DeprecationWarning and **#204** admin bearer-token
     scope round out the 11 open issues.)

### 2. **Admin GUI phase 5 — Users & ACL panel** *(carried, still the design's next slice)*
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
   `AdminView.test.ts` and `MainView.test.ts`** (see risk 12).

### 3. **Smaller, deliberately not done** *(carried verbatim)*
   - **`cli.py` is 1903 lines**, `daemon.py` 573 — both over the 500-line
     guideline. A real refactor; each session adds to it.
   - **165 `docling: File format not allowed` failures on the Mac** (of 182
     total) — each burns `retry_count` three times for a format docling will
     never accept.
   - **Residual implausible labels are dominated by `ja`** (229 of the Mac's
     350). 0.24% of labels — below the original complaint's noise floor;
     the confidence-floor lever was measured useless. If ever chased, **sample
     the `ja` rows first**.
   - **The DGX drops remain uninvestigated and unexplained** (risk 4).
   - **#269's suggestion 2** (sweep off the critical path) — deliberately
     deferred, see *What we shipped* section 1.

## Open decisions & risks

1. **Nothing is pending on GitHub except this handoff's own PR.** #271, #272
   and #273 merged (2026-08-06 / 08-07 / 08-08); #269, #266 and #267
   auto-closed. `main` is **`3b9043d`**. **11 open issues** — 13 minus those
   three, plus **#277**, filed as a known consequence of #272. (An earlier
   draft of this handoff predicted 10; it did not know about #277.)
2. **After cycling the daemon for the NOTIFY fault, verify before re-running**
   *(carried; the fault is LIVE right now — see Verification)*. The gate is
   `SELECT pg_notification_queue_usage()` → `0` **and** `LISTEN
   daemon_commands` succeeding on `localmail_test` (use `-h localhost`). This
   session saw usage `0` with `LISTEN` still failing, so **the queue reading
   alone is not the gate** — both must pass.
3. **An empty `daemon_heartbeats` right after a daemon restart is normal for
   minutes** *(carried — #269)*. It is the startup blob-temp sweep:
   `start_workers` wipes the whole table *before* the sweep (a clean shutdown
   clears only the per-account rows), and workers spawn only after it — so an
   empty table says nothing about how the predecessor died. **Once #271 is
   deployed the log says so explicitly** (`sweeping blob temps under …` with
   no `done` line yet = sweep in progress); before that deploy the old silence
   persists. `sample <pid>` (expect `os_scandir`) remains the confirmation of
   last resort — don't kill the process.
4. **The DGX drops are STILL UNEXPLAINED — five theories refuted, four of them
   mine** *(carried; not investigated this session)*. **Do not propose a sixth
   without a captured outage in which the host was demonstrably up throughout.**
   Triage with `journalctl --list-boots` first. **Power is not a candidate**
   (~5-day UPS; the journal holds exactly one unclean stop, a scheduled PSU
   install). **Do not edit `/etc/wireguard/wg0.conf`.** Any `lan=FAIL` log line
   without an `@addr` suffix predates PR #260 and proves nothing.
   **Addressing, as verified this session:** the WireGuard address
   **`10.0.0.3` worked and is the reliable way in** — SSH, `git log`, and
   `systemctl --user is-active` all succeeded over it. The LAN addresses did
   **not**: `192.168.1.99` is `Network is unreachable` (the Mac is on
   `192.168.68.0/24` now) and `192.168.68.62` answers the probe's ping but
   **refuses SSH** — so a green `lan=ok(3/3)@192.168.68.62` in the probe log
   is not evidence that address is the DGX. **Try `10.0.0.3` first**; the
   probe's `LAN_CANDIDATES` list (`~/localmail-probe/tunnel-probe.sh`) is
   ping-only and can point at a DHCP squatter.
5. **A single `tunnel=FAIL` probe sample is not an outage** *(carried)* —
   Starlink losing three packets on a ~900 ms path. Sustained = several
   consecutive samples. Both probes still running; last samples all-green.
6. **`--relabel` is the only destructive verb in the lang path** *(carried)*.
   Discards **every** label; archive unsearchable by `lang:` until the drain
   completes. Prompts unless `--yes`. Reach for `--retry-declined` first.
   Budget ~45 min for a 100k-row archive, and note that **`reopen_all`'s bulk
   UPDATE shows no progress in `pg_stat_activity` until it commits** — tens of
   minutes of apparent hang is expected; do **not** cancel it, the labels are
   already discarded by then.
7. **`body_lang_pending` means claimable work only** *(carried)*; the
   turned-away remainder is `body_lang_declined`. A steady non-zero `declined`
   is **normal**.
8. **Do not add normalisation steps to `lang_text.py` without a measurement**
   *(carried)*. Every candidate step beyond URL-stripping measured zero.
   Note #266's whitespace rule lives in `text_empty.is_blank` /
   `ExtractedText` / `chunk_attachment_text` — it is **not** a lang-detection
   change.
9. **`ExtractorConfigurationError` subclasses `TransientExtractorError`, and
   that subclassing *is* the #248 fix — do not "clean it up"** *(carried)*.
10. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    Clear deliberately: `DELETE FROM attachment_text WHERE extractor =
    'type-skipped'`. **#266's whitespace-heal is a one-way door too** — the
    same `_claim_batch` rule means nothing re-extracts a healed blob. What
    makes it safe is the `is_blank` gate, not the nature of the data: relax
    that gate and the heal becomes an archive-wide silent delete (*What we
    shipped* section 2).
11. **Test trap: `_try_import_docling` must be monkeypatched in BOTH
    namespaces** *(carried)*. **OCR is macOS-only by default**; the
    `[extraction]` extra is the whole fix on Linux (~5.5 GB venv on aarch64).
12. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`.
13. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `--all-extras` on the Mac and `--extra mcp --extra extraction` on the DGX.
    **`uv` is not on the DGX's default non-interactive PATH** — use
    `~/.local/bin/uv` over SSH. The **pytest count depends on the extras**:
    **2279** under the deselect with all extras on the Mac; fewer without
    (`mcp` alone gates 3 integration tests).
14. **Do not run the test suite while a backfill is draining** *(carried)* —
    shared-cluster contention produces dozens of false failures.
15. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
    locally (`AF_UNIX path too long`, 3 failed + 3 errors of its 14 tests);
    deselect it, Linux CI is the real signal.
16. **The stale NOTIFY queue recurs** *(carried; recurred THIS session and is
    still live)* — see risk 2. Keep full pytest output; a `| tail` pipe once
    hid the signature from grep.
17. **No ROADMAP.md** *(carried)* — the `/nextsession` ROADMAP step is a no-op;
    slice status lives here + `docs/handoffs/` + the specs. README needed no
    update this session (none of the changed behaviours are documented there).
18. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    kept though #245 is fixed; #246 warns on group-write; `InsecureSecretsFile`
    refuses; #239's manual tombstone retention deliberate; admin bearer tokens
    have no per-token scope (#204).
19. **Run vitest from `gui/`, not the repo root** *(carried)*.
20. **`cargo clippy --all-targets` is clean but ungated** *(carried)*.
21. **`run_embed_worker_once`'s `failure_log` defaults to the process-wide
    `embed_worker._FAILURE_LOG`, and that default is the fix** *(new — #267)*.
    Throttling log output is a process concern, and making the *default*
    correct is what removes the footgun: every looping caller (the daemon
    loop, `embed-backfill`, the three acceptance harnesses) passes nothing and
    is throttled anyway. **A new looping caller should pass nothing.** Do not
    hand it a fresh per-call dict "to be safe" — every sweep would then report
    a first-on-record failure and the ~24 tracebacks/min flood returns.
    `reset_failure_log()` plus an autouse conftest fixture keep one test's
    broken backend from silencing the next test's WARNING (the same shape as
    `secrets.reset_to_default()`). This is deliberately **not** #234's
    keyword-only-no-default shape — that is for a parameter whose safe value
    cannot be the default; here it can.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status                               # expect clean
git branch --show-current                # main
git log --oneline origin/main..main      # expect 0

# Nothing pending but the session-19 handoff PR (#274) — merge or ignore.
# 11 open issues (#277 newest, filed as a consequence of #272). 0 Dependabot.
gh pr list; gh issue list --limit 15

# DEPLOY BOTH HOSTS FIRST — neither runs the merged fixes (What's next, 0):
#   Mac:  git pull && uv sync --all-extras && launchctl kickstart -k gui/$UID/com.localmail.daemon
#   DGX:  ssh 10.0.0.3 'cd ~/src/localmail && git pull && ~/.local/bin/uv sync --extra mcp --extra extraction && systemctl --user restart localmail-daemon localmail-serve'
# Acceptance: restart log shows "sweeping blob temps under ..." then
# "blob-temp sweep done: walked=... took=...s"; fresh heartbeats afterwards.

# Python test suite (deselect the macOS-only socket failure — risk 15).
# Do NOT run while a backfill is draining (risk 14).
unset VIRTUAL_ENV && uv run pytest -q --deselect tests/test_daemon_control_socket.py
#   expect: 2279 passed, 0 failed (with all extras installed — risk 13)
# The three LISTEN/NOTIFY tests were FAILING when this handoff was frozen
# (risk 2/16) — cycle the daemon, then VERIFY BOTH gates before re-running:
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'  # 0
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'            # LISTEN
#   (this session saw usage=0 while LISTEN still errored — usage alone is NOT the gate)

unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 139 source files

# Host health checks:
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect: 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 3)
grep -n 'blob-temp sweep done: walked=' ~/Library/Logs/localmail/daemon.err.log | tail -3
#   expect: a line from the post-deploy restart (absent = #271 not deployed)

# The DGX — use the WireGuard address; the LAN candidates misled this session
# (risk 4). uv is not on its non-interactive PATH (risk 13):
ssh 10.0.0.3 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 10.0.0.3 'cd ~/src/localmail && git log --oneline -1'   # expect 3b9043d after deploy
ssh 10.0.0.3 'cd ~/src/localmail && ~/.local/bin/uv run localmail search-status'
ssh 10.0.0.3 'docker exec localmail-pg psql -U localmail -d localmail -c \
  "SELECT worker_kind, state, now()-last_heartbeat_at AS age FROM daemon_heartbeats"'

# Probe log (control column carries the answering address — PR #260):
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — only if you touch gui/ (MUST run from gui/ — risk 19):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

`main` tip is **`3b9043d`** (merge of #273; carries #272's `b9c3558` and
#271's `32ae1b8`). **Neither host runs it — deploy both.** Latest migration
**`0035_messages_body_lang_attempted_at.sql`**; next free slot `0036_*.sql`
(none of this session's PRs adds one). **Open issues: 11** (#277 filed this
session). Dependabot: **0** open alerts.
