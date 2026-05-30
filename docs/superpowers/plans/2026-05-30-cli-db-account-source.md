# CLI DB Account Source (Sub-plan 2A.2d) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Point the `localmail` CLI account commands (`list-accounts`, `add-account`, `oauth-login`, `remove-account`, one-shot `sync`) at the `accounts` DB table via `localmail.api.admin.accounts`, and delete the now-orphaned `sync.upsert_account`.

**Architecture:** A new name-lookup service accessor (`get_account_by_name`), a pure resolver module (`cli_account_resolve.py`) that decides "use the DB row / seed it from TOML / not found", a shared TOML→`create_account` kwargs helper promoted out of `account_seed.py`, and a `sync_account` signature that takes an explicit `account_id`. The CLI becomes thin glue over these. The DB is canonical for accounts (continuing Sub-plan 2A.2b).

**Tech Stack:** Python 3.12, `click`, `psycopg` v3, `pytest` + `click.testing.CliRunner`, in-memory `keyring` (`memory_keyring` fixture), real Postgres test DB (`db_dsn` / `clean_accounts` fixtures).

---

## Spec

[docs/superpowers/specs/2026-05-30-cli-db-account-source-design.md](../specs/2026-05-30-cli-db-account-source-design.md)

## Background facts (verified against the code)

Signatures the tasks below rely on — do not re-derive, they are confirmed:

- `localmail.secrets`: `set_password(name, pw)`, `get_password(name) -> str|None`,
  `delete_password(name)`, `set_refresh_token(name, tok)`,
  `get_refresh_token(name) -> str|None`, `delete_refresh_token(name)`.
- `localmail.api.admin.accounts`:
  - `Account` dataclass fields: `id, name, email_address, auth_method,
    oauth_provider, imap_host, imap_port, folder_allow, folder_deny,
    folder_deny_flags, sync_enabled, created_at, updated_at`.
  - `_SELECT_FULL` — shared SELECT column list; reuse it for `get_account_by_name`.
  - `get_account(conn, id) -> Account` (raises `NotFound`).
  - `list_accounts_full(conn) -> list[Account]`, `list_syncable_accounts(conn) -> list[Account]`.
  - `create_account(conn, *, name, email_address, auth_method, imap_host,
    imap_port, oauth_provider, folder_allow, folder_deny, folder_deny_flags) -> Account`.
  - `delete_account(conn, account_id, *, force=False)` raises `AccountInUse`
    (subclass of `ValueError`) / `NotFound`.
  - `store_password(account: Account, pw)` (asserts `auth_method=='password'`),
    `clear_secret(account: Account)`.
- `localmail.daemon_accounts.account_config_from_row(account: Account) -> AccountConfig`
  — raises `ValueError` for archive / missing host.
- `localmail.config.AccountConfig` fields: `name, email, imap_host, imap_port,
  auth_method, oauth_provider, folder_allow, folder_deny, folder_deny_flags,
  poll_seconds`.
- `localmail.account_seed.seed_accounts(conn, config_accounts)` builds
  `create_account(...)` kwargs inline (Task 3 promotes this to a helper).
- `localmail.sync.sync_account(conn, imap, *, account, attachments_root,
  max_messages=None, progress=None)` calls `upsert_account` internally
  (Tasks 4 & 8 change this).
- `localmail.oauth_gmail.run_consent_flow(client_secrets_file) -> creds`
  with `creds.refresh_token`.
- CLI test pattern (from `tests/test_cli_init_db_seed.py`): `CliRunner`,
  a `_run(args, config_path)` wrapper invoking `main` with `--config`, a
  `_write_config(tmp_path, dsn, body)` helper, and a `clean_accounts`
  fixture that `TRUNCATE accounts RESTART IDENTITY CASCADE`.

## Out of scope (do not touch)

- `backfill-internal-date` keeps using the TOML `_account_or_die` helper —
  not in this slice's command set. Leave `_account_or_die` in place.
- Admin-UI screens, daemon control, mbox import, a `sync_enabled` CLI toggle.

---

### Task 1: `get_account_by_name` service accessor

**Files:**
- Modify: `src/localmail/api/admin/accounts.py` (add after `get_account`)
- Test: `tests/test_admin_accounts.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_admin_accounts.py` (it already has a `db_conn` fixture and
`create_account` helpers — follow the file's existing style for building a row):

```python
def test_get_account_by_name_returns_row(db_conn):
    from localmail.api.admin.accounts import create_account, get_account_by_name
    created = create_account(
        db_conn, name="work", email_address="w@example.com",
        auth_method="password", imap_host="imap.example.com", imap_port=993,
        oauth_provider=None, folder_allow=None, folder_deny=None,
        folder_deny_flags=None,
    )
    got = get_account_by_name(db_conn, "work")
    assert got is not None
    assert got.id == created.id
    assert got.name == "work"


def test_get_account_by_name_missing_returns_none(db_conn):
    from localmail.api.admin.accounts import get_account_by_name
    assert get_account_by_name(db_conn, "nope") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_accounts.py::test_get_account_by_name_returns_row -v`
Expected: FAIL with `ImportError: cannot import name 'get_account_by_name'`.

- [ ] **Step 3: Write minimal implementation**

In `src/localmail/api/admin/accounts.py`, add directly after `get_account`:

```python
def get_account_by_name(conn: psycopg.Connection, name: str) -> Account | None:
    """Return one account by name, or None if absent.

    Unlike `get_account`, absence returns None (not NotFound): CLI callers
    treat a missing name as a normal branch (seed from TOML, or fail with a
    domain-specific message). Reuses `_SELECT_FULL` so the column shape cannot
    drift from the other readers.
    """
    with conn.cursor(row_factory=class_row(Account)) as cur:
        cur.execute(_SELECT_FULL + " WHERE name = %s", (name,))
        return cur.fetchone()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_admin_accounts.py -k get_account_by_name -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/admin/accounts.py tests/test_admin_accounts.py
git commit -m "feat(admin): get_account_by_name accessor for CLI name lookup"
```

---

### Task 2: Shared `account_create_kwargs` helper in `account_seed.py`

This promotes the inline `create_account(...)` kwargs (currently built inside
`seed_accounts`) to a reusable pure helper, so the CLI's "seed then use" branch
(Task 5/6) and `seed_accounts` insert identically.

**Files:**
- Modify: `src/localmail/account_seed.py`
- Test: `tests/test_account_seed.py` (exists; add a unit test)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_account_seed.py`:

```python
def test_account_create_kwargs_maps_all_fields():
    from localmail.account_seed import account_create_kwargs
    from localmail.config import AccountConfig
    cfg = AccountConfig(
        name="work", email="w@example.com",
        imap_host="imap.example.com", imap_port=993,
        auth_method="password", oauth_provider=None,
        folder_allow=["INBOX"], folder_deny=["Spam"], folder_deny_flags=["\\Junk"],
    )
    kw = account_create_kwargs(cfg)
    assert kw == {
        "name": "work", "email_address": "w@example.com",
        "auth_method": "password", "imap_host": "imap.example.com",
        "imap_port": 993, "oauth_provider": None,
        "folder_allow": ["INBOX"], "folder_deny": ["Spam"],
        "folder_deny_flags": ["\\Junk"],
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_account_seed.py::test_account_create_kwargs_maps_all_fields -v`
Expected: FAIL with `ImportError: cannot import name 'account_create_kwargs'`.

- [ ] **Step 3: Write minimal implementation**

In `src/localmail/account_seed.py`, add a module-level helper and refactor
`seed_accounts` to use it. Add after the dataclasses (before `plan_account_seed`):

```python
def account_create_kwargs(cfg: AccountConfig) -> dict:
    """Map an AccountConfig to create_account(...) keyword arguments.

    Single source of truth for the TOML->DB field mapping, shared by the
    init-db seed and the CLI add-account/oauth-login seed-from-TOML bridge.
    """
    return {
        "name": cfg.name,
        "email_address": cfg.email,
        "auth_method": cfg.auth_method,
        "imap_host": cfg.imap_host,
        "imap_port": cfg.imap_port,
        "oauth_provider": cfg.oauth_provider,
        "folder_allow": cfg.folder_allow,
        "folder_deny": cfg.folder_deny,
        "folder_deny_flags": cfg.folder_deny_flags,
    }
```

Then replace the inline `create_account(...)` call inside `seed_accounts`:

```python
    for cfg in plan.to_insert:
        create_account(conn, **account_create_kwargs(cfg))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_account_seed.py tests/test_cli_init_db_seed.py -v`
Expected: all pass (the new unit test + the existing seed/regression tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/account_seed.py tests/test_account_seed.py
git commit -m "refactor(account-seed): extract shared account_create_kwargs helper"
```

---

### Task 3: Pure resolver module `cli_account_resolve.py`

**Files:**
- Create: `src/localmail/cli_account_resolve.py`
- Test: `tests/test_cli_account_resolve.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_account_resolve.py`:

```python
"""Pure resolver: DB row vs seed-from-TOML vs not-found. No IO."""
from __future__ import annotations

from datetime import datetime, timezone

from localmail.api.admin.accounts import Account
from localmail.cli_account_resolve import (
    Found, NotFound, SeedThenUse, plan_account_resolution,
)
from localmail.config import AccountConfig


def _db_account(name: str) -> Account:
    now = datetime(2026, 5, 30, tzinfo=timezone.utc)
    return Account(
        id=1, name=name, email_address=f"{name}@example.com",
        auth_method="password", oauth_provider=None,
        imap_host="imap.example.com", imap_port=993,
        folder_allow=None, folder_deny=None, folder_deny_flags=None,
        sync_enabled=True, created_at=now, updated_at=now,
    )


def _toml_account(name: str) -> AccountConfig:
    return AccountConfig(
        name=name, email=f"{name}@example.com",
        imap_host="imap.example.com", imap_port=993,
        auth_method="password", oauth_provider=None,
    )


def test_found_when_in_db():
    db = {"work": _db_account("work")}
    res = plan_account_resolution("work", [_toml_account("work")], db)
    assert isinstance(res, Found)
    assert res.account.name == "work"


def test_seed_when_only_in_toml():
    res = plan_account_resolution("work", [_toml_account("work")], {})
    assert isinstance(res, SeedThenUse)
    assert res.config.name == "work"


def test_not_found_when_in_neither():
    res = plan_account_resolution("ghost", [_toml_account("work")], {})
    assert isinstance(res, NotFound)
    assert res.name == "ghost"


def test_db_wins_over_toml():
    db = {"work": _db_account("work")}
    res = plan_account_resolution("work", [_toml_account("work")], db)
    assert isinstance(res, Found)  # never SeedThenUse when the row exists
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_account_resolve.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'localmail.cli_account_resolve'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/localmail/cli_account_resolve.py`:

```python
"""Resolve a CLI account name to an action: use the DB row, seed it from
TOML first, or report it missing. Pure: no IO, no clock.

The DB is canonical for accounts (Sub-plan 2A.2b/2A.2d). When a row already
exists, TOML is irrelevant; when it does not but a [[accounts]] block names
it, the caller seeds the row from TOML before acting; otherwise the name is
unknown.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from localmail.api.admin.accounts import Account
from localmail.config import AccountConfig


@dataclass(frozen=True)
class Found:
    """The account already exists in the DB."""

    account: Account


@dataclass(frozen=True)
class SeedThenUse:
    """The account is absent from the DB but present in config.toml."""

    config: AccountConfig


@dataclass(frozen=True)
class NotFound:
    """The account is in neither the DB nor config.toml."""

    name: str


Resolution = Found | SeedThenUse | NotFound


def plan_account_resolution(
    name: str,
    toml_accounts: list[AccountConfig],
    existing: Mapping[str, Account],
) -> Resolution:
    """Decide how a CLI command should obtain the account row for `name`."""
    db_row = existing.get(name)
    if db_row is not None:
        return Found(db_row)
    for cfg in toml_accounts:
        if cfg.name == name:
            return SeedThenUse(cfg)
    return NotFound(name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_account_resolve.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/cli_account_resolve.py tests/test_cli_account_resolve.py
git commit -m "feat(cli): pure account-name resolver (DB / seed-from-TOML / not-found)"
```

---

### Task 4: Rewire `list-accounts` to the DB

**Files:**
- Modify: `src/localmail/cli.py` (the `list_accounts` command, ~lines 130-147)
- Test: `tests/test_cli_accounts_db.py` (new — shared helpers used by Tasks 4-8)

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_accounts_db.py`:

```python
"""CLI account commands operate on the DB (Sub-plan 2A.2d)."""
from __future__ import annotations

import textwrap
from pathlib import Path

import psycopg
import pytest
from click.testing import CliRunner

from localmail.api.admin.accounts import create_account, get_account_by_name
from localmail.cli import main

pytestmark = pytest.mark.usefixtures("memory_keyring")


def _write_config(tmp_path: Path, dsn: str, body: str = "") -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        textwrap.dedent(
            f"""
            [database]
            dsn = "{dsn}"

            [attachments]
            root = "{tmp_path / 'attachments'}"

            {body}
            """
        ).strip()
    )
    return cfg


def _toml_block(name: str, email: str, auth: str = "password",
                oauth_provider: str | None = None) -> str:
    extra = f'\noauth_provider = "{oauth_provider}"' if oauth_provider else ""
    return textwrap.dedent(
        f"""
        [[accounts]]
        name = "{name}"
        email = "{email}"
        imap_host = "imap.example.com"
        imap_port = 993
        auth_method = "{auth}"{extra}
        """
    ).strip()


def _run(args: list[str], config_path: Path, **kw) -> object:
    runner = CliRunner()
    return runner.invoke(main, ["--config", str(config_path), *args],
                         obj=None, **kw)


@pytest.fixture
def clean_accounts(db_dsn: str) -> str:
    with psycopg.connect(db_dsn) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE accounts RESTART IDENTITY CASCADE")
        conn.commit()
    return db_dsn


def _make_db_account(dsn: str, name: str, *, auth: str = "password",
                     oauth_provider: str | None = None,
                     sync_enabled: bool = True) -> int:
    with psycopg.connect(dsn) as conn:
        acct = create_account(
            conn, name=name, email_address=f"{name}@example.com",
            auth_method=auth,
            imap_host="imap.example.com", imap_port=993,
            oauth_provider=oauth_provider,
            folder_allow=None, folder_deny=None, folder_deny_flags=None,
        )
        if not sync_enabled:
            with conn.cursor() as cur:
                cur.execute("UPDATE accounts SET sync_enabled = FALSE WHERE id = %s",
                            (acct.id,))
        conn.commit()
        return acct.id


def test_list_accounts_reads_db(clean_accounts: str, tmp_path: Path) -> None:
    dsn = clean_accounts
    _make_db_account(dsn, "work")
    cfg = _write_config(tmp_path, dsn)  # no TOML accounts at all
    result = _run(["list-accounts"], cfg)
    assert result.exit_code == 0, result.output
    assert "work" in result.output


def test_list_accounts_empty_db(clean_accounts: str, tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, clean_accounts)
    result = _run(["list-accounts"], cfg)
    assert result.exit_code == 0, result.output
    assert "no accounts" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_accounts_db.py::test_list_accounts_reads_db -v`
Expected: FAIL — current `list-accounts` reads `cfg.accounts` (TOML), which is
empty here, so it prints "no accounts configured" and "work" is absent.

- [ ] **Step 3: Write minimal implementation**

Replace the `list_accounts` command body in `src/localmail/cli.py`:

```python
@main.command("list-accounts")
@click.pass_context
def list_accounts(ctx: click.Context) -> None:
    """Show accounts in the DB and whether a secret is stored."""
    cfg = load_config(ctx.obj["config_path"])
    with psycopg.connect(cfg.database.dsn) as conn:
        rows = list_accounts_full(conn)
    if not rows:
        click.echo("no accounts")
        return
    for a in rows:
        if a.auth_method == "archive":
            endpoint, secret_label = "archive", "n/a"
        elif a.auth_method == "password":
            endpoint = f"{a.imap_host}:{a.imap_port}"
            secret_label = "password" if secrets.get_password(a.name) else "MISSING"
        else:
            endpoint = f"{a.imap_host}:{a.imap_port}"
            secret_label = "oauth-token" if secrets.get_refresh_token(a.name) else "MISSING"
        click.echo(
            f"{a.name}\t{a.email_address}\t{endpoint}\t{a.auth_method}"
            f"\tsync={a.sync_enabled}\t[{secret_label}]"
        )
```

Add to the imports block at the top of `cli.py`:

```python
from .api.admin.accounts import AccountFieldError, list_accounts_full
```

(extend the existing `from .api.admin.accounts import AccountFieldError` line).

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_accounts_db.py -k list_accounts -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/cli.py tests/test_cli_accounts_db.py
git commit -m "feat(cli): list-accounts reads the DB"
```

---

### Task 5: Rewire `add-account` to the DB (with seed-from-TOML)

**Files:**
- Modify: `src/localmail/cli.py` (the `add_account` command)
- Test: `tests/test_cli_accounts_db.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_accounts_db.py`:

```python
def test_add_account_stores_password_for_existing_db_row(
    clean_accounts: str, tmp_path: Path
) -> None:
    dsn = clean_accounts
    _make_db_account(dsn, "work")
    cfg = _write_config(tmp_path, dsn)
    result = _run(["add-account", "work", "--password", "s3cret"], cfg)
    assert result.exit_code == 0, result.output
    from localmail import secrets as s
    assert s.get_password("work") == "s3cret"


def test_add_account_seeds_from_toml_when_absent(
    clean_accounts: str, tmp_path: Path
) -> None:
    dsn = clean_accounts
    cfg = _write_config(tmp_path, dsn, _toml_block("work", "work@example.com"))
    result = _run(["add-account", "work", "--password", "s3cret"], cfg)
    assert result.exit_code == 0, result.output
    with psycopg.connect(dsn) as conn:
        assert get_account_by_name(conn, "work") is not None  # row created
    from localmail import secrets as s
    assert s.get_password("work") == "s3cret"


def test_add_account_unknown_name_fails(
    clean_accounts: str, tmp_path: Path
) -> None:
    cfg = _write_config(tmp_path, clean_accounts)
    result = _run(["add-account", "ghost", "--password", "x"], cfg)
    assert result.exit_code != 0
    assert "ghost" in result.output


def test_add_account_rejects_oauth_row(
    clean_accounts: str, tmp_path: Path
) -> None:
    dsn = clean_accounts
    _make_db_account(dsn, "gmail", auth="oauth2", oauth_provider="gmail")
    cfg = _write_config(tmp_path, dsn)
    result = _run(["add-account", "gmail", "--password", "x"], cfg)
    assert result.exit_code != 0
    assert "oauth-login" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_accounts_db.py -k add_account -v`
Expected: FAIL — current `add_account` reads `cfg.accounts` via `_account_or_die`.

- [ ] **Step 3: Write minimal implementation**

Add a small IO helper near the top-level CLI helpers in `cli.py` (after
`_account_or_die`), used by both `add-account` and `oauth-login`:

```python
def _resolve_account_row(conn, cfg: Config, name: str):
    """Resolve `name` to an Account row, seeding it from TOML if absent.

    Returns the DB Account. Raises click.ClickException when the name is in
    neither the DB nor config.toml, or when a malformed TOML block fails
    create_account validation.
    """
    from .api.admin.accounts import Account, create_account, get_account_by_name
    from .account_seed import account_create_kwargs
    from .cli_account_resolve import (
        Found, NotFound, SeedThenUse, plan_account_resolution,
    )

    existing = {row.name: row for row in list_accounts_full(conn)}
    res = plan_account_resolution(name, cfg.accounts, existing)
    if isinstance(res, Found):
        return res.account
    if isinstance(res, NotFound):
        raise click.ClickException(
            f"unknown account {name!r}: not in the DB and no matching "
            f"[[accounts]] block in config.toml"
        )
    try:
        return create_account(conn, **account_create_kwargs(res.config))
    except AccountFieldError as exc:
        raise click.ClickException(f"cannot create account {name!r}: {exc}") from exc
```

Replace the `add_account` command body:

```python
@main.command("add-account")
@click.argument("name")
@click.option("--password", "password_opt", default=None,
              help="Password (prompted securely if omitted). "
                   "Only for auth_method='password'.")
@click.pass_context
def add_account(ctx: click.Context, name: str, password_opt: str | None) -> None:
    """Store the IMAP password for an account in the keyring.

    Resolves NAME against the DB; if absent but declared in config.toml, the
    DB row is created from that block first, then the password is stored.
    """
    cfg = load_config(ctx.obj["config_path"])
    with psycopg.connect(cfg.database.dsn) as conn:
        account = _resolve_account_row(conn, cfg, name)
        conn.commit()
    if account.auth_method == "password":
        pw = password_opt or click.prompt(
            f"IMAP password for {account.email_address}",
            hide_input=True, confirmation_prompt=True,
        )
        secrets.set_password(name, pw)
        click.echo(f"stored password for {name} in keyring")
    elif account.auth_method == "oauth2":
        raise click.ClickException(
            f"account {name!r} uses oauth2; run `localmail oauth-login {name}` instead"
        )
    else:
        raise click.ClickException(
            f"account {name!r} is an archive account; it has no IMAP secret"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_accounts_db.py -k add_account -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/cli.py tests/test_cli_accounts_db.py
git commit -m "feat(cli): add-account writes to the DB, seeding from TOML when absent"
```

---

### Task 6: Rewire `oauth-login` to the DB

**Files:**
- Modify: `src/localmail/cli.py` (the `oauth_login` command)
- Test: `tests/test_cli_accounts_db.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_accounts_db.py`:

```python
def _gmail_config(tmp_path: Path, dsn: str, body: str = "") -> Path:
    secrets_json = tmp_path / "client_secret.json"
    secrets_json.write_text("{}")
    return _write_config(
        tmp_path, dsn,
        f'[gmail_oauth]\nclient_secrets_file = "{secrets_json}"\n\n{body}',
    )


def test_oauth_login_stores_refresh_token(
    clean_accounts: str, tmp_path: Path, monkeypatch
) -> None:
    dsn = clean_accounts
    _make_db_account(dsn, "gmail", auth="oauth2", oauth_provider="gmail")
    cfg = _gmail_config(tmp_path, dsn)

    class _Creds:
        refresh_token = "refresh-123"

    monkeypatch.setattr("localmail.cli.run_consent_flow", lambda _f: _Creds())
    result = _run(["oauth-login", "gmail"], cfg)
    assert result.exit_code == 0, result.output
    from localmail import secrets as s
    assert s.get_refresh_token("gmail") == "refresh-123"


def test_oauth_login_rejects_password_row(
    clean_accounts: str, tmp_path: Path
) -> None:
    dsn = clean_accounts
    _make_db_account(dsn, "work")  # password account
    cfg = _gmail_config(tmp_path, dsn)
    result = _run(["oauth-login", "work"], cfg)
    assert result.exit_code != 0
    assert "oauth" in result.output.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_accounts_db.py -k oauth_login -v`
Expected: FAIL — current `oauth_login` reads `_account_or_die` (TOML).

- [ ] **Step 3: Write minimal implementation**

Replace the `oauth_login` command body in `cli.py`:

```python
@main.command("oauth-login")
@click.argument("name")
@click.pass_context
def oauth_login(ctx: click.Context, name: str) -> None:
    """Run the Gmail OAuth2 consent flow and store the refresh token.

    Resolves NAME against the DB (seeding from config.toml if absent). The
    account must be auth_method='oauth2' with oauth_provider='gmail', and
    [gmail_oauth] client_secrets_file must be set.
    """
    cfg = load_config(ctx.obj["config_path"])
    with psycopg.connect(cfg.database.dsn) as conn:
        account = _resolve_account_row(conn, cfg, name)
        conn.commit()
    if account.auth_method != "oauth2":
        raise click.ClickException(
            f"account {name!r} uses auth_method={account.auth_method!r}; "
            f"oauth-login only applies to OAuth2 accounts"
        )
    if account.oauth_provider != "gmail":
        raise click.ClickException(
            f"unsupported oauth_provider: {account.oauth_provider!r}"
        )
    if cfg.gmail_oauth is None:
        raise click.ClickException(
            "config.toml is missing [gmail_oauth] client_secrets_file"
        )
    click.echo("opening browser for Google consent ...")
    creds = run_consent_flow(cfg.gmail_oauth.client_secrets_file)
    secrets.set_refresh_token(name, creds.refresh_token)
    click.echo(f"stored OAuth refresh token for {name} in keyring")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_accounts_db.py -k oauth_login -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/cli.py tests/test_cli_accounts_db.py
git commit -m "feat(cli): oauth-login resolves the account from the DB"
```

---

### Task 7: Rewire `remove-account` (secrets-only default + `--delete-row`)

**Files:**
- Modify: `src/localmail/cli.py` (the `remove_account` command)
- Test: `tests/test_cli_accounts_db.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_accounts_db.py`:

```python
def test_remove_account_default_clears_secrets_keeps_row(
    clean_accounts: str, tmp_path: Path
) -> None:
    dsn = clean_accounts
    _make_db_account(dsn, "work")
    from localmail import secrets as s
    s.set_password("work", "pw")
    cfg = _write_config(tmp_path, dsn)
    result = _run(["remove-account", "work"], cfg)
    assert result.exit_code == 0, result.output
    assert s.get_password("work") is None          # secret cleared
    with psycopg.connect(dsn) as conn:
        assert get_account_by_name(conn, "work") is not None  # row survives


def test_remove_account_delete_row_removes_row(
    clean_accounts: str, tmp_path: Path
) -> None:
    dsn = clean_accounts
    _make_db_account(dsn, "work")
    cfg = _write_config(tmp_path, dsn)
    result = _run(["remove-account", "work", "--delete-row"], cfg)
    assert result.exit_code == 0, result.output
    with psycopg.connect(dsn) as conn:
        assert get_account_by_name(conn, "work") is None


def test_remove_account_delete_row_refuses_when_messages_without_force(
    clean_accounts: str, tmp_path: Path
) -> None:
    dsn = clean_accounts
    acct_id = _make_db_account(dsn, "work")
    # give the account a message + the mailbox it needs (FK).
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mailboxes (account_id, name, uidvalidity, uidnext) "
            "VALUES (%s, 'INBOX', 1, 1) RETURNING id", (acct_id,))
        row = cur.fetchone()
        assert row is not None
        mbox_id = row[0]
        cur.execute(
            "INSERT INTO messages (account_id, mailbox_id, uid, raw_bytes, "
            "  size_bytes, headers, attachments, raw_sha256, date_received) "
            "VALUES (%s, %s, 1, %s, 3, '{}'::jsonb, '[]'::jsonb, %s, now())",
            (acct_id, mbox_id, b"abc", "deadbeef" * 8))
        conn.commit()
    cfg = _write_config(tmp_path, dsn)
    result = _run(["remove-account", "work", "--delete-row"], cfg)
    assert result.exit_code != 0
    assert "force" in result.output.lower()
    # --force succeeds
    ok = _run(["remove-account", "work", "--delete-row", "--force"], cfg)
    assert ok.exit_code == 0, ok.output
    with psycopg.connect(dsn) as conn:
        assert get_account_by_name(conn, "work") is None
```

NOTE: the `messages` INSERT columns above are the NOT NULL set per CLAUDE.md
(`raw_bytes, size_bytes, headers, attachments`) plus `date_received` and the
`raw_sha256` fallback key. If the live schema rejects this insert, adjust the
column list to the actual NOT NULL columns — do not weaken the test's intent
(an account with a referencing message must need `--force`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_accounts_db.py -k remove_account -v`
Expected: the `--delete-row` tests FAIL (current command has no such flag and
never touches the DB).

- [ ] **Step 3: Write minimal implementation**

Replace the `remove_account` command body in `cli.py`:

```python
@main.command("remove-account")
@click.argument("name")
@click.option("--delete-row", is_flag=True, default=False,
              help="Also delete the account row from the DB (not just secrets).")
@click.option("--force", is_flag=True, default=False,
              help="With --delete-row: cascade-delete even if messages exist.")
@click.pass_context
def remove_account(ctx: click.Context, name: str,
                   delete_row: bool, force: bool) -> None:
    """Clear stored secrets for an account. With --delete-row, also remove
    the DB row (refusing if messages reference it unless --force)."""
    if force and not delete_row:
        raise click.ClickException("--force only applies with --delete-row")
    cfg = load_config(ctx.obj["config_path"])
    if not delete_row:
        secrets.delete_password(name)
        secrets.delete_refresh_token(name)
        click.echo(f"cleared secrets for {name}")
        return
    from .api.admin.accounts import (
        AccountInUse, delete_account, get_account_by_name,
    )
    with psycopg.connect(cfg.database.dsn) as conn:
        account = get_account_by_name(conn, name)
        if account is None:
            secrets.delete_password(name)
            secrets.delete_refresh_token(name)
            click.echo(f"no DB row for {name}; cleared keyring only")
            return
        try:
            delete_account(conn, account.id, force=force)
        except AccountInUse as exc:
            raise click.ClickException(
                f"{exc}; pass --force to delete it and its messages"
            ) from exc
        conn.commit()
    secrets.delete_password(name)
    secrets.delete_refresh_token(name)
    click.echo(f"deleted account {name} and cleared its secrets")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_accounts_db.py -k remove_account -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/cli.py tests/test_cli_accounts_db.py
git commit -m "feat(cli): remove-account gains --delete-row (secrets-only by default)"
```

---

### Task 8: `sync_account` takes `account_id`; rewire `sync`; delete `upsert_account`

This is the coupled task: changing `sync_account`'s signature breaks its only
non-test caller (the CLI `sync` command), so both change together, and
`upsert_account` is deleted in the same commit. Tests that referenced
`upsert_account` are updated here too.

**Files:**
- Modify: `src/localmail/sync.py` (`sync_account` signature; delete `upsert_account`)
- Modify: `src/localmail/cli.py` (the `sync_cmd` command)
- Modify: `tests/test_sync.py` (drop `upsert_account` tests; pass `account_id`)
- Modify: `tests/test_daemon.py` (fixture seeding → `create_account`; drop the no-upsert test)
- Test: `tests/test_cli_accounts_db.py`

- [ ] **Step 1: Write the failing test (CLI sync over the DB)**

Append to `tests/test_cli_accounts_db.py`. The sync path needs a fake IMAP;
reuse the project's `FakeIMAPClient` by monkeypatching `open_connection` to
yield it (mirror how `tests/test_sync.py` / `tests/test_daemon.py` build a
fake — match their construction exactly):

```python
def test_sync_iterates_syncable_db_accounts(
    clean_accounts: str, tmp_path: Path, monkeypatch
) -> None:
    from contextlib import contextmanager
    from tests._fake_imap import FakeIMAPClient  # match the existing import path

    dsn = clean_accounts
    _make_db_account(dsn, "work")
    _make_db_account(dsn, "paused", sync_enabled=False)

    seen: list[str] = []

    @contextmanager
    def _fake_open(account, **kw):
        seen.append(account.name)
        yield FakeIMAPClient(folders=["INBOX"], messages={})

    monkeypatch.setattr("localmail.cli.open_connection", _fake_open)
    cfg = _write_config(tmp_path, dsn)
    result = _run(["sync"], cfg)
    assert result.exit_code == 0, result.output
    assert seen == ["work"]          # paused account skipped


def test_sync_account_override_syncs_paused_account(
    clean_accounts: str, tmp_path: Path, monkeypatch
) -> None:
    from contextlib import contextmanager
    from tests._fake_imap import FakeIMAPClient

    dsn = clean_accounts
    _make_db_account(dsn, "paused", sync_enabled=False)
    seen: list[str] = []

    @contextmanager
    def _fake_open(account, **kw):
        seen.append(account.name)
        yield FakeIMAPClient(folders=["INBOX"], messages={})

    monkeypatch.setattr("localmail.cli.open_connection", _fake_open)
    cfg = _write_config(tmp_path, dsn)
    result = _run(["sync", "--account", "paused"], cfg)
    assert result.exit_code == 0, result.output
    assert seen == ["paused"]


def test_sync_rejects_archive_account(
    clean_accounts: str, tmp_path: Path
) -> None:
    dsn = clean_accounts
    with psycopg.connect(dsn) as conn:
        from localmail.api.admin.accounts import create_account
        create_account(
            conn, name="legacy", email_address="l@example.com",
            auth_method="archive", imap_host=None, imap_port=None,
            oauth_provider=None, folder_allow=None, folder_deny=None,
            folder_deny_flags=None,
        )
        conn.commit()
    cfg = _write_config(tmp_path, dsn)
    result = _run(["sync", "--account", "legacy"], cfg)
    assert result.exit_code != 0
    assert "archive" in result.output.lower()


def test_sync_no_accounts_errors(clean_accounts: str, tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, clean_accounts)
    result = _run(["sync"], cfg)
    assert result.exit_code != 0
    assert "no syncable accounts" in result.output.lower()
```

IMPORTANT: confirm `FakeIMAPClient`'s real constructor signature in
`tests/_fake_imap.py` and how `tests/test_sync.py` instantiates it; copy that
exact construction rather than the placeholder `folders=/messages=` above.

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_accounts_db.py -k sync -v`
Expected: FAIL — `sync_cmd` still reads `cfg.accounts` (TOML, empty).

- [ ] **Step 3a: Change `sync_account` signature in `sync.py`**

In `src/localmail/sync.py`, replace the `sync_account` head and drop the
`upsert_account` call:

```python
def sync_account(
    conn: psycopg.Connection,
    imap: ImapLike,
    *,
    account: AccountConfig,
    account_id: int,
    attachments_root: Path,
    max_messages: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Sync every mailbox of an account. Returns {mailbox_name: inserted}.

    The caller resolves `account_id` from the DB (the DB is canonical for
    accounts — Sub-plan 2A.2d). This function never creates the account row.
    """
    folders = imap.list_folders()
```

(i.e. delete the `account_id = upsert_account(conn, account)` and the
`conn.commit()` line that followed it; keep everything from `folders =
imap.list_folders()` onward unchanged.)

- [ ] **Step 3b: Delete `upsert_account` from `sync.py`**

Remove the entire `def upsert_account(...)` function (the `INSERT ... ON
CONFLICT (name) DO UPDATE ... RETURNING id` block).

- [ ] **Step 3c: Rewire the CLI `sync` command in `cli.py`**

```python
@main.command("sync")
@click.option("--account", "account_name", default=None,
              help="Sync only this account (default: all syncable DB accounts).")
@click.option("--no-ssl", is_flag=True, default=False,
              help="Disable TLS — for testing against a local IMAP server only.")
@click.option("--limit-per-folder", "limit_per_folder", type=int, default=None,
              help="Fetch at most N new UIDs per folder in this run. "
                   "The next run resumes from the checkpoint.")
@click.pass_context
def sync_cmd(ctx: click.Context, account_name: str | None,
             no_ssl: bool, limit_per_folder: int | None) -> None:
    """One-shot incremental sync over the DB accounts. For cron + manual testing."""
    from .api.admin.accounts import get_account_by_name, list_syncable_accounts
    from .daemon_accounts import account_config_from_row

    cfg = load_config(ctx.obj["config_path"])
    gmail_secrets = cfg.gmail_oauth.client_secrets_file if cfg.gmail_oauth else None
    with psycopg.connect(cfg.database.dsn, autocommit=False) as conn:
        if account_name:
            row = get_account_by_name(conn, account_name)
            if row is None:
                raise click.ClickException(f"no such account: {account_name!r}")
            if row.auth_method == "archive":
                raise click.ClickException(
                    f"account {account_name!r} is an archive account; not synced"
                )
            rows = [row]
        else:
            rows = list_syncable_accounts(conn)
        if not rows:
            raise click.ClickException("no syncable accounts")

        for row in rows:
            account = account_config_from_row(row)
            click.echo(f"--- syncing {account.name} ---")
            with open_connection(
                account, ssl=not no_ssl, gmail_client_secrets=gmail_secrets
            ) as imap:
                results = sync_account(
                    conn, imap, account=account, account_id=row.id,
                    attachments_root=cfg.attachments.root,
                    max_messages=limit_per_folder, progress=click.echo,
                )
            for folder, n in results.items():
                click.echo(f"  {folder}: +{n} new")
```

- [ ] **Step 3d: Update `tests/test_sync.py`**

- Delete `test_upsert_account_does_not_overwrite_canonical_columns` and
  `test_upsert_account_inserts_brand_new_name`.
- Remove `upsert_account` from the `from localmail.sync import ...` line.
- For every remaining `sync_account(...)` call, create the account row first
  (via `create_account` or the test's existing helper) and pass its id:

```python
# before: account_id implicitly created by sync_account via upsert_account
# after:
from localmail.api.admin.accounts import create_account
acct = create_account(
    conn, name=account.name, email_address=account.email,
    auth_method=account.auth_method, imap_host=account.imap_host,
    imap_port=account.imap_port, oauth_provider=account.oauth_provider,
    folder_allow=account.folder_allow or None,
    folder_deny=account.folder_deny or None,
    folder_deny_flags=account.folder_deny_flags or None,
)
results = sync_account(conn, imap, account=account, account_id=acct.id,
                       attachments_root=root)
```

Match the existing test's variable names (`account`, `conn`, `imap`, `root`).

- [ ] **Step 3e: Update `tests/test_daemon.py`**

- Replace the four fixture seeds `account_id = upsert_account(conn, account)`
  (lines ~45, 88, 108, 133) with `create_account`:

```python
from localmail.api.admin.accounts import create_account
acct = create_account(
    conn, name=account.name, email_address=account.email,
    auth_method=account.auth_method, imap_host=account.imap_host,
    imap_port=account.imap_port, oauth_provider=account.oauth_provider,
    folder_allow=None, folder_deny=None, folder_deny_flags=None,
)
account_id = acct.id
```

- Remove the `upsert_account` import from the `from localmail.sync import ...`
  line.
- Delete `test_one_poll_pass_does_not_call_upsert_account` entirely (the symbol
  no longer exists; the daemon's no-upsert behavior is locked by 2A.2b's
  `ctx.account_id` design and the remaining daemon tests).

- [ ] **Step 4: Run the affected suites to verify green**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_accounts_db.py tests/test_sync.py tests/test_daemon.py -v`
Expected: all pass; no `upsert_account` references remain.
Then confirm it is truly gone:
Run: `grep -rn "upsert_account" src/ tests/`
Expected: no matches.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/sync.py src/localmail/cli.py tests/test_sync.py tests/test_daemon.py tests/test_cli_accounts_db.py
git commit -m "feat(cli): one-shot sync reads DB accounts; delete sync.upsert_account"
```

---

### Task 9: Full gate + docs

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `src/localmail/config.py` (comment)

- [ ] **Step 1: Full test suite + mypy**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/`
Expected: all pass (≈ prior count + the new tests, minus the 3 deleted
upsert/no-upsert tests).
Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: clean.

- [ ] **Step 2: Update README**

Add a "Managing accounts" subsection documenting that account commands are
DB-backed and that `init-db` seeds from `config.toml` (DB canonical
thereafter), plus the operator command surface. Concretely add:

```markdown
### Managing accounts

`config.toml` `[[accounts]]` blocks are a **seed**: `localmail init-db` merges
them into the `accounts` table, after which the DB is authoritative. The
account commands read and write the DB:

- `localmail list-accounts` — list DB accounts + whether a secret is stored.
- `localmail add-account NAME` — store the IMAP password (creates the DB row
  from a matching `config.toml` block if it does not exist yet).
- `localmail oauth-login NAME` — Gmail consent flow → refresh token.
- `localmail remove-account NAME` — clear stored secrets. Add `--delete-row`
  to also remove the DB account row (`--force` if it still has messages).
- `localmail sync [--account NAME]` — one-shot sync over syncable DB accounts
  (`--account` overrides a paused `sync_enabled=false` account).

Operator/admin commands: `localmail grant-admin USERNAME` /
`localmail revoke-admin USERNAME` (toggle `api_users.is_admin`),
`localmail revoke-admin-sessions USERNAME` (invalidate admin cookie sessions),
and the API-user commands (`add-api-user`, `list-api-users`, `remove-api-user`,
`grant-account`, `revoke-account`).
```

(Place it near the existing account/config documentation; match README's
heading depth.)

- [ ] **Step 3: Update CLAUDE.md**

In the Sub-plan 2A.2 status area, record that 2A.2d shipped: CLI account
commands (`list-accounts`, `add-account`, `oauth-login`, `remove-account`) and
the one-shot `localmail sync` now read/write the `accounts` table via
`api.admin.accounts`; `sync.upsert_account` has been **deleted** (no callers
remain); `sync_account` now takes an explicit `account_id`. Note
`remove-account` is secrets-only by default with `--delete-row [--force]` to
remove the row, and that `backfill-internal-date` remains TOML-driven (out of
scope). Update the `migrations/` line only if needed (no new migration).

- [ ] **Step 4: Update `config.py` comment**

Where `AccountConfig` is defined, extend the existing "no longer consumed by
the daemon" note to add: the `[[accounts]]` blocks are read only as the
`init-db` seed and the `add-account`/`oauth-login` seed-from-TOML bridge — no
account command reads TOML at runtime once the DB row exists.

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md src/localmail/config.py
git commit -m "docs: CLI account commands are DB-canonical (Sub-plan 2A.2d)"
```

---

## Self-review notes

- **Spec coverage:** list-accounts (T4), add-account+seed (T5), oauth-login
  (T6), remove-account secrets-only default + `--delete-row`/`--force` (T7),
  sync syncable + `--account` override + archive reject (T8),
  `get_account_by_name` (T1), pure resolver (T3), shared kwargs (T2),
  `sync_account(account_id)` + delete `upsert_account` (T8), README/CLAUDE/
  config docs (T9). All spec sections map to a task.
- **Type consistency:** `Resolution = Found | SeedThenUse | NotFound` used
  identically in T3 (def) and T5 (`_resolve_account_row`). `account_create_kwargs`
  defined T2, used T2 + T5. `sync_account(..., account_id=...)` defined T8,
  used in CLI + tests T8. `get_account_by_name` defined T1, used T5/T7/T8.
- **Verification gaps flagged for the implementer:** the exact `FakeIMAPClient`
  constructor (T8) and the `messages` NOT NULL column list (T7) must be copied
  from the live code/tests, not from the illustrative placeholders.
```
