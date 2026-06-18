# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""OS-keyring-backed secret storage for IMAP passwords and OAuth refresh tokens.

Keyed by:
  service  = "localmail"
  username = account.name              (IMAP password)
  username = f"{account.name}:refresh" (OAuth2 refresh token)
"""

from __future__ import annotations

import keyring

SERVICE = "localmail"


def _refresh_user(account_name: str) -> str:
    return f"{account_name}:refresh"


def set_password(account_name: str, password: str) -> None:
    keyring.set_password(SERVICE, account_name, password)


def get_password(account_name: str) -> str | None:
    return keyring.get_password(SERVICE, account_name)


def delete_password(account_name: str) -> None:
    try:
        keyring.delete_password(SERVICE, account_name)
    except keyring.errors.PasswordDeleteError:
        pass


def set_refresh_token(account_name: str, token: str) -> None:
    keyring.set_password(SERVICE, _refresh_user(account_name), token)


def get_refresh_token(account_name: str) -> str | None:
    return keyring.get_password(SERVICE, _refresh_user(account_name))


def delete_refresh_token(account_name: str) -> None:
    try:
        keyring.delete_password(SERVICE, _refresh_user(account_name))
    except keyring.errors.PasswordDeleteError:
        pass
