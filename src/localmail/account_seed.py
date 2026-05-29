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
