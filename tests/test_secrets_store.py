# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Pure helpers shared by both secret backends — no IO, no keyring, no files."""
from __future__ import annotations

import pytest

from localmail.secrets_store import (
    SECRETS_DIR_MODE,
    SECRETS_FILE_MODE,
    DirectoryExposure,
    SecretsFileCorrupt,
    deserialise,
    directory_exposure,
    mode_is_private,
    refresh_username,
    serialise,
)


def test_refresh_username_appends_the_separator() -> None:
    assert refresh_username("gmail") == "gmail:refresh"


def test_password_and_refresh_usernames_never_collide() -> None:
    """The #217 rule in one assertion: because the refresh key is derived by
    appending `:refresh`, an account name may not itself contain `:` — else a
    password account could be stored over another account's refresh token."""
    assert refresh_username("gmail") != "gmail"


@pytest.mark.parametrize("mode", [0o600, 0o400, 0o200, 0o000])
def test_private_modes_are_accepted(mode: int) -> None:
    assert mode_is_private(mode) is True


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o666, 0o660, 0o606])
def test_modes_readable_by_group_or_other_are_rejected(mode: int) -> None:
    assert mode_is_private(mode) is False


def test_owner_execute_bit_does_not_make_a_file_public() -> None:
    """Only the group/other bits matter — an owner-execute bit is odd for a
    secrets file but exposes it to nobody."""
    assert mode_is_private(0o700) is True


def test_file_and_dir_modes_are_owner_only() -> None:
    assert mode_is_private(SECRETS_FILE_MODE) is True
    assert mode_is_private(SECRETS_DIR_MODE) is True


@pytest.mark.parametrize("mode", [0o700, 0o500, 0o000, SECRETS_DIR_MODE])
def test_a_directory_nobody_else_can_write_is_private(mode: int) -> None:
    assert directory_exposure(mode) is DirectoryExposure.PRIVATE


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o705, 0o744])
def test_a_readable_directory_is_still_private(mode: int) -> None:
    """The rule is about *write* bits, not read bits. Being able to list a
    directory leaks the secrets file's name and nothing more — its own 0600
    covers the contents. Being able to *write* the directory allows unlink and
    rename of entries whatever their own mode, which is the #246 substitution.
    `~/.config` is routinely 0755, so this branch has to stay silent.
    """
    assert directory_exposure(mode) is DirectoryExposure.PRIVATE


@pytest.mark.parametrize("mode", [0o770, 0o775, 0o720, 0o730])
def test_a_group_writable_directory_is_reported_as_such(mode: int) -> None:
    """Ambiguous, not damning: under the umask-002 + per-user-private-group
    default of several distros a directory the user made lands at 0775, where
    the group is that user alone."""
    assert directory_exposure(mode) is DirectoryExposure.GROUP_WRITABLE


@pytest.mark.parametrize("mode", [0o707, 0o702, 0o755 | 0o002])
def test_a_world_writable_directory_is_reported_as_such(mode: int) -> None:
    assert directory_exposure(mode) is DirectoryExposure.WORLD_WRITABLE


@pytest.mark.parametrize("mode", [0o777, 0o722, 0o733])
def test_world_writable_outranks_group_writable(mode: int) -> None:
    """A mode that is both must report the verdict that refuses, not the one
    that warns."""
    assert directory_exposure(mode) is DirectoryExposure.WORLD_WRITABLE


def test_the_file_mode_predicate_ignores_directory_write_bits() -> None:
    """The two rules must not be confused: `mode_is_private` judges a file and
    refuses any group/other bit at all, while `directory_exposure` tolerates the
    read/execute bits that make a config directory usable."""
    assert mode_is_private(0o755) is False
    assert directory_exposure(0o755) is DirectoryExposure.PRIVATE


def test_serialise_deserialise_round_trip() -> None:
    original = {"gmail": "hunter2", "gmail:refresh": "1//0aBc-_"}
    assert deserialise(serialise(original)) == original


def test_serialise_round_trips_non_ascii_and_control_characters() -> None:
    """Passwords are arbitrary text; JSON must carry them verbatim."""
    original = {"acct": "pä§§\twörd\n✓"}
    assert deserialise(serialise(original)) == original


def test_deserialise_of_an_empty_store_is_an_empty_mapping() -> None:
    assert deserialise(serialise({})) == {}


@pytest.mark.parametrize(
    "text",
    [
        "",                                # truncated to nothing
        "not json at all",
        "[]",                              # valid JSON, wrong shape
        '{"version": 1}',                  # no secrets key
        '{"version": 1, "secrets": []}',   # secrets is not a mapping
        '{"version": 1, "secrets": {"a": 1}}',  # value is not a string
    ],
)
def test_corrupt_content_raises_rather_than_reading_as_empty(text: str) -> None:
    """A corrupt store must not look like "no secrets configured" — that sends
    the operator hunting for a missing `add-account` instead of a damaged file.
    """
    with pytest.raises(SecretsFileCorrupt):
        deserialise(text)
