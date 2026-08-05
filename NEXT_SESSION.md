# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-06 (session 18).** A **deploy-and-verify session — no
> code changed.** Both hosts now run `main` at **`ef831c0`** (sessions 16+17
> fixes: #255, #259, #235, cryptography 50.0.0): Mac daemon restarted with 7
> fresh heartbeats, DGX pulled + synced + restarted with 5. As predicted by the
> previous handoff, **no observable behaviour change** — both lang queues were
> already drained (`claimable = 0` on both hosts). PR **#265** turned out to be
> **merged** (as `ef831c0`), not closed as advised — verified **harmless**
> (lockfile and `pyproject` floor agree at cryptography 50.0.0, `uv lock
> --check` clean, Dependabot still 0). The **stale NOTIFY queue recurred**
> (runbook Fault 1) and was cleared by the deploy's own daemon cycle; full
> suite now **2249 passed, 0 failed**. One new issue filed: **#269** (silent
> multi-minute startup sweep). Next session starts on the carried backlog —
> the robustness issues or the Users & ACL GUI panel.

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

No code. The session executed the previous handoff's §0 (deploy) and resolved
what it found along the way. The only commit is this handoff (SHA in the PR).

### 1. PR #265 was merged, not closed — verified harmless

The previous handoff said close #265 unmerged (superseded by #268). The
operator merged it instead, as **`ef831c0`** on `main`. Verified no regression:
`uv.lock` has cryptography **50.0.0**, `pyproject.toml` keeps the `>= 50.0.0`
CVE floor from #268, `uv lock --check` passes, Dependabot shows **0 open
alerts**. The merge was a no-op on top of #268 — nothing to unwind.

### 2. Both hosts deployed to `ef831c0` and verified

- **Mac**: `uv sync --all-extras`, `launchctl kickstart -k`. All 7 heartbeats
  fresh (embed, extract, reconcile, idle×2, poll×2; ages < 20 s). Lang state
  exactly as predicted — `claimable 0`, `implausible 350` (unchanged),
  `populated 95663` / `declined 12171` (small growth = new mail).
- **DGX** (at `192.168.1.99` this session): `git pull`, `~/.local/bin/uv sync
  --extra mcp --extra extraction`, `systemctl --user restart localmail-daemon
  localmail-serve`. All three units `active`, 5 fresh heartbeats,
  `search-status` healthy (`body_lang_pending 0`, `chunks_pending 0`).
- Probe log healthy throughout: `tunnel=ok lan=ok@192.168.1.99 hub=ok` on
  every sample.

### 3. The stale NOTIFY queue recurred — and the "wait a minute" advice is too optimistic

The first full-suite run failed **exactly** the three LISTEN/NOTIFY tests with
**exactly** the runbook Fault-1 signature (`could not access status of
transaction … pg_xact/0606`). The deploy's daemon cycle doubled as the fix, but
note the timing: a re-run **~5 minutes after** the restart still failed; the
queue read clean (`pg_notification_queue_usage() = 0`, `LISTEN` ok on
`localmail_test`) only ~9 minutes after. **Verify with the probe, don't wait a
fixed time** — see the updated resume commands. After that, 11/11 in both
NOTIFY modules and a clean full suite.

Also a diagnosis trap hit this session: piping pytest through `tail -8` had
discarded the tracebacks, so a grep for the Fault-1 signature in the saved
output found nothing and briefly pointed *away* from the known fault. Keep full
output (or `--tb=short` to a file) before pattern-matching failures.

### 4. New issue #269 — the silent startup sweep

The Mac restart sat **~3 minutes** between `daemon pool sizing: …` (07:29:20)
and `started workers …` (07:32:19) with no log line and **no heartbeat rows**
(`start_workers` wipes the table at startup, right before the sweep). A
process sample showed the main thread in `os_scandir`/`readdir` — the #237
blob-temp sweep at `Daemon.start_workers`, cold-cache-slow over 16,640 blobs /
14,912 dirs (a warm `find` walks the same tree in 0.3 s; the previous
restart's gap was 35 s).
Filed as **#269** with two suggestions: an INFO line around the sweep
(zero-risk), and possibly moving the sweep off the critical path (needs a
`wind_down_threads` interaction check). Until then: **an empty
`daemon_heartbeats` for a few minutes after a restart is the sweep, not a
crash** — confirm with `sample <pid>` before reaching for anything stronger.

### 5. Verification

- `uv run pytest -q --deselect tests/test_daemon_control_socket.py` →
  **2249 passed, 0 failed**, 14 deselected. The baseline moved from session
  17's 2244 because the count is **environment-dependent**: this session's
  `--all-extras` sync installed the `mcp` client (+3 integration tests) and
  other extras-gated tests now run. 2249 is the number for a fully-extra'd Mac.
- `uv run mypy src/localmail` → clean, 137 source files.
- `uv lock --check` → clean.

## What's next

The deploy backlog is empty. Start on the carried work — either the robustness
issues (1) or the GUI panel (2), whichever the operator prefers.

### 1. **Remaining robustness issues** *(carried; #269 new)*
   - **#269** — daemon startup: silent multi-minute blob-temp sweep before
     worker spawn *(filed this session; the INFO-line half is a ~10-line fix)*.
   - **#266** — attachment chunking re-claims whitespace-only `extracted_text`
     rows forever.
   - **#267** — a persistently broken embedding backend logs a traceback per
     sweep at the base poll interval.
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
   - **`cli.py` is 1904 lines**, `daemon.py` 567 — both over the 500-line
     guideline. A real refactor; each session adds to it.
   - **165 `docling: File format not allowed` failures on the Mac** (of 182
     total) — each burns `retry_count` three times for a format docling will
     never accept.
   - **Residual implausible labels are dominated by `ja`** (229 of the Mac's
     350). 0.24% of labels — below the original complaint's noise floor;
     the confidence-floor lever was measured useless. If ever chased, **sample
     the `ja` rows first**.
   - **The DGX drops remain uninvestigated and unexplained** (risk 4).

## Open decisions & risks

1. **#265 is resolved — merged harmlessly** *(supersedes last session's risk
   1)*. Nothing to unwind; `chore/bump-cryptography-50` branch can be deleted
   if it still exists. Branch from `main` as usual.
2. **After cycling the daemon for the NOTIFY fault, verify before re-running**
   *(updated from "give it a minute")*. This session the queue stayed pinned
   ~5+ minutes after the restart. The gate is
   `SELECT pg_notification_queue_usage()` → `0` **and** `LISTEN
   daemon_commands` succeeding on `localmail_test` (use `-h localhost` — the
   runbook's socket-path psql invocations don't resolve from every shell).
3. **An empty `daemon_heartbeats` right after a daemon restart is normal for
   minutes** *(new — #269)*. It is the startup blob-temp sweep:
   `start_workers` wipes the whole table *before* the sweep (clean shutdown
   clears only the per-account rows), and workers spawn only after it — so an
   empty table says nothing about how the predecessor died. Confirm with
   `sample <pid>` (expect `os_scandir`), don't kill the process.
4. **The DGX drops are STILL UNEXPLAINED — five theories refuted, four of them
   mine** *(carried; not investigated this session)*. **Do not propose a sixth
   without a captured outage in which the host was demonstrably up throughout.**
   Triage with `journalctl --list-boots` first. **Power is not a candidate**
   (~5-day UPS; journal holds exactly one unclean stop, a scheduled PSU
   install). Reach it on the LAN, but **look the address up** — DHCP; it has
   been `192.168.68.62`, `192.168.68.76`, and now `192.168.1.99`. **Do not
   edit `/etc/wireguard/wg0.conf`.** Any `lan=FAIL` log line without an
   `@addr` suffix predates PR #260 and proves nothing.
5. **A single `tunnel=FAIL` probe sample is not an outage** *(carried)* —
   Starlink losing three packets on a ~900 ms path. Sustained = several
   consecutive samples. Both probes still running.
6. **`--relabel` is the only destructive verb in the lang path** *(carried)*.
   Discards **every** label; archive unsearchable by `lang:` until the drain
   completes. Prompts unless `--yes`. Reach for `--retry-declined` first.
   Budget ~45 min for a 100k-row archive; `reopen_all`'s bulk UPDATE shows
   **no** progress in `pg_stat_activity` until it commits.
7. **`body_lang_pending` means claimable work only** *(carried)*; the
   turned-away remainder is `body_lang_declined`. A steady non-zero `declined`
   is **normal**.
8. **Do not add normalisation steps to `lang_text.py` without a measurement**
   *(carried)*. Every candidate step beyond URL-stripping measured zero.
   `body_lang_low_accuracy` is retained but strictly worse — escape hatch only.
9. **`ExtractorConfigurationError` subclasses `TransientExtractorError`, and
   that subclassing *is* the #248 fix — do not "clean it up"** *(carried)*.
10. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
    Clear deliberately: `DELETE FROM attachment_text WHERE extractor =
    'type-skipped'`.
11. **Test trap: `_try_import_docling` must be monkeypatched in BOTH
    namespaces** *(carried)*. **OCR is macOS-only by default**; the
    `[extraction]` extra is the whole fix on Linux (~5.5 GB venv on aarch64).
12. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`.
13. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `--all-extras` on the Mac and `--extra mcp --extra extraction` on the DGX.
    **`uv` is not on the DGX's default non-interactive PATH** — use
    `~/.local/bin/uv` over SSH. Note the **pytest count depends on the extras**:
    2249 with all extras on the Mac; fewer without (`mcp` alone gates 3
    integration tests).
14. **Do not run the test suite while a backfill is draining** *(carried)* —
    shared-cluster contention produces dozens of false failures.
15. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
    locally (`AF_UNIX path too long`); deselect it, Linux CI is the real
    signal. Warning count flickers; websockets `DeprecationWarning` (#25),
    Starlette TestClient `httpx`, jsdom canvas noise all known.
16. **The stale NOTIFY queue recurs** *(carried; recurred THIS session)* — see
    risk 2 for the verified-clear procedure. Also: keep full pytest output —
    a `| tail` pipe discarded the tracebacks and made the known signature
    invisible to grep this session.
17. **No ROADMAP.md** *(carried)* — the `/nextsession` ROADMAP step is a no-op;
    slice status lives here + `docs/handoffs/` + the specs. README needed no
    update this session (no code change).
18. **Secrets/ACL invariants unchanged** *(carried)*: `secrets.configure`'s pin
    kept though #245 is fixed; #246 warns on group-write; `InsecureSecretsFile`
    refuses; #239's manual tombstone retention deliberate; admin bearer tokens
    have no per-token scope (#204).
19. **Run vitest from `gui/`, not the repo root** *(carried)*.
20. **`cargo clippy --all-targets` is clean but ungated** *(carried)*.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status                               # expect clean
git branch --show-current                # main
git log --oneline origin/main..main      # expect 0

# Nothing is pending on GitHub: session-18 PR (this handoff) should be merged
# or awaiting merge; 0 Dependabot alerts; 13 open issues (#269 newest).
gh pr list; gh issue list --limit 15

# Python test suite (deselect the macOS-only socket failure — risk 15).
# Do NOT run while a backfill is draining (risk 14).
unset VIRTUAL_ENV && uv run pytest -q --deselect tests/test_daemon_control_socket.py
#   expect: 2249 passed, 0 failed (with all extras installed — risk 13)
# If EXACTLY the three LISTEN/NOTIFY tests fail (risk 2/16): cycle the daemon,
# then VERIFY before re-running — do not just wait:
psql -h localhost -p 5532 -d postgres -U localmail -tAc 'SELECT pg_notification_queue_usage()'  # 0
psql -h localhost -p 5532 -d localmail_test -U localmail -c 'LISTEN daemon_commands'            # LISTEN

unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 137 source files

# Both hosts run ef831c0 — no deploy pending. Health checks only:
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
#   expect: 7 rows, ages under ~60 s (EMPTY for minutes right after a restart — risk 3)

# The DGX — LAN only, look the address up first (DHCP — risk 4); uv is not on
# the non-interactive PATH there (risk 13):
ssh 192.168.1.99 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
ssh 192.168.1.99 'cd ~/src/localmail && ~/.local/bin/uv run localmail search-status'
ssh 192.168.1.99 'docker exec localmail-pg psql -U localmail -d localmail -c \
  "SELECT worker_kind, state, now()-last_heartbeat_at AS age FROM daemon_heartbeats"'

# Probe log (control column carries the answering address — PR #260):
tail -5 ~/localmail-probe/tunnel-probe.log   # expect lan=ok(3/3)@<addr>

# Frontend — only if you touch gui/ (MUST run from gui/ — risk 19):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

`main` tip is **`ef831c0`** (merge of #265; carries #268's floor, #262/#259,
#263/#235, #255). **Both hosts run it.** Latest migration
**`0035_messages_body_lang_attempted_at.sql`**; next free slot `0036_*.sql`.
**Open issues: 13** (#269 filed this session). Dependabot: **0** open alerts.
