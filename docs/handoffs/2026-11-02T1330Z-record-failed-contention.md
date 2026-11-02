# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-02 (session 78).** `main` is **`a92a83b`** (#438:
> #437 + #439, both closed).
>
> This session has **one open PR**, on `fix/441-record-failed-contention`.
> Its body says **`Closes #441`**. It carries the #441 code and this handoff.
>
> **Both hosts are deployed at `a92a83b`**; nothing from the #441 PR is deployed.
>
> **Open issues: 36** (35 once the #441 PR merges).
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

### Deploy of `a92a83b` (#438) to both hosts (no commit)

- **Mac:** `uv sync --frozen --extra mcp --extra extraction` (no change),
  kickstarted `com.localmail.serve` only (the daemon runs none of #438).
- **DGX:** pulled → `a92a83b`, synced (reinstalled `nvidia-cusparselt-cu13`
  again, lock unchanged), restarted `localmail-serve`. Both units `active`.
- Both `/v1/version`: `api_minor` 6, `build_hash` `a92a83b`.

### PR on `fix/441-record-failed-contention` (based on `main` at `a92a83b`)

| SHA | What |
|---|---|
| `2da4d17` | #441: `record_failed_message` re-raises `DB_CONTENTION_ERRORS` |
| handoff | this file + `docs/handoffs/2026-11-02T1330Z-record-failed-contention.md` |

- **The defect.** `sync.record_failed_message` wraps its `failed_messages`
  upsert in `SAVEPOINT fail_log` + `except Exception`, logs and carries on.
  Contention on that upsert was swallowed too: the daemon then advanced
  `uidnext` past a message recorded nowhere (lost until a manual rewind),
  an import counted it `failed` with no row, and `retry-failed` counted it
  `still_failing` with the row untouched.
- **The fix.** After `ROLLBACK TO` / `RELEASE fail_log`, a contention class
  logs a WARNING (`could not record failed message UID … DB contention …;
  re-raising`) and re-raises. Each caller's existing handling then applies:
  sync aborts the pass (uidnext not checkpointed), `run_import`'s outer
  handler fails the job with the contention remedy in `error_msg`, and
  `retry-failed` stops (the CLI's `ClickException`). Any other failure to
  log is still swallowed. No caller changed.
- New `tests/test_record_failed_contention.py` (16 tests): the function plus
  all three callers, each parametrised over `_CASES` (3 contention classes
  + a `DataException` control). The poison pill is `upsert_message` patched
  to raise; the contention is `_raising_trigger` on the `failed_messages`
  INSERT. CLAUDE.md gained a #441 bullet after #437's; README's
  failed-messages paragraph mentions it. No migration.

## Verification

| check | result |
|---|---|
| RED first | 12 contention cases failed, 4 controls passed |
| mutation | guard → `if True:` (re-raise everything) fails exactly the 4 controls; no re-raise is the RED run |
| `uv run pytest -q` | **4501 passed**, 6 warnings (load ~15) |
| mypy | Success, 165 files |
| GUI | not run (no GUI change) |
| CI | **check on the PR** (`gh pr checks`) |

## What's next

### 1. **Merge the #441 PR** (yours), once CI is green
- **Acceptance:** `gh pr checks <n>` all pass; after the merge
  `gh issue view 441` is `CLOSED`.

### 2. **Deploy #441 once merged**
- Pure Python, no migration. It changes the **sync daemon** path, so restart
  **both** daemon and serve on both hosts (unlike #438).
- **Acceptance:** `build_hash` on `/v1/version` equals the merge SHA on both
  hosts; daemon units `active`, `NRestarts` 0 on the DGX.

### 3. **Tell kastellan** (yours; carried)
- Problem+json 400s on `/v1/search`, `filters.lang` as a list, #419 syntax,
  #415 dates; `api_minor` 5 (no repeats, `attachment_filename` /
  `attachments`); `api_minor` 6 (`filters.attachment_name`, `date_local`).
  Both hosts serve `api_minor` 6.

### 4. **Next small slice: pick with the operator**
- Remaining #438 review follow-ups: #440 (re-running an import duplicates
  poison pills' `failed_messages` rows), #442 (in-serve import stuck
  `running` if the terminal status write fails).
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

1. **The operator asked for #438 on both hosts and picked #441** (over
   #440/#442).
2. **`record_failed_message` re-raises contention only**; every other
   failure to write the row stays swallowed (unchanged).

## Open decisions & risks

0. **Since #438, `localmail import` exits 1 on a failed job** (behaviour
   change for scripts; none known on either host). Since #441, a contended
   poison-pill record also fails the job / aborts the sync pass.
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
git diff main origin/fix/441-record-failed-contention --stat   # empty once merged (squash)
gh pr list --state all --limit 3
gh issue view 441 --json state --jq .state
gh issue list --limit 200 --json number --jq length

# Deploy — Mac (the repo is the install; no migration for #441; restart BOTH)
unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
launchctl kickstart -k gui/$(id -u)/com.localmail.serve
curl -sk https://127.0.0.1:8443/v1/version     # api_minor 6, build_hash = merge SHA

# Deploy — DGX
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user restart localmail-daemon localmail-serve && systemctl --user is-active localmail-daemon localmail-serve'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4501 at session 78
uv run pytest -q tests/test_record_failed_contention.py tests/test_import_retry_db_contention.py tests/test_worker_db_contention.py tests/test_importer_runner.py tests/test_import_uid_collision.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 165 files
cd gui && npm test -- --run && npm run check   # 530 at session 72
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
