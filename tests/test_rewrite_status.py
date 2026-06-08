"""Unit tests for the pure rewrite-outcome status/note helpers."""
import httpx
import pytest

from localmail.search.rewrite_status import (
    APPLIED,
    FAILED,
    NOT_ATTEMPTED,
    NOT_REQUESTED,
    UNAVAILABLE,
    classify_rewrite_failure,
    rewrite_skipped_for_status,
)
from localmail.search.rewriter import RewriteParseError


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://localhost:11434/api/generate")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


def test_classify_404_returns_actionable_model_pull_note():
    note = classify_rewrite_failure(_status_error(404), model="granite4.1:3b-q8_0")
    assert "granite4.1:3b-q8_0" in note
    assert "ollama pull granite4.1:3b-q8_0" in note


def test_classify_non_404_status_returns_unreachable_note():
    note = classify_rewrite_failure(_status_error(500), model="m")
    assert note == "could not reach the rewriter service"


def test_classify_connect_error_returns_unreachable_note():
    note = classify_rewrite_failure(httpx.ConnectError("down"), model="m")
    assert note == "could not reach the rewriter service"


def test_classify_parse_error_returns_unparseable_note():
    note = classify_rewrite_failure(RewriteParseError("bad"), model="m")
    assert note == "the rewriter returned an unparseable response"


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
