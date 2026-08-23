# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The one SQL fragment deciding whether a principal may present a password.

Pure — string composition only, no IO, no psycopg. A sibling of
``revocation_sql.credential_valid_sql``, and for the same reason: four separate
lookups verify a password against ``api_users`` — ``api.auth.login``,
``api.admin.auth.authenticate_admin``, ``serve.oauth.consent_router``'s inline
consent check, and ``api.auth.change_password`` — and until this module they
carried the ``disabled_at IS NULL`` wording by copy. #241 was exactly a rule
applied to one site and not its sibling.

A **service user** is the principal behind an API key. It holds an argon2 hash
of random bytes nobody retains, so no password can match it today — but that is
an accident of how it was created, not a rule, and ``users.set_password`` is one
admin click away from making it usable. This fragment is the rule.
"""
from __future__ import annotations


def login_eligible_sql(*, user: str) -> str:
    """Return a parameter-free SQL boolean over one ``api_users`` alias.

    ``user`` may be a table alias or the bare table name, for the two call sites
    that do not alias it.

    Wrapped in its own parentheses so it survives being spliced after an ``OR``,
    where an unwrapped ``A AND B`` would regroup and silently widen what the
    caller admits.
    """
    return f"({user}.disabled_at IS NULL AND {user}.is_service IS FALSE)"
