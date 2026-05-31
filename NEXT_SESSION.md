# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-31T0643 UTC.**
> Another short hardening slice, the natural follow-up to last session:
> **Issue #142 — bound the daemon's fresh (non-pool) psycopg connects past the
> connect phase**. #140 (merged as PR #141) bounded only the *TCP connect* phase
> via `connect_timeout`; a network black-hole that opens *after* the connect
> succeeds still hung the subsequent single-row SELECT (`list_syncable_accounts`)
> or small DELETE (`clear_all_heartbeats`). Fixed with **two** complementary
> bounds threaded into the single `Daemon._connect()` helper: a server-side
> `statement_timeout` (`DaemonConfig.db_statement_timeout_s`, int 30; `0`
> disables) for a slow/stuck query, **plus** a client-side
> `tcp_user_timeout` (`DaemonConfig.db_tcp_user_timeout_ms`, int 30000; `0` = OS
> default) — the latter is the *actual* post-connect black-hole bound, since
> `statement_timeout` is server-side and does nothing when the server never sees
> the query or the reply is dropped (review caught this; first cut shipped only
> `statement_timeout` and overclaimed). Work is on branch
> **`daemon-142-statement-timeout`** (pushed), opened as **PR #143**
> (<https://github.com/hherb/localmail/pull/143>, **open, not yet merged**;
> **Closes #142**). Full suite **1119 passed**, mypy clean (78 files).
>
> Last session's **PR #141 (#140 connect-timeout) is MERGED** (`7dd02f7`); its
> stale local + remote branch was deleted this session. The big remaining arc is
> still **2B.3–2B.5** (daemon command queue → supervisor+HTTP → admin UI).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The **database is canonical for accounts**
end-to-end. The daemon hot-reloads its account set (2B.1), records per-thread
heartbeats (2B.2), and bounds its fresh connects on the connect phase (#140)
plus the query + post-connect-black-hole phases (#142). Downstream consumers read the DB +
attachment tree directly or via the `localmail serve` HTTPS API. See
[CLAUDE.md](CLAUDE.md), [README.md](README.md), and the 2B spec
[docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md](docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md).

## What we shipped this session

### Issue #142 — bounded query + post-connect-black-hole phases

- **`DaemonConfig.db_statement_timeout_s`** — **int** seconds, default `30`;
  **`0` disables** (libpq/Postgres semantics). Server-side `statement_timeout`,
  applied via `options='-c statement_timeout=<N>s'` (GUC `s` unit suffix, no
  `s→ms` conversion). Bounds a slow/stuck *server-side* query.
  ([config.py](src/localmail/config.py))
- **`DaemonConfig.db_tcp_user_timeout_ms`** — **int** milliseconds (libpq's
  native unit for this param), default `30000`; **`0` = OS default**. Passed as
  the libpq `tcp_user_timeout` kwarg. This is the **actual post-connect
  black-hole bound**: it forces the socket closed after that much unacknowledged
  data. The review correctly flagged that `statement_timeout` is *server-side*
  and does nothing when the server never sees the query (request packets
  dropped) or the reply is dropped — the client stays stuck in `recv` until OS
  TCP defaults. `tcp_user_timeout` is the only one of the three bounds that
  breaks that. Linux-effective; libpq silently ignores it where
  `TCP_USER_TIMEOUT` is unavailable (macOS dev). ([config.py](src/localmail/config.py))
- **`Daemon._connect()`** ([daemon.py](src/localmail/daemon.py)) now passes all
  three — `connect_timeout`, `tcp_user_timeout`, and the `statement_timeout`
  `options` string. All three fresh-connect sites (`_load_syncable_accounts`,
  `reconcile`, `_clear_heartbeats`) inherit them via the one helper (#140's
  single-funnel design paying off). The DSN must not itself carry an `options=`
  entry (the kwarg replaces, not merges; the daemon's DSN never does) — noted in
  the helper docstring.
- **Scope**: `statement_timeout` + `tcp_user_timeout`. TCP keepalive tuning
  (`keepalives_*`) is the heavier alternative and not needed once
  `tcp_user_timeout` is in place. Pool connects untouched (own `wait=False`
  lazy-fill; never go through `_connect()`).
- **Docs**: `config.example.toml` `[daemon]` knobs + README run-row clause
  (both reframed so `statement_timeout` is described as the slow-query bound and
  `tcp_user_timeout` as the black-hole bound).

### Commits on `daemon-142-statement-timeout`

```
2deb658  fix(daemon): bound fresh psycopg statement phase with statement_timeout (#142)
<this>   fix(daemon): add tcp_user_timeout for post-connect black-hole; reframe statement_timeout (#142 review)
```

### Verification (this session)

- `unset VIRTUAL_ENV && uv run pytest -q tests/` — **1119 passed** (baseline
  1111 on merged main + 8 new: 4 config, 4 daemon-connect wiring spies).
- `unset VIRTUAL_ENV && uv run mypy src/localmail` — **clean, 78 files**.
- TDD: wiring spies in `tests/test_daemon_connect_timeout.py` capture
  `kwargs.get("options")` (statement_timeout) and `kwargs.get("tcp_user_timeout")`
  and assert every fresh connect carries the config-sourced values.
- **Platform check**: confirmed libpq accepts the `tcp_user_timeout` kwarg on
  darwin without error (silently ignored where `TCP_USER_TIMEOUT` is absent), so
  the macOS test suite is unaffected; effective on the Linux deploy target.

## What's next

### 0. **Review & merge PR #143** *(immediate)*

PR #143 (<https://github.com/hherb/localmail/pull/143>) is **open and green**
(1119 passed, mypy clean). It **Closes #142** on merge. After merge:

```bash
gh pr merge 143 --squash --delete-branch
git checkout main && git fetch --prune origin && git merge --ff-only origin/main
git branch -D daemon-142-statement-timeout
```

### 1. **2B.3 — Command queue** *(next feature slice)*

Per [the spec](docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md) §2B.3:
- Migration `0024_daemon_commands.sql` — `reload-now` / `restart-account` /
  `drain-stop` command rows.
- Daemon drains the queue on each reconcile tick (`FOR UPDATE SKIP LOCKED`) +
  optional `LISTEN/NOTIFY` for low-latency wake.
- **Enqueue accessor only** — no HTTP/CLI surface yet (that's 2B.4).
- Acceptance: a `reload-now` row makes the *next* reconcile tick converge
  immediately instead of waiting out `reload_seconds`; `restart-account N`
  tears down + respawns just account N; `drain-stop` stops the daemon
  gracefully. Poison/duplicate commands are idempotent and don't wedge the
  queue. All TDD, no magic numbers (any new timing knob → `DaemonConfig`).

Then **2B.4** (DaemonSupervisor + `/v1/admin/daemon*` routes — **must wire
`require_admin_session`**, since `get_daemon_status` has no ACL of its own by
design — + Unix control socket + `localmail daemon {status,start,stop,restart,reload}`;
this is the consumer of 2B.2's `get_daemon_status`) and **2B.5** (admin UI
panel; method-bound CSRF per #122/#125).

### 2. **Other open arcs / deferred** *(unchanged)*

- **Admin-UI Sub-plan 2A.3** (account screens; fold #125 method-bound CSRF
  mint) — independent of 2B, still open.
- Externally-blocked / measured: **#90** (glib/Tauri Dependabot), **#47**
  (extract_worker transient opt-in), **#25** (websockets.legacy depwarn),
  **#5** (search batch INSERT), **#134** (oauth_state flake — environmental).
- **Open issues: 7** (#5, #25, #47, #90, #125, #134, #142). #142 closes when
  PR #143 merges → back to 6.

## Open decisions & risks

1. **Three distinct bounds, three distinct units — don't conflate.**
   `db_connect_timeout_s` and `db_statement_timeout_s` are integer **seconds**
   (the latter emitted as the GUC `{N}s` form, no `s→ms` multiply);
   `db_tcp_user_timeout_ms` is integer **milliseconds** (libpq's native unit for
   `tcp_user_timeout` — do not reuse the `_s` knobs for it). They protect
   different things: connect = handshake, statement = slow/stuck *server-side*
   query, tcp_user_timeout = post-connect *network black-hole* (the only
   client-side one). Keep new fresh-connect sites routed through
   `Daemon._connect()` so all three are never forgotten.
2. **`statement_timeout` is server-side and does NOT bound a network
   black-hole** — it was the original #142 cut's overclaim (caught in review).
   When the server never receives the query, or its reply is dropped, the timer
   never helps; `tcp_user_timeout` is what breaks that. Keep this distinction in
   any future hardening (don't reach for `statement_timeout` against a hang).
3. **`0` is the disable/default escape hatch on both query-phase knobs** —
   `db_statement_timeout_s=0` disables `statement_timeout` (libpq/Postgres),
   `db_tcp_user_timeout_ms=0` falls back to the OS default. No special-casing
   needed; both fall out naturally.
4. **Pool connects are NOT affected by #140 or #142.** All three bounds apply
   only to the three *fresh* `psycopg.connect` sites via `_connect()`. The pool
   (`open_pool`, `self.pool.connection()`) has its own `wait=False` lazy-fill
   (2B.1/#133) and fills via `connection_class.connect`, not module-level
   `psycopg.connect` — which is also why the monkeypatch-based wiring tests
   don't disturb it.
5. **Migration numbering.** Latest applied is **0023** (daemon_heartbeats).
   Next free slot: `0024_daemon_commands.sql` (2B.3). #142 added **no**
   migration. Re-check `ls migrations/` at plan-time; never edit an
   already-applied/merged migration.
6. **Heartbeat vocabulary still load-bearing** (carried from 2B.2): any new
   heartbeat call site must use a `worker_kind`/`state` present in *both* the
   SQL CHECK lists (migration 0023) and the `WorkerKind`/`WorkerState`
   Literals in `heartbeat.py`; all loop heartbeats go through `safe_heartbeat`
   (never bare `record_heartbeat`).
7. **Tooling note** (carried): if the harness mangles/truncates tool output,
   run one command at a time and trust `pytest`/`mypy` exit signals over
   rendered text. The full-suite run emits a harmless psycopg pool `__del__`
   `RuntimeError: cannot join current thread` ResourceWarning at interpreter
   teardown — *not* a test failure (`1119 passed`). Also: `pytest -k FOO fileA
   fileB` applies `-k` across *both* files — run a file without `-k` to see all
   its tests.
8. **`.claude/` local files** stay untracked, by design.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # clean apart from .claude/ local files
git branch -vv                           # main + daemon-142-statement-timeout (tip 2deb658)
git --no-pager log --oneline -3
gh pr view 143                           # the #142 PR (open until merged)
gh issue list --state open --limit 40    # 7 open (#142 closes on merge → 6)

# Verify state (expect 1119 passed, mypy clean):
unset VIRTUAL_ENV && uv run pytest -q tests/
unset VIRTUAL_ENV && uv run mypy src/localmail
# This slice's tests specifically:
unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_connect_timeout.py
unset VIRTUAL_ENV && uv run pytest -q tests/test_config.py -k "db_statement_timeout or db_connect_timeout or db_tcp_user_timeout"
```

Pick up **2B.3 (command queue)** after PR #143 merges:

```bash
git checkout main && git pull
git checkout -b daemon-control-2b3-commands
# Plan from docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md §2B.3
ls migrations/    # next slot: 0024_daemon_commands.sql
```

## File map (this session)

```
NEXT_SESSION.md                                # REPLACED this session
src/localmail/config.py                        # +db_statement_timeout_s (int=30) +db_tcp_user_timeout_ms (int=30000); reframed comments
src/localmail/daemon.py                         # _connect() passes connect_timeout + tcp_user_timeout + statement_timeout options
config.example.toml                             # [daemon] db_statement_timeout_s + db_tcp_user_timeout_ms (reframed)
README.md                                       # run-row: all-phase bound clause (connect/statement/tcp_user_timeout)
tests/test_config.py                            # +4 knob tests (statement + tcp_user_timeout default/override)
tests/test_daemon_connect_timeout.py            # +4 wiring spies (statement + tcp_user_timeout); _cfg extended
docs/handoffs/2026-05-31T0643-utc-post-142-daemon-statement-timeout.md   # frozen pre-review snapshot (statement_timeout-only cut)
```

`main` at `7dd02f7` (== `origin/main`, the merged 2B.2 + #140). Branch
`daemon-142-statement-timeout` at `2deb658`, **pushed**
(== `origin/daemon-142-statement-timeout`), **PR #143 open**. Working tree clean
(only `.claude/` local files). 2 local branches (`main`,
`daemon-142-statement-timeout`); 1 open PR (#143).
