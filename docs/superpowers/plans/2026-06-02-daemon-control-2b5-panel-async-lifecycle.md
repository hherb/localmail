# 2B.5 Daemon Panel + 202-Async Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the admin HTML daemon-control panel (spec §2B.5) and make `start`/`stop`/`restart` non-blocking (return 202 / immediate status, run the op on a supervisor-owned thread) so a long stop no longer pins a request or socket worker (#146).

**Architecture:** `DaemonSupervisor` grows `request_start/stop/restart` that set the transitional state synchronously and run the existing blocking body on one dedicated thread, guarded so only one lifecycle op is in flight (else `SupervisorUnavailable`). HTTP routes + the control socket switch to those and return immediately; the CLI polls status until settled. A new admin HTML router renders a status panel + control buttons, polling an HTMX partial; both reuse a shared `build_daemon_view` fusion and a new method-bound CSRF mint helper.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, HTMX, psycopg v3, click, pytest. No new migration (reuses `0023`/`0024`).

**Branch:** `daemon-control-2b5-panel-async` (already created; spec committed at `b6573f7`).

**Run prefix for all commands:** `unset VIRTUAL_ENV && uv run …`

---

## File structure

| File | Responsibility | Action |
|------|----------------|--------|
| `src/localmail/serve/daemon_supervisor.py` | `request_*` async lifecycle + busy-guard; stub `request_*` | Modify |
| `src/localmail/serve/admin/daemon_router.py` | 202 + busy-guard 409; extract `build_daemon_view` | Modify |
| `src/localmail/serve/daemon_control_socket.py` | dispatch start/stop/restart → `request_*`; Protocol grows methods | Modify |
| `src/localmail/daemon_cli.py` | poll-until-settled + `--no-wait`; named-constant timeouts | Modify |
| `src/localmail/serve/admin/csrf.py` | `csrf_token_context` (legacy + method-bound mint helpers) | Modify |
| `src/localmail/serve/admin/daemon_panel_router.py` | `/admin/daemon` page + `/admin/_partials/daemon-status` partial | Create |
| `src/localmail/serve/admin/templates/daemon/panel.html` | full page (extends base) | Create |
| `src/localmail/serve/admin/templates/daemon/_status.html` | self-polling status fragment + buttons | Create |
| `src/localmail/serve/admin/static/admin.css` | `.daemon-stale`, `.daemon-controls`, `.daemon-note` styles | Modify |
| `src/localmail/serve/app.py` | register panel router in the admin block | Modify |
| `tests/test_daemon_supervisor.py` | async lifecycle + busy-guard tests | Modify |
| `tests/test_serve_daemon_routes.py` | 202 + busy-guard 409; `build_daemon_view` | Modify |
| `tests/test_daemon_control_socket.py` | async dispatch tests | Modify |
| `tests/test_daemon_cli.py` | poll-until-settled + `--no-wait` | Modify |
| `tests/test_serve_daemon_panel.py` | panel render / stale / external / CSRF | Create |
| `README.md`, `CLAUDE.md` | document panel + async contract | Modify |

---

## Task 1: Supervisor async lifecycle thread + busy-guard

**Files:**
- Modify: `src/localmail/serve/daemon_supervisor.py`
- Test: `tests/test_daemon_supervisor.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_daemon_supervisor.py` (top: add `import signal` is not needed — the deaf child sets it inline; keep existing imports, add `SupervisorUnavailable` is already imported):

```python
# --- async lifecycle (request_*) -----------------------------------------

# A child that ignores SIGTERM so stop() blocks the full grace window,
# giving a deterministic interval to observe STOPPING / hit the busy-guard.
_DEAF_SLEEPER = [
    sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print('up', flush=True); time.sleep(60)",
]


def _wait_state(sup: DaemonSupervisor, target: str, timeout: float = 6.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sup.status().state == target:
            return
        time.sleep(0.02)
    raise AssertionError(f"never reached {target}; now {sup.status().state}")


def test_request_start_settles_to_running() -> None:
    s = DaemonSupervisor(argv=_SLEEPER, grace_seconds=2.0)
    try:
        s.request_start()
        _wait_state(s, SupervisorState.RUNNING)
        assert s.status().pid is not None
    finally:
        s.stop()


def test_request_stop_sets_transitional_then_stopped() -> None:
    s = DaemonSupervisor(argv=_DEAF_SLEEPER, grace_seconds=1.0)
    try:
        s.request_start()
        _wait_state(s, SupervisorState.RUNNING)
        s.request_stop()
        # Transitional state is visible immediately, before the grace wait ends.
        assert s.status().state == SupervisorState.STOPPING
        _wait_state(s, SupervisorState.STOPPED)
    finally:
        s.stop()


def test_busy_guard_rejects_second_lifecycle_op() -> None:
    s = DaemonSupervisor(argv=_DEAF_SLEEPER, grace_seconds=1.0)
    try:
        s.request_start()
        _wait_state(s, SupervisorState.RUNNING)
        s.request_stop()  # now in flight, blocking on the 1s grace wait
        assert s.status().state == SupervisorState.STOPPING
        with pytest.raises(SupervisorUnavailable):
            s.request_stop()
        _wait_state(s, SupervisorState.STOPPED)
    finally:
        s.stop()


def test_request_start_idempotent_when_running() -> None:
    s = DaemonSupervisor(argv=_SLEEPER, grace_seconds=2.0)
    try:
        s.request_start()
        _wait_state(s, SupervisorState.RUNNING)
        pid1 = s.status().pid
        s.request_start()  # no-op, not a busy error
        assert s.status().state == SupervisorState.RUNNING
        assert s.status().pid == pid1
    finally:
        s.stop()


def test_request_restart_settles_to_running_new_pid() -> None:
    s = DaemonSupervisor(argv=_SLEEPER, grace_seconds=2.0)
    try:
        s.request_start()
        _wait_state(s, SupervisorState.RUNNING)
        pid1 = s.status().pid
        s.request_restart()
        _wait_state(s, SupervisorState.RUNNING)
        assert s.status().pid != pid1
    finally:
        s.stop()


@pytest.mark.parametrize("method", ["request_start", "request_stop", "request_restart"])
def test_external_stub_refuses_request_lifecycle(method: str) -> None:
    s = ExternalDaemonSupervisor()
    with pytest.raises(SupervisorUnavailable):
        getattr(s, method)()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_supervisor.py -k request_ -x`
Expected: FAIL with `AttributeError: 'DaemonSupervisor' object has no attribute 'request_start'`.

- [ ] **Step 3: Implement `request_*` + busy-guard on `DaemonSupervisor`**

In `src/localmail/serve/daemon_supervisor.py`, add `Callable` to the typing import line:

```python
from typing import Callable, Mapping, Sequence
```

In `DaemonSupervisor.__init__`, add the lifecycle-thread handle next to `_reader`:

```python
        self._reader: threading.Thread | None = None
        self._lifecycle_thread: threading.Thread | None = None
```

Add these methods to `DaemonSupervisor` (place them right after `restart()`):

```python
    # -- async lifecycle (request_*): return immediately, run on one thread --

    def _lifecycle_in_flight(self) -> bool:
        t = self._lifecycle_thread
        return t is not None and t.is_alive()

    def _spawn_lifecycle(self, body: Callable[[], None]) -> None:
        t = threading.Thread(
            target=body, name="daemon-supervisor-lifecycle", daemon=True
        )
        self._lifecycle_thread = t
        t.start()

    def request_start(self) -> None:
        """Start the child on a background thread; return at once.

        Idempotent no-op if already running. Raises SupervisorUnavailable if a
        lifecycle op is already in flight (the busy-guard)."""
        with self._lock:
            if self._lifecycle_in_flight():
                raise SupervisorUnavailable(
                    "a lifecycle operation is already in progress"
                )
            if self._proc is not None and self._proc.poll() is None:
                return
            self._state = SupervisorState.STARTING
            self._spawn_lifecycle(self.start)

    def request_stop(self) -> None:
        """Stop the child on a background thread; return at once.

        Idempotent no-op if already stopped. Sets STOPPING synchronously so a
        clean shutdown is never misread as a crash. Busy-guarded."""
        with self._lock:
            if self._lifecycle_in_flight():
                raise SupervisorUnavailable(
                    "a lifecycle operation is already in progress"
                )
            if self._proc is None or self._proc.poll() is not None:
                self._proc = None
                self._started_at = None
                self._state = SupervisorState.STOPPED
                return
            self._state = SupervisorState.STOPPING
            self._spawn_lifecycle(self.stop)

    def request_restart(self) -> None:
        """Restart the child on a background thread; return at once. Busy-guarded.

        Settle target is RUNNING; a transient STOPPED mid-restart is expected."""
        with self._lock:
            if self._lifecycle_in_flight():
                raise SupervisorUnavailable(
                    "a lifecycle operation is already in progress"
                )
            self._state = SupervisorState.STOPPING
            self._spawn_lifecycle(self.restart)
```

Add the three stubs to `ExternalDaemonSupervisor` (after its `restart`):

```python
    def request_start(self) -> None:
        raise SupervisorUnavailable("daemon is supervised externally")

    def request_stop(self) -> None:
        raise SupervisorUnavailable("daemon is supervised externally")

    def request_restart(self) -> None:
        raise SupervisorUnavailable("daemon is supervised externally")
```

> Note: the blocking `start()`/`stop()` set the transitional state again at their
> own start — harmless and keeps them usable standalone (tests, `close()`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_supervisor.py`
Expected: PASS (all existing + 6 new).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/daemon_supervisor.py tests/test_daemon_supervisor.py
git commit -m "feat(serve): async request_start/stop/restart on DaemonSupervisor (2B.5, #146)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Routes — 202, busy-guard 409, shared `build_daemon_view`

**Files:**
- Modify: `src/localmail/serve/admin/daemon_router.py`
- Test: `tests/test_serve_daemon_routes.py`

- [ ] **Step 1: Update the failing tests**

In `tests/test_serve_daemon_routes.py`, replace `test_start_then_stop` and add busy-guard + `build_daemon_view` coverage. Add imports at top:

```python
import time

from localmail.serve.admin.daemon_router import build_daemon_view
```

Replace `test_start_then_stop` with:

```python
def _poll_state(client, target: str, timeout: float = 6.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = client.get("/v1/admin/daemon").json()["state"]
        if st == target:
            return st
        time.sleep(0.05)
    raise AssertionError(f"never reached {target}; last {st}")


def test_start_returns_202_and_settles_running(admin_client, app) -> None:
    sup = DaemonSupervisor(argv=_SLEEPER, grace_seconds=2.0)
    app.state.daemon_supervisor = sup
    try:
        r = admin_client.post(
            "/v1/admin/daemon/start",
            headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/daemon/start")},
        )
        assert r.status_code == 202, r.text
        assert r.json()["state"] in (
            SupervisorState.STARTING, SupervisorState.RUNNING
        )
        assert _poll_state(admin_client, SupervisorState.RUNNING)
        r = admin_client.post(
            "/v1/admin/daemon/stop",
            headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/daemon/stop")},
        )
        assert r.status_code == 202
        assert _poll_state(admin_client, SupervisorState.STOPPED)
    finally:
        sup.stop()
```

Add a busy-guard route test (uses the deaf sleeper so the first stop is in flight):

```python
_DEAF_SLEEPER = [
    sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "import sys; print('up', flush=True); time.sleep(60)",
]


def test_second_lifecycle_op_while_busy_is_409(admin_client, app) -> None:
    sup = DaemonSupervisor(argv=_DEAF_SLEEPER, grace_seconds=1.0)
    app.state.daemon_supervisor = sup
    try:
        admin_client.post(
            "/v1/admin/daemon/start",
            headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/daemon/start")},
        )
        assert _poll_state(admin_client, SupervisorState.RUNNING)
        # First stop is now in flight (blocking on the 1s grace wait).
        admin_client.post(
            "/v1/admin/daemon/stop",
            headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/daemon/stop")},
        )
        r = admin_client.post(
            "/v1/admin/daemon/stop",
            headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/daemon/stop")},
        )
        assert r.status_code == 409
        assert _poll_state(admin_client, SupervisorState.STOPPED)
    finally:
        sup.stop()


def test_build_daemon_view_matches_get_route_shape(app, db_conn) -> None:
    app.state.daemon_supervisor = ExternalDaemonSupervisor()
    daemon_cfg = app.state.daemon_config
    with app.state.pool.connection() as conn:
        view = build_daemon_view(
            ExternalDaemonSupervisor(), conn,
            stale_seconds=daemon_cfg.heartbeat_stale_seconds,
        )
    assert view["state"] == SupervisorState.EXTERNAL
    assert view["supervise_daemon_externally"] is True
    assert view["heartbeats"] == []
    assert view["recent_log"] == []
```

Update `test_start_on_external_is_409` — it stays valid (external `request_start` raises). No change needed beyond confirming.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_daemon_routes.py -k "202 or busy or build_daemon_view" -x`
Expected: FAIL — `ImportError: cannot import name 'build_daemon_view'` (and 202 assertions fail against the current 200).

- [ ] **Step 3: Implement `build_daemon_view` + 202 + request_* in the router**

In `src/localmail/serve/admin/daemon_router.py`:

Add the response import:

```python
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
```

Extract the fusion into a module function and rewrite `get_daemon` to use it:

```python
def build_daemon_view(supervisor, conn, *, stale_seconds: int) -> dict:
    """Fuse supervisor process state + heartbeats + recent log into one view
    dict. Single source of truth shared by the JSON route and the HTML panel.

    Process state, heartbeats, and log are sampled independently (no global
    snapshot lock) — a read-only monitoring view; momentary skew is acceptable.
    """
    proc = status_to_dict(supervisor.status())
    status = daemon_svc.get_daemon_status(conn, stale_seconds=stale_seconds)
    return {
        **proc,
        "supervise_daemon_externally": proc["state"] == SupervisorState.EXTERNAL,
        "heartbeats": [_heartbeat_dict(hb) for hb in status.heartbeats],
        "recent_log": supervisor.recent_log_lines(),
    }


@router.get("/daemon")
def get_daemon(
    request: Request,
    admin: AdminUser = require_admin_session(),
) -> dict:
    supervisor = request.app.state.daemon_supervisor
    daemon_cfg = request.app.state.daemon_config
    pool = request.app.state.pool
    with pool.connection() as conn:
        return build_daemon_view(
            supervisor, conn, stale_seconds=daemon_cfg.heartbeat_stale_seconds
        )
```

Rewrite `_lifecycle` and the three routes to call `request_<op>` and return 202:

```python
def _lifecycle(
    request: Request, admin: AdminUser, csrf_token: str, op: str
) -> JSONResponse:
    check_csrf(request, admin, csrf_token, f"/v1/admin/daemon/{op}")
    supervisor = request.app.state.daemon_supervisor
    try:
        getattr(supervisor, f"request_{op}")()
    except SupervisorUnavailable as e:
        raise HTTPException(status_code=409, detail=str(e))
    return JSONResponse(status_to_dict(supervisor.status()), status_code=202)


@router.post("/daemon/start")
def start_daemon(
    request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = _CSRF_HEADER,
) -> JSONResponse:
    return _lifecycle(request, admin, x_csrf_token, "start")


@router.post("/daemon/stop")
def stop_daemon(
    request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = _CSRF_HEADER,
) -> JSONResponse:
    return _lifecycle(request, admin, x_csrf_token, "stop")


@router.post("/daemon/restart")
def restart_daemon(
    request: Request,
    admin: AdminUser = require_admin_session(),
    x_csrf_token: str = _CSRF_HEADER,
) -> JSONResponse:
    return _lifecycle(request, admin, x_csrf_token, "restart")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_daemon_routes.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/admin/daemon_router.py tests/test_serve_daemon_routes.py
git commit -m "feat(serve): daemon lifecycle routes return 202; busy-guard 409; build_daemon_view (2B.5, #146)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Control socket — dispatch to `request_*`

**Files:**
- Modify: `src/localmail/serve/daemon_control_socket.py`
- Test: `tests/test_daemon_control_socket.py`

- [ ] **Step 1: Update the failing test**

In `tests/test_daemon_control_socket.py`, add `time` to imports (already imported) and replace `test_dispatch_start_stop_real_child` with a poll-aware version:

```python
def _wait_state(sup, target, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sup.status().state == target:
            return
        time.sleep(0.02)
    raise AssertionError(f"never reached {target}; now {sup.status().state}")


def test_dispatch_start_stop_real_child() -> None:
    sup = DaemonSupervisor(argv=_SLEEPER, grace_seconds=2.0)
    try:
        r1 = handle_control_request(sup, {"cmd": "start"})
        assert r1["ok"] is True
        assert r1["status"]["state"] in (
            SupervisorState.STARTING, SupervisorState.RUNNING
        )
        _wait_state(sup, SupervisorState.RUNNING)
        r2 = handle_control_request(sup, {"cmd": "status"})
        assert r2["status"]["state"] == SupervisorState.RUNNING
    finally:
        handle_control_request(sup, {"cmd": "stop"})
        _wait_state(sup, SupervisorState.STOPPED)
    assert sup.status().state == SupervisorState.STOPPED
```

Add a test that dispatch returns immediately even for a slow (deaf) stop:

```python
_DEAF_SLEEPER = [
    sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print('up', flush=True); time.sleep(60)",
]


def test_dispatch_stop_returns_before_grace_elapses() -> None:
    sup = DaemonSupervisor(argv=_DEAF_SLEEPER, grace_seconds=3.0)
    try:
        handle_control_request(sup, {"cmd": "start"})
        _wait_state(sup, SupervisorState.RUNNING)
        started = time.monotonic()
        resp = handle_control_request(sup, {"cmd": "stop"})
        # Returned promptly (async), not after the 3s grace wait.
        assert time.monotonic() - started < 1.0
        assert resp["status"]["state"] == SupervisorState.STOPPING
        _wait_state(sup, SupervisorState.STOPPED)
    finally:
        sup.stop()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_control_socket.py -k "real_child or before_grace" -x`
Expected: FAIL — `test_dispatch_stop_returns_before_grace_elapses` blocks ~3s (sync stop) so the `< 1.0` assert fails; `real_child` may fail on the new transitional-state assertion.

- [ ] **Step 3: Implement async dispatch**

In `src/localmail/serve/daemon_control_socket.py`, grow the Protocol and switch dispatch:

```python
class _Supervisor(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def restart(self) -> None: ...
    def request_start(self) -> None: ...
    def request_stop(self) -> None: ...
    def request_restart(self) -> None: ...
    def status(self) -> SupervisorStatus: ...
    def recent_log_lines(self) -> list[str]: ...
```

In `handle_control_request`, change the lifecycle branch to call the async variants:

```python
    if cmd in ("start", "stop", "restart"):
        try:
            getattr(supervisor, f"request_{cmd}")()
        except SupervisorUnavailable as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "status": status_to_dict(supervisor.status())}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_control_socket.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/daemon_control_socket.py tests/test_daemon_control_socket.py
git commit -m "feat(serve): control socket dispatches lifecycle to request_* (2B.5, #146)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: CLI — poll-until-settled + `--no-wait`

**Files:**
- Modify: `src/localmail/daemon_cli.py`
- Test: `tests/test_daemon_cli.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_daemon_cli.py`, add (the socket client is mocked so no real server is needed):

```python
import localmail.serve.daemon_control_socket as ctl
from localmail.serve.daemon_supervisor import SupervisorState


def test_stop_polls_until_settled(db_conn, db_dsn, tmp_path, monkeypatch) -> None:
    cfg = _make_cfg(tmp_path, db_dsn, supervise=True)
    seq = iter([
        {"ok": True, "status": {"state": SupervisorState.STOPPING, "pid": 1, "started_at": None}},
        {"ok": True, "status": {"state": SupervisorState.STOPPING, "pid": 1, "started_at": None}},
        {"ok": True, "status": {"state": SupervisorState.STOPPED, "pid": None, "started_at": None}},
    ])

    def fake_send(path, request, *, timeout):
        if request["cmd"] == "stop":
            return {"ok": True, "status": {"state": SupervisorState.STOPPING, "pid": 1, "started_at": None}}
        return next(seq)

    monkeypatch.setattr(ctl, "send_control_request", fake_send)
    res = CliRunner().invoke(main, ["--config", str(cfg), "daemon", "stop"])
    assert res.exit_code == 0, res.output
    assert SupervisorState.STOPPED in res.output


def test_stop_no_wait_does_not_poll(db_conn, db_dsn, tmp_path, monkeypatch) -> None:
    cfg = _make_cfg(tmp_path, db_dsn, supervise=True)
    calls: list[str] = []

    def fake_send(path, request, *, timeout):
        calls.append(request["cmd"])
        return {"ok": True, "status": {"state": SupervisorState.STOPPING, "pid": 1, "started_at": None}}

    monkeypatch.setattr(ctl, "send_control_request", fake_send)
    res = CliRunner().invoke(main, ["--config", str(cfg), "daemon", "stop", "--no-wait"])
    assert res.exit_code == 0, res.output
    assert calls == ["stop"]  # no follow-up status polls
    assert SupervisorState.STOPPING in res.output


def test_stop_crashed_during_poll_exits_nonzero(db_conn, db_dsn, tmp_path, monkeypatch) -> None:
    cfg = _make_cfg(tmp_path, db_dsn, supervise=True)

    def fake_send(path, request, *, timeout):
        if request["cmd"] == "stop":
            return {"ok": True, "status": {"state": SupervisorState.STOPPING, "pid": 1, "started_at": None}}
        return {"ok": True, "status": {"state": SupervisorState.CRASHED, "pid": None, "started_at": None}}

    monkeypatch.setattr(ctl, "send_control_request", fake_send)
    res = CliRunner().invoke(main, ["--config", str(cfg), "daemon", "stop"])
    assert res.exit_code != 0
    assert "crashed" in res.output.lower()
```

> The existing `test_start_external_exits_nonzero` and
> `test_start_no_serve_exits_nonzero` stay valid (external check precedes the
> socket call; an unreachable socket raises before any poll).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_cli.py -k "polls or no_wait or crashed" -x`
Expected: FAIL — `--no-wait` is an unknown option / the stop command doesn't poll.

- [ ] **Step 3: Implement poll-until-settled**

In `src/localmail/daemon_cli.py`, add constants near the existing ones:

```python
_STATUS_TIMEOUT_S = 5.0
_LIFECYCLE_TIMEOUT_BUFFER_S = 5.0
# Gap between status polls while waiting for a lifecycle op to settle.
_LIFECYCLE_POLL_INTERVAL_S = 0.25
# Settle timeout for `start` (it never waits on the SIGTERM grace).
_START_SETTLE_TIMEOUT_S = 10.0

# Terminal state each op settles to.
_SETTLE_TARGET = {
    "start": "running",
    "restart": "running",
    "stop": "stopped",
}
```

Rewrite `_lifecycle` to send then poll, and add a `--no-wait` flag plumbed from the commands:

```python
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
        resp = send_control_request(
            sock, {"cmd": op}, timeout=settle_timeout
        )
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
```

Add the `SupervisorState` import at module top:

```python
from localmail.serve.daemon_supervisor import SupervisorState
```

> `SupervisorState` is a constants-only class with no IO, so importing it at
> module top is cheap and keeps the heavy imports deferred.

Add `--no-wait` to the three commands:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_cli.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/daemon_cli.py tests/test_daemon_cli.py
git commit -m "feat(cli): daemon start/stop/restart poll until settled, add --no-wait (2B.5, #146)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: CSRF method-bound mint helper

**Files:**
- Modify: `src/localmail/serve/admin/csrf.py`
- Test: `tests/test_admin_csrf.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_admin_csrf.py` (check the existing imports; add what's missing):

```python
from localmail.api.admin.csrf import verify_csrf_token
from localmail.serve.admin.csrf import csrf_action, csrf_token_context


def test_csrf_token_context_method_bound_round_trip() -> None:
    key = b"k" * 32
    ctx = csrf_token_context(user_id=7, key=key)
    token = ctx["csrf_token_for_method"]("POST", "/v1/admin/daemon/stop")
    # Verifies for the method-bound action it was minted for.
    verify_csrf_token(
        token, user_id=7,
        action=csrf_action("POST", "/v1/admin/daemon/stop"), key=key,
    )
    # And fails for a different method on the same path.
    import pytest
    from localmail.api.admin.csrf import CSRFError
    with pytest.raises(CSRFError):
        verify_csrf_token(
            token, user_id=7,
            action=csrf_action("GET", "/v1/admin/daemon/stop"), key=key,
        )


def test_csrf_token_context_legacy_single_arg() -> None:
    key = b"k" * 32
    ctx = csrf_token_context(user_id=7, key=key)
    token = ctx["csrf_token_for"]("/admin/logout")
    verify_csrf_token(token, user_id=7, action="/admin/logout", key=key)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_admin_csrf.py -k csrf_token_context -x`
Expected: FAIL — `ImportError: cannot import name 'csrf_token_context'`.

- [ ] **Step 3: Implement the helper**

In `src/localmail/serve/admin/csrf.py`, add `make_csrf_token` to the import and add the helper:

```python
from localmail.api.admin.csrf import CSRFError, make_csrf_token, verify_csrf_token
```

```python
def csrf_token_context(*, user_id: int, key: bytes) -> dict:
    """Jinja context helpers for minting CSRF tokens for one admin user.

    Returns two callables:
      * ``csrf_token_for(action)`` — legacy single-arg (non-method-bound), used
        by ``base.html``'s body-wide htmx header and the logout form.
      * ``csrf_token_for_method(method, action)`` — method-bound (#122/#125):
        the form HTML UIs MUST use this for any route guarded by ``check_csrf``,
        which binds the action to the request method via ``csrf_action``.

    Sharing the mint here keeps every admin HTML template deriving the identical
    bound string the verify side expects (reused by the daemon panel + future
    account screens, 2A.3).
    """
    def csrf_token_for(action: str) -> str:
        return make_csrf_token(user_id=user_id, action=action, key=key)

    def csrf_token_for_method(method: str, action: str) -> str:
        return make_csrf_token(
            user_id=user_id, action=csrf_action(method, action), key=key
        )

    return {
        "csrf_token_for": csrf_token_for,
        "csrf_token_for_method": csrf_token_for_method,
    }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_admin_csrf.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/admin/csrf.py tests/test_admin_csrf.py
git commit -m "feat(serve): csrf_token_context mint helper (legacy + method-bound) (2B.5, #125)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Admin panel router + templates + wiring

**Files:**
- Create: `src/localmail/serve/admin/daemon_panel_router.py`
- Create: `src/localmail/serve/admin/templates/daemon/panel.html`
- Create: `src/localmail/serve/admin/templates/daemon/_status.html`
- Modify: `src/localmail/serve/admin/static/admin.css`
- Modify: `src/localmail/serve/app.py`
- Test: `tests/test_serve_daemon_panel.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_serve_daemon_panel.py`:

```python
"""Admin daemon-control panel (2B.5): GET /admin/daemon page + the
/admin/_partials/daemon-status HTMX partial. Auth-gated; renders normal /
stale / external states; mutating controls carry method-bound CSRF tokens.
"""
from __future__ import annotations

import re

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.admin.csrf import CSRFError, verify_csrf_token
from localmail.api.auth import hash_password
from localmail.config import ServeConfig
from localmail.serve.admin.csrf import csrf_action
from localmail.serve.app import create_app
from localmail.serve.daemon_supervisor import ExternalDaemonSupervisor

_SIGNING_KEY = "x" * 43


@pytest.fixture
def serve_cfg() -> ServeConfig:
    return ServeConfig(
        session_signing_key=_SIGNING_KEY,
        state_signing_key="y" * 43,
        cookie_secure=False,
    )


@pytest.fixture
def app(db_dsn, serve_cfg):
    return create_app(db_dsn=db_dsn, serve_config=serve_cfg)


@pytest.fixture
def admin_user_id(db_conn: psycopg.Connection) -> int:
    pwh = hash_password("hunter2")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, %s, TRUE) RETURNING id",
            ("horst", pwh),
        )
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    return int(row[0])


@pytest.fixture
def admin_client(app, admin_user_id):
    client = TestClient(app, follow_redirects=False)
    form = client.get("/admin/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', form)
    assert m
    r = client.post(
        "/admin/login",
        data={"username": "horst", "password": "hunter2", "csrf_token": m.group(1)},
    )
    assert r.status_code == 303, r.text
    return client


def _seed_heartbeat(conn, *, stale: bool, error: str | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "imap_host, imap_port) VALUES "
            "('acct', 'a@example.com', 'password', 'imap.example.com', 993) "
            "RETURNING id"
        )
        aid = cur.fetchone()[0]
        beat = "now() - interval '1 hour'" if stale else "now()"
        cur.execute(
            f"INSERT INTO daemon_heartbeats (worker_kind, account_id, state, "
            f"current_folder, last_error_msg, started_at, last_heartbeat_at) "
            f"VALUES ('idle', %s, 'idle', 'INBOX', %s, now(), {beat})",
            (aid, error),
        )
    conn.commit()
    return aid


def test_panel_redirects_unauthenticated(app) -> None:
    client = TestClient(app, follow_redirects=False)
    r = client.get("/admin/daemon")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/login")


def test_panel_renders_authenticated(admin_client) -> None:
    r = admin_client.get("/admin/daemon")
    assert r.status_code == 200
    assert "Daemon control" in r.text
    assert 'id="daemon-status"' in r.text


def test_partial_shows_external_note_and_disables_buttons(admin_client, app) -> None:
    app.state.daemon_supervisor = ExternalDaemonSupervisor()
    r = admin_client.get("/admin/_partials/daemon-status")
    assert r.status_code == 200
    assert "supervised externally" in r.text.lower()
    # Lifecycle buttons are disabled in the external state.
    assert re.search(r"<button[^>]*disabled[^>]*>\s*Stop\s*</button>", r.text)


def test_partial_marks_stale_heartbeat(admin_client, db_conn) -> None:
    _seed_heartbeat(db_conn, stale=True)
    r = admin_client.get("/admin/_partials/daemon-status")
    assert r.status_code == 200
    assert "daemon-stale" in r.text


def test_partial_shows_last_error(admin_client, db_conn) -> None:
    _seed_heartbeat(db_conn, stale=False, error="boom: connection reset")
    r = admin_client.get("/admin/_partials/daemon-status")
    assert "boom: connection reset" in r.text


def test_stop_button_carries_method_bound_csrf(admin_client, admin_user_id) -> None:
    r = admin_client.get("/admin/_partials/daemon-status")
    # Extract the X-CSRF-Token from the Stop button's hx-headers.
    m = re.search(
        r'hx-post="/v1/admin/daemon/stop"[^>]*hx-headers=\'[^\']*'
        r'"X-CSRF-Token":\s*"([^"]+)"',
        r.text,
    )
    assert m, r.text
    token = m.group(1)
    key = _SIGNING_KEY.encode("ascii")
    verify_csrf_token(
        token, user_id=admin_user_id,
        action=csrf_action("POST", "/v1/admin/daemon/stop"), key=key,
    )
    with pytest.raises(CSRFError):
        verify_csrf_token(
            token, user_id=admin_user_id,
            action=csrf_action("GET", "/v1/admin/daemon/stop"), key=key,
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_daemon_panel.py -x`
Expected: FAIL — `GET /admin/daemon` 404s (router not mounted).

- [ ] **Step 3a: Create the panel router**

Create `src/localmail/serve/admin/daemon_panel_router.py`:

```python
"""Admin HTML daemon-control panel (2B.5).

`GET /admin/daemon` renders the full page; `GET /admin/_partials/daemon-status`
renders the self-polling status fragment (HTMX `hx-get … every Ns`). Both reuse
`daemon_router.build_daemon_view` for the fusion and `csrf_token_context` for
method-bound CSRF tokens on the mutating controls.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from localmail.api.admin.auth import AdminUser
from localmail.serve.admin.csrf import csrf_token_context
from localmail.serve.admin.daemon_router import build_daemon_view
from localmail.serve.admin.dependencies import require_admin_session

# HTMX poll cadence for the status partial (seconds). Named so the template
# carries no inline magic number.
DAEMON_PANEL_POLL_SECONDS = 2

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter()


def _render(name: str, request: Request, admin: AdminUser) -> HTMLResponse:
    s_key = request.app.state.serve_config.session_signing_key.encode("ascii")
    supervisor = request.app.state.daemon_supervisor
    daemon_cfg = request.app.state.daemon_config
    pool = request.app.state.pool
    with pool.connection() as conn:
        view = build_daemon_view(
            supervisor, conn, stale_seconds=daemon_cfg.heartbeat_stale_seconds
        )
    context = {
        "current_user": admin,
        "flashes": [],
        "view": view,
        "poll_seconds": DAEMON_PANEL_POLL_SECONDS,
        **csrf_token_context(user_id=admin.id, key=s_key),
    }
    return templates.TemplateResponse(request=request, name=name, context=context)


@router.get("/daemon", response_class=HTMLResponse)
def daemon_panel(
    request: Request, admin: AdminUser = require_admin_session()
) -> HTMLResponse:
    return _render("daemon/panel.html", request, admin)


@router.get("/_partials/daemon-status", response_class=HTMLResponse)
def daemon_status_partial(
    request: Request, admin: AdminUser = require_admin_session()
) -> HTMLResponse:
    return _render("daemon/_status.html", request, admin)
```

- [ ] **Step 3b: Create the page template**

Create `src/localmail/serve/admin/templates/daemon/panel.html`:

```html
{% extends "base.html" %}
{% block title %}Daemon — localmail admin{% endblock %}
{% block content %}
<h1>Daemon control</h1>
<div class="admin-card">
  {% include "daemon/_status.html" %}
</div>
{% endblock %}
```

- [ ] **Step 3c: Create the status fragment**

Create `src/localmail/serve/admin/templates/daemon/_status.html`. The root
`#daemon-status` div self-polls (`hx-swap="outerHTML"` replaces itself, so the
replacement keeps the trigger):

```html
<div id="daemon-status"
     hx-get="/admin/_partials/daemon-status"
     hx-trigger="every {{ poll_seconds }}s"
     hx-swap="outerHTML">
  <section class="daemon-process">
    <h2>Process</h2>
    <p>State: <strong>{{ view.state }}</strong>
      {% if view.pid %}(pid {{ view.pid }}){% endif %}
      {% if view.started_at %}— started {{ view.started_at }}{% endif %}
    </p>
    <div class="daemon-controls">
      {% if view.supervise_daemon_externally %}
        <p class="daemon-note">Daemon is supervised externally; start / stop /
          restart are managed by your init system, not here.</p>
        <button type="button" disabled>Start</button>
        <button type="button" disabled>Stop</button>
        <button type="button" disabled>Restart</button>
      {% else %}
        <button type="button" hx-post="/v1/admin/daemon/start" hx-swap="none"
          hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/v1/admin/daemon/start") }}"}'>Start</button>
        <button type="button" hx-post="/v1/admin/daemon/stop" hx-swap="none"
          hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/v1/admin/daemon/stop") }}"}'>Stop</button>
        <button type="button" hx-post="/v1/admin/daemon/restart" hx-swap="none"
          hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/v1/admin/daemon/restart") }}"}'>Restart</button>
      {% endif %}
      <button type="button" hx-post="/v1/admin/daemon/reload" hx-swap="none"
        hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/v1/admin/daemon/reload") }}"}'>Reload now</button>
    </div>
  </section>

  <section class="daemon-workers">
    <h2>Workers</h2>
    {% if view.heartbeats %}
    <table class="daemon-heartbeats">
      <thead>
        <tr><th>Worker</th><th>Account</th><th>State</th><th>Folder</th>
            <th>Last heartbeat</th><th>Error</th><th></th></tr>
      </thead>
      <tbody>
        {% for hb in view.heartbeats %}
        <tr class="{% if hb.stale %}daemon-stale{% endif %}">
          <td>{{ hb.worker_kind }}</td>
          <td>{{ hb.account_id or "—" }}</td>
          <td>{{ hb.state }}</td>
          <td>{{ hb.current_folder or "—" }}</td>
          <td>{{ hb.last_heartbeat_at }}{% if hb.stale %} <span class="daemon-stale-tag">stale</span>{% endif %}</td>
          <td>{{ hb.last_error_msg or "" }}</td>
          <td>
            {% if hb.account_id %}
            <button type="button"
              hx-post="/v1/admin/accounts/{{ hb.account_id }}/restart-sync"
              hx-swap="none"
              hx-headers='{"X-CSRF-Token": "{{ csrf_token_for_method("POST", "/v1/admin/accounts/" ~ hb.account_id ~ "/restart-sync") }}"}'>Restart sync</button>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <p>No heartbeats recorded.</p>
    {% endif %}
  </section>

  <section class="daemon-log">
    <h2>Recent log</h2>
    <pre>{% for line in view.recent_log %}{{ line }}
{% endfor %}</pre>
  </section>
</div>
```

- [ ] **Step 3d: Add CSS**

Append to `src/localmail/serve/admin/static/admin.css`:

```css
.daemon-controls { margin: 0.75rem 0; display: flex; gap: 0.5rem; flex-wrap: wrap; }
.daemon-note { color: #8a6d00; font-style: italic; }
.daemon-heartbeats { border-collapse: collapse; width: 100%; }
.daemon-heartbeats th, .daemon-heartbeats td { padding: 0.25rem 0.5rem; text-align: left; border-bottom: 1px solid #ddd; }
.daemon-stale td { color: #b00020; }
.daemon-stale-tag { color: #b00020; font-weight: bold; }
.daemon-log pre { background: #f4f4f4; padding: 0.5rem; overflow-x: auto; max-height: 16rem; }
```

- [ ] **Step 3e: Wire the router in `app.py`**

In `src/localmail/serve/app.py`, add the import next to the other admin routers:

```python
from localmail.serve.admin import daemon_panel_router as admin_daemon_panel_router
```

In the admin block (after `app.include_router(admin_dashboard_router.router, prefix="/admin")`):

```python
        app.include_router(admin_daemon_panel_router.router, prefix="/admin")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_daemon_panel.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/admin/daemon_panel_router.py \
        src/localmail/serve/admin/templates/daemon/ \
        src/localmail/serve/admin/static/admin.css \
        src/localmail/serve/app.py tests/test_serve_daemon_panel.py
git commit -m "feat(serve): admin daemon-control panel (2B.5)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Full verification, mypy, docs

**Files:**
- Modify: `README.md`, `CLAUDE.md`

- [ ] **Step 1: Full suite + mypy**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/`
Expected: PASS (1193 prior + the new tests; no regressions).

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: clean (now 84 files — the new panel router).

Fix any failures before continuing.

- [ ] **Step 2: Update README.md**

In the daemon-control CLI section, document the new behaviour: `localmail daemon
{start,stop,restart}` now poll until the daemon settles (running/stopped) and
accept `--no-wait` to return immediately; the routes return 202. Add a line that
the admin UI exposes a daemon-control panel at `/admin/daemon`.

- [ ] **Step 3: Update CLAUDE.md**

Extend the **2B.4** bullet (or add a **2B.5** bullet) noting: lifecycle ops are
now async (`request_*` on the supervisor; routes return 202; CLI polls to
settle; `--no-wait`); the admin panel at `/admin/daemon` +
`/admin/_partials/daemon-status` (HTMX self-poll every
`DAEMON_PANEL_POLL_SECONDS`); `build_daemon_view` is the shared fusion; the
method-bound CSRF mint helper `csrf_token_context` (legacy + method-bound) is
the reusable #125 helper. Confirm the migrations line still reads latest =
`0024` (no new migration).

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: 2B.5 daemon panel + async lifecycle (#146, #125)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 5: Push + open PR**

```bash
git push -u origin daemon-control-2b5-panel-async
gh pr create --fill --base main
```

---

## Self-review

**Spec coverage:**
- Part A (supervisor async + busy-guard) → Task 1. ✓
- Part B (202 + 409 + `build_daemon_view`) → Task 2; control socket → Task 3. ✓
- Part C (CLI poll-until-settled + `--no-wait`, named constants) → Task 4. ✓
- Part D (panel router, page + partial, status table stale/error, button gating, Plane A/B buttons, method-bound CSRF) → Tasks 5 + 6. ✓
- #125 reusable mint helper → Task 5 (`csrf_token_context`). ✓
- No new migration → Task 7 confirms. ✓
- Tests for normal/stale/error/external + CSRF method binding → Task 6. ✓

**Placeholder scan:** none — every step carries full code/commands.

**Type/name consistency:** `request_start/stop/restart`, `_lifecycle_in_flight`,
`_spawn_lifecycle`, `build_daemon_view(supervisor, conn, *, stale_seconds)`,
`csrf_token_context(*, user_id, key)` → keys `csrf_token_for` /
`csrf_token_for_method`, `DAEMON_PANEL_POLL_SECONDS`, `_SETTLE_TARGET`,
`_LIFECYCLE_POLL_INTERVAL_S`, `_START_SETTLE_TIMEOUT_S` — all used consistently
across tasks. The panel template uses `csrf_token_for_method` (from the context
helper) and `view.*` keys matching `build_daemon_view`'s output
(`state`, `pid`, `started_at`, `supervise_daemon_externally`, `heartbeats`,
`recent_log`) and `_heartbeat_dict` keys (`worker_kind`, `account_id`, `state`,
`current_folder`, `last_error_msg`, `last_heartbeat_at`, `stale`). ✓

**Risk note:** route/socket lifecycle tests use real subprocesses with a
SIGTERM-ignoring child for the busy-guard window; timing tolerances (1s grace,
6s poll) are generous to avoid CI flakiness.
```
