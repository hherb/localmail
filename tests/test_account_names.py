# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unit tests for the pure account-name rules (#217)."""

import pytest

from localmail.account_names import (
    KEYRING_SUBKEY_SEPARATOR,
    NAME_MAX_CHARS,
    account_name_error,
)


@pytest.mark.parametrize("name", ["gmail", "horst-gmail", "work_2024", "a.b c"])
def test_ordinary_names_are_accepted(name):
    assert account_name_error(name) is None


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_blank_names_are_rejected(name):
    msg = account_name_error(name)
    assert msg is not None and "blank" in msg


def test_name_at_the_cap_is_accepted():
    assert account_name_error("x" * NAME_MAX_CHARS) is None


def test_name_past_the_cap_is_rejected():
    msg = account_name_error("x" * (NAME_MAX_CHARS + 1))
    assert msg is not None and str(NAME_MAX_CHARS) in msg


def test_colon_is_rejected_because_it_addresses_the_refresh_token_slot():
    """The keyring username is `<name>` for the IMAP password and
    `<name>:refresh` for the OAuth refresh token, so a password account named
    `gmail:refresh` writes over the `gmail` account's refresh token."""
    msg = account_name_error("gmail" + KEYRING_SUBKEY_SEPARATOR + "refresh")
    assert msg is not None and KEYRING_SUBKEY_SEPARATOR in msg


def test_colon_anywhere_is_rejected_not_just_the_refresh_suffix():
    assert account_name_error("a:b") is not None
    assert account_name_error(":lead") is not None
    assert account_name_error("trail:") is not None


def test_separator_matches_the_secrets_module_scheme():
    """Pin the constant to the scheme it protects: if secrets.py ever changes
    how it derives the refresh-token username, this rule must move with it."""
    from localmail import secrets

    assert secrets._refresh_user("acct") == f"acct{KEYRING_SUBKEY_SEPARATOR}refresh"
