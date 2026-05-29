# TOML→DB account seed at `init-db` — design

> **Status:** approved 2026-05-29. Scope: the first (lowest-risk) slice of
> Admin-UI **Sub-plan 2A.2**. The other 2A.2 slices — rewiring the CLI
> account commands to the DB, switching the daemon's account source to the
> DB, and honouring `sync_enabled` — are **out of scope here** and tracked
> separately. See
> [docs/superpowers/specs/2026-05-28-admin-ui-design.md](2026-05-28-admin-ui-design.md)
> § "New invariants" and § 5 for the wider arc.

## Problem

Migration `0020_accounts_canonical.sql` made `accounts` the write-authoritative
store for IMAP configuration, and PR #118 shipped a full DB-backed account
CRUD service (`localmail.api.admin.accounts`). But nothing populates the
`accounts` table from the operator's existing `config.toml` `[[accounts]]`
blocks except the daemon's lazy `sync.py:upsert_account`, which only runs on
the first sync of each account and only writes a subset of columns.

The design doc's DB-canonical invariant states "`config.toml` `[[accounts]]`
blocks are a one-time seed merged into the `accounts` table on `init-db`;
thereafter the DB is authoritative." That seed does not yet exist. This spec
defines it.

## Goal

`localmail init-db` performs a one-time, **idempotent** merge of every
`config.toml` `[[accounts]]` block into the `accounts` table, keyed by
`accounts.name`:

- **New** config accounts (name not in DB) are **inserted**.
- **Existing** config accounts (name already in DB) are **skipped** — the DB
  is canonical and is never overwritten by the seed.
- If a skipped account's TOML values **differ** from the DB row, a **WARNING**
  is logged naming the specific fields that drifted, so the operator knows
  their TOML edits are being ignored.

Secrets (passwords, OAuth refresh tokens) are **not** touched — they remain in
the keyring, managed by `add-account` / `oauth-login`.

## Non-goals (explicitly deferred)

- Rewiring `add-account` / `oauth-login` / `remove-account` / `list-accounts`
  to read/write the DB. (Later 2A.2 slice.)
- Switching the daemon to enumerate accounts from the DB. (Later 2A.2 slice.)
- Honouring `accounts.sync_enabled` in the daemon. (Design doc § 7 defers this
  to v1.x.)
- An explicit `localmail migrate-config-accounts` command. (Design doc § 7
  defers it; the implicit `init-db` seed covers the common case.)

## Architecture

A new module **`src/localmail/account_seed.py`** with a pure core and a thin
IO wrapper. Kept well under the 500-line guideline.

### Pure core (no IO)

```python
@dataclass(frozen=True)
class AccountDrift:
    name: str
    fields: list[str]          # seedable field names whose TOML ≠ DB

@dataclass(frozen=True)
class SeedPlan:
    to_insert: list[AccountConfig]
    drift: list[AccountDrift]

def plan_account_seed(
    config_accounts: list[AccountConfig],
    existing: Mapping[str, Account],   # name -> existing DB row
) -> SeedPlan:
    ...
```

`plan_account_seed` is a pure function: given the TOML account list and a
mapping of existing DB rows keyed by name, it decides which accounts to insert
and which existing accounts have drifted. No connection, no logging, no
clock — fully unit-testable in isolation.

**Drift comparison** covers exactly the **seedable field set**:

| Spec field (DB) | Source (`AccountConfig`) |
|-----------------|--------------------------|
| `email_address` | `account.email` |
| `imap_host`     | `account.imap_host` |
| `imap_port`     | `account.imap_port` |
| `auth_method`   | `account.auth_method` |
| `oauth_provider`| `account.oauth_provider` |
| `folder_allow`  | `account.folder_allow` |
| `folder_deny`   | `account.folder_deny` |
| `folder_deny_flags` | `account.folder_deny_flags` |

Excluded from both seeding and drift: `sync_enabled` (no TOML field — left at
the DB default `TRUE`), `poll_seconds` (no DB column), `created_at` /
`updated_at` (DB-managed), `config` (legacy JSONB, defaulted `'{}'`).

**Folder-list normalization.** `AccountConfig.folder_*` default to `[]` (empty
list); `create_account` stores `[]` as a JSONB array, but an admin-UI-created
row may store `NULL`. Before comparing, normalize `None → []` on both sides so
a NULL DB column does not false-positive as drift against TOML's empty default.
The comparison is set equality on the normalized values: folder allow/deny
lists are set-like in IMAP semantics (reordering or repeating an entry does
not change which folders sync), so a pure reorder is not reported as drift.

### IO wrapper

```python
@dataclass(frozen=True)
class SeedResult:
    inserted: int
    skipped: int
    drifted: int

def seed_accounts(
    conn: psycopg.Connection,
    config_accounts: list[AccountConfig],
    *,
    logger: logging.Logger = ...,   # module logger default
) -> SeedResult:
    ...
```

`seed_accounts`:

1. Reads existing rows into a `name -> Account` dict via a single
   `SELECT` (reusing the `_SELECT_FULL` column shape / `Account` dataclass
   from `api.admin.accounts`, or a small local SELECT with the same columns).
2. Calls `plan_account_seed(config_accounts, existing)`.
3. For each `plan.to_insert`, calls
   `localmail.api.admin.accounts.create_account(conn, name=…,
   email_address=account.email, auth_method=…, imap_host=…, imap_port=…,
   oauth_provider=…, folder_allow=…, folder_deny=…, folder_deny_flags=…)` —
   reusing its validation so the seed and the admin UI cannot diverge.
4. For each `plan.drift`, logs one `WARNING` of the form
   `account '<name>': config.toml differs from DB (fields: a, b); DB is
   canonical, TOML ignored`.
5. Returns `SeedResult(inserted=len(to_insert), skipped=len(existing-hits),
   drifted=len(drift))`.

Transaction: `seed_accounts` operates on the passed connection; the caller
commits. An empty `config_accounts` is a no-op returning all-zero counts.

### CLI wiring

`cli.py:init_db` is extended to seed after migrations succeed:

```python
applied = apply_migrations(cfg.database.dsn, index_build_work_mem_mb=…)
# … existing migration echo …
with psycopg.connect(cfg.database.dsn) as conn:
    result = seed_accounts(conn, cfg.accounts)
    conn.commit()
click.echo(
    f"seeded accounts: inserted={result.inserted} "
    f"skipped={result.skipped} drifted={result.drifted}"
)
```

Seeding runs **after** `apply_migrations` so the 0020 columns exist. It runs on
**every** `init-db` invocation; idempotence comes from the name-keyed skip, not
from a one-shot guard.

## Error handling

- **Validation failure** on an inserted account (`create_account` raises
  `AccountFieldError`): surfaces as a `click.ClickException` naming the
  offending account, aborting `init-db` non-zero. A malformed `[[accounts]]`
  block is an operator error worth failing loudly — partial seeding is avoided
  because the whole seed shares one transaction that the caller only commits on
  success.
- **`UniqueViolation`** from `create_account` should not occur (the planner
  excludes existing names) but, if it does (e.g. a concurrent writer), it
  propagates as `AccountFieldError` and aborts — safe under the single
  uncommitted transaction.

## Testing (TDD)

### Pure-planner unit tests (no DB)

- empty config → empty plan (no inserts, no drift).
- all-new names → every account in `to_insert`, no drift.
- name match, all seedable fields identical → skipped, **no** drift entry.
- name match, one field differs (e.g. `imap_port`) → skipped, drift entry lists
  exactly `["imap_port"]`.
- name match, multiple fields differ → drift lists all differing fields.
- folder `None` (DB) vs `[]` (TOML) → **no** drift (normalization).
- folder `["A"]` vs `["B"]` and `["A","B"]` vs `["B","A"]` → drift (order
  matters).
- `email` (config) maps to `email_address` (DB) correctly in the comparison.

### IO tests (real test DB)

- fresh DB + N config accounts → N rows inserted; `SeedResult(N, 0, 0)`; rows
  carry the folder fields and `sync_enabled=TRUE`.
- re-run on the same config → no new rows; `SeedResult(0, N, 0)`; no WARNING.
- pre-existing DB row with a drifted field → row **unchanged** after seed;
  `SeedResult(0, 1, 1)`; exactly one WARNING captured (via `caplog`) naming the
  field.
- empty `cfg.accounts` → no-op, `SeedResult(0, 0, 0)`.
- mixed batch (some new, some matching, some drifted) → counts correct.

### CLI test

- `init-db` invoked (CliRunner) against the test DSN with config accounts →
  echoes `seeded accounts: inserted=… skipped=… drifted=…`; rows present in DB.

No real Keychain (the autouse `memory_keyring` fixture applies); no `.eml`
fixtures; tests TRUNCATE per the existing `db_conn` fixture convention.

## Known interaction / follow-up risk

`sync.py:upsert_account` still runs `INSERT … ON CONFLICT (name) DO UPDATE SET
email_address, imap_host, imap_port, auth_method, oauth_provider` from
`config.toml` on the first sync of each account. Until the deferred
**daemon-source switch** slice lands, the daemon both (a) reads accounts from
`cfg.accounts`, not the DB, and (b) overwrites those five columns in the DB
from TOML on sync. So this seed makes the table *populated and authoritative
for the admin UI / future readers*, but the DB is not yet *fully* canonical
against the running daemon. This is expected: the seed is the prerequisite
groundwork, and the tension closes when the daemon-source slice rewires
`Daemon.__init__` and retires the config-driven `upsert_account` overwrite.
Documented here so the next session does not mistake it for a bug.

## Files

```
src/localmail/account_seed.py        # NEW — pure plan_account_seed() + IO seed_accounts()
src/localmail/cli.py                  # init_db: seed after apply_migrations
tests/test_account_seed.py           # NEW — pure-planner + IO tests
tests/test_cli_init_db_seed.py       # NEW — CLI echo + DB-effect test (or fold into above)
CLAUDE.md                            # note the init-db seed behaviour once shipped
```
