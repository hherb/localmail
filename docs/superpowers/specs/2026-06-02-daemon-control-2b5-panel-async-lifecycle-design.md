# 2B.5 — Admin daemon-control panel + 202-async lifecycle (#146)

> Design for the **final 2B slice**: the admin HTML daemon-control panel
> (spec §2B.5) plus the folded-in #146 fix (long lifecycle ops no longer
> occupy a request/socket worker). One branch, one PR. No new migration.

## Context

2B.1–2B.4 made the database canonical for accounts and gave the daemon two
control planes: **Plane A** (DB-mediated `reload` / per-account `restart-sync`
via the `daemon_commands` queue) and **Plane B** (process lifecycle via the
in-process `DaemonSupervisor`, reachable over a Unix control socket). 2B.4
shipped the JSON routes (`/v1/admin/daemon*`), the CLI (`localmail daemon …`),
and the supervisor, but:

1. There is **no admin HTML UI** — the `/admin/daemon` nav link in
   [base.html](../../../src/localmail/serve/admin/templates/base.html) 404s.
2. **`DaemonSupervisor.stop()` / `restart()` block** for up to
   `[daemon] shutdown_grace_seconds` (default 30 s). The sync route handlers
   and the control-socket connection handler call them inline, so a long stop
   pins a Starlette threadpool worker / socket handler thread for the duration
   ([#146](https://github.com/hherb/localmail/issues/146)).

This slice closes both, and is the first new admin HTML UI to mint
**method-bound CSRF tokens** ([#125](https://github.com/hherb/localmail/issues/125)).

## Goals

- Admin panel at `GET /admin/daemon`: status table + lifecycle/Plane-A buttons,
  HTMX-polled status partial.
- Lifecycle ops (`start` / `stop` / `restart`) run on a supervisor-owned thread;
  routes and the control socket return **immediately** with the *transitional*
  status. Settle is observed by polling.
- Reusable method-bound CSRF mint helper (#125), shared with future admin HTML
  (2A.3 account screens).

## Non-goals

- Auto-restart-on-crash, multi-host, per-thread metrics, admin audit log
  (unchanged 2B out-of-scope).
- Content-negotiating the `/v1/admin/*` JSON endpoints to return HTML — they
  stay pure machine-JSON; the panel polls a dedicated HTML partial.

---

## Part A — Supervisor: async lifecycle thread

`DaemonSupervisor` grows three non-blocking request methods alongside the
existing blocking ones (which stay as the thread bodies and for unit tests):

- `request_start()`, `request_stop()`, `request_restart()`.

Each, under `_lock`:

1. Validates state. If a lifecycle op is **already in flight** (a lifecycle
   thread is alive / state is transitional), raise `SupervisorUnavailable`
   ("a lifecycle operation is already in progress") — the **busy-guard**.
2. Sets the **transitional** state synchronously (`starting` for start/restart,
   `stopping` for stop) so the very next `status()` reflects it.
3. Spawns **one** dedicated lifecycle thread (`name="daemon-supervisor-lifecycle"`,
   daemon) running the corresponding blocking body (`start` / `stop` /
   `restart`), then returns.

The lifecycle thread runs the existing blocking logic unchanged
(`stop()` already releases `_lock` before the grace wait; `restart()` =
stop-then-start on the one thread — no nested spawn). On completion the
blocking body sets the terminal state (`running` / `stopped`) under `_lock` as
today.

**State-machine invariants (preserved):**

- The reader thread flips `running → crashed` only when state is still
  `running` at EOF; an intentional stop sets `stopping` *before* SIGTERM, so a
  clean shutdown is never misread as a crash. The async path keeps this because
  `request_stop()` sets `stopping` synchronously before spawning the thread.
- `request_start()` is valid from `stopped` / `crashed`; `request_stop()` from
  `running` / `crashed` (and a no-op fast-path from `stopped`);
  `request_restart()` from `running` / `crashed` / `stopped`. A request that
  arrives while transitional hits the busy-guard → `SupervisorUnavailable`.

**Tracking the in-flight thread:** a `self._lifecycle_thread: threading.Thread |
None`. The busy-guard is `thread is not None and thread.is_alive()`. Set under
`_lock` in the request methods; the thread clears nothing (liveness is the
signal). `close()` (serve shutdown) keeps calling the **blocking** `stop()`
directly — teardown wants to block until the child is dead, not fire-and-forget.

`ExternalDaemonSupervisor` grows `request_start/stop/restart` that raise
`SupervisorUnavailable` exactly like its blocking trio.

**Determinism in tests:** the lifecycle thread is driven with a fake
`Popen`-like object (no real grace sleep); tests `join()` the lifecycle thread
(exposed via a test-only `_join_lifecycle(timeout)` helper, or by polling
`status()`), assert the transitional state is visible *before* the body
completes (use an `Event` the fake proc blocks on), then assert the terminal
state after.

---

## Part B — Routes & control socket: 202 + status

### HTTP ([daemon_router.py](../../../src/localmail/serve/admin/daemon_router.py))

- `_lifecycle(...)` calls `request_<op>()` instead of `<op>()`, and the three
  routes return **`JSONResponse(status_to_dict(...), status_code=202)`**.
  `SupervisorUnavailable` (external stub **or** busy-guard) → **409** (unchanged
  mapping; now also covers the busy case).
- `GET /v1/admin/daemon` shape is unchanged. The proc/heartbeats/log fusion in
  its body is **extracted** into a shared builder so the HTML partial reuses it:

  ```python
  def build_daemon_view(supervisor, conn, *, stale_seconds) -> dict
  ```

  returning `{**proc, "supervise_daemon_externally": …, "heartbeats": [...],
  "recent_log": [...]}`. The JSON route becomes a thin wrapper; the HTML partial
  consumes the same dict. Single source of truth for the view shape.

### Control socket ([daemon_control_socket.py](../../../src/localmail/serve/daemon_control_socket.py))

- `handle_control_request` dispatches `start/stop/restart` to
  `request_<cmd>()` instead of `<cmd>()`, returning the transitional status
  immediately. This also removes the socket-handler-thread occupancy (a 30 s
  stop no longer pins the per-connection handler thread).
- The `_Supervisor` Protocol grows `request_start/stop/restart`.

---

## Part C — CLI: poll-until-settled ([daemon_cli.py](../../../src/localmail/daemon_cli.py))

`localmail daemon {start,stop,restart}`:

- Send `{"cmd": op}` (now non-blocking server-side), then **poll
  `{"cmd": "status"}`** over the socket until the state settles or the timeout
  elapses, then print the final state.
  - Settle target: `running` for start/restart, `stopped` for stop.
  - `crashed` (or `external`, defensively) → print + exit non-zero.
- `--no-wait`: send the command, print the immediate transitional status, don't
  poll.
- **Named constants** (no magic numbers):
  - `_LIFECYCLE_POLL_INTERVAL_S` — gap between status polls.
  - settle timeout = `shutdown_grace_seconds + _LIFECYCLE_TIMEOUT_BUFFER_S` for
    stop/restart (reuse the existing buffer); `_START_SETTLE_TIMEOUT_S` for
    start (it never waits on grace).
  - The per-poll socket read timeout reuses `_STATUS_TIMEOUT_S`.
- External-supervised / unreachable-socket handling is unchanged (clear
  non-zero `ClickException`).

---

## Part D — Admin UI panel

### Router — `serve/admin/daemon_panel_router.py` (mounted at `/admin`)

- `GET /admin/daemon` → full page `templates/daemon/panel.html` (extends
  `base.html`): a dashboard card containing the status table + control buttons.
  Polls the partial via `hx-get="/admin/_partials/daemon-status"
  hx-trigger="every {{ poll_seconds }}s"` and includes the partial inline for
  first paint (no empty flash).
- `GET /admin/_partials/daemon-status` → renders `templates/daemon/_status.html`
  (the table + buttons fragment) from `build_daemon_view(...)`.
- Both are admin-gated via `require_admin_session()`; both build the
  `csrf_token_for` context helper (below). Poll cadence is a **named constant**
  `DAEMON_PANEL_POLL_SECONDS` passed into the context — no inline magic number
  in the template.

### Templates — `templates/daemon/{panel.html,_status.html}`

`_status.html` renders:

- **Process row:** supervisor `state` (+ `pid`, `started_at`), recent-log tail
  (`<pre>`, last N lines from `recent_log`).
- **Heartbeat table:** one row per heartbeat — worker kind, account, state,
  current folder, **heartbeat age** (rendered with a `stale` CSS class →
  red, driven by the server-computed `stale` flag so there's no client clock
  dependency), last error.
- **Buttons:**
  - Plane B: **Start / Stop / Restart** — `hx-post` to
    `/v1/admin/daemon/{start,stop,restart}`, `hx-swap="none"`. **Disabled with
    an inline note** when `supervise_daemon_externally` is true (gate on the
    wire field, not config).
  - Plane A: **Reload now** → `hx-post /v1/admin/daemon/reload`; per-account
    **Restart sync** → `hx-post /v1/admin/accounts/{id}/restart-sync` (one
    button per heartbeat account row). These stay enabled even when externally
    supervised (they only enqueue).

The table self-refreshes every `DAEMON_PANEL_POLL_SECONDS`; after a button POST
the next poll reflects the transitional → terminal progression (≤ poll interval
lag, immaterial for multi-second ops). Endpoint-pure event-triggered refresh is
a noted optional follow-up, not in this cut.

### Method-bound CSRF (#125)

`check_csrf` verifies against `csrf_action(request.method, action)` =
`"POST:/v1/admin/daemon/stop"`. The body-wide `hx-headers='{"X-CSRF-Token":
csrf_token_for("htmx")}'` token in `base.html` is **not** method-bound and won't
satisfy these routes. So:

- Add a context helper `csrf_token_for(method, action)` that mints
  `make_csrf_token(user_id, action=csrf_action(method, action), key)` — it
  reuses the existing pure `csrf_action` from
  [serve/admin/csrf.py](../../../src/localmail/serve/admin/csrf.py) so mint and
  verify derive the identical bound string. This is the **reusable #125 mint
  helper** to be shared with 2A.3.
- Each button sets its own `hx-headers='{"X-CSRF-Token":
  "{{ csrf_token_for("POST", "/v1/admin/daemon/stop") }}"}'` (per-control, since
  the action string differs per button, and the per-account restart-sync token
  is bound to that account's URL).

To avoid two `csrf_token_for` signatures colliding (the existing dashboard
lambda takes a single `action`), the new helper is named distinctly —
`csrf_token_for_method(method, action)` — and the legacy single-arg
`csrf_token_for(action)` is left intact for the body-wide `htmx`/logout tokens.

---

## Testing (all TDD)

**Supervisor** (`tests/test_daemon_supervisor.py`):
- `request_start/stop/restart` set the transitional state synchronously and
  return before the (fake-proc-blocked) body completes.
- Busy-guard: a second `request_*` while one is in flight raises
  `SupervisorUnavailable`.
- Terminal state after the lifecycle thread joins (`running` / `stopped`).
- An intentional async stop is **not** misread as `crashed`.
- `ExternalDaemonSupervisor.request_*` raise `SupervisorUnavailable`.

**Routes** (`tests/test_serve_daemon_routes.py`):
- start/stop/restart return **202** + transitional status (update existing
  200-expecting assertions).
- 409 on the external stub **and** on the busy-guard.
- `build_daemon_view` shared helper produces the same dict the JSON route emits.
- CSRF + admin gating unchanged.

**Control socket** (`tests/test_daemon_control_socket.py`):
- `handle_control_request` start/stop/restart dispatch to `request_*` and return
  immediately with transitional status (no block on a fake slow stop).

**CLI** (`tests/test_daemon_cli.py`):
- start/stop/restart poll until settled, printing the terminal state.
- `--no-wait` prints the transitional state without polling.
- settle timeout → non-zero; `crashed` → non-zero.
- external / unreachable notes unchanged.

**Panel** (`tests/test_serve_daemon_panel.py`, new):
- `GET /admin/daemon` redirects unauthenticated; renders authenticated.
- Partial renders with **normal / stale / error / external** states (stale row
  carries the stale class; external disables the lifecycle buttons with a note).
- Per-control method-bound CSRF token present and bound to the correct
  `"<METHOD>:<url>"` action (assert the minted token verifies for the bound
  action and **fails** for a different method/url).

No magic numbers (poll interval, settle timeouts, poll cadence are named
constants). Files kept < 500 lines; the new panel router + templates are
small and focused.

## Migration

**None.** Reuses `0023_daemon_heartbeats.sql` + `0024_daemon_commands.sql`.
Latest applied is `0024`; re-check `ls migrations/` at plan time.

## Risks & decisions

1. **Busy-guard via thread liveness, not a separate flag** — one source of
   truth; `status()` already refreshes crash detection defensively, so a
   crashed-mid-op child still surfaces correctly on the next poll.
2. **`close()` stays blocking** — serve shutdown must wait for the child to die;
   only the operator-facing routes/socket/CLI go async.
3. **JSON endpoints stay pure** — the panel polls a dedicated HTML partial; we
   do not content-negotiate `/v1/admin/*`. Preserves the machine/JSON boundary
   (cf. the cookie-scope invariant test).
4. **Two `csrf_token_for` helpers** (single-arg legacy + method-bound new) to
   avoid breaking `base.html`'s body-wide/logout tokens while introducing #125's
   method-bound mint. The method-bound one is the reusable helper for 2A.3.
5. **202 vs 200** — all three lifecycle routes return 202 (uniform contract);
   the CLI/UI poll to settle. Updates existing route tests.
