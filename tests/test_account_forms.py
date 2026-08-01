# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unit tests for the pure account-form helpers (no IO)."""
from __future__ import annotations

import pytest

from localmail.account_names import account_name_error
from localmail.api.admin.accounts import AccountFieldError
from localmail.serve.admin import account_forms as af


def test_deny_flags_constant_is_rfc6154_set():
    assert af.DENY_FLAGS == (
        r"\Trash", r"\Junk", r"\All", r"\Drafts",
        r"\Sent", r"\Important", r"\Flagged",
    )


@pytest.mark.parametrize("raw,expected", [
    ("", None),
    ("   \n  \n", None),
    ("INBOX", ["INBOX"]),
    ("INBOX\nLists/dev", ["INBOX", "Lists/dev"]),
    ("INBOX\r\nLists/dev\r\n", ["INBOX", "Lists/dev"]),
    ("  INBOX  \n\n  Spam ", ["INBOX", "Spam"]),
])
def test_parse_lines(raw, expected):
    assert af.parse_lines(raw) == expected


def test_parse_deny_flags_keeps_only_known():
    assert af.parse_deny_flags([r"\Trash", r"\Junk"]) == [r"\Trash", r"\Junk"]


def test_parse_deny_flags_empty_is_none():
    assert af.parse_deny_flags([]) is None


def test_parse_deny_flags_rejects_unknown():
    with pytest.raises(af.FormError):
        af.parse_deny_flags([r"\Trash", r"\Bogus"])


def test_form_to_create_kwargs_password():
    form = {
        "name": "fastmail", "email_address": "me@fastmail.com",
        "auth_method": "password", "imap_host": "imap.fastmail.com",
        "imap_port": "993", "oauth_provider": "",
        "folder_allow": "INBOX", "folder_deny": "", "deny_flags": [r"\Trash"],
    }
    kw = af.form_to_create_kwargs(form, deny_flags_selected=[r"\Trash"])
    assert kw == {
        "name": "fastmail", "email_address": "me@fastmail.com",
        "auth_method": "password", "imap_host": "imap.fastmail.com",
        "imap_port": 993, "oauth_provider": None,
        "folder_allow": ["INBOX"], "folder_deny": None,
        "folder_deny_flags": [r"\Trash"],
    }


def test_form_to_create_kwargs_bad_port_is_form_error():
    form = {
        "name": "x", "email_address": "x@x.com", "auth_method": "password",
        "imap_host": "h", "imap_port": "not-a-number", "oauth_provider": "",
        "folder_allow": "", "folder_deny": "",
    }
    with pytest.raises(af.FormError):
        af.form_to_create_kwargs(form, deny_flags_selected=[])


def test_form_to_create_kwargs_archive_nulls_host_port():
    form = {
        "name": "arch", "email_address": "a@x.org", "auth_method": "archive",
        "imap_host": "", "imap_port": "", "oauth_provider": "",
        "folder_allow": "", "folder_deny": "",
    }
    kw = af.form_to_create_kwargs(form, deny_flags_selected=[])
    assert kw["imap_host"] is None
    assert kw["imap_port"] is None
    assert kw["auth_method"] == "archive"


def test_account_to_form_values_roundtrips_lists():
    from datetime import datetime, timezone
    from localmail.api.admin.accounts import Account
    acct = Account(
        id=7, name="g", email_address="g@gmail.com", auth_method="oauth2",
        oauth_provider="gmail", imap_host=None, imap_port=None,
        folder_allow=["INBOX", "Lists/dev"], folder_deny=["Spam"],
        folder_deny_flags=[r"\Trash"], sync_enabled=True,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    vals = af.account_to_form_values(acct)
    assert vals["folder_allow"] == "INBOX\nLists/dev"
    assert vals["folder_deny"] == "Spam"
    assert vals["deny_flags_checked"] == {r"\Trash"}
    assert vals["auth_method"] == "oauth2"


def test_field_errors_from_maps_known_field():
    err = AccountFieldError("live accounts require imap_port in 1..65535")
    fe = af.field_errors_from(err)
    assert "imap_port" in fe


def test_colon_name_rejection_renders_beside_the_name_field():
    """Pin the #217 message against _FIELD_HINTS: the hints match on substrings
    in order, so a rewording (or a reordering) could silently demote this to a
    form-level error instead of showing it beside the offending input."""
    err = AccountFieldError(account_name_error("gmail:refresh"))
    assert set(af.field_errors_from(err)) == {"name"}


def test_field_errors_from_unknown_falls_back_to_form_level():
    err = AccountFieldError("some unmapped failure")
    fe = af.field_errors_from(err)
    assert fe == {"_form": "some unmapped failure"}
