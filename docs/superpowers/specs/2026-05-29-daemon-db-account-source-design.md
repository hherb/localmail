# Sub-plan 2A.2b — Daemon reads accounts from the DB

> **Status:** design approved 2026-05-29. Implements the "DB is canonical for
> accounts" invariant from
> [2026-05-28-admin-ui-design.md](2026-05-28-admin-ui-design.md) for the
> running `localmail run` daemon. Folds in 2A.2c (`sync_enabled`) per session
> decision.

## Goal

Switch the `localmail run` daemon from reading accounts out of `config.toml`
(`cfg.accounts`) to enumerating the `accounts` table, and retire the
config-driven column overwrite in `sync.py:upsert_account`. After this slice
the DB is authoritative for the daemon's account set; the running daemon no
longer clobbers DB-canonical columns from TOML.

This is the highest-risk slice of 2A.2 — it touches the live sync path.

## Scope (decided this session)

- **Daemon only.** `localmail run` enumerates DB accounts. The one-shot
  `localmail sync` CLI still reads `cfg.accounts` (its rewiring is 2A.2d).
- **`sync_enabled` honored now** (2A.2c folded in). The column already exists
  (migration 0020); the daemon skips `sync_enabled = FALSE` accounts.
- **Neuter the `upsert_account` overwrite** so the still-TOML-driven
  `localmail sync` CLI also stops clobbering DB-canonical columns. Fully
  retires risk #2 from the prior handoff.
- **Per-account `poll_seconds` TOML override dropped** for v1. The DB has no
  such column; the daemon uses the daemon-wide `cfg.daemon.poll_seconds` for
  every account. YAGNI — add a column later if an operator needs it.
- **Out of scope:** CLI account commands (`add-account` / `oauth-login` /
  `remove-account` / one-shot `sync`) — that's 2A.2d. Daemon heartbeats (2B),
  mbox import (2C). No new migration (0020 already carries every column).

## Approach (chosen)

Keep the `AccountConfig` boundary that `imap_client.open_connection`,
`idle.py`, `poller.py`, and `sync.py` already depend on. Bridge the DB row to
that boundary with one small **pure** adapter, carry the DB `account_id`
explicitly on `WorkerContext`, and have workers read `ctx.account_id` instead
of calling `upsert_account`.

Rejected alternatives:
- **Replace `AccountConfig` in `WorkerContext` with the DB `Account`
  dataclass** — forces edits to `imap_client`, `idle`, `poller`, `sync`
  (`open_connection` wants `AccountConfig`; `Account.auth_method` includes
  `'archive'`; `email_address`/nullable host differ). Larger blast radius, no
  gain.
- **Keep calling `upsert_account` but neuter only the overwrite** — leaves a
  redundant per-loop DB write and the daemon still wouldn't carry
  `account_id`. Half-cleaned.

## Components & boundaries

| Unit | Location | Kind | Responsibility |
|---|---|---|---|
| `list_syncable_accounts(conn)` | `api/admin/accounts.py` | IO (1 query) | Return `Account` rows where `auth_method IN ('password','oauth2') AND sync_enabled`, oldest-first. Reuses `_SELECT_FULL`. |
| `account_config_from_row(Account) -> AccountConfig` | new `daemon_accounts.py` | **pure** | Map DB row → `AccountConfig`: `email_address→email`, `None` folder lists → `[]`, `poll_seconds=None`, cast `oauth_provider` to the `Literal['gmail'] \| None`. Raises `ValueError` on `'archive'` (defensive; enumeration already filters). |
| `WorkerContext.account_id: int` | `worker.py` | data | New field carrying the DB id into workers. |
| `Daemon` | `daemon.py` | orchestration | Enumerate syncable accounts once at `__init__` via a one-shot connection; size the pool from `len(self._syncable)`; build `WorkerContext(account=…, account_id=row.id, …)` in `start_workers`; `run_forever` empty-guard uses the DB list. |
| `_ensure_inbox_row` / `_one_poll_pass` | `idle.py` / `poller.py` | edit | Replace `upsert_account(conn, ctx.account)` with `ctx.account_id`; drop the now-unused `upsert_account` import. |
| `upsert_account` | `sync.py` | edit | Neuter `ON CONFLICT DO UPDATE` → no-op get-or-create (`SET name = accounts.name RETURNING id`). Canonical columns never overwritten. Only the one-shot `localmail sync` CLI still calls it. |

## Data flow

### Startup enumeration (chicken-and-egg with pool sizing)

Pool sizing needs the account count, but enumeration needs a connection — and
we don't want the pool open before we've sized it. So `Daemon.__init__`:

1. Opens a **one-shot** `psycopg.connect(self._dsn)` (short-lived, closed in a
   `finally`), calls `list_syncable_accounts(conn)`, stores the rows as
   `self._syncable: list[Account]`.
2. Sizes the pool from `len(self._syncable)` (replacing every
   `len(cfg.accounts)` in the sizing block), then opens `self.pool`.

`start_workers` maps each stored row:
`ctx = WorkerContext(account=account_config_from_row(row), account_id=row.id, …)`.
`run_forever`'s empty guard becomes `if not self._syncable`.

Everything non-account — gmail secrets, attachments root, idle/poll seconds,
embed/extract worker toggles — still comes from `cfg`.

### Worker DB writes

`_ensure_inbox_row` and `_one_poll_pass` drop the `upsert_account` call and use
`ctx.account_id`. The row provably exists (the daemon read it from the DB), so
there is no "ensure exists" need. `upsert_mailbox` is unchanged.

### `upsert_account` neutering

```sql
INSERT INTO accounts (name, email_address, imap_host, imap_port,
                      auth_method, oauth_provider)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (name) DO UPDATE SET name = accounts.name
RETURNING id
```

The `DO UPDATE SET name = accounts.name` is the standard race-safe "return id
on conflict" idiom — it touches no canonical column. New accounts from a
TOML-only `localmail sync` are still auto-created (unchanged); existing rows
are never clobbered.

## Error handling

Unchanged. Per-message SAVEPOINT poison-pill handling, IDLE/poll reconnect
backoff, and the shared stop `Event` are untouched. The one new failure
surface is the `__init__` enumeration query; if the DB is unreachable at
construction it raises there (same as `open_pool` already would), surfaced by
`run_cmd`.

## Behavioral deltas (documented)

1. Per-account `poll_seconds` TOML override no longer honored — daemon-wide
   default applies to every account.
2. `sync_enabled = FALSE` accounts get no threads.
3. `'archive'` accounts get no threads (they never had IMAP anyway).

## Testing (TDD)

- **`account_config_from_row` (pure)** — full password row; oauth2 row
  (`oauth_provider='gmail'` cast); `None` folder lists → `[]`;
  `poll_seconds is None`; `'archive'` row raises `ValueError`.
- **`list_syncable_accounts`** — DB test: seed password + oauth2 + archive +
  `sync_enabled=FALSE` rows; assert only the two live+enabled come back,
  oldest-first.
- **Daemon enumeration** — DB test: seed N syncable accounts; assert
  `Daemon.__init__` sizes the pool from the DB count (extends
  `test_daemon_pool.py`, which currently asserts `n_accounts=0` on an empty
  table — still passes); assert `start_workers` spawns 2 threads per syncable
  row and skips archive/disabled.
- **`upsert_account` neutering** — DB test: pre-insert an account; call
  `upsert_account` with *different* host/email; assert the returned id matches
  and the canonical columns are **unchanged**; and that a brand-new name still
  inserts.
- **Worker integration** — assert `_ensure_inbox_row` / `_one_poll_pass` no
  longer call `upsert_account` (existing `FakeIMAPClient` + test DB path).
- Full suite + `mypy src/localmail` stay green.

## Follow-ups (not this slice)

- **2A.2d** — rewire CLI `add-account` / `oauth-login` / `remove-account` /
  one-shot `sync` to the DB; document the admin command surface in README.
- Per-account `poll_seconds` column, if requested.
