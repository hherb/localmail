# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Account-name rules. Pure: no IO, no DB, no keyring.

The account name is the canonical account key everywhere — the DB
``accounts.name`` unique constraint, the ``init-db`` seed's dedup key, and the
**secret-store username**. That last one is why the character rule exists:
secrets are stored under ``<name>`` (IMAP password) and ``<name>:refresh``
(OAuth refresh token), so a colon in a name lets one account address another's
slot.
Creating a password account literally named ``gmail:refresh`` would write a
plaintext IMAP password over the ``gmail`` account's OAuth refresh token, and
that account's next token refresh would fail (#217).

Shared by both validation boundaries — ``api.admin.accounts`` (admin UI, JSON
API, CLI) and ``config.Config`` (the TOML seed) — so the two cannot drift.

Both are *create* boundaries, and that is the whole surface: the name is not
editable afterwards (``accounts._UPDATABLE`` has no ``name``). Note the TOML
check lives on ``Config``, not on the ``AccountConfig`` field, because
``AccountConfig`` doubles as the DB-row adapter — see the comment on
``Config._reject_unusable_account_names``.
"""

from __future__ import annotations

#: Separates the account name from the secret kind in a secret-store username.
#: Must match ``secrets_store.refresh_username`` — the authority for *both*
#: backends, since the keyring and the file store key on identical usernames.
#: Pinned by tests/test_account_names.py.
KEYRING_SUBKEY_SEPARATOR = ":"

#: Upper bound on an account name, in characters.
NAME_MAX_CHARS = 128


def account_name_error(name: str) -> str | None:
    """Return why ``name`` is unusable as an account name, or None if it is fine.

    Returns a message rather than raising so each caller can wrap it in its own
    error type (``AccountFieldError`` in the service layer, ``ValueError`` in
    the pydantic validator) and render it beside the offending field.
    """
    if not name or not name.strip():
        return "name must not be blank"
    if len(name) > NAME_MAX_CHARS:
        return f"name longer than {NAME_MAX_CHARS} chars"
    if KEYRING_SUBKEY_SEPARATOR in name:
        return (
            f"name must not contain {KEYRING_SUBKEY_SEPARATOR!r}: it separates "
            f"the account name from the secret kind in the keyring "
            f"(<name>{KEYRING_SUBKEY_SEPARATOR}refresh), so such a name could "
            f"overwrite another account's OAuth refresh token"
        )
    return None
