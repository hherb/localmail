# TOML→DB account seed at `init-db` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `localmail init-db` perform a one-time, idempotent, name-keyed merge of `config.toml` `[[accounts]]` blocks into the `accounts` table — inserting new accounts, skipping existing ones (DB is canonical), and logging a WARNING when a skipped account's TOML values have drifted from the DB row.

**Architecture:** A new module `src/localmail/account_seed.py` with a **pure** planner (`plan_account_seed`) and a thin **IO** wrapper (`seed_accounts`). The planner decides inserts + drift from plain data; the wrapper reads existing rows, inserts via the existing `api.admin.accounts.create_account` (reusing its validation), logs drift, and returns counts. `init-db` calls the wrapper after migrations apply. A small public `list_accounts_full` accessor is added to `accounts.py` so the seed reads full rows without importing a private SELECT or duplicating its column list.

**Tech Stack:** Python 3.12, psycopg v3 (`class_row`), pydantic v2 (`AccountConfig`), click, pytest.

**Spec:** [docs/superpowers/specs/2026-05-29-toml-db-account-seed-design.md](../specs/2026-05-29-toml-db-account-seed-design.md)

---

## File structure

```
src/localmail/api/admin/accounts.py   # MODIFY: add public list_accounts_full(conn) -> list[Account]
src/localmail/account_seed.py         # CREATE: AccountDrift, SeedPlan, SeedResult, plan_account_seed, seed_accounts
src/localmail/cli.py                  # MODIFY: init_db seeds after apply_migrations
tests/test_account_seed.py            # CREATE: list_accounts_full + pure planner + IO seed tests
tests/test_cli_init_db_seed.py        # CREATE: init-db CLI echo + DB-effect test
CLAUDE.md                             # MODIFY: document the init-db seed behaviour
```

Reference types (already exist, do not redefine):
- `localmail.config.AccountConfig` — fields: `name, email, imap_host, imap_port, auth_method, oauth_provider, folder_allow, folder_deny, folder_deny_flags, poll_seconds`.
- `localmail.api.admin.accounts.Account` (frozen dataclass) — fields: `id, name, email_address, auth_method, oauth_provider, imap_host, imap_port, folder_allow, folder_deny, folder_deny_flags, sync_enabled, created_at, updated_at`.
- `localmail.api.admin.accounts.create_account(conn, *, name, email_address, auth_method, imap_host, imap_port, oauth_provider, folder_allow, folder_deny, folder_deny_flags) -> Account`.
- `localmail.api.admin.accounts.AccountFieldError(ValueError)`.
- `_SELECT_FULL` constant in `accounts.py` — the canonical full-row column list.

---

## Task 1: Public `list_accounts_full` accessor on the admin service

**Files:**
- Modify: `src/localmail/api/admin/accounts.py` (add after `get_account`, ~line 87)
- Test: `tests/test_account_seed.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_account_seed.py` with:

```python
"""Tests for the TOML->DB account seed (init-db)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from localmail.api.admin.accounts import (
    Account,
    create_account,
    list_accounts_full,
)
from localmail.config import AccountConfig

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _cfg(name: str, **overrides) -> AccountConfig:
    """An AccountConfig with sensible live-IMAP defaults."""
    base = dict(
        name=name,
        email=f"{name}@example.com",
        imap_host="imap.example.com",
        imap_port=993,
        auth_method="password",
        oauth_provider=None,
        folder_allow=[],
        folder_deny=[],
        folder_deny_flags=[],
    )
    base.update(overrides)
    return AccountConfig(**base)


def _db_account(name: str, **overrides) -> Account:
    """An Account (DB-row dataclass) for pure-planner tests."""
    base = dict(
        id=1,
        name=name,
        email_address=f"{name}@example.com",
        auth_method="password",
        oauth_provider=None,
        imap_host="imap.example.com",
        imap_port=993,
        folder_allow=[],
        folder_deny=[],
        folder_deny_flags=[],
        sync_enabled=True,
        created_at=_T0,
        updated_at=_T0,
    )
    base.update(overrides)
    return Account(**base)


def test_list_accounts_full_returns_full_rows(db_conn) -> None:
    create_account(
        db_conn, name="alice", email_address="alice@example.com",
        auth_method="password", imap_host="imap.example.com", imap_port=993,
        oauth_provider=None, folder_allow=["INBOX"], folder_deny=[],
        folder_deny_flags=["\\Trash"],
    )
    db_conn.commit()

    rows = list_accounts_full(db_conn)

    assert [r.name for r in rows] == ["alice"]
    row = rows[0]
    assert row.email_address == "alice@example.com"
    assert row.imap_host == "imap.example.com"
    assert row.folder_allow == ["INBOX"]
    assert row.folder_deny_flags == ["\\Trash"]
    assert row.sync_enabled is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_account_seed.py::test_list_accounts_full_returns_full_rows -v`
Expected: FAIL with `ImportError: cannot import name 'list_accounts_full'`.

- [ ] **Step 3: Write minimal implementation**

In `src/localmail/api/admin/accounts.py`, add directly after the `get_account` function (after line ~86):

```python
def list_accounts_full(conn: psycopg.Connection) -> list[Account]:
    """Return every account as a full Account row, oldest first.

    Shares the `_SELECT_FULL` column shape with `get_account` so the two
    cannot drift. Used by the init-db TOML->DB seed to detect config drift.
    """
    with conn.cursor(row_factory=class_row(Account)) as cur:
        cur.execute(_SELECT_FULL + " ORDER BY id")
        return cur.fetchall()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_account_seed.py::test_list_accounts_full_returns_full_rows -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/admin/accounts.py tests/test_account_seed.py
git commit -m "feat(admin): add public list_accounts_full accessor

Reuses _SELECT_FULL so it cannot drift from get_account. Needed by the
init-db TOML->DB account seed to read full rows for drift detection.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Pure planner `plan_account_seed`

**Files:**
- Create: `src/localmail/account_seed.py`
- Test: `tests/test_account_seed.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_account_seed.py`:

```python
from localmail.account_seed import (
    AccountDrift,
    SeedPlan,
    plan_account_seed,
)


def test_plan_empty_config_is_empty_plan() -> None:
    plan = plan_account_seed([], {})
    assert plan == SeedPlan(to_insert=[], drift=[])


def test_plan_all_new_names_all_insert() -> None:
    cfgs = [_cfg("alice"), _cfg("bob")]
    plan = plan_account_seed(cfgs, {})
    assert plan.to_insert == cfgs
    assert plan.drift == []


def test_plan_identical_match_is_skipped_no_drift() -> None:
    cfg = _cfg("alice")
    existing = {"alice": _db_account("alice")}
    plan = plan_account_seed([cfg], existing)
    assert plan.to_insert == []
    assert plan.drift == []


def test_plan_single_field_drift_lists_that_field() -> None:
    cfg = _cfg("alice", imap_port=143)
    existing = {"alice": _db_account("alice", imap_port=993)}
    plan = plan_account_seed([cfg], existing)
    assert plan.to_insert == []
    assert plan.drift == [AccountDrift(name="alice", fields=["imap_port"])]


def test_plan_multi_field_drift_lists_all() -> None:
    cfg = _cfg("alice", imap_port=143, email="new@example.com")
    existing = {"alice": _db_account("alice", imap_port=993,
                                     email_address="old@example.com")}
    plan = plan_account_seed([cfg], existing)
    assert plan.to_insert == []
    assert len(plan.drift) == 1
    assert set(plan.drift[0].fields) == {"imap_port", "email_address"}


def test_plan_folder_none_vs_empty_is_not_drift() -> None:
    cfg = _cfg("alice", folder_allow=[])
    existing = {"alice": _db_account("alice", folder_allow=None)}
    plan = plan_account_seed([cfg], existing)
    assert plan.drift == []


def test_plan_folder_order_matters() -> None:
    cfg = _cfg("alice", folder_allow=["A", "B"])
    existing = {"alice": _db_account("alice", folder_allow=["B", "A"])}
    plan = plan_account_seed([cfg], existing)
    assert plan.drift == [AccountDrift(name="alice", fields=["folder_allow"])]


def test_plan_mixed_batch() -> None:
    cfgs = [
        _cfg("new"),                       # insert
        _cfg("same"),                      # skip, no drift
        _cfg("drift", imap_port=143),      # skip, drift
    ]
    existing = {
        "same": _db_account("same"),
        "drift": _db_account("drift", imap_port=993),
    }
    plan = plan_account_seed(cfgs, existing)
    assert [c.name for c in plan.to_insert] == ["new"]
    assert plan.drift == [AccountDrift(name="drift", fields=["imap_port"])]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_account_seed.py -k plan -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'localmail.account_seed'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/localmail/account_seed.py`:

```python
"""One-time TOML->DB account seed, run at init-db.

A pure planner (`plan_account_seed`) decides which config.toml accounts to
insert and which existing accounts have drifted from the DB; a thin IO
wrapper (`seed_accounts`) reads existing rows, inserts via the admin service
layer, logs drift, and returns counts. The DB is canonical: existing rows
are never overwritten by the seed.

See docs/superpowers/specs/2026-05-29-toml-db-account-seed-design.md.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

import psycopg

from localmail.api.admin.accounts import (
    Account,
    create_account,
    list_accounts_full,
)
from localmail.config import AccountConfig

logger = logging.getLogger("localmail.account_seed")


@dataclass(frozen=True)
class AccountDrift:
    """An existing account whose config.toml values differ from the DB."""

    name: str
    fields: list[str]


@dataclass(frozen=True)
class SeedPlan:
    """The decided seed: rows to insert + accounts that drifted."""

    to_insert: list[AccountConfig]
    drift: list[AccountDrift]


@dataclass(frozen=True)
class SeedResult:
    """Outcome counts from a seed run."""

    inserted: int
    skipped: int
    drifted: int


def _norm_folders(value: list[str] | None) -> list[str]:
    """Normalize a folder list so NULL (DB) and [] (TOML default) compare equal."""
    return list(value) if value is not None else []


def _drifted_fields(cfg: AccountConfig, db: Account) -> list[str]:
    """Return the seedable field names whose config value differs from the DB row.

    Folder lists are compared order-sensitively after None->[] normalization.
    """
    drifted: list[str] = []
    if cfg.email != db.email_address:
        drifted.append("email_address")
    if cfg.imap_host != db.imap_host:
        drifted.append("imap_host")
    if cfg.imap_port != db.imap_port:
        drifted.append("imap_port")
    if cfg.auth_method != db.auth_method:
        drifted.append("auth_method")
    if cfg.oauth_provider != db.oauth_provider:
        drifted.append("oauth_provider")
    if _norm_folders(cfg.folder_allow) != _norm_folders(db.folder_allow):
        drifted.append("folder_allow")
    if _norm_folders(cfg.folder_deny) != _norm_folders(db.folder_deny):
        drifted.append("folder_deny")
    if _norm_folders(cfg.folder_deny_flags) != _norm_folders(db.folder_deny_flags):
        drifted.append("folder_deny_flags")
    return drifted


def plan_account_seed(
    config_accounts: list[AccountConfig],
    existing: Mapping[str, Account],
) -> SeedPlan:
    """Decide the seed from config accounts + existing DB rows (keyed by name).

    New names are inserted; existing names are skipped, with drifted fields
    recorded for warning. Pure: no IO, no logging, no clock.
    """
    to_insert: list[AccountConfig] = []
    drift: list[AccountDrift] = []
    for cfg in config_accounts:
        db = existing.get(cfg.name)
        if db is None:
            to_insert.append(cfg)
            continue
        fields = _drifted_fields(cfg, db)
        if fields:
            drift.append(AccountDrift(name=cfg.name, fields=fields))
    return SeedPlan(to_insert=to_insert, drift=drift)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_account_seed.py -k plan -v`
Expected: PASS (8 planner tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/account_seed.py tests/test_account_seed.py
git commit -m "feat(seed): pure plan_account_seed planner for TOML->DB merge

Decides inserts vs skip-with-drift from config accounts + existing DB
rows. Folder lists normalized None->[] and compared order-sensitively.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: IO wrapper `seed_accounts`

**Files:**
- Modify: `src/localmail/account_seed.py` (append `seed_accounts`)
- Test: `tests/test_account_seed.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_account_seed.py`:

```python
from localmail.account_seed import SeedResult, seed_accounts


def test_seed_inserts_new_accounts(db_conn) -> None:
    cfgs = [
        _cfg("alice", folder_allow=["INBOX"], folder_deny_flags=["\\Trash"]),
        _cfg("bob"),
    ]
    result = seed_accounts(db_conn, cfgs)
    db_conn.commit()

    assert result == SeedResult(inserted=2, skipped=0, drifted=0)
    rows = {r.name: r for r in list_accounts_full(db_conn)}
    assert set(rows) == {"alice", "bob"}
    assert rows["alice"].folder_allow == ["INBOX"]
    assert rows["alice"].folder_deny_flags == ["\\Trash"]
    assert rows["alice"].sync_enabled is True


def test_seed_is_idempotent(db_conn) -> None:
    cfgs = [_cfg("alice"), _cfg("bob")]
    seed_accounts(db_conn, cfgs)
    db_conn.commit()

    result = seed_accounts(db_conn, cfgs)
    db_conn.commit()

    assert result == SeedResult(inserted=0, skipped=2, drifted=0)
    assert len(list_accounts_full(db_conn)) == 2


def test_seed_empty_config_is_noop(db_conn) -> None:
    result = seed_accounts(db_conn, [])
    assert result == SeedResult(inserted=0, skipped=0, drifted=0)
    assert list_accounts_full(db_conn) == []


def test_seed_drift_warns_without_mutating(db_conn, caplog) -> None:
    seed_accounts(db_conn, [_cfg("alice", imap_port=993)])
    db_conn.commit()

    with caplog.at_level(logging.WARNING, logger="localmail.account_seed"):
        result = seed_accounts(db_conn, [_cfg("alice", imap_port=143)])
        db_conn.commit()

    assert result == SeedResult(inserted=0, skipped=1, drifted=1)
    # DB row unchanged — DB is canonical.
    row = list_accounts_full(db_conn)[0]
    assert row.imap_port == 993
    # Exactly one WARNING naming the drifted field.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "imap_port" in warnings[0].getMessage()
    assert "alice" in warnings[0].getMessage()


def test_seed_mixed_batch_counts(db_conn) -> None:
    seed_accounts(db_conn, [_cfg("same"), _cfg("drift", imap_port=993)])
    db_conn.commit()

    cfgs = [
        _cfg("new"),
        _cfg("same"),
        _cfg("drift", imap_port=143),
    ]
    result = seed_accounts(db_conn, cfgs)
    db_conn.commit()

    assert result == SeedResult(inserted=1, skipped=2, drifted=1)
    assert {r.name for r in list_accounts_full(db_conn)} == {"same", "drift", "new"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_account_seed.py -k seed -v`
Expected: FAIL with `ImportError: cannot import name 'seed_accounts'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/localmail/account_seed.py`:

```python
def seed_accounts(
    conn: psycopg.Connection,
    config_accounts: list[AccountConfig],
    *,
    logger: logging.Logger = logger,
) -> SeedResult:
    """Merge config.toml accounts into the DB, keyed by name.

    New accounts are inserted via the admin service layer (reusing its
    validation); existing accounts are skipped and any drift is logged at
    WARNING. The DB is canonical — existing rows are never modified. The
    caller owns the transaction (commit on success).
    """
    existing = {row.name: row for row in list_accounts_full(conn)}
    plan = plan_account_seed(config_accounts, existing)

    for cfg in plan.to_insert:
        create_account(
            conn,
            name=cfg.name,
            email_address=cfg.email,
            auth_method=cfg.auth_method,
            imap_host=cfg.imap_host,
            imap_port=cfg.imap_port,
            oauth_provider=cfg.oauth_provider,
            folder_allow=cfg.folder_allow,
            folder_deny=cfg.folder_deny,
            folder_deny_flags=cfg.folder_deny_flags,
        )

    for d in plan.drift:
        logger.warning(
            "account %r: config.toml differs from DB (fields: %s); "
            "DB is canonical, TOML ignored",
            d.name,
            ", ".join(d.fields),
        )

    return SeedResult(
        inserted=len(plan.to_insert),
        skipped=len(config_accounts) - len(plan.to_insert),
        drifted=len(plan.drift),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_account_seed.py -v`
Expected: PASS (all planner + IO tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/account_seed.py tests/test_account_seed.py
git commit -m "feat(seed): IO seed_accounts wrapper over the planner

Reads existing rows, inserts new accounts via create_account (DRY
validation), logs drift at WARNING, returns inserted/skipped/drifted
counts. DB-canonical: never overwrites existing rows.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Wire the seed into `init-db`

**Files:**
- Modify: `src/localmail/cli.py` (imports + `init_db`, lines 16-24 and 101-114)
- Test: `tests/test_cli_init_db_seed.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_init_db_seed.py`:

```python
"""init-db seeds config.toml [[accounts]] into the DB (Sub-plan 2A.2)."""
from __future__ import annotations

import os
from pathlib import Path

from click.testing import CliRunner

from localmail.api.admin.accounts import list_accounts_full
from localmail.cli import main


def _config_with_accounts(tmp_path: Path, dsn: str) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[database]\ndsn = "{dsn}"\n\n'
        f'[attachments]\nroot = "{tmp_path / "att"}"\n\n'
        '[[accounts]]\n'
        'name = "alice"\n'
        'email = "alice@example.com"\n'
        'imap_host = "imap.example.com"\n'
        'imap_port = 993\n'
        'auth_method = "password"\n\n'
        '[[accounts]]\n'
        'name = "bob"\n'
        'email = "bob@example.com"\n'
        'imap_host = "imap.example.com"\n'
        'imap_port = 993\n'
        'auth_method = "password"\n'
    )
    return cfg


def test_init_db_seeds_accounts(db_conn, db_dsn: str, tmp_path: Path) -> None:
    cfg = _config_with_accounts(tmp_path, db_dsn)
    env = {**os.environ, "LOCALMAIL_DSN_OVERRIDE": db_dsn}
    runner = CliRunner()

    r = runner.invoke(main, ["--config", str(cfg), "init-db"], env=env)

    assert r.exit_code == 0, r.output
    assert "seeded accounts: inserted=2 skipped=0 drifted=0" in r.output
    # Re-read on a fresh connection so we see the CLI's committed rows.
    import psycopg
    with psycopg.connect(db_dsn) as conn:
        names = {row.name for row in list_accounts_full(conn)}
    assert names == {"alice", "bob"}


def test_init_db_seed_is_idempotent(db_conn, db_dsn: str, tmp_path: Path) -> None:
    cfg = _config_with_accounts(tmp_path, db_dsn)
    env = {**os.environ, "LOCALMAIL_DSN_OVERRIDE": db_dsn}
    runner = CliRunner()

    runner.invoke(main, ["--config", str(cfg), "init-db"], env=env)
    r = runner.invoke(main, ["--config", str(cfg), "init-db"], env=env)

    assert r.exit_code == 0, r.output
    assert "seeded accounts: inserted=0 skipped=2 drifted=0" in r.output
```

Note: the `db_conn` fixture dependency is present only to TRUNCATE the
`accounts` table before each test; the CLI opens its own connection.

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_init_db_seed.py -v`
Expected: FAIL — `seeded accounts:` line absent from output (init-db does not seed yet).

- [ ] **Step 3: Write minimal implementation**

In `src/localmail/cli.py`, add to the existing local imports block (after line 19, `from .db import apply_migrations`):

```python
from .account_seed import seed_accounts
from .api.admin.accounts import AccountFieldError
```

Replace the `init_db` body (lines 103-114) with:

```python
def init_db(ctx: click.Context) -> None:
    """Apply pending schema migrations, then seed accounts from config.toml."""
    cfg = load_config(ctx.obj["config_path"])
    applied = apply_migrations(
        cfg.database.dsn,
        index_build_work_mem_mb=cfg.search.index_build_maintenance_work_mem_mb,
    )
    if applied:
        for rev in applied:
            click.echo(f"applied {rev}")
    else:
        click.echo("schema already up to date")

    try:
        with psycopg.connect(cfg.database.dsn) as conn:
            result = seed_accounts(conn, cfg.accounts)
            conn.commit()
    except AccountFieldError as exc:
        raise click.ClickException(f"account seed failed: {exc}") from exc
    click.echo(
        f"seeded accounts: inserted={result.inserted} "
        f"skipped={result.skipped} drifted={result.drifted}"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_init_db_seed.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/cli.py tests/test_cli_init_db_seed.py
git commit -m "feat(cli): init-db seeds config.toml accounts into the DB

After migrations apply, merge [[accounts]] blocks into the accounts
table (idempotent, name-keyed, DB-canonical) and echo the counts. A
malformed block aborts non-zero via ClickException.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Document the behaviour + full verification

**Files:**
- Modify: `CLAUDE.md` (the "DB-canonical accounts + admin CRUD (Sub-plan 2A)" section)

- [ ] **Step 1: Add the CLAUDE.md note**

In `CLAUDE.md`, in the Sub-plan 2A paragraph, after the sentence ending "Deferred to Sub-plan 2A.2: rewiring CLI commands ... and TOML→DB seed at `init-db`.", update that deferral note to reflect what now ships. Replace:

```
Deferred to Sub-plan 2A.2: rewiring CLI
commands (`add-account`, `oauth-login`, `remove-account`) to write to the
DB and TOML→DB seed at `init-db`.
```

with:

```
**TOML→DB seed (Sub-plan 2A.2 slice 1, shipped):** `init-db` now merges
`config.toml` `[[accounts]]` into the `accounts` table after migrations
apply — idempotent, keyed by `name`, **DB-canonical** (existing rows are
never overwritten; a drifted TOML value logs a WARNING naming the fields
and is otherwise ignored). Implemented as a pure planner
(`account_seed.plan_account_seed`) + IO wrapper (`account_seed.seed_accounts`,
inserting via `create_account` to reuse validation); `init-db` echoes
`seeded accounts: inserted=N skipped=M drifted=K`. Still deferred to later
2A.2 slices: rewiring CLI `add-account` / `oauth-login` / `remove-account`
to the DB, switching the daemon's account source to the DB, and honouring
`sync_enabled`. Note `sync.py:upsert_account` still overwrites
`email/host/port/auth_method/oauth_provider` from config on first sync, so
the DB is not yet *fully* canonical against the running daemon until the
daemon-source slice lands.
```

- [ ] **Step 2: Run the full account-seed + CLI test subset**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_account_seed.py tests/test_cli_init_db_seed.py tests/test_admin_accounts.py -v`
Expected: PASS (all).

- [ ] **Step 3: Type-check the touched modules**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail/account_seed.py src/localmail/api/admin/accounts.py src/localmail/cli.py`
Expected: `Success: no issues found` (the pre-existing 4 `parser.py` errors are in a different module and out of scope).

- [ ] **Step 4: Run the full suite**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/`
Expected: all pass (was 974 passed at session start; +~15 new tests).

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): document the init-db TOML->DB account seed

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** Goal (idempotent name-keyed merge) → Tasks 2-4. DB-wins + drift-warn → Task 2 planner + Task 3 logging. `None≡[]` normalization → Task 2 (`_norm_folders`) + `test_plan_folder_none_vs_empty_is_not_drift`. Order-sensitive folders → `test_plan_folder_order_matters`. `email→email_address` mapping → planner + `test_plan_multi_field_drift_lists_all`. Abort-on-validation-error → Task 4 `AccountFieldError`→`ClickException`. Seedable field set excludes `sync_enabled`/`poll_seconds` → planner only compares the eight fields. CLI summary echo → Task 4. Known `upsert_account` interaction → documented in Task 5 CLAUDE.md note.
- **Placeholder scan:** none — every code step is complete.
- **Type consistency:** `plan_account_seed(config_accounts, existing)`, `seed_accounts(conn, config_accounts, *, logger)`, `SeedResult(inserted, skipped, drifted)`, `AccountDrift(name, fields)`, `list_accounts_full(conn)` used identically across tasks and tests.
- **Reuse:** `list_accounts_full` shares `_SELECT_FULL`; `seed_accounts` reuses `create_account`'s validation; no duplicated SQL or validation.
```
