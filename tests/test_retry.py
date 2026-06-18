# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Tests for the reusable bounded-backoff retry helper (#133).

These pin the contract that:
1. ``next_backoff`` is a pure doubling-with-cap function.
2. ``retry_with_backoff`` returns the operation's result once it stops raising.
3. It waits on the stop event between attempts and aborts (``RetryAborted``)
   the moment the event fires — never crash-looping, never blocking past a
   stop signal.
"""

from __future__ import annotations

import threading

import pytest

from localmail.retry import RetryAborted, next_backoff, retry_with_backoff


# --- next_backoff (pure) -----------------------------------------------------


def test_next_backoff_doubles() -> None:
    assert next_backoff(1.0, factor=2.0, max_s=60.0) == 2.0
    assert next_backoff(2.0, factor=2.0, max_s=60.0) == 4.0


def test_next_backoff_caps_at_max() -> None:
    assert next_backoff(40.0, factor=2.0, max_s=60.0) == 60.0
    assert next_backoff(60.0, factor=2.0, max_s=60.0) == 60.0


def test_next_backoff_honours_factor() -> None:
    assert next_backoff(1.0, factor=3.0, max_s=100.0) == 3.0


# --- retry_with_backoff ------------------------------------------------------


def test_returns_immediately_on_first_success() -> None:
    calls = {"n": 0}

    def op() -> str:
        calls["n"] += 1
        return "ok"

    result = retry_with_backoff(
        op,
        stop_event=threading.Event(),
        initial_s=0.01,
        max_s=0.05,
        description="op",
    )
    assert result == "ok"
    assert calls["n"] == 1


def test_retries_until_success() -> None:
    attempts = {"n": 0}

    def op() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("not yet")
        return "recovered"

    result = retry_with_backoff(
        op,
        stop_event=threading.Event(),
        initial_s=0.001,
        max_s=0.01,
        description="flaky op",
    )
    assert result == "recovered"
    assert attempts["n"] == 3


def test_aborts_when_stop_already_set() -> None:
    stop = threading.Event()
    stop.set()
    calls = {"n": 0}

    def op() -> str:
        calls["n"] += 1
        return "ok"

    with pytest.raises(RetryAborted):
        retry_with_backoff(
            op,
            stop_event=stop,
            initial_s=0.01,
            max_s=0.05,
            description="op",
        )
    assert calls["n"] == 0  # never even attempted


def test_aborts_when_stop_fires_during_backoff() -> None:
    stop = threading.Event()
    attempts = {"n": 0}

    def op() -> str:
        attempts["n"] += 1
        # Trip the stop signal so the post-failure wait returns True.
        stop.set()
        raise ConnectionError("always fails")

    with pytest.raises(RetryAborted):
        retry_with_backoff(
            op,
            stop_event=stop,
            initial_s=0.01,
            max_s=0.05,
            description="op",
        )
    assert attempts["n"] == 1  # failed once, then stop aborted the retry


def test_logs_traceback_only_on_first_failure(caplog) -> None:
    """A sustained outage must not re-log the same traceback every cycle:
    full trace on the first failure, one-liner thereafter."""
    attempts = {"n": 0}

    def op() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("not yet")
        return "recovered"

    with caplog.at_level("WARNING", logger="localmail.retry"):
        result = retry_with_backoff(
            op,
            stop_event=threading.Event(),
            initial_s=0.001,
            max_s=0.01,
            description="flaky op",
        )

    assert result == "recovered"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2  # two failures before recovery
    assert warnings[0].exc_info  # first carries the traceback
    assert not warnings[1].exc_info  # subsequent ones do not
