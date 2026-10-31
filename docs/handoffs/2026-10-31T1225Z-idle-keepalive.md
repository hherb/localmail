# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-10-31 (session 68).** `main` was **`f4ab719`** at
> session start (#422, closing #415, had merged).
>
> This session opened **one PR, #423**, on `fix/416-idle-renew-before-drop`.
> It has one code commit (`39e1c75`), closes **#416**, and also carries this
> handoff.
>
> **Open issues: 35** (34 once #423 merges).
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

### Operations: #415 deployed and repaired on both hosts (no commit)

- Redeployed `f4ab719` to the Mac and the DGX. Both `/v1/version` report
  `build_hash: f4ab719`.
- Ran `backfill-internal-date --refetch` on both, after the redeploy.

| host | updated / candidates | not on the server | unusable | time | exit |
|---|---|---|---|---|---|
| Mac | 94,544 / 132,296 | 2,720 | 0 | 36 min | 0 |
| DGX | 38,595 / 39,070 | 474 | 0 | ~10 min | 0 |

- **Mac skew histogram:** the peak moved from +2 h (87,422 rows) to 0
  (123,205). No row has an `internal_date` in the future. The residual +2 h
  bucket (2,143) is about the size of the "not on the server" count.
- **DGX:** the peak is at 0 (35,475).
- **The DGX acceptance message 39045 is still +11 h, and cannot be fixed by
  refetch.** It was labelled only INBOX UID 65847. That UID is gone from
  INBOX, and a `HEADER Message-ID` search of `[Gmail]/All Mail` finds
  nothing. So it was deleted (or sits in Trash/Spam, which All Mail
  excludes). It is one of the 474. See open decision 1.

### PR #423 on `fix/416-idle-renew-before-drop` (based on `main` at `f4ab719`)

| SHA | What |
|---|---|
| `39e1c75` | TCP keepalive on every IMAP socket; IDLE renewal default 1740 → 600 s; `config.example.toml` pinned to the default |
| handoff | this file + `docs/handoffs/2026-10-31T1225Z-idle-keepalive.md` |

**The issue's diagnosis was wrong, and that is the main finding.**

- #416 blamed a Gmail 29-minute IDLE drop. Every crash was at `idle.py`'s
  renewal `idle_done()`. That was true on Gmail **and** on `hherb-dmc`, from
  **both** hosts. The server never closed the connection first.
- The 29-minute cadence was our own `idle_renew_seconds = 1740`. Any
  shorter silent timeout looks the same.
- **The first fix (renew at 600 s) was tested live and failed.** The DGX
  reset at its first 600 s renewal. The Mac reset at its second, for both
  accounts.
- **Measured instead**, with read-only parallel IDLE probes to Gmail
  (SELECT readonly, IDLE, sleep, DONE). The probe was a scratch script and
  is not kept.

| silence | Mac | DGX |
|---|---|---|
| 60–300 s | OK | – |
| 420 s | OK | reset |
| 540 s | reset | reset |
| 540 s + TCP keepalive | **OK** | **OK** |

- So something on the network path drops a silent TCP flow without telling
  either end.

**The fix:**

- `imap_client.open_connection` calls the new `enable_tcp_keepalive`: 60 s
  idle, 30 s interval. Linux spells the idle option `TCP_KEEPIDLE`, macOS
  `TCP_KEEPALIVE`.
- This covers IDLE, the poll plane, long FETCHes and the admin probe.
- **The renewal default moves 1740 → 600**, because 1740 renewed *at* RFC
  2177's 29-minute limit. `idle.RFC2177_IDLE_LIMIT_S` names that limit.
  - Both hosts carried 1740 because they copied `config.example.toml`.
    `tests/test_idle_renew_default.py` pins the example to the default.
  - **Both hosts' `config.toml` were edited to 600 this session.**
    Backups: the Mac's is in this session's scratchpad; the DGX's is
    `~/.config/localmail/config.toml.bak-416`.
- Test fakes that replace `IMAPClient`
  (`test_internal_date_utc.py`, `test_sync_concurrent_writers.py`) gained a
  `socket()`.
- CLAUDE.md has a new note under **Sync model**.

## Verification

| check | result |
|---|---|
| RED first | both new test files failed (import error; then 2 failed for keepalive) |
| 4 mutations (keepalive call removed, idle 60 → 600, default back to 1740, example drifted) | each caught |
| full `pytest -q` | **4321 passed**, 6 warnings, 207 s |
| `mypy src/localmail` | Success, 162 files |
| CI #423 | both pytest legs pass |
| **live, Mac** (daemon runs from this checkout) | 0 IDLE crashes in ~30 min since 21:56:35. The unpatched 600 s build crashed both accounts within 20 min |

- **Not live-tested on the DGX.** It still runs `main` (`f4ab719`) with
  `idle_renew_seconds = 600`, and resets every 10 min until #423 is
  deployed there. The DGX probe proves Linux keepalive works.
- **The 6 warnings:** at least some are docling `DeprecationWarning`s from
  `test_ocr_language_default.py`. The rest are unidentified (carried).

## What's next

### 1. **Merge #423, redeploy both hosts** (yours)
- After the merge, check that `gh issue view 416` reports closed.
- Redeploy (commands below). The Mac already runs this code; the DGX needs
  it.
- **Acceptance:** no `inbox-idle session crashed` line in either daemon log
  for an hour after the restart (one is expected right after the restart at
  most). Before the fix it was one every ~10 min on the DGX.
  - Mac: `grep "session crashed" ~/Library/Logs/localmail/daemon.err.log | tail`
  - DGX: `journalctl --user -u localmail-daemon --since "-1h" | grep -c "session crashed"`

### 2. **Tell kastellan** (yours; carried)
- Problem+json 400s on `/v1/search`, `filters.lang` as a list, and
  web-search syntax in `query` (#419).
- Dates are no longer in the future (#415 repaired on both hosts).

### 3. **Next small slice**
- **#414:** stalled workers on unbounded pool connections. Note: #414's
  incident followed one of these resets. Re-read it now that #416's cause
  is known; part of it may be the same silent-drop mechanism.
- **#421:** the GUI drops quotes on a single quoted word (`"OR"`, `"-acme"`).
- **Acceptance (either):** RED test first, the fix, and the full suite
  green.

### 4. **Carried**
- **Dependabot #71 / #185:** left open on purpose.
- **docling > 2.144:** watch for a release and probe it.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **Issues:** #383, #384, #378, #369, #373, #372, #368, #365; #375;
  #374 + #305; #358/#357; #330/#327/#328; #319/#318/#320 (migration
  `0037_*.sql`); #340, #363, #285; robustness #218, #226, #225/#227,
  #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **Deploy and repair both hosts this session** (operator).
2. **#416 next** (operator).
3. **Keepalive, not a shorter renewal**, is #416's fix: measured, after the
   shorter renewal failed live.

## Open decisions & risks

1. **3,194 rows keep a skewed `internal_date`** (Mac 2,720, DGX 474): the
   server no longer has them, so refetch cannot fix them. Options:
   - Leave them.
   - NULL them, so search falls back to `date_sent`.
   - A per-host SQL shift. On the DGX the offset is deterministic (Sydney
     local read as UTC); on the Mac it is not. This session's ruling was "no
     SQL offset", and that ruling was made for the mixed Mac history.
   - Not decided.
2. **What drops the silent flows is unknown** (home router, ISP CGNAT,
   WireGuard?). It differs per host: the DGX path drops under 420 s. If a
   path ever drops under 60 s, keepalive at 60 s will not cover it.
3. **Should a wire widening bump `api_minor`?** Still open.
4. **#375 under load:** `build_hash` is null on the Mac until a quiet
   restart.
5. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.**
6. **Never run two localmail pytest sessions against one test DB.** The
   test DB is on port 5532, the same cluster as the live archive.
7. **Restore a mutation from a file copy, never with `git checkout`.**
8. **Always sync with `--extra mcp --extra extraction`**, and use
   `~/.local/bin/uv` on the DGX. `uv sync --dry-run` is not read-only.
9. **Run long DGX jobs detached** (`nohup setsid … > /tmp/x.log`). A quiet
   ssh session can be dropped by the same silent-flow drop.
10. **No ROADMAP.md.** That `/nextsession` step is a no-op.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git log --oneline main..origin/fix/416-idle-renew-before-drop   # empty once merged
gh issue view 416 --json state
gh issue list --limit 200 --json number --jq length

# Redeploy — Mac (the repo is the install)
unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
launchctl kickstart -k gui/$(id -u)/com.localmail.serve
curl -sk https://127.0.0.1:8443/v1/version

# Redeploy — DGX
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user restart localmail-daemon localmail-serve'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version; journalctl --user -u localmail-daemon --since "-1h" | grep -c "session crashed"'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4321 at session 68
uv run pytest -q tests/test_imap_keepalive.py tests/test_idle_renew_default.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 162 files
cd gui && npm test -- --run && npm run check
cd gui/src-tauri && cargo test --locked && cargo clippy --locked --all-targets -- -D warnings
```

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`.
