# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""`kill -USR1 <daemon pid>` dumps every thread's stack to stderr (#414).

#414's stall left both sync workers parked with no log line and no way to see
where; the cause had to be guessed. With this registered, the next hang is
read off the journal / `daemon.err.log` instead.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading

import psycopg

import localmail.daemon as daemon_mod
from localmail.config import LocalmailConfig
from localmail.daemon import STACK_DUMP_SIGNAL, Daemon


def test_the_dump_signal_is_sigusr1() -> None:
    # Literal on purpose: the runbook and the issue tell an operator SIGUSR1.
    assert STACK_DUMP_SIGNAL == signal.SIGUSR1


def test_run_forever_registers_the_stack_dump(db_dsn: str, monkeypatch) -> None:
    with psycopg.connect(db_dsn) as conn:
        conn.execute("TRUNCATE accounts, daemon_heartbeats RESTART IDENTITY CASCADE")
        conn.commit()
    cfg = LocalmailConfig.model_validate(
        {"database": {"dsn": db_dsn}, "daemon": {"command_listen_enabled": False}}
    )
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False

    registered: list[tuple[object, dict]] = []
    monkeypatch.setattr(daemon_mod.faulthandler, "register",
                        lambda sig, **kw: registered.append((sig, kw)))
    # run_forever runs on the main thread here; keep pytest's own handlers.
    monkeypatch.setattr(daemon_mod.signal, "signal", lambda *a: None)

    d = Daemon(cfg, ssl=False, stop_event=threading.Event())
    d.stop()  # the loop exits on its first wake
    d.run_forever()

    assert registered == [(STACK_DUMP_SIGNAL, {"all_threads": True})]


def test_the_registered_signal_dumps_every_thread() -> None:
    """End to end in a child process: the signal prints a stack for a thread
    other than the one that received it, which is the one #414 needed."""
    child = (
        "import faulthandler, os, threading, time\n"
        "from localmail.daemon import STACK_DUMP_SIGNAL\n"
        "faulthandler.register(STACK_DUMP_SIGNAL, all_threads=True)\n"
        "def parked_worker(): time.sleep(30)\n"
        "threading.Thread(target=parked_worker, daemon=True).start()\n"
        "time.sleep(0.2)\n"
        "os.kill(os.getpid(), STACK_DUMP_SIGNAL)\n"
        "time.sleep(0.2)\n"
    )
    out = subprocess.run([sys.executable, "-c", child], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "parked_worker" in out.stderr, out.stderr
