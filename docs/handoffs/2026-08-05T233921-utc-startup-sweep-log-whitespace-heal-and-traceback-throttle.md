# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-06 (session 19).** A **three-fix robustness session** —
> the operator picked the carried robustness backlog over the Users & ACL GUI
> panel. Three issues fixed TDD-style, each on its own branch + PR, all CI-green
> and **awaiting operator merge**: **#271** (fixes #269 — the startup blob-temp
> sweep now logs before/after, unconditionally, with elapsed time), **#272**
> (fixes #266 — whitespace-only `extracted_text` collapses to the `''` sentinel
> at the `ExtractedText` boundary, plus a worker self-heal for legacy rows) and
> **#273** (fixes #267 — a persistently broken embedding backend logs its full
> traceback once per consecutive streak, one-line WARNINGs thereafter). No
> migration in any of them. `main` is still **`ef831c0`**; both hosts still run
> it, so after merging, **deploy both hosts** to get the fixes live. Baseline
> full suite was verified green before work started (2249) and per-branch after
> (2254 / 2253); expected post-merge count on a fully-extra'd Mac: **2258**.

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

Three PRs, all TDD (every test watched fail first), all mypy-clean, each with a
green full-suite run on its branch. None adds a migration or touches config.

### 1. PR #271 — announce the startup blob-temp sweep (fixes #269)

Branch `fix/269-startup-sweep-logging`, commit **`ce95988`**.
`Daemon._sweep_blob_temps` now logs `sweeping blob temps under <root> ...`
before the walk and an **unconditional**
`blob-temp sweep done: scanned=… removed=… bytes=… errors=… took=…s` after it —
the old completion line was gated on `removed or errors`, i.e. skipped on
exactly the silent-but-slow cold-cache startups the issue is about (~3 minutes
observed on the Mac, with heartbeats freshly wiped and `pool sizing` as the
last log line). The issue's suggestion 2 (moving the sweep off the critical
path) was deliberately **not** done — warm-cache timing is sub-second and the
thread move needs a `wind_down_threads` (#221 A) interaction check; reopen only
if cold-cache startups grow past tolerable.

### 2. PR #272 — whitespace-only `extracted_text` becomes the sentinel (fixes #266)

Branch `fix/266-whitespace-only-attachment-text`, commits **`ffa438d`** (fix) +
**`070b674`** (CLAUDE.md consistency). Two halves:

- **Boundary:** `ExtractedText.__post_init__` collapses whitespace-only text to
  the `''` sentinel, beside the #249 NUL strip, same by-construction reasoning.
  `str.strip()` is exactly the chunker's emptiness rule (`normalize_whitespace`
  strips every line then the whole text).
- **Backstop:** `_chunk_attachments_lazily` heals a claimed row that chunks to
  `[]` by stamping `extracted_text = ''` in place (same SAVEPOINT, one INFO
  line). The trigger is the chunker's **own** verdict, so legacy rows on live
  archives drain out on their first claim — no data migration, no SQL
  predicate that could drift — and future drift in the chunker's notion of
  empty self-heals instead of wedging the queue (the #216 shape the issue
  warned about: ~50 such rows sorting low in the sha256 order would have
  silently stopped attachment ingestion archive-wide).

`search-status` effect after deploy: healed rows leave `blobs_extracted`
(they count like every other sentinel). Incidence can be pre-counted with the
diagnostic SQL in #266 (note its `btrim` undercounts unicode whitespace; the
worker's heal is the authoritative rule).

### 3. PR #273 — traceback once per streak, not per sweep (fixes #267)

Branch `fix/267-throttle-backend-failure-logs`, commit **`87e485d`**.
`run_embed_worker` owns a run-long `failure_streaks` mapping (keyed by chunk
table) threaded into `_embed_table`; the pure `should_log_traceback`
(`streak == 1`) keeps the full traceback on the **first** failure of a
consecutive streak, repeats are one-line WARNINGs naming the count, and a
successful batch resets the streak (next incident → traceback again). An
empty-claim sweep touches nothing. The retry *pace* is untouched — throttling
it would re-introduce #259; only the log volume changes (was ~24 tracebacks/min
while a language backlog drained against a broken backend). One-shot callers
(`embed-backfill`, tests) default to a fresh mapping per call and behave as
before. CLAUDE.md's #267 tracking bullet rewritten to describe the fix.

### 4. Verification

- Baseline on `main` before work: **2249 passed, 0 failed** (2:20).
- `fix/266` branch full suite: **2254 passed** (+5 tests). `fix/267` branch:
  **2253 passed** (+4 tests). `fix/269` adds 1 test (module-level runs green;
  full suite via CI, green).
- `uv run mypy src/localmail` → clean, 137 files, on every branch.
- CI green on all three PRs (#272's re-run after the docs-only `070b674`
  included) — verified before this handoff was frozen.

## What's next

### 0. **Merge the three PRs, then deploy both hosts** *(operator action first)*
   Merge order doesn't matter. #272 and #273 both touch
   `src/localmail/search/embed_worker.py`, `tests/test_embed_worker.py`, and
   adjacent CLAUDE.md bullets — expect GitHub to auto-merge cleanly; if the
   *second* one reports a conflict it will be trivial (disjoint hunks in those
   three files). After merging:
   - **Mac**: `git pull`, `uv sync --all-extras`, `launchctl kickstart -k
     gui/$UID/com.localmail.daemon` (and the serve agent if running).
   - **DGX**: `git pull`, `~/.local/bin/uv sync --extra mcp --extra
     extraction`, `systemctl --user restart localmail-daemon localmail-serve`.
   - **Acceptance:** the restart log shows the two new sweep lines
     (`sweeping blob temps under …` → `blob-temp sweep done: … took=…s`);
     heartbeats fresh after the sweep completes; `search-status` healthy.
     Watch for a few `healed to the '' sentinel` INFO lines on the Mac as the
     embed worker touches any legacy whitespace-only rows.

### 1. **Remaining robustness issues** *(carried; three fewer)*
   - **#218** — GUI download commands buffer the full body before enforcing the
     size ceiling.
   - **#226** — self-signed cert misses the reachable IP when `--bind 0.0.0.0`.
   - **#225 / #227** — `/v1/changes` subscription lifecycle gaps.
   - **#200 / #211 / #208** — admin panels silently swallow 4xx; surface as a toast.
   - **#206** — GUI AccountForm: folder filters not editable.

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
   - **`cli.py` is 1904 lines**, `daemon.py` ~570 — both over the 500-line
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
     deferred, see above.

## Open decisions & risks

1. **Three PRs are open and unmerged — the session stopped at PR-open +
   CI-green per the standing workflow.** Merge them before starting new code
   work, or new branches will need rebasing over the same files. `main` tip is
   still `ef831c0` until then. After merge, the three issues (#269, #266,
   #267) auto-close → 10 open issues.
2. **After cycling the daemon for the NOTIFY fault, verify before re-running**
   *(carried)*. The gate is `SELECT pg_notification_queue_usage()` → `0`
   **and** `LISTEN daemon_commands` succeeding on `localmail_test` (use
   `-h localhost`). This session the fault did **not** recur.
3. **An empty `daemon_heartbeats` right after a daemon restart is normal for
   minutes** *(carried — #269)*. It is the startup blob-temp sweep. **After
   #271 deploys, the log now says so explicitly** (`sweeping blob temps
   under …` with no `done` line yet = sweep in progress); before that deploy
   the old silence persists. `sample <pid>` (expect `os_scandir`) remains the
   confirmation of last resort.
4. **The DGX drops are STILL UNEXPLAINED — five theories refuted, four of them
   mine** *(carried; not investigated this session)*. **Do not propose a sixth
   without a captured outage in which the host was demonstrably up throughout.**
   Triage with `journalctl --list-boots` first. **Power is not a candidate**
   (~5-day UPS). Reach it on the LAN, but **look the address up** — DHCP; it
   has been `192.168.68.62`, `192.168.68.76`, and `192.168.1.99`. **Do not
   edit `/etc/wireguard/wg0.conf`.** Any `lan=FAIL` log line without an
   `@addr` suffix predates PR #260 and proves nothing.
5. **A single `tunnel=FAIL` probe sample is not an outage** *(carried)* —
   sustained = several consecutive samples. Both probes still running.
6. **`--relabel` is the only destructive verb in the lang path** *(carried)*.
   Discards **every** label; prompts unless `--yes`. Reach for
   `--retry-declined` first. Budget ~45 min for a 100k-row archive.
7. **`body_lang_pending` means claimable work only** *(carried)*; a steady
   non-zero `declined` is **normal**.
8. **Do not add normalisation steps to `lang_text.py` without a measurement**
   *(carried)*. Note #266's whitespace rule lives in `ExtractedText` /
   `chunk_attachment_text` — it is **not** a lang-detection change.
9. **`ExtractorConfigurationError` subclasses `TransientExtractorError`, and
   that subclassing *is* the #248 fix — do not "clean it up"** *(carried)*.
10. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    Clear deliberately: `DELETE FROM attachment_text WHERE extractor =
    'type-skipped'`. The #266 whitespace-heal writes the same `''` sentinel but
    is *not* a one-way door in the same sense — there is genuinely nothing to
    index in those blobs, under any future config.
11. **Test trap: `_try_import_docling` must be monkeypatched in BOTH
    namespaces** *(carried)*. **OCR is macOS-only by default**; the
    `[extraction]` extra is the whole fix on Linux (~5.5 GB venv on aarch64).
12. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`.
13. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `--all-extras` on the Mac and `--extra mcp --extra extraction` on the DGX.
    **`uv` is not on the DGX's default non-interactive PATH** — use
    `~/.local/bin/uv` over SSH. Pytest count depends on the extras: expected
    **2258** post-merge with all extras on the Mac; fewer without.
14. **Do not run the test suite while a backfill is draining** *(carried)*.
15. **macOS test noise** *(carried)* — deselect
    `test_daemon_control_socket.py` (`AF_UNIX path too long`); Linux CI is the
    real signal.
16. **The stale NOTIFY queue recurs** *(carried; did NOT recur this session)* —
    see risk 2. Keep full pytest output; a `| tail` pipe once hid the
    signature from grep.
17. **No ROADMAP.md** *(carried)* — the `/nextsession` ROADMAP step is a no-op;
    README needed no update this session (none of the changed behaviours are
    documented there).
18. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    kept though #245 is fixed; #246 warns on group-write; `InsecureSecretsFile`
    refuses; #239's manual tombstone retention deliberate; admin bearer tokens
    have no per-token scope (#204).
19. **Run vitest from `gui/`, not the repo root** *(carried)*.
20. **`cargo clippy --all-targets` is clean but ungated** *(carried)*.
21. **`failure_streaks` defaults to fresh-per-call** *(new — #267)*: only the
    daemon loop passes a persistent mapping. If a future looping caller of
    `run_embed_worker_once` wants throttled tracebacks, it must own and pass
    its own dict — the default deliberately preserves per-call tracebacks for
    one-shot use.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status                               # expect clean
git branch --show-current                # main
git log --oneline origin/main..main      # expect 0

# Pending on GitHub: PRs #271, #272, #273 (this session's fixes) + the
# session-19 handoff PR — merge them (any order; a conflict on the second of
# #272/#273 would be trivial, see risk 1). 13 open issues → 10 after merge.
gh pr list; gh issue list --limit 15
gh pr checks 271; gh pr checks 272; gh pr checks 273

# After merging, deploy BOTH hosts (fixes are daemon/worker-side):
#   Mac:  git pull && uv sync --all-extras && launchctl kickstart -k gui/$UID/com.localmail.daemon
#   DGX:  ssh <addr> 'cd ~/src/localmail && git pull && ~/.local/bin/uv sync --extra mcp --extra extraction && systemctl --user restart localmail-daemon localmail-serve'
# Acceptance: restart log shows "sweeping blob temps under ..." then
# "blob-temp sweep done: ... took=...s"; fresh heartbeats afterwards.

# Python test suite (deselect the macOS-only socket failure — risk 15).
# Do NOT run while a backfill is draining (risk 14).
unset VIRTUAL_ENV && uv run pytest -q --deselect tests/test_daemon_control_socket.py
#   expect: 2258 passed, 0 failed once all three PRs are merged (risk 13)
# If EXACTLY the three LISTEN/NOTIFY tests fail (risk 2/16): cycle the daemon,
# then VERIFY before re-running — do not just wait:
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'  # 0
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'            # LISTEN

unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 137 source files

# Host health checks:
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect: 7 rows, ages under ~60 s (EMPTY during the startup sweep — risk 3)

# The DGX — LAN only, look the address up first (DHCP — risk 4); uv is not on
# the non-interactive PATH there (risk 13). Last seen at 192.168.1.99:
ssh 192.168.1.99 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 192.168.1.99 'cd ~/src/localmail && ~/.local/bin/uv run localmail search-status'

# Probe log (control column carries the answering address — PR #260):
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — only if you touch gui/ (MUST run from gui/ — risk 19):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

`main` tip is **`ef831c0`** until the PRs merge (branch heads: `ce95988` /
`070b674` / `87e485d`). **Both hosts run `ef831c0`** — deploy after merging.
Latest migration **`0035_messages_body_lang_attempted_at.sql`**; next free slot
`0036_*.sql` (none of this session's PRs adds one). **Open issues: 13**, → 10
after the PRs merge. Dependabot: **0** open alerts.
