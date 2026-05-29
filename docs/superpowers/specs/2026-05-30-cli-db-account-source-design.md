# Sub-plan 2A.2d — CLI account commands read/write the DB

**Status:** Approved, ready for implementation planning
**Date:** 2026-05-30
**Author:** Horst Herb, with Claude (brainstorming session)
**Predecessors:**
[2026-05-28 admin-ui design](2026-05-28-admin-ui-design.md) § 5 (CLI parity),
[2026-05-29 daemon-db-account-source design](2026-05-29-daemon-db-account-source-design.md)
(Sub-plan 2A.2b — daemon reads the DB).

## Goal

Make the `localmail` CLI account commands read and write the `accounts` table
(via the `localmail.api.admin.accounts` service layer) instead of the
`config.toml` `[[accounts]]` blocks. After 2A.2b the daemon is already
DB-canonical; the CLI is the last TOML-coupled account surface and the sole
remaining caller of `sync.upsert_account`.

Concretely:

- `list-accounts` lists DB rows (not TOML).
- `add-account NAME` stores the IMAP password against a DB row, seeding the
  row from TOML if it is missing from the DB.
- `oauth-login NAME` runs the desktop consent flow against a DB row (same
  seed-from-TOML bridge), storing the refresh token.
- `remove-account NAME` clears keyring secrets (back-compat default) and,
  with `--delete-row`, deletes the DB row (refusing if messages reference it
  unless `--force`).
- one-shot `sync` iterates **DB** accounts (`list_syncable_accounts`,
  mirroring the daemon), with `--account NAME` as a manual override.
- `sync.upsert_account` is deleted (no callers remain).

## Non-goals

- No admin-UI screens (Sub-plan 2A.3), daemon control (2B), or mbox import
  (2C).
- No new migration — `0020_accounts_canonical.sql` already carries every
  column this slice needs.
- No CLI toggle for `sync_enabled` (only `update_account` / direct SQL set it
  today). Flagged as a follow-up; the daemon already honors the column.
- No change to the read-only-with-respect-to-IMAP invariant.

## Decisions (from brainstorming)

1. **`add-account` when the DB row is absent → seed from TOML, then store the
   password.** If `NAME` is missing from the DB but present in `config.toml`,
   create the DB row from that TOML block (via `create_account`), then store
   the password. If `NAME` is in neither place, fail. (A password account
   cannot be conjured without a host/port, so TOML — or a pre-existing DB row —
   is required.)
2. **`remove-account` is secrets-only by default.** It clears the keyring
   secret and leaves the DB row untouched (back-compat with today's behavior),
   protecting archives from accidental loss. An explicit `--delete-row` flag
   deletes the DB row, honoring `delete_account`'s force semantics
   (`--force` required when `messages` reference the account).
3. **one-shot `sync` acts on syncable accounts; `--account` overrides.** Bare
   `sync` iterates `list_syncable_accounts` (live + `sync_enabled = TRUE`),
   mirroring the daemon. `sync --account NAME` resolves any **live** account by
   name even if `sync_enabled = FALSE` (manual one-shot override); an
   `archive` account is rejected.

## Architecture

Three layers, keeping `cli.py` glue thin (it is already ~1300 lines):

### 1. Service-layer accessor (new)

`localmail.api.admin.accounts.get_account_by_name(conn, name) -> Account | None`

The CLI addresses accounts by name; the service layer currently exposes only
`get_account(conn, account_id)`. This adds the missing name lookup, reusing the
shared `_SELECT_FULL` column shape so it cannot drift from `get_account` /
`list_accounts_full`. Returns `None` when absent (the CLI maps that to its own
"seed from TOML or fail" logic) — distinct from `get_account`, which raises
`NotFound`, because absence is a normal branch here, not an error.

### 2. Pure resolver module (new)

`src/localmail/cli_account_resolve.py` — no IO, no clock, fully unit-testable.

```python
@dataclass(frozen=True)
class Found:
    account: Account            # already in the DB

@dataclass(frozen=True)
class SeedThenUse:
    config: AccountConfig       # absent from DB, present in TOML — create first

@dataclass(frozen=True)
class NotFound:
    name: str                   # absent from both

Resolution = Found | SeedThenUse | NotFound

def plan_account_resolution(
    name: str,
    toml_accounts: list[AccountConfig],
    existing: Mapping[str, Account],
) -> Resolution: ...
```

Decision table (pure):

| name in DB? | name in TOML? | result |
|-------------|---------------|--------|
| yes         | (either)      | `Found(db_row)` |
| no          | yes           | `SeedThenUse(toml_config)` |
| no          | no            | `NotFound(name)` |

The DB is canonical: when the row exists, TOML is irrelevant (matches the
existing seed model where drifted TOML is logged and ignored).

### 3. Shared config→create mapping

`account_seed.py` currently builds `create_account(...)` kwargs inline inside
`seed_accounts`. Promote that to a module-level pure helper
(`account_create_kwargs(cfg: AccountConfig) -> dict`) and reuse it from both
`seed_accounts` and the CLI's `SeedThenUse` branch, so the two insert paths
cannot diverge.

### 4. `sync_account` takes `account_id`

`sync.sync_account` currently calls `upsert_account(conn, account)` internally.
Change its signature to accept an explicit `account_id: int` (resolved by the
caller from the DB) and drop the internal call:

```python
def sync_account(conn, imap, *, account: AccountConfig, account_id: int,
                 attachments_root, max_messages=None, progress=None) -> dict[str, int]: ...
```

The one-shot `sync` CLI resolves each target row from the DB, adapts it to an
`AccountConfig` via the existing `daemon_accounts.account_config_from_row`, and
passes both `account` and `account_id`. With no callers left,
`sync.upsert_account` is deleted.

## Command behavior

### `list-accounts`

Reads `list_accounts_full(conn)`. One line per row:
`name · email · host:port` (or `archive`) `· auth_method · sync=<bool> ·
[secret]`, where `[secret]` is `password`/`oauth-token`/`MISSING` for live
accounts (keyring probed by `auth_method`) and `n/a` for archive rows. Empty
DB → `no accounts`.

### `add-account NAME [--password PW]`

`plan_account_resolution(NAME, cfg.accounts, db_rows)`:

- `Found(acct)` / `SeedThenUse(cfg)` with a **password** auth_method →
  (seed the row first if `SeedThenUse`), then prompt/accept the password and
  store it in the keyring keyed by `NAME`.
- auth_method is `oauth2` → `ClickException` pointing at `oauth-login`.
- auth_method is `archive` → `ClickException` (archives have no live secret).
- `NotFound` → `ClickException` naming the account and the two places checked.

### `oauth-login NAME`

Same resolution bridge. Requires `auth_method = oauth2`,
`oauth_provider = gmail`, and `[gmail_oauth] client_secrets_file`. Seeds from
TOML if `SeedThenUse`. Runs the existing desktop loopback consent flow and
stores the refresh token keyed by `NAME`. (No `account_config_from_row`
needed — the consent flow only needs the client-secrets file and the keyring
key, both already in hand.)

### `remove-account NAME [--delete-row] [--force]`

- Default (no `--delete-row`): clears keyring password + refresh token for
  `NAME`. DB row untouched. Echoes what was cleared.
- `--delete-row`: resolve via `get_account_by_name`.
  - Found → `delete_account(conn, account_id=acct.id, force=force)` then clear
    keyring secrets. `AccountInUse` (messages exist, no `--force`) →
    `ClickException` with a hint to pass `--force`.
  - Not found → clear any orphaned keyring secret and report "no DB row;
    cleared keyring only".
- `--force` without `--delete-row` is rejected (`--force` only modifies
  `--delete-row` behavior).

### `sync [--account NAME] [--no-ssl] [--limit-per-folder K]`

- Bare → `list_syncable_accounts(conn)`.
- `--account NAME` → `get_account_by_name`; `None` → `ClickException`;
  `auth_method = archive` → `ClickException` ("archive accounts are not
  synced"); otherwise used even if `sync_enabled = FALSE`.
- Empty selection → `ClickException` ("no syncable accounts" /
  "no such account").
- Each selected row → `account_config_from_row(row)` + `row.id` →
  `sync_account(conn, imap, account=cfg, account_id=row.id, …)`.

## Error handling

All operator-facing failures surface as `click.ClickException` (clean non-zero
exit, no traceback): unknown account, wrong auth_method for the command,
archive where a live account is required, `AccountInUse` without `--force`,
missing `[gmail_oauth]`. `AccountFieldError` from a malformed TOML block during
`SeedThenUse` is wrapped the same way (mirrors `init-db`'s seed error
handling). The whole `sync` run shares one uncommitted transaction per the
existing pattern.

## Testing (TDD)

- **`tests/test_cli_account_resolve.py`** — pure planner: `Found`,
  `SeedThenUse`, `NotFound`, and DB-wins-over-TOML. No DB, no keyring.
- **`tests/test_admin_accounts.py`** — `get_account_by_name` returns the row /
  `None`; column shape matches `get_account`.
- **`tests/test_cli_accounts_db.py`** (new) — each rewired command via Click's
  `CliRunner` against the test DB + `memory_keyring`:
  - `list-accounts` reads DB rows; empty-DB message.
  - `add-account` on an existing DB row stores the password; on a TOML-only
    name seeds then stores (**DB-empty / TOML-only edge case**); on an
    unknown name fails; on an oauth/archive row fails with the right hint.
  - `oauth-login` (consent flow faked) stores the token; seeds from TOML when
    absent; rejects non-oauth rows.
  - `remove-account` default clears secrets only (row survives);
    `--delete-row` removes the row; `--delete-row` with referencing messages
    needs `--force`; orphaned-secret path.
  - `sync` bare iterates syncable rows; `--account` overrides a disabled row;
    rejects archive; "no syncable accounts" on empty.
- **`tests/test_sync.py`** — delete the two `upsert_account` tests; update
  `sync_account` call sites to pass `account_id`.
- **`tests/test_daemon.py`** — swap fixture `upsert_account(...)` seeding for
  `create_account(...)`; retire `test_one_poll_pass_does_not_call_upsert_account`
  (the symbol is gone; 2A.2b's other guards already lock the daemon's
  no-upsert behavior).
- **mypy** clean across `src/localmail`.

## Docs

- **README** — add a short "managing accounts" note (commands are DB-backed;
  `init-db` seeds from TOML; DB is canonical thereafter) and document the
  admin operator command surface (`grant-admin`, `revoke-admin`,
  `revoke-admin-sessions`, account CRUD CLI) — the carried-forward README gap.
- **CLAUDE.md** — record that the CLI account commands and one-shot `sync`
  are DB-canonical and that `sync.upsert_account` is deleted; update the
  2A.2 status line.
- **config.py** — the `[[accounts]]` TOML blocks remain the seed source for
  `init-db` and the `SeedThenUse` bridge; note they are no longer read at
  runtime by any account command except as seed input.

## Risks

1. **Operator UX change.** `remove-account` no longer deletes config (it never
   deleted the DB row before either). The secrets-only default preserves prior
   behavior; `--delete-row` is the new, explicit, guarded path. Low risk.
2. **`sync_account` signature change** ripples to `test_sync.py` and any other
   caller — grep confirms only the CLI and tests call it. Low risk.
3. **TOML-only operators who never ran `init-db`** get a clean
   `SeedThenUse`-then-create path on `add-account`/`oauth-login`, and a clear
   `ClickException` from `sync` if no rows are syncable yet (with the existing
   `init-db` seed as the documented fix). Acceptable.
