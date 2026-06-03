"""`localmail daemon …` CLI subgroup (2B.4).

Two planes (see the 2B spec):
  * Plane A — DB-mediated, works regardless of who supervises the process:
    `status` (read heartbeats), `reload` (enqueue reload-now),
    `restart-account NAME` (enqueue restart-account).
  * Plane B — process lifecycle over the supervisor's Unix control socket:
    `start` / `stop` / `restart`. These require `localmail serve` to be running
    with `supervise_daemon = true`; otherwise they exit non-zero with a clear
    note (external-supervisor or unreachable-socket).

Kept in its own module (registered via `main.add_command`) so `cli.py` doesn't
grow further. Heavy imports are deferred into the command bodies.
"""
from __future__ import annotations

import os
from pathlib import Path

import click

from localmail.serve.daemon_supervisor import SupervisorState

# Control-socket read timeouts (seconds). `status` is a cheap query; lifecycle
# ops must outlast a stop() that itself waits up to shutdown_grace_seconds for
# SIGTERM before SIGKILL, hence the grace + buffer below.
_STATUS_TIMEOUT_S = 5.0
_LIFECYCLE_TIMEOUT_BUFFER_S = 5.0

# Gap between status polls while waiting for a lifecycle op to settle.
_LIFECYCLE_POLL_INTERVAL_S = 0.25
# Settle timeout for `start` (it never waits on the SIGTERM grace).
_START_SETTLE_TIMEOUT_S = 10.0

# Terminal state each op settles to.
_SETTLE_TARGET = {
    "start": SupervisorState.RUNNING,
    "restart": SupervisorState.RUNNING,
    "stop": SupervisorState.STOPPED,
}


def _load(ctx: click.Context):
    from localmail.config import load_config

    return load_config(ctx.obj["config_path"])


def _socket_path(cfg) -> Path:
    from localmail.serve.daemon_supervisor import resolve_runtime_dir, socket_path

    return socket_path(resolve_runtime_dir(cfg.serve.runtime_dir, env=os.environ))


def _lifecycle(ctx: click.Context, op: str, *, no_wait: bool) -> None:
    """Drive a Plane B op over the control socket. After the (non-blocking)
    command, poll status until the op settles, unless --no-wait."""
    import time

    from localmail.serve.daemon_control_socket import (
        ControlSocketError,
        send_control_request,
    )

    cfg = _load(ctx)
    if not cfg.serve.supervise_daemon:
        raise click.ClickException(
            f"cannot {op} the daemon: it is supervised externally "
            "([serve] supervise_daemon = false). Use your init system "
            "(systemctl/launchctl), or `localmail daemon reload` / "
            "`restart-account` for DB-mediated control."
        )
    sock = _socket_path(cfg)
    settle_timeout = (
        _START_SETTLE_TIMEOUT_S
        if op == "start"
        else cfg.daemon.shutdown_grace_seconds + _LIFECYCLE_TIMEOUT_BUFFER_S
    )
    try:
        resp = send_control_request(sock, {"cmd": op}, timeout=settle_timeout)
        if not resp.get("ok"):
            raise click.ClickException(f"{op} failed: {resp.get('error')}")
        state = resp.get("status", {}).get("state", "?")
        if no_wait:
            click.echo(f"daemon {op}: {state} (not waiting)")
            return
        target = _SETTLE_TARGET[op]
        deadline = time.monotonic() + settle_timeout
        while state != target:
            if state == SupervisorState.CRASHED:
                raise click.ClickException(f"{op} failed: daemon crashed")
            if time.monotonic() >= deadline:
                raise click.ClickException(
                    f"{op} did not settle to {target} (last state: {state})"
                )
            time.sleep(_LIFECYCLE_POLL_INTERVAL_S)
            st = send_control_request(
                sock, {"cmd": "status"}, timeout=_STATUS_TIMEOUT_S
            )
            state = st.get("status", {}).get("state", "?")
    except ControlSocketError as e:
        raise click.ClickException(
            f"cannot {op} the daemon: {e}. Is `localmail serve` running?"
        )
    click.echo(f"daemon {op}: {state}")


@click.group("daemon")
def daemon_group() -> None:
    """Inspect and control the sync daemon."""


@daemon_group.command("status")
@click.pass_context
def daemon_status(ctx: click.Context) -> None:
    """Show daemon process state (if supervised) and per-thread heartbeats."""
    import psycopg

    from localmail.api.admin.daemon import get_daemon_status
    from localmail.serve.daemon_control_socket import (
        ControlSocketError,
        send_control_request,
    )

    cfg = _load(ctx)

    if not cfg.serve.supervise_daemon:
        click.echo("daemon process: external (managed outside localmail serve)")
    else:
        try:
            resp = send_control_request(
                _socket_path(cfg), {"cmd": "status"}, timeout=_STATUS_TIMEOUT_S
            )
            st = resp.get("status", {})
            click.echo(
                f"daemon process: {st.get('state', '?')} "
                f"(pid={st.get('pid')}, started_at={st.get('started_at')})"
            )
        except ControlSocketError:
            click.echo("daemon process: unreachable (is `localmail serve` running?)")

    with psycopg.connect(cfg.database.dsn) as conn:
        status = get_daemon_status(
            conn, stale_seconds=cfg.daemon.heartbeat_stale_seconds
        )
    if not status.heartbeats:
        click.echo("heartbeats: none")
        return
    click.echo("heartbeats:")
    for hb in status.heartbeats:
        acct = hb.account_id if hb.account_id is not None else "-"
        stale = " STALE" if hb.stale else ""
        folder = f" folder={hb.current_folder}" if hb.current_folder else ""
        err = f" error={hb.last_error_msg}" if hb.last_error_msg else ""
        click.echo(
            f"  {hb.worker_kind} acct={acct} state={hb.state}{folder}"
            f" last_beat={hb.last_heartbeat_at.isoformat()}{stale}{err}"
        )


@daemon_group.command("reload")
@click.pass_context
def daemon_reload(ctx: click.Context) -> None:
    """Enqueue a reload-now command (re-read the account set immediately)."""
    import psycopg

    from localmail.api.admin.daemon import enqueue_command

    cfg = _load(ctx)
    with psycopg.connect(cfg.database.dsn) as conn:
        cid = enqueue_command(conn, command="reload-now", requested_by=None)
    click.echo(f"queued reload-now (command #{cid})")


@daemon_group.command("restart-account")
@click.argument("name")
@click.pass_context
def daemon_restart_account(ctx: click.Context, name: str) -> None:
    """Enqueue a restart-account command for the named account."""
    import psycopg

    from localmail.api.admin.accounts import get_account_by_name
    from localmail.api.admin.daemon import enqueue_command

    cfg = _load(ctx)
    with psycopg.connect(cfg.database.dsn) as conn:
        account = get_account_by_name(conn, name)
        if account is None:
            raise click.ClickException(f"no account named {name!r}")
        cid = enqueue_command(
            conn,
            command="restart-account",
            account_id=account.id,
            requested_by=None,
        )
    click.echo(f"queued restart-account for {name} (command #{cid})")


@daemon_group.command("start")
@click.option("--no-wait", is_flag=True, help="Return without waiting to settle.")
@click.pass_context
def daemon_start(ctx: click.Context, no_wait: bool) -> None:
    """Start the supervised daemon process (Plane B)."""
    _lifecycle(ctx, "start", no_wait=no_wait)


@daemon_group.command("stop")
@click.option("--no-wait", is_flag=True, help="Return without waiting to settle.")
@click.pass_context
def daemon_stop(ctx: click.Context, no_wait: bool) -> None:
    """Stop the supervised daemon process (Plane B)."""
    _lifecycle(ctx, "stop", no_wait=no_wait)


@daemon_group.command("restart")
@click.option("--no-wait", is_flag=True, help="Return without waiting to settle.")
@click.pass_context
def daemon_restart(ctx: click.Context, no_wait: bool) -> None:
    """Restart the supervised daemon process (Plane B)."""
    _lifecycle(ctx, "restart", no_wait=no_wait)
