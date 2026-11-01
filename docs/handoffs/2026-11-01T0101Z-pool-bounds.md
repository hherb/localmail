# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-01 (session 69).** `main` was **`b07b78e`** at
> session start (#423, closing #416, had merged).
>
> This session opened **one PR, #424**, on `fix/414-bound-pool-connections`.
> It has one code commit (`c4fc08f`) and this handoff. It is **part of #414**:
> the issue stays open for the watchdog.
>
> **Open issues: 34.**
>
> **Leftover worktree `.claude/worktrees/bottie-bearer-token-dbc203`**
> (detached at `5b85180`). Untouched; ask before removing it.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth or password) into Postgres, and
is **strictly read-only w.r.t. IMAP**. The database is canonical for accounts.

- **Daemon** (IDLE + poll per account), **web admin UI** (HTMX).
- **Hybrid search**, the HTTPS `/v1` server, a remote **MCP server**, and the
  opt-in `--smart` rewriter.
- **Tauri 2 + Svelte 5 GUI** under `gui/`.
- Version **0.3.0**, Python **3.13** pinned (CI tests 3.12 + 3.13),
  AGPL-3.0-or-later.
- **kastellan** consumes `/v1` over REST with an API key. Only slice C
  remains.

## What we shipped this session

### Operations: #416 verified on both hosts (no commit)

- #423 merged as `b07b78e`. Its tree is identical to the branch head
  `2f8c62a`, so the review fixes landed.
- **DGX:** running `b07b78e` since 2026-11-01 01:04 AEDT. One IDLE crash
  since then (04:15, a `BrokenPipeError` on `DONE`, which the loop
  reconnected from). Before the fix there was one every ~10 min.
- **Mac:** no IDLE crash since 2026-10-31 21:52. I restarted `serve`, and
  `/v1/version` now reports `b07b78e`.

### PR #424 on `fix/414-bound-pool-connections` (based on `main` at `b07b78e`)

| SHA | What |
|---|---|
| `c4fc08f` | Pool connections get the #140/#142 bounds; `faulthandler` on SIGUSR1 |
| handoff | this file + `docs/handoffs/2026-11-01T0101Z-pool-bounds.md` |

- **Pool bounds.** `Daemon._connect` applied `connect_timeout`,
  `tcp_user_timeout` and `statement_timeout`. `open_pool` used the bare DSN,
  though every IDLE/poll heartbeat and sync goes through the pool. One rule
  now, `db.bounded_connect_kwargs`, passed to both. `open_pool` gained a
  `connect_kwargs=` argument; its CLI callers are unchanged.
- **Stack dump.** `run_forever` (main-thread branch, beside the signal
  handlers) calls `faulthandler.register(STACK_DUMP_SIGNAL, all_threads=True)`.
  `STACK_DUMP_SIGNAL = signal.SIGUSR1` lives in `daemon.py`.
- **Not done, by operator decision:** #414's stale-heartbeat watchdog.
- **Why the cause is still unknown:** the DGX journal for the 10-29 stall has
  rotated away (reboots on 10-31). Everything in the stall window
  (`open_connection`, then the first pool heartbeat) is bounded except pool
  queries, which had no `statement_timeout`. That is not proof.
- Docs: README (`localmail run` row, plus the SIGUSR1 note),
  `config.example.toml`, the `config.py` comments, and a CLAUDE.md note under
  **Sync model**.

## Verification

| check | result |
|---|---|
| RED first | pool test failed (`connect_timeout` absent from the pool connection's parameters); stack-dump test file failed to import |
| 2 mutations (pool kwargs → `None`; `register` → `pass`) | each caught by exactly its own test |
| full `pytest -q` | **4325 passed**, 6 warnings (same as before), 215 s |
| `mypy src/localmail` | Success, 162 files |
| statement_timeout headroom | slowest pool claim on the 130k Mac archive: 144 ms (embed chunk claim) |
| CI #424 | **not checked at handoff time** |

## What's next

### 1. **Merge #424, redeploy both hosts** (yours)
- After the merge, leave #414 open (the watchdog part remains). Comment on it
  that bounds + SIGUSR1 shipped.
- **Acceptance:** after the restart, `kill -USR1 <daemon pid>` on the DGX
  prints thread stacks in `journalctl --user -u localmail-daemon`, and the
  daemon keeps running (heartbeats fresh in `localmail daemon status`).
- **If a stall recurs:** run `kill -USR1` *before* restarting, and save the
  dump into #414.

### 2. **Tell kastellan** (yours; carried)
- Problem+json 400s on `/v1/search`, `filters.lang` as a list, web-search
  syntax in `query` (#419), dates no longer in the future (#415).

### 3. **Next small slice**
- **#421:** the GUI tokenizer drops quotes on a single quoted word (`"OR"`,
  `"-acme"`). `filter_parse.tokenize` must record which tokens were quoted,
  so `joinTokens` re-quotes them.
- **Acceptance:** a vitest RED test for `a "OR" b` and `"-acme"` surviving
  a popover Apply and `absorbConflictingOperators`; fix; `npm test -- --run`
  and `npm run check` green.

### 4. **Carried**
- **#414 watchdog:** waits for a stack dump.
- **Dependabot #71 / #185:** left open on purpose.
- **docling > 2.144:** watch for a release and probe it.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **Issues:** #383, #384, #378, #369, #373, #372, #368, #365; #375;
  #374 + #305; #358/#357; #330/#327/#328; #319/#318/#320 (migration
  `0037_*.sql`); #340, #363, #285; robustness #218, #226, #225/#227,
  #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **#414 scope: pool bounds + SIGUSR1 now; watchdog deferred** until a dump
   shows what hangs (operator).

## Open decisions & risks

1. **New failure mode:** a pool statement past `db_statement_timeout_s` (30 s)
   now aborts. In sync that lands in `failed_messages` (`retry-failed`
   recovers). Watch for `canceling statement due to statement timeout` in
   either daemon log after the deploy.
2. **3,194 rows keep a skewed `internal_date`** (Mac 2,720, DGX 474); the
   server no longer has them. Leave / NULL / per-host SQL shift: not decided.
3. **What drops the silent IMAP flows is unknown** (router, CGNAT,
   WireGuard?). If a path ever drops under 60 s, keepalive at 60 s won't
   cover it.
4. **Should a wire widening bump `api_minor`?** Still open.
5. **#375 under load:** `build_hash` is null on the Mac until a quiet
   restart.
6. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.**
7. **Never run two localmail pytest sessions against one test DB** (port
   5532, same cluster as the live archive).
8. **Restore a mutation from a file copy, never with `git checkout`.**
9. **Always sync with `--extra mcp --extra extraction`**; `~/.local/bin/uv`
   on the DGX. `uv sync --dry-run` is not read-only.
10. **Run long DGX jobs detached** (`nohup setsid … > /tmp/x.log`).
11. **No ROADMAP.md.** That `/nextsession` step is a no-op.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git log --oneline main..origin/fix/414-bound-pool-connections   # empty once merged
gh pr checks 424
gh issue list --limit 200 --json number --jq length

# Redeploy — Mac (the repo is the install)
unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
launchctl kickstart -k gui/$(id -u)/com.localmail.serve
curl -sk https://127.0.0.1:8443/v1/version

# Redeploy — DGX, then prove the stack dump
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user restart localmail-daemon localmail-serve'
ssh 10.0.0.3 'kill -USR1 $(systemctl --user show -p MainPID --value localmail-daemon); sleep 2; journalctl --user -u localmail-daemon --since "-1min" | grep -c "Thread 0x"'
ssh 10.0.0.3 'cd ~/src/localmail && .venv/bin/localmail daemon status | head -8'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4325 at session 69
uv run pytest -q tests/test_daemon_connect_timeout.py tests/test_daemon_stack_dump.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 162 files
cd gui && npm test -- --run && npm run check
```

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`.
