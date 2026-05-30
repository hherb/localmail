# Design — `sync_enabled` CLI setter (`enable-account` / `disable-account`)

> Status: approved design, 2026-05-30. Follow-up to Sub-plan 2A.2 (DB-canonical
> accounts). Small slice; no migration.

## Problem

The `accounts.sync_enabled` column (added in `0020_accounts_canonical.sql`) is
**honoured** by the daemon — `list_syncable_accounts` filters
`auth_method IN ('password','oauth2') AND sync_enabled`, so a paused account
spawns no worker threads — and **respected** by one-shot `sync --account NAME`
(which overrides it). But there is no first-class way to *set* it: only
`update_account(...)` or direct SQL can flip the flag. An operator pausing or
resuming an account today must reach into the service layer or the database.

## Goal

Two CLI commands that toggle `sync_enabled` on an existing DB account, by name:

```
localmail enable-account NAME     # sync_enabled = TRUE   (resume)
localmail disable-account NAME    # sync_enabled = FALSE  (pause)
```

Non-goals: no admin-UI screen (that is Sub-plan 2A.3); no new column; no change
to how the daemon or `sync` read the flag.

## Behaviour

Name resolution is **DB-only** — unlike `add-account` / `oauth-login`, these
commands do **not** seed from `config.toml`. Toggling sync presupposes the
account already exists; an unknown name is an error, not a create.

| Condition | Outcome | Exit |
|-----------|---------|------|
| name not in `accounts` | `ClickException("no such account: 'NAME'")` | non-zero |
| `auth_method == "archive"` | `ClickException` — sync has no meaning on archive rows (the daemon never syncs them) | non-zero |
| already in the target state | echo `account 'NAME' sync already {enabled\|disabled}`, **no DB write** | 0 |
| otherwise | `update_account(conn, id, sync_enabled=…)`, commit, echo `account 'NAME' sync {enabled\|disabled}` | 0 |

Idempotent: re-running `enable-account` on an already-enabled account succeeds
with the "already" message and does **not** bump `updated_at`. Archive rejection
is deliberate (per design decision) so the operator gets a clear signal the knob
does nothing there, rather than silently setting a no-op flag.

## Design — pure planner + thin CLI

To keep the branching IO-free and unit-testable (mirrors the existing
[`cli_account_resolve.py`](../../../src/localmail/cli_account_resolve.py)
precedent), the decision lives in a new pure module
`src/localmail/cli_sync_toggle.py`:

```python
@dataclass(frozen=True)
class SyncTogglePlan:
    action: Literal["reject", "noop", "apply"]
    message: str

def plan_sync_toggle(*, name: str, auth_method: str,
                     currently_enabled: bool, enable: bool) -> SyncTogglePlan:
    """Decide what enable/disable-account should do for one account row.

    Pure: no IO, no DB, no keyring. The CLI maps `action` to side effects —
    reject → ClickException, noop → echo only, apply → update_account + echo.
    """
```

- `auth_method == "archive"` → `SyncTogglePlan("reject", "account 'NAME' is an archive account; sync cannot be enabled")`
- `currently_enabled == enable` → `SyncTogglePlan("noop", "account 'NAME' sync already {enabled|disabled}")`
- else → `SyncTogglePlan("apply", "account 'NAME' sync {enabled|disabled}")`

The two CLI commands share one private helper in `cli.py` that:

1. `cfg = load_config(ctx.obj["config_path"])`
2. `with psycopg.connect(cfg.database.dsn) as conn:`
3. `account = get_account_by_name(conn, name)` — `None` ⇒ `ClickException`
4. `plan = plan_sync_toggle(name=name, auth_method=account.auth_method, currently_enabled=account.sync_enabled, enable=enable)`
5. dispatch on `plan.action`: `reject` ⇒ `ClickException(plan.message)`; `noop` ⇒ `click.echo(plan.message)` and return (no commit needed); `apply` ⇒ `update_account(conn, account.id, sync_enabled=enable)`, `conn.commit()`, `click.echo(plan.message)`.

`enable-account` calls the helper with `enable=True`; `disable-account` with
`enable=False`. Both register as `@main.command(...)` + `@click.argument("name")`
+ `@click.pass_context`, exactly like `add-account` / `remove-account`.

## Testing (TDD)

**`tests/test_cli_sync_toggle.py`** — pure planner, no DB:
- archive + enable → `reject`; archive + disable → `reject`
- enabled + enable → `noop` ("already enabled"); disabled + disable → `noop` ("already disabled")
- disabled + enable → `apply` ("enabled"); enabled + disable → `apply` ("disabled")
- message contains the account name

**`tests/test_cli_accounts_db.py`** (extend) — DB integration via Click runner:
- `disable-account` on a syncable account sets `sync_enabled = FALSE` in the row
- `enable-account` on a paused account sets it back to `TRUE`
- idempotent re-run echoes "already" and leaves `updated_at` unchanged
- unknown name → non-zero exit + "no such account"
- archive account → non-zero exit + archive message

## Files

```
src/localmail/cli_sync_toggle.py     # NEW: pure planner
src/localmail/cli.py                 # +enable-account/+disable-account + shared helper
tests/test_cli_sync_toggle.py        # NEW: planner unit tests
tests/test_cli_accounts_db.py        # +enable/disable DB integration
README.md                            # Sync & accounts: document enable/disable-account
CLAUDE.md                            # commands list + 2A note
```

No migration. mypy must stay clean; full suite green.
