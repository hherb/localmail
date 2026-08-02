# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""File-backed secret store for headless deployments.

The OS keyring cannot serve a process that starts before any interactive
session exists: on Linux the gnome-keyring `login` collection is unlocked by
PAM at login and by nothing else, so a lingering systemd *user* service reads a
locked collection at every boot. This backend removes that dependency —
the daemon reads a plain file owned by its own user.

File permissions are the *only* protection here, by design (see
docs/superpowers/specs/2026-08-02-headless-secrets-design.md), which is why the
mode is enforced on read rather than merely set on write.
"""
from __future__ import annotations

import os
from pathlib import Path

from localmail.secrets_store import (
    SECRETS_DIR_MODE,
    SECRETS_FILE_MODE,
    deserialise,
    mode_is_private,
    serialise,
)


class InsecureSecretsFile(RuntimeError):
    """The secrets file is readable by somebody other than its owner."""


class StaleSecretsTempFile(RuntimeError):
    """A temp file from an interrupted write is blocking the next one."""


class FileSecretStore:
    """Read/write secrets in a 0600 JSON file. Not thread-safe against a
    concurrent writer in another process — writes are last-one-wins, which is
    fine because they only ever come from an operator-driven CLI or admin
    action, never from the daemon's hot path.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self, username: str) -> str | None:
        return self._read().get(username)

    def set(self, username: str, value: str) -> None:
        secrets = self._read()
        secrets[username] = value
        self._write(secrets)

    def delete(self, username: str) -> None:
        secrets = self._read()
        if secrets.pop(username, None) is None:
            # Mirrors the keyring backend swallowing PasswordDeleteError, and
            # avoids rewriting the file (and its mtime) for a no-op.
            return
        self._write(secrets)

    def _read(self) -> dict[str, str]:
        try:
            mode = os.stat(self.path).st_mode
        except FileNotFoundError:
            # No file yet is not an error: it is a fresh install with no
            # secrets stored, exactly like an empty keyring.
            return {}
        if not mode_is_private(mode):
            raise InsecureSecretsFile(
                f"{self.path} is readable by group or other; refusing to use it. "
                f"Fix with: chmod 600 {self.path}"
            )
        return deserialise(self.path.read_text(encoding="utf-8"))

    def _write(self, secrets: dict[str, str]) -> None:
        # `mode` applies only when we create the directory. An existing one is
        # left alone on purpose: the default path sits in the operator's config
        # directory alongside config.toml, and silently tightening that is both
        # surprising and unnecessary — the 0600 file is the protection, and a
        # 0755 directory grants no read of its contents and no rename.
        self.path.parent.mkdir(mode=SECRETS_DIR_MODE, parents=True, exist_ok=True)
        # Write to a temp in the *same* directory and rename over the target:
        # a reader never sees a half-written file, and the secret is never
        # briefly present at a wider mode. O_EXCL so an existing stray temp is
        # an error rather than something we silently write a secret into.
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, SECRETS_FILE_MODE)
        except FileExistsError as exc:
            # The handler below clears the temp on every failure it can see, so
            # reaching here means a write died where no handler runs — SIGKILL,
            # OOM, power loss. Every later write then fails too, and a bare
            # FileExistsError naming a dotfile the operator has never heard of
            # is not a recoverable message. It may itself hold a secret, so say
            # what it is rather than quietly clobbering it.
            raise StaleSecretsTempFile(
                f"{tmp} is left over from an interrupted write and is blocking "
                f"this one. It may contain secrets; inspect it if {self.path} "
                f"looks wrong, then remove it with: rm {tmp}"
            ) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(serialise(secrets))
                # Durability, not just atomicity: os.replace orders the rename
                # against concurrent *readers*, but on an unclean shutdown an
                # unflushed rename can land pointing at a zero-length file. A
                # store whose whole purpose is surviving reboots cannot take
                # that trade — recovering costs an interactive OAuth consent
                # round trip, on a host with no browser.
                fh.flush()
                os.fsync(fh.fileno())
            # mkdir/open honour the umask, so set the mode explicitly rather
            # than trusting the process environment.
            os.chmod(tmp, SECRETS_FILE_MODE)
            os.replace(tmp, self.path)
            # fsync the directory too, or the rename itself can be lost even
            # though the file's own contents were durable.
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
