# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

import pytest

from localmail.mcp.oauth.resource_indicator import (
    ResourceDecision,
    canonicalize_resource,
    decide_resource,
    resolve_accepted_resources,
)

CANON = "https://mail.example.com/mcp"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://mail.example.com/mcp", CANON),
        ("https://mail.example.com/mcp/", CANON),          # trailing slash stripped
        ("https://MAIL.Example.COM/mcp", CANON),           # host lowercased
        ("HTTPS://mail.example.com/mcp", CANON),           # scheme lowercased
        ("https://mail.example.com:443/mcp", CANON),       # default port dropped
        ("http://h/mcp", "http://h/mcp"),
        ("http://h:80/mcp", "http://h/mcp"),               # default http port dropped
        ("https://h:8443/mcp", "https://h:8443/mcp"),      # non-default port kept
        ("https://h/", "https://h"),                       # bare root slash stripped
        ("https://mail.example.com/mcp#frag", None),       # fragment rejected
        ("ftp://h/mcp", None),                             # non-http scheme
        ("/mcp", None),                                     # relative
        ("not a url", None),
        ("", None),
        ("https://h:abc/mcp", None),                       # non-numeric port
        ("https://h:999999/mcp", None),                    # out-of-range port
    ],
)
def test_canonicalize(raw, expected):
    assert canonicalize_resource(raw) == expected


def test_resolve_defaults_to_derived_when_configured_none():
    assert resolve_accepted_resources(None, "https://h/mcp/") == ["https://h/mcp"]


def test_resolve_empty_list_falls_back_to_derived():
    assert resolve_accepted_resources([], "https://h/mcp") == ["https://h/mcp"]


def test_resolve_uses_configured_and_canonicalizes():
    assert resolve_accepted_resources(
        ["https://A.com/mcp/", "https://b.com:443/mcp"], "https://h/mcp"
    ) == ["https://a.com/mcp", "https://b.com/mcp"]


def test_resolve_drops_malformed_but_keeps_valid():
    assert resolve_accepted_resources(
        ["https://a.com/mcp", "bogus"], "https://h/mcp"
    ) == ["https://a.com/mcp"]


def test_resolve_all_malformed_configured_raises():
    with pytest.raises(ValueError):
        resolve_accepted_resources(["bogus", "also bad"], "https://h/mcp")


def test_decide_absent_not_required_binds_first_accepted():
    d = decide_resource(None, [CANON], require=False)
    assert d == ResourceDecision(ok=True, bound=CANON, error=None)


def test_decide_absent_required_errors():
    d = decide_resource(None, [CANON], require=True)
    assert d.ok is False and d.bound is None and d.error


def test_decide_match_binds_canonical():
    d = decide_resource("https://mail.example.com/mcp/", [CANON], require=False)
    assert d.ok is True and d.bound == CANON


def test_decide_mismatch_errors():
    d = decide_resource("https://evil.com/mcp", [CANON], require=False)
    assert d.ok is False and d.bound is None and d.error


def test_decide_malformed_errors():
    d = decide_resource("https://h/mcp#x", [CANON], require=False)
    assert d.ok is False and d.error
