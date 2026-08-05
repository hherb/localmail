# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unit tests for the pure rewriter base-URL validator (#235)."""

from __future__ import annotations

import pytest

from localmail.search.rewriter_url import base_url_error


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:11434",
        "https://api.openai.com/v1",
        "https://api.anthropic.com",
        "http://[::1]:11434",
        "http://127.0.0.1:8080/prefix",
    ],
)
def test_a_usable_base_url_reports_no_error(value):
    assert base_url_error(value) is None


def test_an_empty_value_is_rejected():
    assert base_url_error("") is not None


def test_a_whitespace_only_value_is_rejected():
    assert base_url_error("   ") is not None


def test_a_missing_scheme_is_rejected():
    """The common mistake: httpx parses `localhost:11434` as scheme='localhost'.

    It never raises, so without this check the request fails at call time and
    is reported as "could not reach the rewriter service" — pointing the
    operator at the network instead of at config.toml.
    """
    error = base_url_error("localhost:11434")
    assert error is not None
    assert "http://" in error


def test_a_bare_hostname_is_rejected():
    assert base_url_error("api.openai.com") is not None


def test_a_non_http_scheme_is_rejected():
    error = base_url_error("ftp://example.com")
    assert error is not None
    assert "ftp" in error


def test_a_scheme_with_no_host_is_rejected():
    error = base_url_error("http://")
    assert error is not None
    assert "host" in error


def test_an_unparseable_port_is_rejected():
    """The #235 case proper: the one input httpx.URL actually raises on."""
    assert base_url_error("http://localhost:notaport") is not None


def test_a_path_only_value_is_rejected():
    assert base_url_error("/v1/chat") is not None


def test_the_error_is_a_plain_sentence_not_an_exception():
    """Callers interpolate it into a message naming the config setting."""
    error = base_url_error("")
    assert isinstance(error, str)
    assert error and not error.endswith(".")
