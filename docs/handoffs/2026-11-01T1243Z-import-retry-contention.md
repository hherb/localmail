# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-01 (session 77).** `main` is **`d5f1277`**: #436
> (#425) merged before this session, and #425 is closed.
>
> This session opened **one PR** on `fix/437-import-retry-contention`:
> `4083794` (the code) and this handoff. Its body says **`Closes #437`**.
>
> **Both hosts are deployed at `d5f1277`** (see below).
>
> **Open issues: 35** (34 once #437 closes; #439 filed in review).
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

### Deploy of `d5f1277` (#425) to both hosts (no commit)

- **Mac:** `uv sync --frozen --extra mcp --extra extraction` (no change),
  kickstarted `com.localmail.daemon` and `com.localmail.serve`. `/v1/version`
  reports `api_minor` 6, `build_hash` `d5f1277`.
- **DGX:** pulled `3326a75` → `d5f1277`, synced (it reinstalled
  `nvidia-cusparselt-cu13==0.8.1`, lock unchanged), restarted both units.
  `api_minor` 6, `build_hash` `d5f1277`, `NRestarts` 0, daemon `active`.

### PR on `fix/437-import-retry-contention` (based on `main` at `d5f1277`)

| SHA | What |
|---|---|
| `4083794` | #437: importer and `retry-failed` re-raise `db.DB_CONTENTION_ERRORS` |
| handoff | this file + `docs/handoffs/2026-11-01T1243Z-import-retry-contention.md` |
| review | review fixes: contention WARNINGs, `import` exits 1 on a failed job, `retry-failed` `ClickException`, three-row + CLI tests, corrected trigger wording |

- **The defect.** Two per-message SAVEPOINT handlers recorded
  `QueryCanceled` / `DeadlockDetected` / `LockNotAvailable` in
  `failed_messages` as if the message were malformed:
  `importer/runner.py::run_import` and `sync.py::retry_failed_messages`.
- **The fix**, the sync idiom: after `ROLLBACK TO SAVEPOINT`,
  `if isinstance(exc, DB_CONTENTION_ERRORS): raise`.
  - `run_import` lands in its outer handler, which writes `status='failed'`
    with `error_msg` (`"QueryCanceled: …"`) on a fresh connection. The
    importer's "skipping poison message" WARNING now runs after the check,
    so it no longer lies about a contended message.
  - `retry_failed_messages` raises out of the command; rows retried earlier
    were committed one by one, so they keep their outcome.
- **Review round** (four reviewers): the first cut's trigger story was
  wrong. These connections carry no timeout, so a wait on the daemon just
  blocks, and an import cannot meet the daemon on a Message-Id (per-account
  dedup; the daemon never syncs archive accounts). Fixed in the comments,
  test docstring, CLAUDE.md and README. Also: a contention WARNING naming
  the message at both sites, `localmail import` exits 1 on a failed job,
  `retry-failed` turns contention into a `ClickException`, and a three-row
  test pins "rows recovered before it stay committed" (moving the commit
  to the end of the loop survived every test before it). Each new pin was
  mutation-checked. The counters defect is now #439.
- New `tests/test_import_retry_db_contention.py` (11 tests), reusing
  `_raising_trigger` / `_CASES` from `tests/test_worker_db_contention.py`.
- CLAUDE.md gained a #437 bullet beside #425's; README's failed-messages
  section says a lock timeout/deadlock is not counted against the message.
  No migration.

## Verification

| check | result |
|---|---|
| RED first | 6 contention cases failed, 2 `DataException` controls passed |
| mutation | disabling each re-raise (`python -B`) fails exactly its own 3 cases |
| `uv run pytest -q` | **4479 passed**, 6 warnings (load ~10–15) |
| mypy | Success, 165 files |
| GUI | not run (no GUI change) |
| CI | **check on the PR** (`gh pr checks`) |

## What's next

### 1. **Merge this PR** (yours)
- **Acceptance:** `gh issue view 437 --json state` is `CLOSED`.

### 2. **Deploy #437 once merged** (low urgency)
- Pure Python, no migration. It only changes `localmail import`,
  admin-panel imports (in-serve) and `retry-failed`, so **restart serve**;
  the daemon runs none of it and needs no restart.
- **Acceptance:** `build_hash` on `/v1/version` equals the merge SHA on both hosts.

### 3. **Tell kastellan** (yours; carried)
- Problem+json 400s on `/v1/search`, `filters.lang` as a list, #419 syntax,
  #415 dates; `api_minor` 5 (no repeats, `attachment_filename` /
  `attachments`); `api_minor` 6 (`filters.attachment_name`, `date_local`).
  Both hosts serve `api_minor` 6.

### 4. **Next small slice: pick with the operator**
- Candidates:
  - #383: `messages.headers` and its GIN index are write-only. Dropping them
    is a one-way door and needs `0039`.
  - #365: images embedded in the HTML are counted as attachments. One edit
    to `HAS_ATTACHMENT_SQL`, but which images count is a product decision.
  - #384: comment or close (2.7 ms warm for 50 prefixes).
  - #369: last-token-wins on scalar operators.
  - #373: an apostrophe inside a word is dropped.
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

1. **The operator picked #437** and asked for both hosts to be deployed at
   `d5f1277`.
2. **The failed job's counters were left as they are** (my call). On any
   mid-run failure, the counters include messages since the last commit
   (a checkpoint or a new mailbox) that the closing connection rolls back,
   and genuine poison pills since then lose their `failed_messages` rows.
   Pre-existing, now reachable by contention too; a re-run is idempotent.
   Filed as #439.

## Open decisions & risks

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
git diff main origin/fix/437-import-retry-contention --stat   # empty once merged (squash)
gh pr list --state all --limit 3
gh issue view 437 --json state --jq .state
gh issue list --limit 200 --json number --jq length

# Deploy — Mac (the repo is the install; no migration for #437)
unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
launchctl kickstart -k gui/$(id -u)/com.localmail.serve
curl -sk https://127.0.0.1:8443/v1/version     # api_minor 6, build_hash = merge SHA

# Deploy — DGX
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user restart localmail-daemon localmail-serve'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4479 at session 77
uv run pytest -q tests/test_import_retry_db_contention.py tests/test_worker_db_contention.py tests/test_importer_runner.py tests/test_import_uid_collision.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 165 files
cd gui && npm test -- --run && npm run check   # 530 at session 72
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
