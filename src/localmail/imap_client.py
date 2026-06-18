# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Open authenticated IMAP connections (password or Gmail XOAUTH2)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from imapclient import IMAPClient

from . import secrets
from .config import AccountConfig
from .oauth_gmail import fresh_access_token


@contextmanager
def open_connection(
    account: AccountConfig,
    *,
    ssl: bool = True,
    gmail_client_secrets: Path | None = None,
) -> Iterator[IMAPClient]:
    client = IMAPClient(host=account.imap_host, port=account.imap_port, ssl=ssl)
    try:
        if account.auth_method == "password":
            password = secrets.get_password(account.name)
            if password is None:
                raise RuntimeError(
                    f"no password stored for {account.name!r}; "
                    f"run `localmail add-account {account.name}`"
                )
            client.login(account.email, password)

        elif account.auth_method == "oauth2":
            if account.oauth_provider != "gmail":
                raise NotImplementedError(
                    f"OAuth2 provider {account.oauth_provider!r} is not supported "
                    f"(only 'gmail' for now)"
                )
            if gmail_client_secrets is None:
                raise RuntimeError(
                    "account uses OAuth2 but no [gmail_oauth] client_secrets_file "
                    "was provided"
                )
            refresh_token = secrets.get_refresh_token(account.name)
            if refresh_token is None:
                raise RuntimeError(
                    f"no OAuth refresh token stored for {account.name!r}; "
                    f"run `localmail oauth-login {account.name}`"
                )
            access_token = fresh_access_token(refresh_token, gmail_client_secrets)
            client.oauth2_login(account.email, access_token)

        else:
            raise ValueError(f"unknown auth_method: {account.auth_method!r}")

        yield client
    finally:
        try:
            client.logout()
        except Exception:
            pass
