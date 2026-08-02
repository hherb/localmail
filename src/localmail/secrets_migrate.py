# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Pure planner for the one-shot keyring → file secret migration.

Reading and writing belong to the caller (`cli.migrate_secrets`); this module
only decides *what* to copy, so the partitioning is testable without a keyring,
a filesystem, or a database.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from localmail.secrets_store import refresh_username

SecretKind = Literal["password", "refresh"]


@dataclass(frozen=True)
class MigrationItem:
    account_name: str
    username: str
    kind: SecretKind
    value: str | None


@dataclass(frozen=True)
class MigrationPlan:
    #: Entries the source holds; these get written to the target.
    to_copy: list[MigrationItem]
    #: Entries the source does not hold. Not an error — a password account has
    #: no refresh token and an OAuth account has no password — but reporting
    #: them is what makes "copied N" a number the operator can check.
    absent: list[MigrationItem]


def plan_secret_migration(
    account_names: Sequence[str], source: Mapping[str, str | None]
) -> MigrationPlan:
    """Partition every account's two store keys by whether `source` holds them.

    `source` maps store username → value, with a missing key and a `None` value
    treated alike: `keyring.get_password` returns `None` for an absent entry,
    and copying that through as a literal would write the string "None" into
    the target.
    """
    to_copy: list[MigrationItem] = []
    absent: list[MigrationItem] = []
    for name in account_names:
        for kind, username in (
            ("password", name),
            ("refresh", refresh_username(name)),
        ):
            value = source.get(username)
            item = MigrationItem(
                account_name=name,
                username=username,
                kind=kind,  # type: ignore[arg-type]
                value=value,
            )
            (to_copy if value is not None else absent).append(item)
    return MigrationPlan(to_copy=to_copy, absent=absent)
