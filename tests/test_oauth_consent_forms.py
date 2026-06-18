# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

import pytest

from localmail.mcp.oauth.consent_forms import (
    ConsentDecision,
    ConsentFormError,
    parse_consent_form,
)


def test_allow_with_credentials():
    d = parse_consent_form({"req": "blob", "username": "alice",
                            "password": "pw", "decision": "allow"})
    assert d == ConsentDecision(req="blob", username="alice", password="pw", allow=True)


def test_deny_needs_no_credentials():
    d = parse_consent_form({"req": "blob", "decision": "deny"})
    assert d.allow is False
    assert d.req == "blob"


def test_missing_req_rejected():
    with pytest.raises(ConsentFormError):
        parse_consent_form({"decision": "allow", "username": "a", "password": "b"})


def test_allow_missing_password_rejected():
    with pytest.raises(ConsentFormError):
        parse_consent_form({"req": "blob", "username": "alice", "decision": "allow"})


def test_unknown_decision_rejected():
    with pytest.raises(ConsentFormError):
        parse_consent_form({"req": "blob", "decision": "maybe"})
