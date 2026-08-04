# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-05 (session 15).** `origin/main` is at **`5c7f6f2`**.
> This session closed **#251** — `body_lang` detection had been wedged
> **archive-wide on both deployments for weeks** — through PR **#253**, then
> deployed it to the Mac and the DGX and drained both archives. The deploy also
> finally landed **#221**, which session 14 shipped but never rolled out. The
> DGX "WireGuard drops" were **not** worked on and remain unexplained.

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

### 1. #251 — a declined body now leaves the lang-detect queue (PR #253 → `5c7f6f2`)

`run_lang_detect_pass` claimed `body_lang IS NULL AND body_text IS NOT NULL
ORDER BY id LIMIT N`. A row the detector **declined** — below
`body_lang_min_text_chars`, below `body_lang_min_confidence`, or a body that
made it raise — stayed NULL, so it kept satisfying the predicate and, under the
stable ordering, was re-claimed **in the same position forever**. Once the first
`body_lang_detect_batch_size` rows were all unlabelable, nothing behind them was
ever reached. Same shape as #216's un-rowed blob.

**The measured cost, against the live archive's actual head-600:**

| pass | visited | labelled |
|---|---|---|
| 1 | 200 | **0** ← the dead prefix |
| 2 | 200 | **193** |
| 3 | 200 | **199** |

Pass 1 returns 0, every drain loop reads that as "queue drained", and passes 2
and 3 never happen. The batch immediately behind the dead prefix is **96.5%
labelable** — the wedge was costing essentially the whole archive, not a sliver.
`body_lang` backs the `lang:` DSL token, so `lang:` had been silently matching
an arbitrary oldest-first sliver rather than erroring.

**The fix has two halves, and only the first is in the issue.**

**Half one — where "we tried and declined" is recorded.** Migration `0035` adds
`messages.body_lang_attempted_at`; the claim gains `AND body_lang_attempted_at
IS NULL`. A sentinel language value (`'und'`) was **rejected**: it would have
needed four readers to learn to exclude it (`arms.py`'s `lang:` filter,
`searcher._maybe_warn_unpopulated_body_lang`, `search-status`, migration 0015's
index) and would have repeated the **one-way door** CLAUDE.md already documents
for `type-skipped` — lowering a threshold would silently not re-open the rows it
was lowered for. `localmail lang-backfill --retry-declined` is the escape hatch a
sentinel cannot have.

**Half two — the return value, which the issue does not name.** The function
returned rows *labelled*, and both drain loops broke on 0. That was itself a
deliberate earlier fix (pinned by
`test_run_lang_detect_pass_loop_terminates_on_persistent_null`) to stop the loops
spinning forever. So skipping declined rows alone is **necessary but not
sufficient** — a batch that declines everything still returns 0 while having made
real progress. It now returns `LangDetectPass(visited, labelled)` and loops
terminate on `visited == 0`. There is deliberately **no `__bool__`**: an implicit
reading of this exact value is the defect.

Also in the fix:

- **One uniform write** stamps every visited row whether or not it gained a
  label, so labelling and declining cannot diverge.
- **Poison rows are stamped too.** The exception branch rolls back to its
  savepoint, discarding the stamp, so `_mark_attempted_safely` rewrites it under
  a *second* nested savepoint (`SAVEPOINT` outside the `try`, like
  `record_failed_message`). Without it a body that reliably crashes the detector
  starves the queue exactly as a declined one did.
- **`CLAIMABLE_WHERE_SQL` / `DECLINED_WHERE_SQL`** are the one authority, shared
  by the claim and both `search-status` counters. A test pins that they are
  disjoint and jointly exhaustive. The drift they prevent is what hid the bug.
- **The index is replaced under a NEW name**
  (`messages_body_lang_pending_idx` → `messages_body_lang_claimable_idx`).
  `CREATE INDEX IF NOT EXISTS` matches on **name only**, so recreating the old
  name with the new predicate would have silently no-opped on every host that
  already had it.
- **`body_lang_pending` was redefined** to claimable work only; the turned-away
  remainder is the new `body_lang_declined`.

`test_run_lang_detect_pass_loop_terminates_on_persistent_null` was **corrected,
not deleted** — termination now comes from `visited` reaching 0.

Design:
[docs/superpowers/specs/2026-08-05-lang-detect-starvation-design.md](docs/superpowers/specs/2026-08-05-lang-detect-starvation-design.md).

### 2. Deployed to both hosts — which also landed #221 at last

Neither host had #221 (session 14 shipped it but never rolled it out). Both are
now at `5c7f6f2`, with migration `0035` applied and services restarted.

| | before | after |
|---|---|---|
| **Mac** populated | 7744 (frozen for weeks) | **100914** |
| **Mac** claimable | 100025 | **0** |
| **Mac** declined | — | **6857** |
| **DGX** populated | 8324 (unchanged since session 13) | **28615** |
| **DGX** claimable | 21157 | **0** |
| **DGX** declined | — | **866** |

**Both archives drained completely** — `claimable` is **0** on each, which is
exactly the acceptance criterion the issue asked for.

- Mac: `done: 90427 messages processed, 84085 labelled`, ~25 minutes at
  ~4400 rows/min. `populated` rose by more than the backfill labelled (7744 →
  100914) because the restarted daemon's embed worker was detecting
  concurrently — the two cooperate through `FOR UPDATE SKIP LOCKED`.
- DGX: `done: 18757 messages processed, 18160 labelled`.

The residual `declined` counts are the genuinely-unlabelable remainder: **6.4%**
of bodied messages on the Mac, **2.9%** on the DGX — separator lines, bare URLs,
one-word replies.

### 3. Ops observations worth carrying

- **The stale NOTIFY queue recurred and cleared on the daemon restart** (Fault 1
  in [docs/operations/postgres-maintenance-runbook.md](docs/operations/postgres-maintenance-runbook.md)).
  Note it did **not** clear instantly — a re-run immediately after the restart
  still failed; a minute later all 11 tests passed. Do not conclude Option A
  failed without waiting.
- **The Mac daemon had the `ssl.SSLEOFError` IDLE crash again** before the
  restart (`08:51:14 inbox-idle session crashed for horst-gmail`), alongside a
  `psycopg.errors.ConnectionTimeout` in reconcile. Both predate the restart and
  the daemon is healthy now — 7 heartbeat rows, both accounts. Still the lead if
  per-account rows vanish again.
- **Do not run the test suite while a backfill is running.** Two full-suite runs
  during the Mac drain reported 75 failed/47 errors and 35 failed/7 errors; the
  same files pass alone. It is contention on the shared cluster, not a
  regression — but it will look like one.

### 4. Verification

- `uv run pytest -q --deselect tests/test_daemon_control_socket.py` →
  **2186 passed, 0 failed** (was 2173; **+13** — new tests in
  `test_lang_detect.py`, `test_cli_lang_backfill.py`, `test_search_schema.py`)
- `uv run mypy src/localmail` → clean, **134** source files
- `uv run ruff check src/localmail` → the pre-existing **10**, none in touched
  files
- CI green on PR #253 (Linux, PG pg18, Python 3.12)
- **Migration `0035`. No `gui/` changes.**

## What's next

### 0. Confirm the deployments held

```bash
# Mac
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
# DGX — LAN, and look the address up first (risk 4)
ssh 192.168.1.99 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'
```

**Acceptance:** 7 Mac heartbeat rows, ages under ~60 s, both accounts present;
three DGX units `active`. `body_lang_pending` should stay at or near 0 on both —
new mail is detected on the daemon's own sweeps.

### 1. **The embed worker's backoff does not count lang progress** *(new, unfiled)*

`run_embed_worker_once` returns embedded-chunk count only, and
`run_embed_worker` backs off on `wrote == 0` — up to `5 s × 7 = 35 s` per sweep.
Language detection runs inside the same sweep at 200 rows a time, so with the
embedding queue drained a host catches up at ~340 rows/minute. On the Mac's
100k-row backlog that would have been **~5 hours**; `lang-backfill` did it in
~25 minutes.

Harmless in steady state (new mail arrives far below 200/sweep) and the
docstring says the omission is deliberate ("orthogonal to the embedding queue").
File it, decide, don't drive-by fix. **Acceptance:** either a comment explaining
why a sweep that labelled 200 messages counts as empty, or a return shape that
resets the backoff.

### 2. **Remaining robustness issues** *(carried)*
   - **#218** — GUI download commands buffer the full body before enforcing the
     size ceiling.
   - **#235** — `search --smart` reports "could not reach the rewriter service"
     forever on a malformed `rewriter_base_url`.
   - **#226** — self-signed cert misses the reachable IP when `--bind 0.0.0.0`.
   - **#225 / #227** — `/v1/changes` subscription lifecycle gaps.
   - **#200 / #211 / #208** — admin panels silently swallow 4xx; surface as a toast.

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
   `AdminView.test.ts` and `MainView.test.ts`** (see risk 15).

### 4. **Close #90?** *(carried, still unanswered)*
   Its premise (the `glib` Dependabot alert) is dismissed as `not_used` and no
   longer appears among open alerts. Either close it, or repurpose it explicitly
   as "bump the Tauri stack for its own sake" with a real acceptance criterion.

### 5. **Smaller, deliberately not done this session**
   - **`daemon.py` is 567 lines** and `cli.py` is **1864**, both over the
     500-line guideline. `cli.py` grew ~40 lines this session. Real refactors,
     out of #251's scope.
   - **165 `docling: File format not allowed` failures on the Mac** (of 182
     total). Legitimate content-level failures, but each burns `retry_count`
     three times for a format docling will never accept — an earlier gate might
     be worth it. Low priority.
   - **The Mac probe's `lan` control column is broken** — it still targets
     `192.168.68.76`, which the DGX no longer holds, so it reads `FAIL`
     permanently and the probe has lost its "is the host alive" control. The
     address is a DHCP lease, so resolve it per-sample rather than hardcoding
     it again.

## Open decisions & risks

1. **`body_lang_pending` changed meaning** *(new)*. It now counts only what the
   worker will claim; rows the detector ran on and declined are
   `body_lang_declined`. A steady non-zero `declined` is **normal** (separator
   lines, bare URLs, one-word replies) — 866 on the DGX, **6857** on the Mac.
   Do not "fix" it by lowering thresholds without measuring: only
   `lang-backfill --retry-declined` moves rows back to pending.
2. **Rows labelled before migration 0035 keep a NULL `body_lang_attempted_at`**
   *(new)*. Legal and never consulted — the claim excludes `body_lang IS NOT
   NULL` first. Do not "backfill for consistency"; it would be a 30k-row write
   that changes nothing.
3. **`LangDetectPass` deliberately has no `__bool__`** *(new)*. `if not result:`
   reads ambiguously and an implicit reading of this value is exactly what
   wedged the archive. Callers must write `result.visited == 0`.
4. **The DGX drops are STILL UNEXPLAINED — five theories refuted, four of them
   mine** *(carried; not investigated this session)*. Every one was a single
   observation confidently generalised. **Do not propose a sixth without a
   captured outage in which the host was demonstrably up throughout.** Triage
   with `journalctl --list-boots` first. **Power is not a candidate**: the DGX is
   on a ~5-day UPS and the journal holds exactly one unclean stop, a scheduled
   PSU install. Reach it on the LAN, but **look the address up** — it is DHCP and
   has been `192.168.68.62`, `192.168.68.76`, and now `192.168.1.99`, on two
   different subnets. **Do not edit `/etc/wireguard/wg0.conf`.**
5. **A single `tunnel=FAIL` probe sample is not an outage** *(carried)* — it is
   Starlink losing three packets on a ~900 ms path. Sustained means several
   consecutive samples. Both probes are still running.
6. **`ExtractorConfigurationError` subclasses `TransientExtractorError`, and that
   subclassing *is* the #248 fix — do not "clean it up"** *(carried)*.
7. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
   Clear deliberately: `DELETE FROM attachment_text WHERE extractor =
   'type-skipped'`. Note #251 deliberately did **not** repeat this shape.
8. **Test trap: `_try_import_docling` must be monkeypatched in BOTH namespaces**
   *(carried)*.
9. **OCR is macOS-only by default** *(carried)*, but installing the
   `[extraction]` extra is the whole fix on Linux. Budget ~5.5 GB of venv on
   aarch64.
10. **`secrets.configure`'s pin is kept even though #245 is fixed** *(carried)*.
11. **#246 warns rather than refuses on group-write** *(carried)*.
12. **`InsecureSecretsFile` refuses rather than warns** *(carried, deliberate)*.
13. **#239's manual tombstone retention is deliberate** *(carried)*.
14. **Admin bearer blast radius** *(carried, #204)*: a token issued to an
    `is_admin` user is an admin credential — no per-token scope.
15. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`.
16. **`uv sync` without extras silently downgrades a host** *(carried)*. Use
    `--all-extras` on the Mac and `--extra mcp --extra extraction` on the DGX.
17. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
    locally (`AF_UNIX path too long`); deselect it, Linux CI is the real signal.
    Also carried: psycopg_pool teardown `ResourceWarning`s, the websockets
    `DeprecationWarning` (#25), Starlette TestClient `httpx` `DeprecationWarning`,
    jsdom canvas noise in gui vitest.
18. **No ROADMAP.md** *(carried, re-confirmed)* — the `/nextsession` ROADMAP step
    is a no-op; slice status lives in NEXT_SESSION + `docs/handoffs/` + the specs.
    README **was** updated this session (`--retry-declined`, the
    pending/declined split).
19. **Run vitest from `gui/`, not the repo root** *(carried)*.
20. **`cargo clippy --all-targets` is clean but ungated** *(carried)*.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status                               # expect clean
git branch --show-current                # main
git log --oneline origin/main..main      # expect 0 — everything is pushed

# Python test suite (deselect the macOS-only socket failure — see risk 17).
# Do NOT run this while a backfill is draining — contention looks like a
# regression (see "Ops observations").
unset VIRTUAL_ENV && uv run pytest -q --deselect tests/test_daemon_control_socket.py
#   expect: **2186 passed, 0 failed**
unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 134 source files

# If exactly 3 LISTEN/NOTIFY tests fail with "could not access status of
# transaction N", it is the stale NOTIFY queue, not a code bug — Fault 1 in
# docs/operations/postgres-maintenance-runbook.md. Cycling the daemon clears
# it, but give it a minute before re-running.

# #251 — confirm both hosts stayed drained (claimable should be ~0):
psql -h localhost -p 5532 -U localmail -d localmail -c "
  SELECT count(*) FILTER (WHERE body_lang IS NOT NULL) AS populated,
         count(*) FILTER (WHERE body_lang IS NULL AND body_text IS NOT NULL
                            AND body_lang_attempted_at IS NULL) AS claimable,
         count(*) FILTER (WHERE body_lang IS NULL AND body_text IS NOT NULL
                            AND body_lang_attempted_at IS NOT NULL) AS declined
    FROM messages"

# The DGX — LAN only, and look the address up; it is DHCP (risk 4):
ssh 192.168.1.99 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'

# Frontend — only if you touch gui/ (MUST be run from gui/ — see risk 19):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

Session-15 work is **`5c7f6f2`** (PR #253, squash-merged; contains the design
spec and the fix). Both deployments are at `5c7f6f2` — **both now have #221 and
#251**. Latest migration **`0035_messages_body_lang_attempted_at.sql`**; next
free slot `0036_*.sql`. **Open issues: 12** — **#251 closed**. Dependabot: **0**
open alerts.
