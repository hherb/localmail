# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-06-06 (MCP server / search Phase 3 — PR open).**
> This session designed, planned, and implemented the **remote HTTP MCP
> server** (search Phase 3): a multi-user, ACL-scoped MCP server mounted into
> `localmail serve` at `/mcp` over **Streamable HTTP**, exposing five read-only
> tools to AI agents. Built TDD via subagent-driven development (implementer +
> spec review + code-quality review per task; final holistic review =
> **Ready to merge**). Work is on branch `mcp-server-phase3`, pushed and open as
> **PR #164** (https://github.com/hherb/localmail/pull/164), **CI pending at
> handoff time**. `main` is at `cef43c2` (not yet merged). **Local: full suite
> 1431 passed** (only the pre-existing macOS `test_daemon_control_socket`
> AF_UNIX-path-too-long failures excluded), **mypy clean (103 files)**, all
> new/touched files **ruff-clean**. **No new migration** (latest is still
> `0026_import_jobs.sql`).
>
> **Also at session start:** confirmed the prior handoff's "immediate" task was
> already done — **PR #161 (2A.5 imports) was already merged** into `main`
> (`cef43c2`); the `admin-ui-2a5-imports` branch was already deleted.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The **database is canonical for accounts**
end-to-end. Daemon: hot-reload account set, heartbeats, DB command queue,
two-plane supervision, non-blocking lifecycle + admin panel (2B.1–2B.5). Admin
UI: account CRUD (2A.3), user management (2A.4), archive imports (2A.5). Hybrid
search (Phases 1+2) + an HTTPS GUI server are shipped. **This session adds the
MCP server (search Phase 3).** A Tauri + Svelte GUI lives under `gui/`. See
[CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we did this session

### MCP server / search Phase 3 (branch `mcp-server-phase3`)

A remote, multi-user, ACL-scoped MCP server mounted into `serve` at `/mcp` over
Streamable HTTP. Brainstorm → spec → plan → TDD implementation (11 tasks, fresh
subagent + two-stage review each).

- Design: `docs/superpowers/specs/2026-06-05-mcp-server-design.md` (`1956b06`)
- Plan: `docs/superpowers/plans/2026-06-05-mcp-server.md` (`fa3599b`)
- Usage guide: `docs/mcp-usage.md`

**Architecture.** New `src/localmail/mcp/` package; tools call the existing
transport-free `localmail.api` accessors directly (no HTTP hop), reusing
`app.state.pool` and `app.state.searcher`.

- `mcp/auth.py` — `LocalmailTokenVerifier` wraps `api.auth.verify_token`
  (opaque bearer, reuses `api_tokens`); offloads the blocking DB lookup off the
  event loop via `anyio.to_thread`; carries the user id in `AccessToken.subject`.
- `mcp/tools.py` — five thin, ACL-scoped tool bodies over the `api/` accessors.
- `mcp/server.py` — `FastMCP(token_verifier=…, auth=AuthSettings(issuer_url,
  resource_server_url, required_scopes=[]), stateless_http=True,
  json_response=True, streamable_http_path="/")`; registers `search`,
  `get_message`, `get_attachment`, `list_messages`, `list_accounts`; maps
  `SearchCursorExpired`/`NotFound`/`ValidationFailed` → clean `ToolError`.
- `serve/app.py` — `create_app(enable_mcp=…, mcp_config=…)`: conditional mount
  at `/mcp` (gated by `enable_mcp` + the importable `[mcp]` extra), session
  manager started in the lifespan (`async with mcp_server.session_manager.run()`,
  `nullcontext()` when off), teardown unconditional.
- `config.py` — `McpConfig` (`[mcp]`: `enabled` default false, `issuer_url`/
  `resource_server_url` as `AnyHttpUrl` so a bad URL fails at config-load, not
  serve startup). `cli.py` serve forwards `cfg.mcp`.
- `pyproject.toml` — optional `[mcp]` extra (`mcp>=1.13.0`; resolves to 1.27.2).

**Auth = opaque bearer**: agents get a token via `POST /v1/auth/login`
(response `{token, expires_at}`; `/v1/auth/refresh` rotates it) and pass
`Authorization: Bearer …` to `/mcp`. **Raw attachment bytes are NOT exposed
over MCP** — only extracted text/metadata; byte download stays the HTTP
`/v1/attachments/{sha256}` route.

**Three design reconciliations vs the spec** (all narrow scope, none cut
capability): (1) **no `wire.py`** — the `api/` layer already returns the
wire-shaped dicts; (2) **one `search` tool, not three** — `run_search` takes a
single optional `cursor` and auto-grows the pool, paging = re-call with
`next_cursor`; (3) **`get_message(full_headers=…)`** not
`include_body`/`include_attachments`.

**Implementation commits (TDD, each spec- + quality-reviewed):**
`a0f03ac` [mcp] extra + McpConfig · `2a58969`/`7295a99` TokenVerifier
(+event-loop offload, subject) · `e7818b0` tool_search · `f2f9b3f`
message/browse/accounts bodies · `c0a9b34` get_attachment · `0963115`/`15bc9a4`/
`1364e8b` FastMCP server (+ValidationFailed mapping, auth guard, Literal mode,
limit clamp) · `f66ebe1`/`7a931bb` mount + lifespan · `f9f5c84` serve CLI ·
`078c57a`/`24d3017`/`d7670ae` e2e integration test · `60beec6`/`1782ab3` docs ·
`dff9f7d` McpConfig URL validation (final-review fix).

### Verification (this session)
```bash
unset VIRTUAL_ENV && uv run pytest -q tests/ --deselect tests/test_daemon_control_socket.py
                                                 # 1431 passed (+ ~31 new MCP tests)
unset VIRTUAL_ENV && uv run mypy src/localmail   # clean, 103 files
unset VIRTUAL_ENV && uv run ruff check src/localmail/mcp tests/test_mcp_*.py   # clean
```

## What's next

### 0. **Merge PR #164 for the MCP server** *(immediate)*
```bash
gh pr checks 164                          # let CI finish
gh pr merge 164 --squash --delete-branch
git checkout main && git pull
```
Note: CI runs the full suite. The macOS-only `test_daemon_control_socket`
AF_UNIX failures are a LOCAL env issue (long tmp socket path) — CI on Linux
should not hit them. If CI surfaces anything, it's real.

### 1. **MCP follow-ups (filed-as-notes, low priority; non-blocking).**
   - **Full OAuth 2.1 discovery (Approach B)** — the v1 server uses opaque
     bearer (clients configure the token directly). A spec-strict MCP client
     that *requires* the OAuth discovery dance (protected-resource-metadata,
     `WWW-Authenticate` auto-negotiation) won't auto-connect. Add the discovery
     surface if such a client appears. (Design spec §"Out of scope".)
   - **Richer per-tool docstrings** — the tool docstrings become the agent-facing
     descriptions; current ones are accurate but thin on *when to use each tool*.
   - **`streamable_http_client` rename** — the integration test uses the
     deprecated `streamablehttp_client` (works; the non-deprecated
     `streamable_http_client` has a *different* signature — no `headers` kwarg —
     so it's not a drop-in; needs an `httpx.AsyncClient` rewrite). Revisit on a
     future `mcp` bump.
   - **`--smart` query expansion** (search Phase 4) — separate design/plan.
   - Each non-trivial item needs its own brainstorm → spec → plan.

### 2. **Other open issues** *(triage)*
   - **#163** imports: progress flush + cooperative cancel only act on
     `checkpoint_every` boundaries (time-based cadence + flush-after-first).
   - **#162** imports: serve-startup reconcile can wrongly fail a concurrent CLI
     import (needs `owner_host`/`owner_pid` or `supervised` column → migration
     0027).
   - **#90** (glib via Tauri) and **#25** (websockets/uvicorn depwarn) — both
     upstream-blocked, not actionable.

## Open decisions & risks
1. **PR #164 open, not yet merged** — `main` at `cef43c2`. Branch HEAD `dff9f7d`,
   CI pending at handoff. First action next session: confirm CI green + merge (§0).
2. **Auth is opaque-bearer, not spec-strict OAuth 2.1.** Deliberate v1 decision
   (confirmed with user). `FastMCP` still requires `AuthSettings` with
   `issuer_url`/`resource_server_url` — defaulted to `http://localhost:8443`;
   operators should set them to the public serve URL (advertised in the SDK's
   resource-metadata; opaque-bearer clients ignore them). Approach B is the
   escape hatch.
3. **MCP is opt-in and off by default.** Requires BOTH `uv sync --extra mcp`
   AND `[mcp] enabled = true`. Extra absent → serve runs, logs INFO, skips mount.
4. **`mcp` pinned `>=1.13.0`, resolves to 1.27.2.** The SDK requires `auth=`
   whenever `token_verifier=` is set; `AccessToken` fields used:
   `token`/`client_id`/`scopes`/`subject`. `CallToolResult` payload arrives via
   `structuredContent` (top-level list wrapped under `"result"`). A future major
   `mcp` bump could move these — the integration test is the canary.
5. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
   locally on macOS (`AF_UNIX path too long`, long tmp socket path); pre-existing,
   present on `main`, env-specific. Excluded from the local gate; CI on Linux is
   the real signal.
6. **No ROADMAP.md** in this repo *(carried)* — slice status lives in
   NEXT_SESSION/handoffs + the specs. The `/nextsession` ROADMAP step is a no-op.
7. **`.claude/` local files** stay untracked, by design (incl.
   `.claude/scheduled_tasks.lock`).

## Exact commands to resume
```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # on mcp-server-phase3, clean (ignore .claude/*.lock)
git branch -vv                           # main (cef43c2) + mcp-server-phase3 (dff9f7d)
git --no-pager log --oneline -8
gh pr list --state open                  # #164 (MCP server)
gh pr checks 164                          # CI status before merging
gh issue list --state open --limit 40    # #163, #162, #90, #25

# Confirm the suite (the live, canonical signal this session):
unset VIRTUAL_ENV && uv sync --extra mcp                          # MCP tests need the extra
unset VIRTUAL_ENV && uv run pytest -q tests/ --deselect tests/test_daemon_control_socket.py
                                                                 # expect 1431 passed
unset VIRTUAL_ENV && uv run mypy src/localmail                   # expect clean, 103 files
```

After PR #164 merges, pick the next work (brainstorm → spec → plan FIRST):
```bash
git checkout main && git pull
ls migrations/    # latest is 0026_import_jobs.sql; next free slot 0027_*.sql
```

`main` at `cef43c2` (== `origin/main`). Branch `mcp-server-phase3` pushed
(HEAD `dff9f7d`), open as **PR #164**. Working tree clean apart from the
untracked `.claude/scheduled_tasks.lock`. **No migration added this session.**
