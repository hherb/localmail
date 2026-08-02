# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The one SQL fragment that decides whether a stored credential is still
honoured: the owning user must be enabled, and the credential must not predate
the operator's last session revocation.

Pure — string composition only, no IO, no psycopg. It exists because the same
predicate has to appear in every credential lookup there is (bearer tokens,
OAuth refresh tokens, OAuth authorization codes) *and* in the statements that
consume them. #241 was exactly a place where it had been applied to the load
but not to the consume, so keeping one authority for the wording is what stops
the next such gap from being invisible.
"""
from __future__ import annotations


def credential_valid_sql(*, user: str, credential: str) -> str:
    """Return a parameter-free SQL boolean expression over two aliases.

    ``user`` names an ``api_users`` row; ``credential`` names any row carrying
    a ``created_at`` (``api_tokens``, ``oauth_refresh_tokens``,
    ``oauth_authorization_codes``, or a CTE projecting one).

    The comparison is ``>=`` rather than ``>``: ``sessions_invalidated_at`` is a
    cutoff moment, so a credential minted *at* that moment is on the surviving
    side of it. That keeps "revoke, then log in again" working — a revocation is
    not a ban.
    """
    return (
        f"{user}.disabled_at IS NULL "
        f"AND ({user}.sessions_invalidated_at IS NULL "
        f"OR {credential}.created_at >= {user}.sessions_invalidated_at)"
    )
