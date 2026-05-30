# Daemon startup backoff — design (#133)

**Status:** shipped 2026-05-30.

## Problem

`Daemon.__init__` performs DB IO *during construction*:

1. `_load_syncable_accounts()` — a one-shot `psycopg.connect(self._dsn)` to
   enumerate live, `sync_enabled` accounts. This must run *before* the pool
   opens because pool sizing depends on the account count.
2. `open_pool(...)` — opens the shared connection pool (eager when
   `min_size > 0`).

If Postgres is briefly unreachable at launch (DB still coming up under
systemd, a transient network blip), construction **raises** instead of waiting.
The existing 1s→60s exponential backoff only lives *inside* the IDLE/poll
worker loops, which run after construction succeeds. Surfaced in review of
PR #132 (Sub-plan 2A.2b). Not a regression — the DB dependency at startup
predates #132 via the eager `open_pool`; #132 only moved the first DB touch a
few lines earlier.

## Decision

**Option B from the issue:** wrap both construction-time DB touches in a
bounded exponential-backoff loop that respects the stop signal, so the daemon
*waits for* Postgres rather than crash-looping under the supervisor.

## Design

A small reusable module `src/localmail/retry.py`:

- `next_backoff(current_s, *, factor, max_s) -> float` — **pure**:
  `min(current_s * factor, max_s)`. The doubling-with-cap rule the worker
  loops already use, in one tested place.
- `retry_with_backoff(operation, *, stop_event, initial_s, max_s,
  description, factor=2.0, log=None) -> T` — calls `operation` until it
  returns without raising and returns its result. First attempt is immediate.
  After each failure it waits on `stop_event` for the current backoff, then
  doubles it (capped). If `stop_event` is set — before the first attempt or
  during a wait — it raises `RetryAborted` so a stop signal always wins over
  the retry loop.

Wired into `Daemon.__init__`: the **synchronous** `_load_syncable_accounts`
touch goes through `retry_with_backoff`, parameterised by two new
`DaemonConfig` knobs:

- `startup_backoff_initial_s: float = 1.0`
- `startup_backoff_max_s: float = 60.0`

`open_pool(...)` is **not** wrapped. `open_pool` calls
`ConnectionPool(..., open=True)`, which defaults to `wait=False`: it returns
immediately and fills the pool lazily on background threads, so it never raises
synchronously on an unreachable DB. A retry wrapper there would only ever catch
configuration errors (`min_size`/`max_size`), which aren't transient and must
not be retried. `_load_syncable_accounts` is the real synchronous gate — by the
time it returns, Postgres has answered; a blip in the window before a worker
first acquires a pooled connection is absorbed by the IDLE/poll loops' existing
1s→60s backoff.

`Daemon.__init__` gains an optional `stop_event: threading.Event | None`
parameter (defaults to a fresh `Event`) so the retry can be driven/aborted in
tests and, later, shared by daemon control (2B).

## Deliberate boundaries

- **No magic numbers**: backoff bounds live in `DaemonConfig`; the factor
  constant lives once in `retry.py` (`_DEFAULT_FACTOR`).
- **Signal handlers install in `run_forever`, *after* construction.** During a
  startup-backoff wait, SIGTERM/SIGINT therefore hit the default handler (the
  process exits) — which is correct for the systemd path, where the supervisor
  owns kill semantics. The `RetryAborted` escape exists for an *injected*
  `stop_event` (tests today, daemon control later), not for the systemd case.
- **Scope:** the IDLE/poll worker loops keep their inline `1.0`/`60.0`
  literals — consolidating them onto `next_backoff` is a separate cleanup, out
  of scope here.
- **No migration** (config-only).

## Acceptance

- `Daemon.__init__` retries a connection failure with capped exponential
  backoff and a stop-event escape.
- A unit test drives a flaky connect factory (two `OperationalError`s then
  success) and asserts the daemon constructs without dying.
- A unit test trips the stop event during a backoff wait and asserts
  `RetryAborted`.
- `next_backoff` and `retry_with_backoff` have direct unit coverage.
