# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-01 (session 71).** `main` was **`3bd0c72`** at
> session start (#427, #426 items 1–3, had merged).
>
> This session opened **one PR** on `fix/426-attachment-name-filter-date-local`:
> `ba64877` (the code) and this handoff. Its body says **`Closes #426`**:
> all five items are now shipped.
>
> **Open issues: 35** (34 once #426 closes).
>
> **Leftover worktree `.claude/worktrees/bottie-bearer-token-dbc203`**
> (detached at `5b85180`). Untouched; ask before removing it.
>
> **Neither host has #424 or #427 deployed.** The DGX is at `b07b78e`, the
> Mac DB at migration `0036`.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth or password) into Postgres, and
is **strictly read-only w.r.t. IMAP**. The database is canonical for accounts.

- **Daemon** (IDLE + poll per account), **web admin UI** (HTMX).
- **Hybrid search**, the HTTPS `/v1` server, a remote **MCP server**, and the
  opt-in `--smart` rewriter.
- **Tauri 2 + Svelte 5 GUI** under `gui/`.
- **kastellan** consumes `/v1` over REST with an API key. #426 is its list of
  search asks, finished by this PR.

## What we shipped this session

### Operations check (no commit)

- The handoff for #427 said to check the DGX's `schema_migrations` before
  merging, because `0037` was edited in place. #427 had already merged.
  Checked afterwards: the DGX DB stops at `0036_api_keys` and its checkout is
  at `b07b78e`, so no host ever applied the old `0037` text. **No reset is
  needed.**

### PR on `fix/426-attachment-name-filter-date-local` (based on `main` at `3bd0c72`), `api_minor` 6

| SHA | What |
|---|---|
| `ba64877` | #426 item 4 (`filters.attachment_name`), item 5 (`date_local`), `api_minor` 6, websearch grouping pin |
| handoff | this file + `docs/handoffs/2026-11-01T0403Z-attachment-name-date-local.md` |

- **Item 4: `filters.attachment_name`, DSL `attachment_name:`.** It keeps
  messages where some attachment's file name contains the value,
  case-insensitively. The SQL is `strpos(lower(…), lower(%s))`, so `_` and
  `%` are literal. The rule is `attachment_names.NAME_CONTAINS_SQL`, composed
  by `_filter_sql`, so every arm and the date walk apply it. It is on HTTP
  and on the MCP `search` tool.
- **Item 5: `date_local`, an opt-in `fields` name.** It gives the `Date:`
  header's instant on the sender's clock (`2023-07-12T09:58:24+10:00`). It is
  `null` with no parseable header, or for `-0000`.
  - It is read from the raw header block (new module `search/hit_dates.py`),
    the same way `?headers=list` reads it. A block past the 64 KiB ceiling is
    re-read in full with a WARNING.
  - The new pure `date_header.py` is shared with `parser._date_sent`, so
    `date_local` and `date_sent` name the same instant.
  - HTTP only, like the other `fields` names (#368).
- **Pinned kastellan's reliance**: `a b OR c d` → `'a' & 'b' | 'c' & 'd'`
  (`test_search_websearch_syntax.py`).
- Docs: README (filters and `fields` sections), `docs/mcp-usage.md`, the
  CLAUDE.md #426 entry, and the `version.py` comment.
- **Review round (same PR).** `parse_date_header` catches `OverflowError`
  (a huge year was a 500 on `date_local`); the bare `assert` in `hit_dates`
  is a skip; "`date` is UTC" was false (it is the session zone) and is
  corrected; the `NAME_CONTAINS_SQL` comment no longer claims malformed rows
  never match. Tests added: big body served from the prefix, `fields=None`
  skips both reads, lower-case `date:`, `date_sent` through `parse_message`,
  and `attachment_name` in the neutrality/NUL tables — 5 surviving mutations
  now caught. Filed, not fixed: **#429** (strict jsonpath for both name
  rules), **#430** (`Date:` overflow poison-pills sync), **#431** (`-0000`
  `date_sent` stored in the session zone).

## Verification

| check | result |
|---|---|
| RED first | 9 of 11 new tests failed. One of the two passes was for the wrong reason (an "unknown key" refusal) and was tightened |
| 9 mutations | all caught, each by its own test. One (dropping the `type() == "string"` guard) survived at first and got a fixture that catches it |
| full `pytest -q` | **4392 passed**, 6 warnings (the same set as before), 250 s |
| `mypy src/localmail` | Success, 165 files |
| cost, live Mac archive (132,315 msgs, warm) | filter-only `attachment_name` that matches nothing: **~195 ms** (walks the whole archive); a common one (`.pdf`): 1.6 ms. `date_local` for 50 hits: **2.7 ms** warm, 14 ms cold |
| CI | **check on the PR** (`gh pr checks`) |

## What's next

### 1. **Merge this PR, deploy both hosts** (yours)
- Both hosts are behind: #424 (pool bounds, SIGUSR1) and #427 (paging, and
  migration `0037`) are undeployed as well.
- Mac:
  `uv sync --frozen --extra mcp --extra extraction`, then
  `localmail init-db` (builds the `0037` index; stop the daemon first or
  accept the pause on `messages` writes), then restart the daemon and serve.
- DGX: the same through ssh (commands below).
- **Acceptance:**
  - `/v1/version` reports `api_minor` 6 on both hosts.
  - kastellan's #426 probe on the DGX passes:
    - `Fee Proposal`, `2312 Herb Fee Proposal` and `Qantas booking`, paged
      6 pages each, show 0 repeats and keep one token.
    - `230712` finds message 18106.
  - `filters: {"attachment_name": "230712"}` returns 18106.
  - `fields: ["message_id", "date_local"]` gives an offset-bearing time.
  - `kill -USR1 <daemon pid>` on the DGX prints thread stacks, and the
    daemon keeps running (the #424 acceptance, still outstanding).
- Then check that #426 is closed (`gh issue view 426`) and comment there
  with the probe results.

### 2. **Tell kastellan** (yours; carried)
- Problem+json 400s on `/v1/search`, `filters.lang` as a list, web-search
  syntax (#419), #415 dates.
- `api_minor` 5: no repeats after a pool grow; `fields`
  `attachment_filename` / `attachments`.
- `api_minor` 6: `filters.attachment_name`; `fields` `date_local`.

### 3. **Next small slice: #421**
- The GUI tokenizer drops quotes on a single quoted word (`"OR"`,
  `"-acme"`). `filter_parse.tokenize` must record which tokens were quoted,
  so `joinTokens` re-quotes them.
- **Acceptance:**
  - A RED vitest test: `a "OR" b` and `"-acme"` survive a popover Apply and
    `absorbConflictingOperators`.
  - The fix.
  - `npm test -- --run` and `npm run check` both green.

### 4. **Carried**
- **Migrations:** `0037` is taken. #319/#318/#320 need `0038`.
- **#414 watchdog:** waits for a stack dump.
- **Dependabot #71 / #185:** left open on purpose.
- **docling > 2.144:** probe the next release when it lands.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **#384** (the cost of the `raw_bytes` prefix read) is now partly answered:
  2.7 ms warm for 50 prefixes. Comment on it, or close it.
- **Issues:** #383, #378, #369, #373, #372, #368, #365; #375; #374 + #305;
  #358/#357; #330/#327/#328; #340, #363, #285; robustness #218, #226,
  #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **#426 items 4 and 5: build both** (operator).
2. **`date_local` re-reads the full message when the header block passes
   the ceiling**, rather than returning `null`. That matches
   `?headers=list`, and 0 live messages need it (my call).
3. **`api_minor` bumped to 6** for the two wire additions, following the
   house convention of one bump per wire feature (my call; the general
   question, open decision 3, is still open).

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
git log --oneline main..origin/fix/426-attachment-name-filter-date-local   # empty once merged
gh pr list --state all --limit 3
gh issue view 426 --json state --jq .state
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
unset VIRTUAL_ENV && uv run pytest -q              # 4392 at session 71
uv run pytest -q tests/test_search_attachment_name_filter.py tests/test_search_date_local.py tests/test_date_header.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 165 files
cd gui && npm test -- --run && npm run check
```

The latest migration is **`0037_messages_attachment_names_idx.sql`**; the next
free slot is `0038_*.sql`.
