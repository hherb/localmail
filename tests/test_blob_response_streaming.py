# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The blob streamers' failure logging (#386 items 2–4), without a database.

Headers are flushed before the streamer notices anything, so a log line
naming the blob (and, on the index route, the message) is the only signal an
operator gets. These drive the generators directly over in-memory files.
"""
from __future__ import annotations

import io
import logging

import pytest

from localmail.api.range_requests import ByteRange
from localmail.serve.routes.blob_response import _stream_full, _stream_range

_SERVE_LOGGER = "localmail.serve"
_WHERE = "sha256=ab message_id=7 index=2"


class _Tracked(io.BytesIO):
    """A BytesIO that raises OSError on its Nth read, to model an EIO."""

    def __init__(self, data: bytes, *, fail_on_read: int | None = None) -> None:
        super().__init__(data)
        self._reads = 0
        self._fail_on_read = fail_on_read

    def read(self, size: int | None = -1) -> bytes:
        self._reads += 1
        if self._reads == self._fail_on_read:
            raise OSError(5, "Input/output error")
        return super().read(size)


def _messages(caplog, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


def test_a_file_longer_than_its_recorded_size_stops_at_the_size_and_warns(caplog) -> None:
    fp = _Tracked(b"0123456789")
    with caplog.at_level(logging.WARNING, logger=_SERVE_LOGGER):
        body = b"".join(_stream_full(fp, _WHERE, 4))
    assert body == b"0123"  # Content-Length said 4; never send more
    assert fp.closed
    [msg] = _messages(caplog, logging.WARNING)
    assert "overran" in msg and _WHERE in msg and "expected=4" in msg


def test_an_exact_size_file_does_not_warn(caplog) -> None:
    fp = _Tracked(b"0123")
    with caplog.at_level(logging.WARNING, logger=_SERVE_LOGGER):
        assert b"".join(_stream_full(fp, _WHERE, 4)) == b"0123"
    assert caplog.records == []


def test_a_short_file_still_reports_truncation_with_its_context(caplog) -> None:
    fp = _Tracked(b"01")
    with caplog.at_level(logging.WARNING, logger=_SERVE_LOGGER):
        assert b"".join(_stream_full(fp, _WHERE, 4)) == b"01"
    [msg] = _messages(caplog, logging.WARNING)
    assert "truncated" in msg and _WHERE in msg
    assert "expected=4" in msg and "sent=2" in msg


@pytest.mark.parametrize(
    "stream",
    [
        lambda fp: _stream_full(fp, _WHERE, 4),
        lambda fp: _stream_range(fp, ByteRange(start=0, end=3), _WHERE),
    ],
    ids=["full", "range"],
)
def test_a_read_error_mid_stream_is_logged_against_the_blob_and_re_raised(
    stream, caplog,
) -> None:
    fp = _Tracked(b"0123", fail_on_read=1)
    with caplog.at_level(logging.WARNING, logger=_SERVE_LOGGER):
        with pytest.raises(OSError):
            b"".join(stream(fp))
    assert fp.closed
    [msg] = _messages(caplog, logging.ERROR)
    assert "read failed" in msg and _WHERE in msg and "sent=0" in msg


def test_a_range_never_reads_past_its_slice(caplog) -> None:
    fp = _Tracked(b"0123456789")
    with caplog.at_level(logging.WARNING, logger=_SERVE_LOGGER):
        body = b"".join(_stream_range(fp, ByteRange(start=2, end=4), _WHERE))
    assert body == b"234"
    assert caplog.records == []  # a range is a slice; the rest is not an overrun


def test_a_read_error_in_the_overrun_probe_is_logged_and_the_body_completes(
    caplog,
) -> None:
    # Read 1 serves the whole slice; read 2 is the probe past size_bytes.
    fp = _Tracked(b"0123", fail_on_read=2)
    with caplog.at_level(logging.WARNING, logger=_SERVE_LOGGER):
        assert b"".join(_stream_full(fp, _WHERE, 4)) == b"0123"
    assert fp.closed
    [msg] = _messages(caplog, logging.ERROR)
    assert "overrun probe failed" in msg and _WHERE in msg
