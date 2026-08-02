# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Secret storage for IMAP passwords and OAuth refresh tokens.

Keyed by:
  service  = "localmail"
  username = account.name              (IMAP password)
  username = f"{account.name}:refresh" (OAuth2 refresh token)

Two interchangeable backends sit behind these seven functions, selected by
`[secrets] backend` and installed by `configure()`:

- **keyring** (default) — the OS keychain. Right for a desktop session; the
  macOS deployment uses it and is unaffected by any of this.
- **file** — a 0600 JSON file. The only option that works headless: a lingering
  systemd *user* service starts with no PAM session, so the gnome-keyring
  `login` collection is locked at boot and nothing can unlock it.

**`config.load_config()` calls `configure()`.** That coupling is deliberate:
`load_config` is the only place that knows the resolved config — including a
`--config PATH` override — and every process that can touch a secret loads
config first. Threading a store object through `open_connection` → `sync` →
`idle`/`poller` → `Daemon` and the whole admin service layer was the
alternative, and it buys nothing.

Design: docs/superpowers/specs/2026-08-02-headless-secrets-design.md
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import keyring

from localmail.secrets_file import FileSecretStore
from localmail.secrets_store import refresh_username

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from localmail.config import SecretsConfig

SERVICE = "localmail"


class SecretStore(Protocol):
    """The whole contract a backend has to satisfy."""

    def get(self, username: str) -> str | None: ...
    def set(self, username: str, value: str) -> None: ...
    def delete(self, username: str) -> None: ...


class KeyringSecretStore:
    """The OS keychain, via the `keyring` package's active backend."""

    def get(self, username: str) -> str | None:
        return keyring.get_password(SERVICE, username)

    def set(self, username: str, value: str) -> None:
        keyring.set_password(SERVICE, username, value)

    def delete(self, username: str) -> None:
        try:
            keyring.delete_password(SERVICE, username)
        except keyring.errors.PasswordDeleteError:
            pass


_store: SecretStore = KeyringSecretStore()


def configure(cfg: SecretsConfig) -> None:
    """Install the backend named by `cfg` for the rest of this process."""
    global _store
    _store = (
        FileSecretStore(cfg.file_path)
        if cfg.backend == "file"
        else KeyringSecretStore()
    )


def reset_to_default() -> None:
    """Restore the keyring backend. Used by the test suite so a config-loading
    test cannot leak its backend into the next one."""
    global _store
    _store = KeyringSecretStore()


def active_backend_name() -> str:
    """Which backend is installed — for operator-facing output only."""
    return "file" if isinstance(_store, FileSecretStore) else "keyring"


def set_password(account_name: str, password: str) -> None:
    _store.set(account_name, password)


def get_password(account_name: str) -> str | None:
    return _store.get(account_name)


def delete_password(account_name: str) -> None:
    _store.delete(account_name)


def set_refresh_token(account_name: str, token: str) -> None:
    _store.set(refresh_username(account_name), token)


def get_refresh_token(account_name: str) -> str | None:
    return _store.get(refresh_username(account_name))


def delete_refresh_token(account_name: str) -> None:
    _store.delete(refresh_username(account_name))
