# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Serve shutdown must not freeze the asyncio event loop (#221 B).

`lifespan`'s teardown called `supervisor.close()` — which SIGTERMs the child and
then blocks for up to the grace period — directly on the event loop. For the
whole of that wait the serve process answers nothing: no health check, no
in-flight request, no signal handling. If the process supervisor (systemd,
launchd, a container runtime) loses patience and SIGKILLs the parent mid-wait,
the already-SIGTERMed child is orphaned and keeps syncing with nobody watching
it.

The fix is to run the blocking close on a worker thread and await it.
"""
from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from localmail.serve import app as app_mod


def test_lifespan_does_not_call_supervisor_close_synchronously() -> None:
    """Guard the call shape at the source.

    Building a real app needs a database, so this asserts on the lifespan
    source: `supervisor.close()` must not appear as a bare synchronous call.
    """
    src = inspect.getsource(app_mod.create_app)
    assert "supervisor.close()" not in src, (
        "supervisor.close() blocks for up to the grace period; it must be "
        "offloaded (anyio.to_thread.run_sync) so the event loop stays live"
    )
    assert "supervisor.close" in src, "the supervisor must still be closed"


def test_offloading_a_blocking_close_keeps_the_loop_responsive() -> None:
    """The property the fix buys, demonstrated on the same primitive the
    lifespan uses: while a blocking close runs, other tasks still get scheduled.
    """
    import anyio.to_thread

    ticks = 0
    close_ran = False

    def _blocking_close() -> None:
        nonlocal close_ran
        time.sleep(0.3)
        close_ran = True

    async def _ticker() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.02)
            ticks += 1

    async def _main() -> None:
        ticker = asyncio.create_task(_ticker())
        await anyio.to_thread.run_sync(_blocking_close)
        ticker.cancel()

    asyncio.run(_main())

    assert close_ran
    assert ticks > 0, "the event loop made no progress during the blocking close"


def test_a_synchronous_close_would_starve_the_loop() -> None:
    """The control: the same scenario without offloading makes zero progress.

    This is what the pre-fix lifespan did, and it is why the assertion above is
    worth having.
    """
    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.02)
            ticks += 1

    async def _main() -> None:
        ticker = asyncio.create_task(_ticker())
        await asyncio.sleep(0)  # let the ticker reach its first await
        time.sleep(0.3)  # the blocking close, on the loop
        ticker.cancel()

    asyncio.run(_main())

    assert ticks == 0
