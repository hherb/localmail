# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Reusable bounded exponential-backoff retry that respects a stop signal.

Used at daemon startup (#133): the construction-time DB touches
(``_load_syncable_accounts`` + ``open_pool``) go through ``retry_with_backoff``
so a briefly-unreachable Postgres makes the daemon *wait* rather than crash on
construction. The same 1s→60s shape the IDLE/poll worker loops already use,
factored into one tested place with the constants lifted to config.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, TypeVar

T = TypeVar("T")

_DEFAULT_FACTOR = 2.0


class RetryAborted(Exception):
    """The stop event fired before the operation succeeded."""


def next_backoff(current_s: float, *, factor: float, max_s: float) -> float:
    """Pure: the next backoff delay — ``current_s`` grown by ``factor`` and
    capped at ``max_s``."""
    return min(current_s * factor, max_s)


def retry_with_backoff(
    operation: Callable[[], T],
    *,
    stop_event: threading.Event,
    initial_s: float,
    max_s: float,
    description: str,
    factor: float = _DEFAULT_FACTOR,
    log: logging.Logger | None = None,
) -> T:
    """Call ``operation`` until it returns without raising; return its result.

    The first attempt runs immediately (no initial wait). After each failure
    the call waits on ``stop_event`` for the current backoff, then doubles it
    (capped at ``max_s``). If ``stop_event`` is set — either before the first
    attempt or while waiting between attempts — raise :class:`RetryAborted`
    instead of continuing to retry, so a stop signal always wins over the
    retry loop.
    """
    logger = log or logging.getLogger(__name__)
    backoff = initial_s
    first_failure = True
    while True:
        if stop_event.is_set():
            raise RetryAborted(description)
        try:
            return operation()
        except RetryAborted:
            raise
        except Exception:
            # Full traceback only on the first failure; a sustained outage
            # otherwise re-logs the same trace once per backoff cycle forever.
            logger.warning(
                "%s failed; retrying in %.1fs",
                description,
                backoff,
                exc_info=first_failure,
            )
            first_failure = False
            if stop_event.wait(backoff):
                raise RetryAborted(description)
            backoff = next_backoff(backoff, factor=factor, max_s=max_s)
