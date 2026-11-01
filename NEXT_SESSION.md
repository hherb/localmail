# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-01 (session 76).** `main` is **`3326a75`**: #435
> (#429) merged before this session, and #429 is closed.
>
> This session opened **one PR** on `fix/425-contention-errors`:
> `d9670bf` (the code) and this handoff. Its body says **`Closes #425`**.
>
> **Both hosts are deployed at `3326a75`** (see below).
>
> **Open issues: 34** (33 once #425 closes).
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
- **kastellan** consumes `/v1` over REST with an API key. #426 was its list of
  search asks, finished by #428.

## What we shipped this session

### Deploy of `3326a75` to both hosts (no commit)

- **Mac:** `uv sync --frozen --extra mcp --extra extraction`, `init-db`
  applied `0037` and `0038` in 1.5 s, and both launchd agents were
  kickstarted. `/v1/version` reports `api_minor` 6 and `build_hash`
  `3326a75`, and only `messages_attachment_names_strict_idx` remains.
- **DGX:** pulled `62bdec6` → `3326a75`, synced (docling, mcp and rapidocr all
  import), stopped the daemon, `init-db` applied `0038`, then restarted both
  units. `api_minor` 6, `build_hash` `3326a75`, `NRestarts` 0.
  `kill -USR1` logged 9 thread stacks and the daemon kept running under the
  same PID (#424 verified).
- **#430's retry check had nothing to test:** the DGX has no
  `failed_messages`, and the Mac's one row is an old `iso2022_jp`
  `UnicodeDecodeError`, not an `OverflowError`. `retry-failed` was not run.
- **#426 re-check on the DGX** (`sort=date`, in-process):
  `attachment_name:230712`, `230712` and `"230712.pdf"` each return exactly
  `[18106]`; `has:no-attachment attachment_name:230712` returns `[]`. Posted
  to #426. Kastellan had already run the full paging probe at `62bdec6` (its
  comment on #426).

### PR on `fix/425-contention-errors` (based on `main` at `3326a75`)

| SHA | What |
|---|---|
| `d9670bf` | #425: embed worker and lang detection re-raise `db.DB_CONTENTION_ERRORS` |
| handoff | this file + `docs/handoffs/2026-11-01T1143Z-worker-contention-errors.md` |

- **The defect.** Three per-item failure paths recorded `QueryCanceled` /
  `DeadlockDetected` / `LockNotAvailable` as the item's own fault:
  - message chunking wrote a `failed_chunkings` row;
  - the per-chunk embedding UPDATE wrote a `failed_embeddings` row;
  - lang detection stamped the row attempted (a "decline").
- **The fix**, the same shape as `sync.py:878`: after `ROLLBACK TO SAVEPOINT`,
  `if isinstance(exc, DB_CONTENTION_ERRORS): raise`.
  - Chunking and lang detection then fail the sweep; `run_embed_worker`
    logs it and retries next sweep.
  - The embedding UPDATE's raise lands in `_embed_table`'s batch-level
    fallback, which rolls back and returns 0. The chunk stays claimable.
  - `embed-backfill` and `lang-backfill` now exit with the traceback on such
    an error; re-run them.
  - Attachment chunking was left alone: it records nothing, so it was not
    misclassifying.
- New `tests/test_worker_db_contention.py` (12 tests). A trigger, committed
  and dropped in `finally`, raises each SQLSTATE server-side, and a
  `DataError` through the same trigger is the positive control per site.
- Docstrings updated in both modules; the CLAUDE.md #414 bullet now says
  #425 is fixed. README unchanged (it describes neither path). No migration.

## Verification

| check | result |
|---|---|
| RED first | 9 contention cases failed, 3 `DataError` controls passed |
| mutation | deleting each of the 3 re-raises fails exactly its own 3 cases |
| `uv run pytest -q` | **4470 passed**, 6 warnings |
| mypy | Success, 165 files |
| GUI | not run (no GUI change) |
| CI | **check on the PR** (`gh pr checks`) |

## What's next

### 1. **Merge this PR** (yours)
- **Acceptance:** `gh issue view 425 --json state` is `CLOSED`.

### 2. **Deploy `#425` once merged** (low urgency)
- Pure Python, no migration: pull, sync, restart the daemon on both hosts.
- **Acceptance:** `build_hash` on `/v1/version` equals the merge SHA on both hosts.

### 3. **Tell kastellan** (yours; carried)
- Problem+json 400s on `/v1/search`, `filters.lang` as a list, #419 syntax,
  #415 dates; `api_minor` 5 (no repeats, `attachment_filename` /
  `attachments`); `api_minor` 6 (`filters.attachment_name`, `date_local`).
  Both hosts now serve `api_minor` 6.

### 4. **Next small slice: pick with the operator**
- Candidates:
  - #383: `messages.headers` and its GIN index are write-only. Dropping them
    is a one-way door and needs `0039`.
  - #365: images embedded in the HTML are counted as attachments. The fix is
    one edit to `HAS_ATTACHMENT_SQL`, but which images count is a product
    decision.
  - #384: comment or close (2.7 ms warm for 50 prefixes).
  - #369: last-token-wins on scalar operators.
  - #373: an apostrophe inside a word is dropped.
- **Acceptance (any):** a RED test first, then full `uv run pytest -q`
  green and mypy clean.

### 5. **Carried**
- **Migrations:** `0038` is taken. #319/#318/#320 need `0039`.
- **#414 watchdog:** waits for a stack dump (SIGUSR1 works on the DGX now).
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

1. **The operator picked #425** and asked for both hosts to be deployed.
2. **Attachment chunking keeps its log-and-continue handler** (my call). It
   writes no failure row, so a contention error there just leaves the blob
   claimable, which is already the right outcome.

## Open decisions & risks

1. **Repair the pre-#431 `-0000` rows?** Mac: 2,865 of 132,320 rows carry a
   `-0000` Date and are stored 8 h off. **None** has a NULL
   `internal_date`, so the list/search/changes wire `date` (the COALESCE) is
   right. Readers of raw `date_sent` see the skew:
   - the message-detail `date` (`api/messages.py`, so MCP `get_message` too);
   - the `after:`/`before:` filters;
   - arm 4's fan-out cap (`arms.py:360`);
   - `localmail search`'s printed date;
   - the `Date:` line in embedded chunk text.

   The DGX session zone is UTC, so its rows are correct. A repair should
   re-parse `raw_bytes`, not shift by an offset. The counting query below is
   read-only. It reads only the exact `Date` key, so the count may be
   slightly low:
   `select count(*) from messages where exists (select 1 from jsonb_array_elements_text(case when jsonb_typeof(headers->'Date')='array' then headers->'Date' else '[]' end) d where d ~ '-0000\s*(\(.*\))?\s*$')`
2. **`attachment_name` is unindexed** (~0.2 s warm at 132k for a filter-only
   miss). A trigram index would be `0039`.
3. **`date_local` vs `date`:** these are different instants for most mail
   (send vs arrival). README documents it.
4. **Should a wire widening bump `api_minor`?** Still open.
5. **3,194 rows keep a skewed `internal_date`** (Mac 2,720, DGX 474). Not
   decided: leave them, NULL them, or apply a per-host SQL shift.
6. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.** After a squash
   merge `main..origin/<branch>` is non-empty by construction; compare with
   `git diff main origin/<branch> --stat` (empty = landed).
7. **Never run two localmail pytest sessions against one test DB** (port
   5532, the same cluster as the live archive).
8. **Restore a mutation from a file copy, never with `git checkout`.**
9. **Always sync with `--extra mcp --extra extraction`.** On the DGX use
   `~/.local/bin/uv`. `uv sync --dry-run` is not read-only.
10. **No ROADMAP.md.** That `/nextsession` step is a no-op.
11. **The Mac's load average was ~22 this session**, from other projects
    (bmlib, rustc). Not localmail's, but check `uptime` before trusting any
    timing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git diff main origin/fix/425-contention-errors --stat   # empty once merged (squash)
gh pr list --state all --limit 3
gh issue view 425 --json state --jq .state
gh issue list --limit 200 --json number --jq length

# Deploy — Mac (the repo is the install; no migration for #425)
unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
curl -sk https://127.0.0.1:8443/v1/version     # api_minor 6; serve restart only if build_hash matters

# Deploy — DGX
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user restart localmail-daemon localmail-serve'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4470 at session 76
uv run pytest -q tests/test_worker_db_contention.py tests/test_embed_worker.py tests/test_lang_detect.py tests/test_db_contention.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 165 files
cd gui && npm test -- --run && npm run check   # 530 at session 72
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
