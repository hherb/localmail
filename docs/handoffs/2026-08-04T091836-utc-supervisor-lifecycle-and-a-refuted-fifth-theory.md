# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-08-04 (session 14).** `origin/main` was at `f834ad2`.
> This session closed **#221** (five daemon-supervisor lifecycle defects) in
> **`7c063ab`** and found a **new live archive-wide bug, #251**: `body_lang`
> detection has been permanently wedged on **both** deployments. The DGX
> "WireGuard drops" remain **unsolved** — the probes captured their first
> sustained outage, but it was a scheduled PSU installation, so it is eliminated
> rather than diagnostic; a fifth theory ("the DGX loses power") was proposed
> and refuted within the session (§2). Two ops actions were taken on the Mac:
> the stale NOTIFY queue was cleared per the runbook, and the sync daemon
> (which was degraded, see §0.1) was restarted.

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

### 1. #221 — daemon supervisor lifecycle robustness (`7c063ab`)

Five defects sharing the supervisor/shutdown area, each built TDD with the
failing test watched first.

**A — the two shutdown budgets were the same number meaning different things.**
`run_forever`'s teardown joined every thread with its *own*
`shutdown_grace_seconds` timeout — idle then poll per account, sequentially — so
the real worst case was `2 × accounts × grace`, while `DaemonSupervisor.stop()`
waited exactly one `grace` before SIGKILL. With two or more accounts an ordinary
stop or restart **SIGKILLed a healthy child**.

The new pure-ish [src/localmail/shutdown_budget.py](src/localmail/shutdown_budget.py)
owns both halves so they cannot drift apart again:

- `wind_down_threads` sets **every stop event before the first join** — the
  load-bearing part. Signalled up front the workers wind down concurrently, so
  the budget bounds the *slowest* rather than their sum.
- `remaining_seconds` (pure) spends it as one wall-clock deadline. It clamps at
  0 because `Thread.join(timeout=<negative>)` returns immediately rather than
  raising — a negative remainder would silently skip every remaining join while
  looking like a wait.
- `supervisor_kill_after` (pure) = child budget + `SUPERVISOR_KILL_MARGIN_S`,
  covering the fixed work *after* the last join (pool close, final log, exit).

`Daemon._teardown_account` **deliberately keeps its own per-account timeout** —
that path removes one account from a daemon that keeps running, so it has no
global deadline to share.

**B** — `supervisor.close()` blocked the asyncio event loop for up to the grace
period on serve shutdown; now `await anyio.to_thread.run_sync(...)`.

**C** — `request_*` after `close()` stuck the state machine at `starting`
forever (`request_start` set STARTING, then `start()` saw `_closing` and
returned without touching it). All three now refuse via the shared
`_admit_lifecycle_request` guard **before any state is written**. The blocking
`start()` keeps its silent no-op — #149's guard is what an in-flight async
restart lands on during teardown and it must not raise there.

**D** — `send_control_request` wrapped only `connect()`; a stalled peer raised a
bare `socket.timeout` and a mid-write hangup a `BrokenPipeError`, both escaping
the CLI's `except ControlSocketError` as a traceback. The whole exchange is
wrapped now.

**E** — control-socket bind/chmod TOCTOU: bind now runs under a private umask,
restored in `finally` including on bind failure.

### 2. The DGX "WireGuard drops" — still open; a fifth theory proposed and refuted (`bc26847`, `0e8dd6c`)

The probes captured their **first sustained outage** — the Mac saw
`tunnel=FAIL` for 30.5 minutes (2026-08-03T23:31:17Z → 00:02:21Z = 09:31 →
10:02 AEST). Three non-network signals showed the **host was down** for that
window: a `journalctl --list-boots` boundary inside it, a 31.5-minute gap in a
`Restart=always` probe sampling at 30 s, and `wg0` counters back at `0 0 0 0`.
The stop was **unclean** — zero `Reached target Shutdown` lines.

From that I concluded "the DGX loses power" and rewrote the doc as solved.
**That was wrong, and the operator corrected it**: the outage was a scheduled
**redundant PSU installation**, and the DGX is on a **UPS rated ~5 days**.

Checking whether it generalised — which I should have done before concluding —
settles it. The journal holds **exactly one** unclean stop:

| boot | clean-shutdown lines | gap before next boot |
|---|---|---|
| -4 (Aug 1) | 8 | 48 s |
| -3 (Aug 2) | 16 | 28 s |
| -2 (Aug 3) | 7 | 32 s (session 13's cold-boot proof) |
| -1 (Aug 4) | **0** | **31m46s** (the PSU install) |

Every other boundary is an ordinary fast reboot. **So the measurement window
contains zero *unexplained* tunnel outages** — the recurring fault has not been
caught yet. Keep both probes running.

What the data does support: **`hub=FAIL` in 0 of 1971 samples** (first direct
observation of the hub, though weak while the window holds no real outage), and
**isolated single-sample `tunnel=FAIL` readings are packet loss**, not outages —
three lost pings on a ~900 ms hairpin.

[docs/operations/wireguard-drop-measurement.md](docs/operations/wireguard-drop-measurement.md)
is back to an open investigation. Its diagnostic sequence now leads with "was
the host even up?" as **triage** — one command, and it disposed of this event —
not as an answer. All five refuted theories are listed, the fifth being mine
from this session, with the recurring lesson stated: each was one observation
confidently generalised.

**The LAN escape hatch moved: the DGX is `192.168.1.99` now**, not
`192.168.68.76` — it rejoined SSID `STARLINK` on a different subnet from the
Mac's `192.168.68.69/22`. Still the right way in (45 ms vs ~900 ms hairpinned)
but it is a **DHCP lease that moves across boots**, so look it up. Note this
also broke the Mac probe's `lan` control column, which now reads `FAIL`
permanently until the probe's target is updated.

### 3. Verification

- `uv run pytest -q --deselect tests/test_daemon_control_socket.py` →
  **2173 passed** (was 2137; **+36**)
- `uv run mypy src/localmail` → clean, **134** source files (was 133)
- `uv run ruff check src/localmail` → the pre-existing **10**, none in touched files
- **No migration. No `gui/` changes.**

## What's next

### 0. Finish what this session could not

#### 0.0 Everything is pushed; #221 is closed

`origin/main` is at the correction commit. Note that **`bc26847`'s commit
message asserts the refuted "power losses" conclusion** — it was already pushed
when the operator corrected it, so it is superseded forward rather than
rewritten. The doc and this handoff are correct; that one message is not.

#### 0.1 The Mac daemon was degraded, and is now healthy — confirm it stayed that way

Two ops actions were taken:

1. **The stale NOTIFY queue was cleared.** Three `LISTEN`/`NOTIFY` tests failed
   with `could not access status of transaction 491204825` — the documented
   Fault 1 in
   [docs/operations/postgres-maintenance-runbook.md](docs/operations/postgres-maintenance-runbook.md).
   Runbook Option A (cycle the sync daemon) cleared it;
   `pg_notification_queue_usage()` went to 0 and all 11 tests passed. Not caused
   by this session's changes.
2. **The daemon was restarted, and it needed it.** Before the restart it had
   only 3 heartbeat rows (`reconcile`/`extract`/`embed`) and **no per-account
   `idle`/`poll` rows at all**, with an `ssl.SSLEOFError` IDLE crash loop in
   `~/Library/Logs/localmail/daemon.err.log`. After: 6 rows, both accounts
   syncing.

Note `launchctl bootout` printed `Bad request.` yet **did** deliver the SIGTERM
(the daemon log shows `received signal 15`). Do not read that message as "no
effect".

```bash
launchctl print gui/$UID/com.localmail.daemon | grep -E '^\s*(state|pid) '
psql -h localhost -p 5532 -U localmail -d localmail -c \
  "SELECT worker_kind, account_id, state, now()-last_heartbeat_at AS age
     FROM daemon_heartbeats ORDER BY worker_kind, account_id"
```
**Acceptance:** 6 rows, all ages under ~60 s, both accounts present. If the
per-account rows vanish again, the SSLEOFError IDLE loop is the lead.

#### 0.2 Deploy #221 to both hosts

Neither host has it. The DGX is two commits behind at `a7013c5` (all three units
`active`). Note **`supervise_daemon = false` on the Mac**, so A/B/C only bite a
deployment that turns Plane B on — the daemon-side half of A (the shared
teardown deadline) applies everywhere regardless.

```bash
# Mac
unset VIRTUAL_ENV && uv sync --all-extras          # NOT a bare uv sync (risk 12)
launchctl kickstart -k gui/$UID/com.localmail.daemon
# DGX — LAN, and look the address up first (risk 5)
ssh 192.168.1.99 'cd ~/src/localmail && git pull && \
  export PATH=$HOME/.local/bin:$PATH && uv sync --extra mcp --extra extraction && \
  systemctl --user restart localmail-daemon localmail-serve'
```

### 1. **#251 — `body_lang` detection is permanently wedged** *(new, filed this session; the biggest live defect)*

`run_lang_detect_pass` returns **0 on every call, forever**, on **both**
deployments. Same shape as #216.

| host | populated | pending |
|---|---|---|
| Mac | **7744** (frozen across a daemon restart and repeated sweeps) | 99989 → 99998 (growing) |
| DGX | **8324** (identical to the value in the session-13 handoff, days old) | 21078 → 21149 (growing) |

The claim query is `WHERE body_lang IS NULL AND body_text IS NOT NULL ORDER BY
id LIMIT batch`. A row the detector **declines** (too short, below
`body_lang_min_confidence`) stays NULL, so it still satisfies the predicate and —
with a stable `ORDER BY id` — is re-selected in the same position forever. The
head of the Mac's queue is separator lines, bare URLs, and a single space; all
200 are re-read every sweep and `updated` is 0, which the embed worker and the
`lang-backfill` CLI loop both read as "queue drained".

The docstring anticipates half of this and frames it as a *termination*
property; with a stable ordering it is a *starvation* property.

**Blast radius:** `body_lang` backs the `lang:` search filter, so `lang:` matches
a small arbitrary oldest-first subset and returns wrong results rather than
erroring.

**Open decision (see §Risks 1):** sentinel value vs. a `body_lang_attempted_at`
column (migration `0035`). **Acceptance:** with an archive whose first
`batch_size` pending rows are all unlabelable, a sweep still labels rows further
down, and repeated sweeps drive `body_lang_pending` toward the genuinely
unlabelable remainder.

### 2. **Remaining robustness issues** *(carried, #221 now closed)*
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
   `AdminView.test.ts` and `MainView.test.ts`** (see risk 11).

### 4. **Close #90?** *(carried, still unanswered)*
   Its premise (the `glib` Dependabot alert) is dismissed as `not_used` and no
   longer appears among open alerts. Either close it, or repurpose it explicitly
   as "bump the Tauri stack for its own sake" with a real acceptance criterion.

### 5. **Smaller, deliberately not done this session**
   - **`daemon.py` is 567 lines**, over the 500-line guideline. It was already
     526 before this session; `wind_down_threads` was placed in
     `shutdown_budget.py` partly to stop the growth. Reducing further means a
     real refactor, which was out of #221's scope.
   - **165 `docling: File format not allowed` failures on the Mac** (of 182
     total; the other 17 are a `.ics` that is actually binary). Both are
     *legitimate* content-level failures, not a repeat of #248/#249 — but each
     burns `retry_count` three times for a format docling will never accept, so
     an earlier gate might be worth it. Low priority.
   - **The Mac probe's `lan` control column is broken** — it still targets
     `192.168.68.76`, which the DGX no longer holds, so it reads `FAIL`
     permanently and the probe has lost its "is the host alive" control.
     Repointing it at the current address is a one-line edit to
     `~/localmail-probe/tunnel-probe.sh`, but the address is a DHCP lease, so
     consider resolving it per-sample instead of hardcoding it again.

## Open decisions & risks

1. **#251's fix is a real design choice, and #216 is the precedent to read
   first.** The blob path used a sentinel (`type-skipped`) and CLAUDE.md now
   documents that sentinel as a **one-way door**: widening the allowlist does
   *not* re-open skipped blobs. A `body_lang` sentinel would repeat that
   (lowering `body_lang_min_confidence` would silently not re-open the rows it
   was lowered for). A `body_lang_attempted_at` column avoids it at the cost of
   migration `0035`. Do not pick the sentinel just because it needs no migration.
2. **`SUPERVISOR_KILL_MARGIN_S` is a constant, not a config knob** — deliberate.
   It is not a policy an operator tunes, it is the fixed cost of an orderly exit
   (pool close, final log, interpreter teardown). Any value > 0 restores the
   contract; 5 s is generous enough that a loaded host does not turn a clean
   stop into a SIGKILL.
3. **Two #221 tests assert on *source text*** —
   `test_serve_gives_the_supervisor_more_than_the_childs_own_grace` and
   `test_lifespan_does_not_call_supervisor_close_synchronously` grep
   `serve/app.py`. That is deliberate (building a real app needs a DB, and the
   wiring is the claim) but it is brittle to renames. If you refactor
   `create_app`, expect these two to need updating — they are guarding real
   invariants, not style.
4. **The DGX drops are STILL UNEXPLAINED — five theories refuted, four of them
   mine.** Every one was a single observation confidently generalised, including
   this session's ("the DGX loses power"). **Do not propose a sixth without a
   captured outage in which the host was demonstrably up throughout.** Triage
   with `journalctl --list-boots` first — one command, and it eliminated the only
   event captured so far. **Power is not a candidate**: the DGX is on a ~5-day
   UPS and the journal holds exactly one unclean stop, which was a scheduled PSU
   install. Reach it on the LAN, but **look the address up** — it is DHCP and has
   been `192.168.68.62`, `192.168.68.76`, and now `192.168.1.99`, on two
   different subnets. **Do not edit `/etc/wireguard/wg0.conf`.**
5. **A single `tunnel=FAIL` probe sample is not an outage** — it is Starlink
   losing three packets on a ~900 ms path. Sustained means several consecutive
   samples. The probes are left running on both hosts.
6. **`ExtractorConfigurationError` subclasses `TransientExtractorError`, and that
   subclassing *is* the #248 fix — do not "clean it up"** *(carried)*.
   `_is_transient` recognises the base class, so `retry_count` is never burned
   and the bound becomes the #153 transient budget.
7. **`type-skipped` is a one-way door for a widened allowlist** *(carried)*.
   Clear deliberately: `DELETE FROM attachment_text WHERE extractor =
   'type-skipped'`. `retry-failed-extractions` deliberately does not.
8. **Test trap: `_try_import_docling` must be monkeypatched in BOTH namespaces**
   *(carried)*. `extract_worker` holds its own reference and *that* is the one
   gating `docling_avail`; patching only `extractor`'s copy asserts nothing where
   docling is absent (CI).
9. **OCR is macOS-only by default** *(carried)*, but **installing the
   `[extraction]` extra is the whole fix on Linux** — `auto` selects rapidocr on
   onnxruntime. Budget ~5.5 GB of venv on aarch64 (docling drags in torch+CUDA,
   which OCR does not use).
10. **`secrets.configure`'s pin is kept even though #245 is fixed** *(carried)*.
11. **#246 warns rather than refuses on group-write** *(carried)*; `mode_is_private`
    and `directory_exposure` are siblings, not one shared rule. Do not merge.
12. **`InsecureSecretsFile` refuses rather than warns** *(carried, deliberate)*.
13. **#239's manual tombstone retention is deliberate** *(carried)* — an automatic
    sweep would silently delete the only record of permanently lost mail.
14. **Admin bearer blast radius** *(carried, #204)*: a token issued to an
    `is_admin` user is an admin credential — no per-token scope.
15. **CI trap** *(carried)*: any GUI admin panel that fetches on mount MUST be
    stubbed in **both** `AdminView.test.ts` and `MainView.test.ts`.
16. **`uv sync` without `--all-extras` silently downgrades the Mac** *(carried)*.
    Use `--all-extras` on the Mac and `--extra mcp --extra extraction` on the DGX.
17. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
    locally (`AF_UNIX path too long`); deselect it, Linux CI is the real signal.
    The new `test_daemon_control_socket_robustness.py` works around this with a
    short `tempfile.TemporaryDirectory` fixture rather than `tmp_path` — reuse
    that pattern for any new AF_UNIX test. Also carried: psycopg_pool teardown
    `ResourceWarning`s, the websockets `DeprecationWarning` (#25), Starlette
    TestClient `httpx` `DeprecationWarning`, jsdom canvas noise in gui vitest.
18. **No ROADMAP.md** *(carried, re-confirmed)* — the `/nextsession` ROADMAP step
    is a no-op; slice status lives in NEXT_SESSION + `docs/handoffs/` + the specs.
    README **was** updated this session (a "How long a stop takes" subsection
    under Daemon control, plus the `daemon start/stop/restart` row).
19. **Run vitest from `gui/`, not the repo root** *(carried)*.
20. **`cargo clippy --all-targets` is clean but ungated** *(carried)*.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status                               # expect clean
git branch --show-current                # main
git log --oneline origin/main..main      # expect 0 — everything is pushed

# Python test suite (deselect the macOS-only socket failure — see risk 17):
unset VIRTUAL_ENV && uv run pytest -q --deselect tests/test_daemon_control_socket.py
#   expect: 2173 passed
unset VIRTUAL_ENV && uv run mypy src/localmail
#   expect: Success, 134 source files

# If exactly 3 LISTEN/NOTIFY tests fail with "could not access status of
# transaction N", it is the stale NOTIFY queue, not a code bug — Fault 1 in
# docs/operations/postgres-maintenance-runbook.md. Option A worked this session.

# §1 / #251 — confirm the wedge is still there (both numbers should be frozen):
unset VIRTUAL_ENV && uv run localmail search-status   # slow (~5 min under load)

# The DGX — LAN only, and look the address up; it is DHCP (risk 4):
ssh 192.168.1.99 'systemctl --user is-active localmail-daemon localmail-serve localmail-wgprobe'

# Frontend — only if you touch gui/ (MUST be run from gui/ — see risk 19):
cd gui && npm run check && npm test && npm run build && cd ..
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings \
  && cargo clippy --all-targets -- -D warnings && cd ../..
```

Session-14 code is **`7c063ab`**; docs `bc26847` + the correction commit. All
pushed. Deployments are at `e620aa5` (Mac) and `a7013c5` (DGX) — **neither has
#221**. Latest migration **`0034_transient_fetches_gave_up.sql`**; next free
slot `0035_*.sql` (likely #251). **Open issues: 13** — **#221 closed**, **#251
filed**. Dependabot: **0** open alerts.
