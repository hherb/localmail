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


def _daemon(db_dsn: str) -> Daemon:
    with psycopg.connect(db_dsn) as conn:
        conn.execute("TRUNCATE accounts, daemon_heartbeats RESTART IDENTITY CASCADE")
        conn.commit()
    cfg = LocalmailConfig.model_validate(
        {"database": {"dsn": db_dsn}, "daemon": {"command_listen_enabled": False}}
    )
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False
    return Daemon(cfg, ssl=False, stop_event=threading.Event())


def _run(d: Daemon, monkeypatch, register, *, on_main_thread: bool = True) -> list:
    order: list = []
    real_start = d.start_workers

    def start_workers() -> None:
        order.append("start_workers")
        real_start()

    monkeypatch.setattr(daemon_mod.faulthandler, "register",
                        lambda sig, **kw: (order.append((sig, kw)), register())[0])
    monkeypatch.setattr(d, "start_workers", start_workers)
    # run_forever runs on the main thread here; keep pytest's own handlers.
    monkeypatch.setattr(daemon_mod.signal, "signal", lambda *a: None)
    d.stop()  # the loop exits on its first wake
    if on_main_thread:
        d.run_forever()
    else:
        t = threading.Thread(target=d.run_forever)
        t.start()
        t.join(timeout=30)
        assert not t.is_alive()
    return order


def test_run_forever_registers_the_stack_dump_before_any_worker(
    db_dsn: str, monkeypatch,
) -> None:
    """Before the workers, since a dump registered later misses the stall
    it exists for (the test stops the daemon first, so "eventually" passes)."""
    order = _run(_daemon(db_dsn), monkeypatch, lambda: None)
    assert order == [(STACK_DUMP_SIGNAL, {"all_threads": True}), "start_workers"]


def test_off_the_main_thread_the_signal_is_left_alone(db_dsn: str, monkeypatch) -> None:
    """Like SIGTERM/SIGINT: an embedder running the daemon on a thread keeps
    its own SIGUSR1."""
    order = _run(_daemon(db_dsn), monkeypatch, lambda: None, on_main_thread=False)
    assert order == ["start_workers"]


def test_a_refused_registration_does_not_stop_the_daemon(
    db_dsn: str, monkeypatch, caplog,
) -> None:
    def refuse() -> None:
        raise RuntimeError("sys.stderr is None")

    with caplog.at_level("WARNING", logger="localmail.daemon"):
        order = _run(_daemon(db_dsn), monkeypatch, refuse)
    assert order[-1] == "start_workers"
    assert "could not register the SIGUSR1 stack dump" in caplog.text


def test_the_registered_signal_dumps_every_thread() -> None:
    """In a child process: the signal prints a stack for a thread other than
    the one that received it, which is the one #414 needed. This pins
    faulthandler with our signal and arguments, not the Daemon wiring; the
    tests above pin that."""
    child = (
        "import faulthandler, os, threading, time\n"
        "from localmail.daemon import STACK_DUMP_SIGNAL\n"
        "faulthandler.register(STACK_DUMP_SIGNAL, all_threads=True)\n"
        "parked = threading.Event()\n"
        "def parked_worker():\n"
        "    parked.set()\n"
        "    time.sleep(30)\n"
        "threading.Thread(target=parked_worker, daemon=True).start()\n"
        "assert parked.wait(10)\n"
        "os.kill(os.getpid(), STACK_DUMP_SIGNAL)\n"
        "time.sleep(0.2)\n"
    )
    out = subprocess.run([sys.executable, "-c", child], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "parked_worker" in out.stderr, out.stderr
