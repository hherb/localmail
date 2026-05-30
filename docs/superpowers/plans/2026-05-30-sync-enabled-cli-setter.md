# `sync_enabled` CLI setter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `localmail enable-account NAME` / `disable-account NAME` CLI commands that toggle `accounts.sync_enabled` by name, backed by a pure decision planner.

**Architecture:** A pure IO-free planner (`cli_sync_toggle.plan_sync_toggle`) decides reject/noop/apply from an account's `auth_method` + current flag + target; two thin Click commands share one `cli.py` helper that resolves the account via `get_account_by_name`, dispatches the plan, and only writes (`update_account`) on `apply`. Mirrors the existing `cli_account_resolve.py` + account-command patterns. No migration.

**Tech Stack:** Python 3.12, `click`, `psycopg` v3, `pytest` (Click `CliRunner`, real Postgres `localmail_test` DB).

> **Status (2026-05-30): IMPLEMENTED.** All three tasks shipped on branch
> `sync-enabled-cli-setter`. The original spec/plan commits were lost to a
> cross-session git reset and recreated; the implementation commits
> (`27dfac1`, `999617d`, `961b37c`) are intact. Test integration used the
> existing `tests/test_cli_accounts_db.py` helpers (`_run`, `_make_db_account`,
> fixtures `db_conn`/`db_dsn`/`tmp_path`) rather than the placeholder
> `cli_env`/`create_account` names sketched below.

---

## File structure

```
src/localmail/cli_sync_toggle.py     # NEW: SyncTogglePlan + plan_sync_toggle (pure)
src/localmail/cli.py                 # +enable_account / +disable_account + _apply_sync_toggle helper
tests/test_cli_sync_toggle.py        # NEW: planner unit tests (no DB)
tests/test_cli_accounts_db.py        # +enable/disable DB integration tests
README.md                            # Sync & accounts: document enable/disable-account
CLAUDE.md                            # commands block + 2A note
```

Reference patterns to follow exactly:
- `src/localmail/cli_account_resolve.py` — frozen dataclass + pure planner idiom.
- `src/localmail/cli.py` `remove_account` — `@main.command` + `@click.argument("name")` + `@click.pass_context`, `cfg = load_config(ctx.obj["config_path"])`, `with psycopg.connect(cfg.database.dsn) as conn:`, `get_account_by_name(conn, name)`, `ClickException`, `conn.commit()`, `click.echo(...)`.
- `src/localmail/api/admin/accounts.py` `update_account(conn, account_id, **fields)` — accepts `sync_enabled=...`.

---

### Task 1: Pure planner `plan_sync_toggle`

**Files:**
- Create: `src/localmail/cli_sync_toggle.py`
- Test: `tests/test_cli_sync_toggle.py`

- [x] **Step 1: Write the failing tests**

Create `tests/test_cli_sync_toggle.py`:

```python
"""Unit tests for the pure enable/disable-account decision planner."""

from __future__ import annotations

import pytest

from localmail.cli_sync_toggle import SyncTogglePlan, plan_sync_toggle


@pytest.mark.parametrize("enable", [True, False])
def test_archive_account_is_rejected_either_direction(enable: bool) -> None:
    plan = plan_sync_toggle(
        name="arc", auth_method="archive",
        currently_enabled=False, enable=enable,
    )
    assert plan.action == "reject"
    assert "arc" in plan.message
    assert "archive" in plan.message


def test_enabling_already_enabled_is_noop() -> None:
    plan = plan_sync_toggle(
        name="work", auth_method="password",
        currently_enabled=True, enable=True,
    )
    assert plan.action == "noop"
    assert "already" in plan.message
    assert "enabled" in plan.message


def test_disabling_already_disabled_is_noop() -> None:
    plan = plan_sync_toggle(
        name="work", auth_method="oauth2",
        currently_enabled=False, enable=False,
    )
    assert plan.action == "noop"
    assert "already" in plan.message
    assert "disabled" in plan.message


def test_enabling_disabled_account_applies() -> None:
    plan = plan_sync_toggle(
        name="work", auth_method="password",
        currently_enabled=False, enable=True,
    )
    assert plan.action == "apply"
    assert "enabled" in plan.message
    assert "already" not in plan.message


def test_disabling_enabled_account_applies() -> None:
    plan = plan_sync_toggle(
        name="work", auth_method="oauth2",
        currently_enabled=True, enable=False,
    )
    assert plan.action == "apply"
    assert "disabled" in plan.message
    assert "already" not in plan.message


def test_plan_is_frozen() -> None:
    plan = SyncTogglePlan(action="noop", message="x")
    with pytest.raises(Exception):
        plan.action = "apply"  # type: ignore[misc]
```

- [x] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_sync_toggle.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'localmail.cli_sync_toggle'`

- [x] **Step 3: Write the minimal implementation**

Create `src/localmail/cli_sync_toggle.py`:

```python
"""Pure decision planner for the enable-account / disable-account CLI commands.

IO-free: given an account's auth_method, its current `sync_enabled` value, and
the desired target, decide whether the command should reject (archive rows have
no sync), do nothing (already in the target state), or apply the change. The CLI
maps the resulting action to side effects — reject -> ClickException,
noop -> echo only, apply -> update_account + echo. Mirrors the
`cli_account_resolve` planner idiom so the branching stays unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ToggleAction = Literal["reject", "noop", "apply"]


@dataclass(frozen=True)
class SyncTogglePlan:
    """What enable/disable-account should do for one account row."""

    action: ToggleAction
    message: str


def plan_sync_toggle(*, name: str, auth_method: str,
                     currently_enabled: bool, enable: bool) -> SyncTogglePlan:
    """Decide the outcome of enable/disable-account for one account.

    - archive accounts never sync, so toggling is rejected either direction;
    - a no-op (already in the target state) succeeds without a DB write;
    - otherwise the change is applied.
    """
    state_word = "enabled" if enable else "disabled"
    if auth_method == "archive":
        return SyncTogglePlan(
            action="reject",
            message=f"account {name!r} is an archive account; "
                    f"sync cannot be {state_word}",
        )
    if currently_enabled == enable:
        return SyncTogglePlan(
            action="noop",
            message=f"account {name!r} sync already {state_word}",
        )
    return SyncTogglePlan(
        action="apply",
        message=f"account {name!r} sync {state_word}",
    )
```

- [x] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_sync_toggle.py -v`
Expected: PASS (parametrize expands to 7 cases).

- [x] **Step 5: mypy + commit**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail` → clean.

```bash
git add src/localmail/cli_sync_toggle.py tests/test_cli_sync_toggle.py
git commit -m "feat(cli): pure planner for enable/disable-account sync toggle"
```

---

### Task 2: Wire `enable-account` / `disable-account` into the CLI

**Files:**
- Modify: `src/localmail/cli.py` (add import; add shared helper + two commands next to `remove_account`)
- Test: `tests/test_cli_accounts_db.py` (extend)

The DB integration tests reuse the helpers already in
`tests/test_cli_accounts_db.py`: `_run(args, config_path)` (a `CliRunner`
wrapper around `localmail.cli.main`), `_make_db_account(dsn, name, *, auth=...,
sync_enabled=...)`, `_write_config(tmp_path, dsn)`, and the fixtures `db_conn`,
`db_dsn`, `tmp_path`. Account rows are re-read via
`get_account_by_name(conn, name)` (returns an `Account` with `sync_enabled` and
`updated_at`).

- [x] **Step 1: Write the failing DB integration tests**

Append to `tests/test_cli_accounts_db.py`:

```python
def test_disable_then_enable_account_flips_sync_enabled(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    """disable-account clears sync_enabled; enable-account sets it again."""
    _make_db_account(db_dsn, "work")  # password account, sync_enabled defaults TRUE
    cfg = _write_config(tmp_path, db_dsn)

    result = _run(["disable-account", "work"], cfg)
    assert result.exit_code == 0, result.output
    assert "disabled" in result.output
    with psycopg.connect(db_dsn) as conn:
        acct = get_account_by_name(conn, "work")
    assert acct is not None and acct.sync_enabled is False

    result = _run(["enable-account", "work"], cfg)
    assert result.exit_code == 0, result.output
    assert "enabled" in result.output
    with psycopg.connect(db_dsn) as conn:
        acct = get_account_by_name(conn, "work")
    assert acct is not None and acct.sync_enabled is True


def test_enable_account_idempotent_does_not_bump_updated_at(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    """Re-enabling an already-enabled account echoes 'already' and is a no-op."""
    _make_db_account(db_dsn, "work")  # already sync_enabled = TRUE
    cfg = _write_config(tmp_path, db_dsn)
    with psycopg.connect(db_dsn) as conn:
        before = get_account_by_name(conn, "work").updated_at

    result = _run(["enable-account", "work"], cfg)
    assert result.exit_code == 0, result.output
    assert "already enabled" in result.output
    with psycopg.connect(db_dsn) as conn:
        after = get_account_by_name(conn, "work").updated_at
    assert after == before  # no write -> updated_at unchanged


def test_enable_account_unknown_name_errors(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    cfg = _write_config(tmp_path, db_dsn)
    result = _run(["enable-account", "ghost"], cfg)
    assert result.exit_code != 0
    assert "no such account" in result.output


def test_enable_account_archive_is_rejected(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    _make_db_account(db_dsn, "legacy", auth="archive")
    cfg = _write_config(tmp_path, db_dsn)
    result = _run(["enable-account", "legacy"], cfg)
    assert result.exit_code != 0
    assert "archive" in result.output.lower()


def test_disable_account_archive_is_rejected(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    _make_db_account(db_dsn, "legacy", auth="archive")
    cfg = _write_config(tmp_path, db_dsn)
    result = _run(["disable-account", "legacy"], cfg)
    assert result.exit_code != 0
    assert "archive" in result.output.lower()
```

- [x] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_accounts_db.py -k "enable_account or disable_account or flips_sync" -v`
Expected: FAIL — `No such command 'enable-account'` (non-zero exit).

- [x] **Step 3: Add the imports**

In `src/localmail/cli.py`, add `update_account` to the `.api.admin.accounts`
import block and add `from .cli_sync_toggle import plan_sync_toggle`.

- [x] **Step 4: Add the shared helper + two commands**

In `src/localmail/cli.py`, immediately before the `oauth-login` command:

```python
def _apply_sync_toggle(ctx: click.Context, name: str, *, enable: bool) -> None:
    """Resolve NAME in the DB and enable/disable its sync per the pure planner."""
    cfg = load_config(ctx.obj["config_path"])
    with psycopg.connect(cfg.database.dsn) as conn:
        account = get_account_by_name(conn, name)
        if account is None:
            raise click.ClickException(f"no such account: {name!r}")
        plan = plan_sync_toggle(
            name=name, auth_method=account.auth_method,
            currently_enabled=account.sync_enabled, enable=enable,
        )
        if plan.action == "reject":
            raise click.ClickException(plan.message)
        if plan.action == "apply":
            update_account(conn, account.id, sync_enabled=enable)
            conn.commit()
        click.echo(plan.message)


@main.command("enable-account")
@click.argument("name")
@click.pass_context
def enable_account(ctx: click.Context, name: str) -> None:
    """Resume syncing an account (set sync_enabled = TRUE)."""
    _apply_sync_toggle(ctx, name, enable=True)


@main.command("disable-account")
@click.argument("name")
@click.pass_context
def disable_account(ctx: click.Context, name: str) -> None:
    """Pause syncing an account (set sync_enabled = FALSE)."""
    _apply_sync_toggle(ctx, name, enable=False)
```

- [x] **Step 5: Run the new tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_accounts_db.py -k "enable_account or disable_account or flips_sync" -v`
Expected: PASS.

- [x] **Step 6: Full suite + mypy** — `1051 passed`, mypy clean.

- [x] **Step 7: Commit**

```bash
git add src/localmail/cli.py tests/test_cli_accounts_db.py
git commit -m "feat(cli): enable-account / disable-account toggle sync_enabled"
```

---

### Task 3: Docs

**Files:**
- Modify: `README.md` (Sync & accounts command table)
- Modify: `CLAUDE.md` (commands block + 2A invariant note)

- [x] **Step 1: README** — add an `enable-account` / `disable-account` row after
  the `remove-account` row.
- [x] **Step 2: CLAUDE.md** — add the two `uv run localmail …` lines to the
  Commands block and append a `sync_enabled CLI setter` bullet to the 2A notes.
- [x] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: document enable-account / disable-account"
```

---

## Self-review notes

- **Spec coverage:** planner (Task 1) ↔ spec "Design — pure planner"; commands +
  helper + DB tests (Task 2) ↔ spec "Behaviour" table + "Testing"; docs (Task 3)
  ↔ spec "Files" README/CLAUDE rows. All spec rows covered. No migration.
- **Placeholder scan:** none — all code shown in full.
- **Type consistency:** `SyncTogglePlan(action, message)`, `ToggleAction`
  literal, and `plan_sync_toggle(*, name, auth_method, currently_enabled,
  enable)` used identically in Tasks 1 and 2. `update_account(conn, id,
  sync_enabled=...)` matches `api/admin/accounts.py`.
