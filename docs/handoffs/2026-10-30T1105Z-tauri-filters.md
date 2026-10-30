# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-10-30 (session 66).** `main` was **`4257ed1`** at
> session start (#417 merged, #370 closed).
>
> This session:
> - **Redeployed both hosts to `4257ed1`.**
> - Opened **PR #418** on `fix/371-tauri-filters-wire` with one code commit
>   (`1c8bc60`), which closes #371.
>
> **Open issues: 36** (35 once #418 merges and closes #371).
>
> **Leftover worktree `.claude/worktrees/bottie-bearer-token-dbc203`**
> (detached at `5b85180`). Untouched; ask before removing it.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth or password) into Postgres, and
is **strictly read-only w.r.t. IMAP**. The database is canonical for accounts.

- **Daemon:** hot-reloads the account set, writes heartbeats, runs a DB
  command queue, and has two-plane supervision.
- **Web admin UI (HTMX):** account CRUD, user management, archive imports,
  daemon control and API keys.
- **Also shipped:** hybrid search (Phases 1+2), an HTTPS GUI server, a remote
  MCP server (optionally a full OAuth 2.1 authorization server) and the opt-in
  `--smart` LLM query rewriter.
- **Tauri 2 + Svelte 5 GUI** under `gui/`: a read-only viewer plus an admin
  mode.
- **Versions and licence:** version **0.3.0**. Python **3.13** is pinned, and
  CI runs a 3.12 + 3.13 matrix. Licensed AGPL-3.0-or-later.
- **kastellan** consumes `/v1` over REST with an API key. Slice order
  **0 → A → B → D → E → C**. **E is deployed; only C remains.**

## What we shipped this session

### Redeploy (no commit)

| host | before | after | check |
|---|---|---|---|
| DGX | `661112a` | `4257ed1` | `build_hash` = `4257ed1`; `NRestarts=0` on both units |
| Mac | `661112a` | `4257ed1` | `pending_migrations` empty; both agents kickstarted at 20:56; daemon logged "daemon running" |

- **The Mac's Postgres (`var-18`, port 5532) was down when I started.**
  Its log shows a clean *smart shutdown request* at 17:09. The Mac then
  rebooted into macOS 27.2.0 (up since ~20:01), and Postgres.app brought
  5532 back. I started nothing. The likely cause is the OS update preparing
  to reboot; I have not confirmed that. **The archive was offline about
  17:09–20:01.**
- **The Mac's `/v1/version` reports `build_hash: null` / `git_failed`.**
  This is #375: both git probes hit the 2 s timeout at load average
  **~160–210**, caused by another project's `primer_gui` test binary using
  ~9 cores. Per CLAUDE.md, check the process start time against HEAD
  instead: HEAD was `4257ed1` the whole session, `uv sync` was a no-op,
  and both processes started at 20:56. So the Mac runs `4257ed1`.
  - Restart `serve` once the load is down if you want a real hash.

### PR #418 on `fix/371-tauri-filters-wire` (based on `main` at `4257ed1`)

| SHA | What |
|---|---|
| `1c8bc60` | `SearchFiltersWire` gains `date_from`, `date_to` and `lang` (`Option<String>`, `skip_serializing_if`). Adds 2 Rust tests and a Python TS-to-Rust key pin, and updates CLAUDE.md |
| handoff | this file + `docs/handoffs/2026-10-30T1105Z-tauri-filters.md` |

- **The defect:** serde drops unknown fields, so the three keys the TS
  `filtersUiToWire` sets vanished at `invoke`. Neither the `lang` chip nor
  the `defaultLanguage` setting ever reached the server.
- **The pin:** `test_every_key_the_gui_frontend_sets_survives_the_tauri_hop`
  (in `tests/test_serve_search_request_keys.py`) reads the TS interfaces
  `SearchFiltersWire` / `SearchRequest` in `gui/src/lib/api/search.ts`. It
  requires every key there to be a Rust field.
  - This pin is the structural one. The Rust round-trip test covers only
    today's three keys.
  - The TS reader is strict, like `_rust_wire_names`: any line that is not
    `name: type;` fails, and a negative control pins that.
- **Behaviour change:** a user who set `defaultLanguage` now gets
  language-filtered results. It defaults to `null`, so no one else is
  affected.
- **Not done:** the GUI still sends `lang` as a single string. The server
  takes a list since #417, but no GUI control produces one.

## Verification

| check | result |
|---|---|
| RED first | Python pin failed for `SearchFiltersWire` (`SearchRequest` passed); Rust `date_bounds_and_lang_survive_the_tauri_hop` failed |
| `cargo test --locked` | 111 passed |
| `cargo clippy --locked --all-targets -D warnings` | clean |
| `pytest tests/test_serve_search_request_keys.py` | 24 passed |
| CI | see `gh pr checks 418` |

- **Not run:** the full pytest suite, vitest and svelte-check. The only
  Python change is a test file, and no TS changed. Load was ~200, so a
  full-suite timing would have meant nothing. CI runs all of them.

## What's next

### 1. **Merge #418, then redeploy** (yours)
- After the merge, check that `gh issue view 371` reports closed.
- The **server** needs no redeploy: it has no Python change. The fix
  ships in the desktop GUI build. **Rebuild or reinstall the Tauri app**
  for it to take effect.
- **Acceptance:** with the rebuilt GUI, a `lang` chip (e.g. `de`) narrows
  the results.

### 2. **Tell kastellan** (yours; carried from session 65)
- Every schema error on `/v1/search` is a problem+json 400 with a string
  `detail`.
- `filters.lang` takes a list; an empty list is a 400.
- `api_minor` was not bumped.

### 3. **Next small slice: pick from the carried issues**
- Candidates:
  - **#416:** Gmail IDLE drop logged as a crash. Probably a log-level fix
    in `idle.py`.
  - **#415:** INTERNALDATE local time labelled UTC. A data-correctness
    bug; read it before choosing.
  - **#414:** daemon workers stall on unbounded pool connections.
- **Acceptance (any of them):** RED test first, the fix, and the full
  suite green on a quiet machine.

### 4. **Dependabot**: two open, both on purpose
- **#71 accelerate:** no fix exists.
- **#185 deepmerge:** no patched release; dev-scope only.

### 5. **Watch for docling > 2.144** (carried)
- Re-run the probe under rapidocr against any new release. If
  `native_table` passes, move the lock forward and lift the
  `docling<2.143` cap.

### 5a. **emSign G3 in the OS trust stores** (carried)
- Re-checked on the Mac after the 27.2.0 update: G3 is **still present**
  (`grep -c` = 2). The DGX was not re-checked. Re-check after each OS or
  `ca-certificates` update (the command is below).

### 6. **kastellan, slice C** (carried, yours)
- Ask whether it still wants slice C (`/problems/token-expired`).

### 7. **Carried issues**
- #383, #384, #378, #369, #373, #372, #368, #365, #414, #415, #416.
- **Not filed yet:** the `snippet_source="header"` mislabel on a
  whole-message lexical win.
- **Larger work:**
  - #375 (seen live again this session); #374 + #305 (the `cli.py`
    refactor).
  - #358/#357, #330/#327/#328, #319/#318/#320 (next migration `0037_*.sql`).
  - #340, #363, #285.
  - Lifting the rapidocr cap; admin GUI phase 5 (Users panel).
  - Robustness: #218, #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **Redeploy before the slice** (operator decision). Both hosts now run
   `4257ed1`.
2. **Postgres on the Mac: "start it" was the decision.** The reboot had
   already restarted it, so I did nothing.
3. **Pin TS against Rust in Python**, not only with a Rust test. The
   defect lives between the two suites, and Python already reads the Rust
   file.
4. **`lang` stays a single string in the GUI.** No control makes a list.

## Open decisions & risks

1. **Should a wire widening bump `api_minor`?** Still open; nothing was
   bumped.
2. **Why did the Mac's Postgres shut down at 17:09?** Probably the OS
   update. The archive was offline for ~3 h with no alert; nothing in
   localmail reports "DB unreachable" beyond the daemon's own log.
3. **#375 under heavy load:** `build_hash` is null on the Mac until a
   quiet restart.
4. **The native-table regression in docling ≥ 2.143 is unexplained**, only
   localised.
5. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.**
6. **Never run two pytest sessions against one test database.** Other
   projects' pytest runs (e.g. bmlibrarian) are fine; check `pgrep -fl
   pytest` for a localmail one.
7. **Restore a mutation from a file copy, never with `git checkout`.**
8. **The test DB is on port 5532**, the same cluster as the live archive.
9. **Always sync with `--extra mcp --extra extraction` on both hosts**, and
   use `~/.local/bin/uv` on the DGX.
10. **`uv sync --dry-run` is not read-only; `uv lock --dry-run` is.**
11. **No ROADMAP.md.** That `/nextsession` step is a no-op.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git worktree list

gh pr view 418 --json state,mergeCommit
gh issue view 371 --json state
gh issue list --limit 200 --json number --jq length       # 35 after the merge
gh api --paginate repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | .dependency.package.name' | sort | uniq -c   # accelerate, deepmerge

# Redeploy — Mac (the repo is the install)
unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
.venv/bin/python -c "from localmail.config import load_config; from localmail.db import pending_migrations; print(pending_migrations(load_config().database.dsn))"
launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
launchctl kickstart -k gui/$(id -u)/com.localmail.serve
curl -sk https://127.0.0.1:8443/v1/version

# Redeploy — DGX
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user restart localmail-daemon localmail-serve'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version; systemctl --user show -p NRestarts localmail-daemon localmail-serve'

# Deployment health
launchctl list | grep localmail
tail -3 ~/localmail-probe/tunnel-probe.log
grep -c "emSign ECC Root CA - G3" /etc/ssl/cert.pem
ssh 10.0.0.3 'ls /etc/ssl/certs | grep -ic emsign_ecc_root_ca_-_g3'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4230 at session 65; +3 new tests, full suite not re-run (load ~200)
uv run pytest -q tests/test_serve_search_request_keys.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 161 files
cd gui && npm test -- --run && npm run check && npm audit
cd gui/src-tauri && cargo test --locked && cargo clippy --locked --all-targets -- -D warnings
```

`main` was **`4257ed1`** at session start and is unchanged. This session left
**one PR**, #418 on `fix/371-tauri-filters-wire`: commit `1c8bc60`, plus this
handoff. Both hosts run `4257ed1`.

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`.
