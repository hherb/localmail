"""Unit tests for importer.ownership (pure orphan-detection helpers)."""
from __future__ import annotations

import os

from localmail.importer.ownership import pid_is_alive, should_reap

# Above any platform's pid_max (Linux default 4194304, macOS 99998), so this
# pid is never assigned to a live process.
_NEVER_ASSIGNED_PID = 2**31 - 1


def test_pid_is_alive_true_for_own_process():
    assert pid_is_alive(os.getpid()) is True


def test_pid_is_alive_false_for_never_assigned_pid():
    assert pid_is_alive(_NEVER_ASSIGNED_PID) is False


def test_should_reap_keeps_live_local_owner():
    assert should_reap(
        owner_host="host-a", owner_pid=123, current_host="host-a", pid_alive=True
    ) is False


def test_should_reap_reaps_dead_local_owner():
    assert should_reap(
        owner_host="host-a", owner_pid=123, current_host="host-a", pid_alive=False
    ) is True


def test_should_reap_reaps_null_owner_pid():
    assert should_reap(
        owner_host=None, owner_pid=None, current_host="host-a", pid_alive=False
    ) is True


def test_should_reap_keeps_foreign_host_even_if_pid_dead():
    assert should_reap(
        owner_host="host-b", owner_pid=123, current_host="host-a", pid_alive=False
    ) is False
