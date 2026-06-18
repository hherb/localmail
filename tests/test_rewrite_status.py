# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unit tests for the pure rewrite-outcome status/note/code helpers."""
import httpx
import pytest

from localmail.search.rewrite_status import (
    APPLIED,
    CONTINUATION_PAGE,
    FAILED,
    MISSING_MODEL,
    NOT_ATTEMPTED,
    NOT_CONFIGURED,
    NOT_REQUESTED,
    UNAVAILABLE,
    UNPARSEABLE,
    UNREACHABLE,
    classify_rewrite_failure,
    note_for_code,
    rewrite_skipped_for_status,
)
from localmail.search.rewriter import RewriteParseError


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://localhost:11434/api/generate")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


def test_classify_404_returns_missing_model_code():
    assert classify_rewrite_failure(_status_error(404)) == MISSING_MODEL


def test_classify_non_404_status_returns_unreachable_code():
    assert classify_rewrite_failure(_status_error(500)) == UNREACHABLE


def test_classify_connect_error_returns_unreachable_code():
    assert classify_rewrite_failure(httpx.ConnectError("down")) == UNREACHABLE


def test_classify_parse_error_returns_unparseable_code():
    assert classify_rewrite_failure(RewriteParseError("bad")) == UNPARSEABLE


def test_note_for_missing_model_interpolates_model():
    note = note_for_code(MISSING_MODEL, model="granite4.1:3b-q8_0")
    assert "granite4.1:3b-q8_0" in note
    assert "ollama pull granite4.1:3b-q8_0" in note


def test_note_for_missing_model_without_model_raises():
    with pytest.raises(ValueError):
        note_for_code(MISSING_MODEL)


@pytest.mark.parametrize(
    "code,expected",
    [
        (UNREACHABLE, "could not reach the rewriter service"),
        (UNPARSEABLE, "the rewriter returned an unparseable response"),
        (NOT_CONFIGURED, "smart search is not configured on this server"),
        (
            CONTINUATION_PAGE,
            "smart query rewriting applies to the first page only; "
            "this is a continuation page",
        ),
    ],
)
def test_note_for_static_codes(code, expected):
    assert note_for_code(code) == expected


def test_note_for_every_code_is_nonempty():
    for code in (
        MISSING_MODEL, UNREACHABLE, UNPARSEABLE, NOT_CONFIGURED, CONTINUATION_PAGE,
    ):
        assert note_for_code(code, model="m")  # rendered, non-empty


@pytest.mark.parametrize(
    "status,expected",
    [
        (UNAVAILABLE, True),
        (FAILED, True),
        (APPLIED, False),
        (NOT_ATTEMPTED, False),
        (NOT_REQUESTED, False),
    ],
)
def test_rewrite_skipped_for_status(status, expected):
    assert rewrite_skipped_for_status(status) is expected
