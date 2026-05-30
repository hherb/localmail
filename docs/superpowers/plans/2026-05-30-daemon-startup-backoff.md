# Daemon startup backoff — plan (#133)

**Status:** done 2026-05-30. Design:
[2026-05-30-daemon-startup-backoff-design.md](../specs/2026-05-30-daemon-startup-backoff-design.md).

TDD throughout; no migration.

## Task 1 — pure retry module

- **RED** `tests/test_retry.py`: `next_backoff` doubles + caps + honours
  factor; `retry_with_backoff` returns on first success, retries until
  success, aborts when stop already set (never attempts), aborts when stop
  fires during backoff (one attempt then `RetryAborted`).
- **GREEN** `src/localmail/retry.py`: `RetryAborted`, `next_backoff`
  (pure), `retry_with_backoff`.

## Task 2 — wire into the daemon

- **RED** `tests/test_daemon_startup_backoff.py`: flaky `psycopg.connect`
  (two `OperationalError`s then real connect) → daemon constructs, connect
  called 3×; stop fired during backoff → `RetryAborted`.
- **GREEN**:
  - `DaemonConfig.startup_backoff_initial_s = 1.0`,
    `startup_backoff_max_s = 60.0`.
  - `Daemon.__init__` gains `stop_event: threading.Event | None = None`.
  - `_load_syncable_accounts` (the synchronous `psycopg.connect` gate) wrapped
    in `retry_with_backoff` with the config bounds. `open_pool` left plain — it
    opens with `wait=False` and never raises synchronously on an unreachable
    DB, so a wrapper there would be dead code for the connectivity case.

## Task 3 — docs

- CLAUDE.md sync-model: startup-backoff paragraph.
- README `run` row + `config.example.toml` `[daemon]` knobs.
- This spec + plan.

## Verification

- `uv run pytest -q tests/test_retry.py tests/test_daemon_startup_backoff.py`
- `uv run pytest -q tests/` (full suite green)
- `uv run mypy src/localmail` (clean)
