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
