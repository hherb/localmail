# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-02 (session 79).** `main` is **`b8dd9db`** (#443:
> #441, closed).
>
> This session has **one open PR** on `fix/442-import-terminal-status`. Its
> body says **`Closes #442`**. It carries the #442 code and this handoff.
>
> **Both hosts are deployed at `b8dd9db`**; nothing from the #442 PR is
> deployed.
>
> **Open issues: 35** (34 once the #442 PR merges).
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
- **kastellan** consumes `/v1` over REST with an API key.

## What we shipped this session

### Deploy of `b8dd9db` (#443, #441) to both hosts (no commit)

- **Mac:** `uv sync --frozen --extra mcp --extra extraction` (no change),
  kickstarted `com.localmail.daemon` **and** `com.localmail.serve`.
- **DGX:** pulled → `b8dd9db`, synced (reinstalled `nvidia-cusparselt-cu13`
  again, lock unchanged), restarted `localmail-daemon localmail-serve`. Both
  `active`, `NRestarts=0`.
- Both `/v1/version`: `api_minor` 6, `build_hash` `b8dd9db`.

### PR on `fix/442-import-terminal-status` (based on `main` at `b8dd9db`)

| SHA | What |
|---|---|
| `1093821` | #442: retry the terminal import status write, then report it |
| handoff | this file + `docs/handoffs/2026-11-02T2350Z-import-terminal-status.md` |

- **The defect.** `runner._mark_terminal` tried once. A failure (e.g. the
  fresh `conn_factory()` cannot connect) escaped `run_import`; in serve the
  thread's unhandled-exception dump was the only record and the row stayed
  `running`, holding `import_jobs_single_active_uniq` until a serve restart.
- **The fix.** `_mark_terminal` retries `_write_terminal`
  `[imports] terminal_write_attempts` (5) times,
  `terminal_write_backoff_s` (10 s) apart, then logs **one ERROR** naming the
  job and the remedy (restart serve, or the `UPDATE` to run by hand) and
  raises `TerminalStatusUnwritten`. `start_job`'s thread target
  (`_run_import_in_thread`) swallows it; the CLI turns it into a
  `ClickException` (exit 1). The `completed`/`cancelled` write moved to the
  `try`'s `else`, so its exhaustion is **not** relabelled `failed` (ruling 2).
- Both tunables are new `ImportsConfig` fields (floored `ge=1` / `ge=0`),
  required keywords on `run_import`/`start_job`, threaded from `cfg.imports`
  at the CLI and both routers. Existing tests pass `_TW` (1 try, no wait).
- New `tests/test_import_terminal_status.py` (6 tests). #439's
  "failure after the final commit" test now drives the worker connection's
  `close()` raising (the one such failure that still reaches the handler).
- README (imports paragraph), CLAUDE.md (after #162's bullet) and
  `config.example.toml` updated. No migration.

## Verification

| check | result |
|---|---|
| RED first | all 6 new tests failed |
| mutation | final-commit snapshot removed; completed write back inside the `try`; no `sleep`; thread catch narrowed — each fails exactly its own test |
| `uv run pytest -q` | **4507 passed**, 6 warnings (load ~15) |
| mypy | Success, 165 files |
| GUI | not run (no GUI change) |
| CI | **check on the PR** (`gh pr checks`) |

## What's next

### 1. **Merge the #442 PR** (yours), once CI is green
- **Acceptance:** `gh pr checks <n>` all pass; after the merge
  `gh issue view 442` is `CLOSED`.

### 2. **Deploy #442 once merged**
- Pure Python, no migration. Imports run in **serve** (and the CLI), so
  restarting serve is enough; the daemon runs none of it.
- **Acceptance:** `build_hash` on `/v1/version` equals the merge SHA on both
  hosts; units `active`.

### 3. **Tell kastellan** (yours; carried)
- Problem+json 400s on `/v1/search`, `filters.lang` as a list, #419 syntax,
  #415 dates; `api_minor` 5 (no repeats, `attachment_filename` /
  `attachments`); `api_minor` 6 (`filters.attachment_name`, `date_local`).
  Both hosts serve `api_minor` 6.

### 4. **Next small slice: pick with the operator**
- Remaining #438 review follow-up: **#440** (re-running an import duplicates
  poison pills' `failed_messages` rows under fresh uids).
- Others: #383 (drop write-only `messages.headers` + GIN, `0039`, one-way
  door), #365 (inline images counted as attachments; product call), #384
  (comment or close), #369, #373.
- **Acceptance (any):** a RED test first, then full `uv run pytest -q`
  green and mypy clean.

### 5. **Carried**
- **Migrations:** `0038` is taken. #319/#318/#320 need `0039`.
- **#414 watchdog:** waits for a stack dump (SIGUSR1 works on the DGX).
- **Dependabot #71 / #185:** left open on purpose.
- **docling > 2.144:** probe the next release when it lands.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **Mac `failed_messages`:** one `iso2022_jp` decode failure from
  2026-07-25 (`hherb-dmc/[Gmail]/Important` uid 5263). Not investigated.
- **Issues:** #383, #378, #369, #373, #372, #368, #365; #375; #374 + #305;
  #358/#357; #330/#327/#328; #340, #363, #285; robustness #218, #226,
  #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **The operator asked for `b8dd9db` on both hosts and picked #442** (over
   #440/#373/#383).
2. **An exhausted `completed`/`cancelled` write is not relabelled `failed`**:
   one retry window, one ERROR, the row stays `running` until serve restarts.

## Open decisions & risks

0. **Since #438, `localmail import` exits 1 on a failed job** (behaviour
   change for scripts; none known on either host). Since #441, a contended
   poison-pill record also fails the job / aborts the sync pass. Since #442,
   an import whose final status write fails on all 5 attempts (~40 s) also
   exits 1, and the row stays `running` until serve restarts.
1. **Repair the pre-#431 `-0000` rows?** Mac: 2,865 of 132,320 rows carry a
   `-0000` Date and are stored 8 h off. None has a NULL `internal_date`, so
   the list/search/changes wire `date` is right; raw `date_sent` readers
   (message-detail `date`, `after:`/`before:`, arm 4's cap, CLI search,
   chunk `Date:` lines) see the skew. The DGX is correct (UTC session). A
   repair should re-parse `raw_bytes`. Read-only count:
   `select count(*) from messages where exists (select 1 from jsonb_array_elements_text(case when jsonb_typeof(headers->'Date')='array' then headers->'Date' else '[]' end) d where d ~ '-0000\s*(\(.*\))?\s*$')`
2. **`attachment_name` is unindexed** (~0.2 s warm at 132k for a filter-only
   miss). A trigram index would be `0039`.
3. **Should a wire widening bump `api_minor`?** Still open.
4. **3,194 rows keep a skewed `internal_date`** (Mac 2,720, DGX 474). Not
   decided: leave them, NULL them, or apply a per-host SQL shift.
5. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.** After a squash
   merge `main..origin/<branch>` is non-empty by construction; compare with
   `git diff main origin/<branch> --stat` (empty = landed).
6. **Never run two localmail pytest sessions against one test DB** (port
   5532, the same cluster as the live archive).
7. **Restore a mutation from a file copy, never with `git checkout`.**
8. **Always sync with `--extra mcp --extra extraction`.** On the DGX use
   `~/.local/bin/uv`. `uv sync --dry-run` is not read-only.
9. **No ROADMAP.md.** That `/nextsession` step is a no-op.
10. **Load on the Mac was 8–15 this session** (other projects). Check
    `uptime` before trusting any timing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git diff main origin/fix/442-import-terminal-status --stat   # empty once merged (squash)
gh pr list --state all --limit 3
gh issue view 442 --json state --jq .state
gh issue list --limit 200 --json number --jq length

# Deploy — Mac (the repo is the install; no migration for #442; serve only)
unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
launchctl kickstart -k gui/$(id -u)/com.localmail.serve
curl -sk https://127.0.0.1:8443/v1/version     # api_minor 6, build_hash = merge SHA

# Deploy — DGX
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user restart localmail-serve && systemctl --user is-active localmail-daemon localmail-serve'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4507 at session 79
uv run pytest -q tests/test_import_terminal_status.py tests/test_record_failed_contention.py tests/test_import_retry_db_contention.py tests/test_worker_db_contention.py tests/test_importer_runner.py tests/test_import_uid_collision.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 165 files
cd gui && npm test -- --run && npm run check   # 530 at session 72
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
