# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-31T0030 UTC.**
> This session shipped **2B.1 — daemon account hot-reload**: a running
> `localmail run` daemon now converges on the DB's syncable account set
> **without a restart** — spawning threads for newly-syncable accounts, tearing
> down threads for accounts that became non-syncable (deleted / paused /
> archived), and respawning an account whose config *or credentials* changed.
> All work is committed on branch **`daemon-control-2b-respec`** (11 commits;
> tip `9b18747`). **No PR opened yet** (see "What's next §0"). Full suite
> **1081 passed**, mypy clean (76 files). Final holistic code review: **ready
> to merge**, no issues.
>
> 2B.1 is **slice 1 of 5** of a re-spec'd "daemon control (2B)" arc. The
> remaining slices (heartbeats, command queue, supervisor+HTTP, admin-UI panel)
> are designed but unbuilt — see the spec and §2 below.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The **database is canonical for accounts**
end-to-end (init-db TOML→DB seed, daemon, CLI). Until this session the daemon
read its account set **once at construction**, so any account add/remove/
enable/disable/credential-change needed a daemon restart to take effect. 2B.1
makes that live. Downstream consumers read the DB + attachment tree directly or
via the `localmail serve` HTTPS API (admin UI under
[src/localmail/serve/admin/](src/localmail/serve/admin/)). See
[CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we shipped this session

### Design (supersedes §2B of the admin-UI design)

Re-spec'd "daemon control (2B)" as a **full superset** with **two control
planes** and **5 slices**:
- **Plane A — DB-mediated** (supervisor-agnostic, works under systemd too):
  hot-reload/reconcile, heartbeats, command queue.
- **Plane B — process lifecycle** (only when `serve` supervises): the
  subprocess `DaemonSupervisor` (start/stop/restart) from the original design.

Spec: [docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md](docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md)
(commit `61b8614`). Plan for slice 1:
[docs/superpowers/plans/2026-05-31-daemon-hot-reload-2b1.md](docs/superpowers/plans/2026-05-31-daemon-hot-reload-2b1.md)
(`e345ef0`, `6a7f5de`).

### 2B.1 — reconcile engine (the code)

- **NEW `src/localmail/daemon_reconcile.py`** — pure
  `plan_reconcile(running, desired) -> ReconcilePlan` diffing two
  `{account_id: updated_at}` maps into sorted `to_spawn` / `to_teardown` /
  `to_respawn` id tuples. No IO, no threads. (`afcc273`)
- **`src/localmail/daemon.py`** — per-account thread registry: `AccountThreads`
  dataclass, `self._account_threads: dict[int, AccountThreads]` +
  `self._worker_threads`. Each account gets its **own** `threading.Event`; the
  master `_stop_event` still drives the embed/extract workers + shutdown. New
  helpers `_spawn_account` / `_teardown_account` / `_running_fingerprints` /
  `_pool_sizes` / `_resize_pool` / `reconcile`. `run_forever` is now
  `while not self._stop_event.wait(reload_seconds): self.reconcile()` (the old
  zero-accounts early-exit is gone — the daemon stays up and picks accounts up
  live). Pool resizes via `pool.resize(...)` when the account count changes
  (skipped when `pool_max_size` is operator-pinned). (`71d7d27`, `83efe5f`,
  `ad7de27`, `1900385`)
- **`src/localmail/config.py`** — `DaemonConfig.reload_seconds` (int, 30) and
  `shutdown_grace_seconds` (float, 30.0). (`f4f7a4d`)
- **Credential-refresh participation** — new
  `accounts.touch_account_updated_at(conn, id)` (`UPDATE … SET updated_at =
  now()`, raises `NotFound` on unknown id), called from `oauth.complete_oauth`,
  the admin password route, and CLI `add-account` / `oauth-login`. Without this
  a re-login wouldn't change `updated_at` and the daemon would keep the stale
  credential. (`dc9334e`)
- **Docs**: `config.example.toml` + README run-row hot-reload note (`9b18747`).

### Commits on `daemon-control-2b-respec` (oldest → newest, 11 total)

```
61b8614  docs(2b): full re-spec of daemon control
e345ef0  docs(2b.1): implementation plan
6a7f5de  docs(2b.1): credential-touch task robustness
afcc273  feat(daemon): pure account-reconcile diff planner        (Task 1)
f4f7a4d  feat(config): reload_seconds + shutdown_grace_seconds     (Task 2)
71d7d27  refactor(daemon): per-account thread registry             (Task 3)
83efe5f  refactor(daemon): centralise pool-size formula            (Task 3 review)
ad7de27  feat(daemon): reconcile loop for live hot-reload          (Task 4)
1900385  fix(daemon): snapshot account registry in stop()          (Task 4 review)
dc9334e  feat(accounts): bump updated_at on credential change      (Task 5)
9b18747  docs: document the two daemon knobs + README note         (Task 6)
```

### Verification (this session)

- `unset VIRTUAL_ENV && uv run pytest -q tests/` — **1081 passed** (baseline
  1063 + 18 new: 6 reconcile, 3 config, 7 hot-reload, 2 touch-helper, 1
  complete_oauth-bump).
- `unset VIRTUAL_ENV && uv run mypy src/localmail` — **clean, 76 files**.
- Per-task spec + code-quality reviews via subagents (Tasks 1–4); Task 5
  verified by **two** spec reviewers (1 def + 4 call sites, top-level oauth
  import, meaningful tests, no weakening). Final holistic review (opus):
  **ready to merge**, no Critical/Important/Minor issues at ≥80 confidence.

## What's next

### 0. **Open the PR for 2B.1** *(immediate)*

The branch is complete and green but **no PR exists yet**. Open it:

```bash
gh pr create --base main --head daemon-control-2b-respec \
  --title "feat(daemon): account hot-reload without restart (2B.1)" \
  --body "Slice 1 of the re-spec'd daemon-control (2B) arc. …"
```

There is **no GitHub issue** for 2B.1 (it came out of the re-spec), so nothing
to "close". After merge: `git fetch --prune`, ff `main`, delete the branch.

### 1. **Optional follow-ups noted by review (non-blocking)**

The Task-4 review flagged two non-blocking items (final review found nothing to
add):
- **reconcile `connect_timeout`:** `reconcile()`'s `psycopg.connect(self._dsn)`
  has no `connect_timeout`, so a network black-hole to Postgres could block the
  reconcile loop (and shutdown responsiveness) for the OS TCP default. Doing it
  right needs a **new `DaemonConfig` knob** (no-magic-numbers rule), hence a
  deliberate follow-up, not a drive-by.
- **bulk-respawn pool transient:** many accounts changing in one tick can
  briefly need more connections than `max_size` until torn-down threads release
  theirs (sequential join + end-of-loop resize). Not a leak/deadlock — old
  threads exit and release. Worth a one-line note in the spec limitations.

### 2. **2B.2–2B.5** *(the rest of the daemon-control arc — later, each its own plan+PR)*

Per the spec, in order:
- **2B.2 — Heartbeats**: migration `0023_daemon_heartbeats.sql` (NOTE: the spec
  **fixes** the original admin-UI schema — `worker_kind`-keyed rows + two
  partial unique indexes, because each account has *two* threads + there are
  process-level workers). `heartbeat.py` writer called from each loop; reader
  `api/admin/daemon.py::get_daemon_status`. New knob
  `daemon.heartbeat_stale_seconds` (120). Startup `DELETE` of stale rows
  (single-instance assumption).
- **2B.3 — Command queue**: migration `0024_daemon_commands.sql`
  (`reload-now` / `restart-account` / `drain-stop`); daemon drains on each
  reconcile tick (`FOR UPDATE SKIP LOCKED`) + optional `LISTEN/NOTIFY`.
- **2B.4 — DaemonSupervisor + HTTP + CLI**: `serve/daemon_supervisor.py`
  (subprocess), `/v1/admin/daemon*` routes, Unix control socket,
  `localmail daemon {status,start,stop,restart,reload}`. Reuses
  `shutdown_grace_seconds` (shipped this session) for SIGTERM→SIGKILL.
- **2B.5 — Admin UI panel**: `serve/admin/templates/daemon/panel.html` +
  dashboard card; HTMX-polled status; method-bound CSRF (#122/#125).

### 3. **Other open arcs / deferred** *(unchanged from prior handoff)*

- **Admin-UI Sub-plan 2A.3** (account screens; fold #125 method-bound CSRF mint)
  — independent of 2B, still open.
- Externally-blocked / measured: **#90** (glib/Tauri Dependabot), **#47**
  (extract_worker transient opt-in), **#25** (websockets.legacy depwarn),
  **#5** (search batch INSERT), **#134** (oauth_state flake — environmental).
- **Open issues: 6** (#5, #25, #47, #90, #125, #134). 2B.1 closes no issue.

## Open decisions & risks

1. **The reconcile diff key is `(account_id, updated_at)`.** Any change that
   should trigger a respawn MUST bump `accounts.updated_at`. Credential paths
   now do (Task 5). If you add a new account-mutating path, bump `updated_at`
   (via `update_account`, which already does, or `touch_account_updated_at`).
2. **`reconcile()` uses a fresh `psycopg.connect`, not the shared pool.** This
   was deliberate and endorsed in review (pool is sized for the long-lived
   workers; the read is sub-ms; connect/exhaustion both fall into the
   error-swallow). Don't "optimise" it into a pool borrow without re-sizing the
   pool formula. (See §1 follow-up re: adding a `connect_timeout`.)
3. **Per-account vs master event.** Idle/poll threads get a **per-account**
   `stop_event`; embed/extract + the run_forever wait use the **master**
   `_stop_event`. `_handle_signal` sets only the master (→ breaks the wait →
   `finally` tears down accounts). `stop()` sets both (and snapshots the
   registry with `list()` to avoid a reconcile race — `1900385`).
4. **Teardown latency is bounded by the IDLE heartbeat tick (~30s),** not
   `idle_renew_seconds` — `_idle_step` re-checks `ctx.stop` every
   `HEARTBEAT_SECONDS`. Don't "fix" this by shrinking `idle_renew_seconds`.
   `_teardown_account` joins idle then poll **sequentially** (documented
   non-goal; could be made concurrent later).
5. **Tooling instability this session.** The harness intermittently
   **mangled / truncated tool output** (Bash output dropped or appended bogus
   fragments; Read once injected a stray line; some subagent batches cancelled
   mid-cascade). The **repository is intact** — every commit SHA was
   re-verified against `git`, the full suite + mypy were run green directly, and
   the final review confirmed ready-to-merge. If the next session sees the same
   thing: run **one command at a time** (avoid large parallel tool batches with
   inter-dependencies), prefer short single-line outputs, and trust
   `pytest`/`mypy` exit signals over rendered text. A fresh session/terminal
   likely clears it.
6. **Migration numbering.** 2B.1 needed **none**. Next free slots:
   `0023_daemon_heartbeats.sql` then `0024_daemon_commands.sql` (re-check
   `ls migrations/` at plan-time). Latest applied is `0022`.
7. **`.claude/` local files** stay untracked, by design.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # clean apart from .claude/ local files
git branch -vv                           # main + daemon-control-2b-respec (tip 9b18747)
git --no-pager log --oneline -11         # the 2B.1 series
gh pr list --state open                  # (none for 2B.1 yet — see What's next §0)
gh issue list --state open --limit 40    # 6 open

# Verify state (expect 1081 passed, mypy clean):
unset VIRTUAL_ENV && uv run pytest -q tests/
unset VIRTUAL_ENV && uv run mypy src/localmail
# This slice's tests specifically:
unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_reconcile.py tests/test_daemon_hot_reload.py
```

Open the 2B.1 PR:

```bash
gh pr create --base main --head daemon-control-2b-respec \
  --title "feat(daemon): account hot-reload without restart (2B.1)"
```

Pick up **2B.2 (heartbeats)** after merge:

```bash
git checkout main && git pull
git checkout -b daemon-control-2b2-heartbeats
# Plan from docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md §2B.2
# NOTE the corrected heartbeat schema (worker_kind-keyed + two partial unique indexes).
ls migrations/    # next slot: 0023_daemon_heartbeats.sql
```

## File map (this session)

```
NEXT_SESSION.md                                              # REPLACED this session
docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md  # NEW (61b8614) — supersedes §2B
docs/superpowers/plans/2026-05-31-daemon-hot-reload-2b1.md           # NEW (e345ef0/6a7f5de)
src/localmail/daemon_reconcile.py                           # NEW — pure planner
src/localmail/daemon.py                                     # registry, per-account events, reconcile loop, resize
src/localmail/config.py                                     # +reload_seconds, +shutdown_grace_seconds
src/localmail/api/admin/accounts.py                         # +touch_account_updated_at
src/localmail/api/admin/oauth.py                            # complete_oauth bumps updated_at
src/localmail/serve/admin/accounts_router.py               # password route bumps updated_at
src/localmail/cli.py                                        # add-account / oauth-login bump updated_at
config.example.toml                                         # [daemon] reload_seconds + shutdown_grace_seconds
README.md                                                   # run-row hot-reload note
tests/test_daemon_reconcile.py                             # NEW — pure planner tests
tests/test_daemon_hot_reload.py                            # NEW — reconcile orchestration tests
tests/test_config.py                                        # +3 knob tests
tests/test_admin_accounts.py                               # +2 touch_account_updated_at tests
tests/test_admin_oauth.py                                   # +complete_oauth-bumps-updated_at test
tests/test_daemon_extract_thread.py                        # d.threads -> d._worker_threads
docs/handoffs/2026-05-31T0030-utc-post-2b1-daemon-hot-reload.md   # frozen snapshot of this file
```

`main` at `77741d0` (== `origin/main`). Branch `daemon-control-2b-respec` at
`9b18747` (11 commits, **not pushed, no PR**). Working tree clean (only
`.claude/` local files). 2 local branches (`main`, `daemon-control-2b-respec`);
0 open PRs.
