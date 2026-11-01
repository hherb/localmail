# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-01 (session 73).** `main` was **`f906aa6`** at
> session start (#432 merged; #421 is closed).
>
> This session opened **one PR** on `fix/430-date-header-overflow`:
> `5fc1322` (the code) and this handoff. Its body says **`Closes #430`**.
>
> **Open issues: 37** (36 once #430 closes).
>
> **Leftover worktree `.claude/worktrees/bottie-bearer-token-dbc203`**
> (detached at `5b85180`). Untouched; ask before removing it.
>
> **Neither host has #424, #427, #428 or #432 deployed.** The DGX is at
> `b07b78e`, the Mac DB at migration `0036`.

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

### PR on `fix/430-date-header-overflow` (based on `main` at `f906aa6`)

| SHA | What |
|---|---|
| `5fc1322` | #430: an overflowing `Date:` year is stored with `date_sent` NULL, not poison-pilled |
| handoff | this file + `docs/handoffs/2026-11-01T0600Z-date-header-overflow.md` |

- **The defect.** `parser._date_sent` called `msg.get("Date")` unguarded.
  A year too large for a C long raises `OverflowError` out of the stdlib's
  `DateHeader.parse` (it catches only `ValueError`), so the message went to
  `failed_messages` permanently.
- **The fix.** One line: `_date_sent` reads through the existing
  `_header_text` guard (#314), so an unreadable `Date:` is absent and
  `date_sent` is NULL. `date_local` was already safe (#428).
- **Not done:** existing `failed_messages` rows of this cause were not
  looked for; `localmail retry-failed` on each host will now store them.
- New test file `tests/test_date_header_overflow_sync.py` (real header,
  through `parse_message` and `process_one_message`). CLAUDE.md's #426
  `date_local` bullet updated.

## Verification

| check | result |
|---|---|
| RED first | both new tests failed with `OverflowError` before the fix |
| `uv run pytest -q` | **4424 passed**, 6 warnings (4392 at session 71) |
| mypy | Success, 165 files |
| GUI | not run (no GUI change) |
| CI | **check on the PR** (`gh pr checks`) |

## What's next

### 1. **Deploy both hosts** (yours; carried from session 72)
- Undeployed: #424 (pool bounds, SIGUSR1), #427 (paging, migration
  `0037`), #428 (`api_minor` 6), #432 (GUI), and this PR once merged.
- Mac: `uv sync --frozen --extra mcp --extra extraction`, then
  `localmail init-db` (builds the `0037` index), then restart daemon and serve.
- DGX: the same through ssh (commands below).
- **Acceptance:**
  - `/v1/version` reports `api_minor` 6 on both hosts.
  - kastellan's #426 probe on the DGX passes (`Fee Proposal`,
    `2312 Herb Fee Proposal`, `Qantas booking`: 6 pages each, 0 repeats;
    `230712` finds 18106; `filters.attachment_name` `230712` → 18106;
    `fields: ["message_id","date_local"]` gives an offset-bearing time).
  - `kill -USR1 <daemon pid>` on the DGX prints stacks and the daemon keeps
    running (#424).
  - After this PR deploys: `localmail retry-failed` on each host; any
    `OverflowError` rows in `failed_messages` succeed.
- Then comment on #426 with the probe results.

### 2. **Tell kastellan** (yours; carried)
- Problem+json 400s on `/v1/search`, `filters.lang` as a list, #419 syntax,
  #415 dates; `api_minor` 5 (no repeats, `attachment_filename` /
  `attachments`); `api_minor` 6 (`filters.attachment_name`, `date_local`).

### 3. **Merge this PR** (yours)
- **Acceptance:** `gh issue view 430 --json state` is `CLOSED`.

### 4. **Next small slice: #431** (a `-0000` `date_sent` is stored in the session zone)
- `parse_date_header` returns a *naive* datetime for `-0000`; psycopg sends
  naive as `timestamp`, which Postgres reads in the session zone — #415's
  shape for `date_sent`.
- **Acceptance:**
  - A RED test: a `Date: … -0000` message stored through
    `process_one_message` has `date_sent` equal to the header's UTC instant
    regardless of the session `TimeZone` (set it to a non-UTC zone in the test).
  - Fix where `date_sent` is produced (treat `-0000` as UTC for storage only;
    `date_local` must still be `None` for it).
  - Full `uv run pytest -q` green.

### 5. **Carried**
- **Migrations:** `0037` is taken. #319/#318/#320 need `0038`.
- **#414 watchdog:** waits for a stack dump.
- **Dependabot #71 / #185:** left open on purpose.
- **docling > 2.144:** probe the next release when it lands.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **#384** (the cost of the `raw_bytes` prefix read) is now partly answered:
  2.7 ms warm for 50 prefixes. Comment on it, or close it.
- **Issues:** #431, #429, #425, #383, #378, #369, #373, #372, #368, #365; #375; #374 + #305;
  #358/#357; #330/#327/#328; #340, #363, #285; robustness #218, #226,
  #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **#430: reuse `_header_text` rather than a Date-specific catch** (my
   call). Same failure class as #314, same guard; one rule for reading a
   structured header.

## Open decisions & risks

1. **`attachment_name` is unindexed.** A selective filter-only miss walks the
   whole archive (~0.2 s warm at 132k). Combined with a text query, it costs
   only the rows that query admits. If kastellan leans on it, a trigram index
   over the names would be its own migration (`0038`).
2. **`date_local` vs `date`:** `date` is `COALESCE(internal_date,
   date_sent)`, so for most mail `date_local` names a *different* instant:
   send time, where `date` is arrival time. That is intended (it is "the
   sender's clock"), but a consumer could read it as `date` in another zone.
   The README says so (and that `date` is in the server's session zone,
   not UTC as #428 first claimed).
3. **Should a wire widening bump `api_minor`?** The general question is still
   open; this session followed precedent.
4. **3,194 rows keep a skewed `internal_date`** (Mac 2,720, DGX 474). Not
   decided: leave them, NULL them, or apply a per-host SQL shift.
5. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.**
6. **Never run two localmail pytest sessions against one test DB** (port
   5532, the same cluster as the live archive).
7. **Restore a mutation from a file copy, never with `git checkout`.** Quote
   mutation patterns through a heredoc'd script. A zsh `"…\"…"` silently
   changes the pattern, and the "mutation" then tests nothing (happened this
   session; it was caught by the assert in the script).
8. **Always sync with `--extra mcp --extra extraction`**. On the DGX use
   `~/.local/bin/uv`. `uv sync --dry-run` is not read-only.
9. **No ROADMAP.md.** That `/nextsession` step is a no-op.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git log --oneline main..origin/fix/430-date-header-overflow   # empty once merged
gh pr list --state all --limit 3
gh issue view 430 --json state --jq .state
gh issue list --limit 200 --json number --jq length

# Deploy — Mac (the repo is the install)
unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
uv run localmail init-db                # builds the 0037 index; messages writes pause meanwhile
launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
launchctl kickstart -k gui/$(id -u)/com.localmail.serve
curl -sk https://127.0.0.1:8443/v1/version     # api_minor 6

# Deploy — DGX
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user stop localmail-daemon && .venv/bin/localmail init-db && systemctl --user start localmail-daemon && systemctl --user restart localmail-serve'
ssh 10.0.0.3 'docker exec localmail-pg psql -U localmail -d localmail -Atc "select revision from schema_migrations order by 1 desc limit 1"'   # 0037_…
ssh 10.0.0.3 'kill -USR1 $(systemctl --user show -p MainPID --value localmail-daemon); sleep 2; journalctl --user -u localmail-daemon --since "-1min" | grep -c "Thread 0x"'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4424 at session 73
uv run pytest -q tests/test_search_attachment_name_filter.py tests/test_search_date_local.py tests/test_date_header.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 165 files
cd gui && npm test -- --run && npm run check   # 530 at session 72
```

The latest migration is **`0037_messages_attachment_names_idx.sql`**; the next
free slot is `0038_*.sql`.
