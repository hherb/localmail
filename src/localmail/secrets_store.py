# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Pure helpers shared by every secret backend: the username scheme, the
on-disk JSON encoding, and the file-permission predicate.

No IO, no keyring, no filesystem — so the rules can be tested without a
platform. The username scheme lives here rather than in `secrets.py` because
**both** backends must key on byte-identical strings; that is what lets an
operator migrate between them, and what keeps the #217 colon rule meaningful
for the file store as well as the keyring.
"""
from __future__ import annotations

import enum
import json
from collections.abc import Mapping

#: Owner read/write only. The store holds IMAP passwords and OAuth refresh
#: tokens, and file permissions are the *only* protection the file backend
#: offers by design (see the design doc) — so this is load-bearing, not tidiness.
SECRETS_FILE_MODE = 0o600

#: Applied **only when `FileSecretStore` creates the directory** — an existing
#: one is left at whatever mode it has (the default path is the operator's own
#: config dir; see `secrets_file._write`). What an existing parent is held to
#: instead is `directory_exposure`, which is weaker on purpose: see #246 and the
#: enum's docstring.
SECRETS_DIR_MODE = 0o700

#: Any group or other bit means somebody besides the owner can reach it.
_GROUP_AND_OTHER_BITS = 0o077

#: Directory *write* bits. Read/execute bits are deliberately absent: they leak
#: the secrets file's name, which its own 0600 mode already survives.
_GROUP_WRITE_BIT = 0o020
_OTHER_WRITE_BIT = 0o002

#: Bumped only if the on-disk shape changes incompatibly. Readers reject an
#: unknown version rather than guess.
SECRETS_FORMAT_VERSION = 1

_REFRESH_SUFFIX = ":refresh"


class SecretsFileCorrupt(RuntimeError):
    """The secrets file exists but could not be understood."""


def refresh_username(account_name: str) -> str:
    """Return the store key holding `account_name`'s OAuth refresh token.

    The password key is the bare account name, so the two spaces share a
    namespace separated only by this suffix — which is exactly why an account
    name may not contain `:` (#217, enforced by `account_names.py` at every
    create boundary).
    """
    return f"{account_name}{_REFRESH_SUFFIX}"


def mode_is_private(mode: int) -> bool:
    """True iff `mode`'s permission bits grant nothing to group or other.

    Judges a *file*, never its directory: a writable parent can substitute the
    file by rename whatever this returns, which is #246, not something a
    file-mode predicate can answer.
    """
    return not mode & _GROUP_AND_OTHER_BITS


class DirectoryExposure(enum.Enum):
    """How far the directory holding the secrets file lets somebody else reach.

    Write access to a directory permits `unlink` and `rename` of the entries in
    it **regardless of their own modes**, so a writable parent can swap a 0600
    secrets file for one the attacker wrote — the read-side `mode_is_private`
    check passes, and the daemon authenticates with the substituted credentials.
    That is what this grades (#246).

    Two verdicts rather than one boolean because the two cases warrant different
    answers. `WORLD_WRITABLE` is never legitimate on a config directory, so it
    is refused. `GROUP_WRITABLE` is genuinely ambiguous: under the umask-002 +
    per-user-private-group default of the Debian/Ubuntu and RHEL families, a
    directory the user created lands at 0775 with the *user's own* group — safe
    in practice, and refusing it would wedge a stock install over a distro
    default. So it warns.
    """

    PRIVATE = "private"
    GROUP_WRITABLE = "group-writable"
    WORLD_WRITABLE = "world-writable"


def directory_exposure(mode: int) -> DirectoryExposure:
    """Grade a *directory's* mode. The sibling of `mode_is_private`, kept
    separate so the file rule and the directory rule cannot be confused for one
    another — they differ in both which bits they read and what they cost.

    A mode that is writable by group *and* other reports `WORLD_WRITABLE`: the
    caller must see the verdict that refuses, not the one that merely warns.
    """
    if mode & _OTHER_WRITE_BIT:
        return DirectoryExposure.WORLD_WRITABLE
    if mode & _GROUP_WRITE_BIT:
        return DirectoryExposure.GROUP_WRITABLE
    return DirectoryExposure.PRIVATE


def serialise(secrets: Mapping[str, str]) -> str:
    """Encode the store. `ensure_ascii=False` keeps non-ASCII passwords legible
    to an operator reading the file; JSON escaping still round-trips them."""
    return json.dumps(
        {"version": SECRETS_FORMAT_VERSION, "secrets": dict(secrets)},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def deserialise(text: str) -> dict[str, str]:
    """Decode the store, or raise `SecretsFileCorrupt`.

    Every malformed shape raises rather than degrading to an empty mapping: an
    empty store means "no secret configured", which sends the operator to
    `add-account` instead of to the damaged file that is the actual problem.
    """
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SecretsFileCorrupt(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SecretsFileCorrupt(f"expected a JSON object, got {type(data).__name__}")
    version = data.get("version")
    if version != SECRETS_FORMAT_VERSION:
        raise SecretsFileCorrupt(
            f"unsupported format version {version!r} "
            f"(this build reads version {SECRETS_FORMAT_VERSION})"
        )
    secrets = data.get("secrets")
    if not isinstance(secrets, dict):
        raise SecretsFileCorrupt("the 'secrets' key must hold a JSON object")
    for key, value in secrets.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise SecretsFileCorrupt(f"entry {key!r} is not a string-to-string pair")
    return dict(secrets)
