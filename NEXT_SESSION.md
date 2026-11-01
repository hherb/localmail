# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-01 (session 72).** `main` was **`62bdec6`** at
> session start (#428 had merged; #426 is closed).
>
> This session opened **one PR** on `fix/421-gui-requote-quoted-tokens`:
> `f6072bd` (the code) and this handoff. Its body says **`Closes #421`**.
>
> **Open issues: 38** (37 once #421 closes).
>
> **Leftover worktree `.claude/worktrees/bottie-bearer-token-dbc203`**
> (detached at `5b85180`). Untouched; ask before removing it.
>
> **Neither host has #424, #427 or #428 deployed.** The DGX is at
> `b07b78e`, the Mac DB at migration `0036`. This PR is GUI-only and needs
> no server deploy.

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

### PR on `fix/421-gui-requote-quoted-tokens` (based on `main` at `62bdec6`)

| SHA | What |
|---|---|
| `f6072bd` | #421: a token typed with quotes keeps them when the GUI rebuilds the query |
| handoff | this file + `docs/handoffs/2026-11-01T0517Z-gui-requote-quoted-tokens.md` |

- **The defect.** `filter_parse.tokenize` dropped every `"`, so
  `joinTokens` could not tell `"OR"` from `OR` or `"-acme"` from `-acme`.
  On a popover Apply, or when a typed operator is absorbed into a chip, the
  quoted word came back as the operator / an exclusion (#419 syntax).
- **The fix.** New `tokenizeRaw(s): Token[]` with `{text, raw}`; `raw`
  keeps closed `"…"` delimiters, as the server's `_tokenize` does (an
  unclosed `"` is dropped, where the server keeps it). `joinTokens`
  takes `Token[]` and re-emits a quoted token as typed. A raw spelling with
  an apostrophe *outside* its quotes falls back to the old whole-token
  wrapping, because the server reads that `'` as a quote opener.
  `tokenize` keeps returning `string[]` (it is `tokenizeRaw(...).map(text)`).
- **Visible change:** a kept `to:"bob smith"` is now rebuilt as typed,
  `to:"bob smith"`, rather than `"to:bob smith"`. Two test expectations
  moved accordingly.
- CLAUDE.md: the #419 GUI bullet and the #376 `joinTokens` bullet.

## Verification

| check | result |
|---|---|
| RED first | 7 new tests, all 7 failed before the fix |
| 4 mutations | all caught: no raw return (9 fail), no closing `"` in raw (14), no apostrophe guard (1, its own case), no `raw` reset on an unclosed quote (1, its own round-trip case) |
| `npm test -- --run` | **530 passed**, 49 files (528 + 2 added in review) |
| review | 2 more mutations survived (no `rawBeforeQuote` save, non-global quote strip); each now has its own test and fails it. Server-token differential re-run on the new join: 3 seeds × 30,000, 0 mismatches (51 with the apostrophe guard removed) |
| `npm run check` | 0 errors, 0 warnings |
| server agreement | `parse_query` keeps `a "OR" b` and `"-acme" invoice` as free text with no exclusion; `to:"bob smith"` is the `to` filter |
| Python suite | not run (no Python changed) |
| CI | **check on the PR** (`gh pr checks`) |

## What's next

### 1. **Deploy both hosts** (yours; #428 is merged, #426 closed)
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
- Then comment on #426 with the probe results.

### 2. **Tell kastellan** (yours; carried)
- Problem+json 400s on `/v1/search`, `filters.lang` as a list, web-search
  syntax (#419), #415 dates.
- `api_minor` 5: no repeats after a pool grow; `fields`
  `attachment_filename` / `attachments`.
- `api_minor` 6: `filters.attachment_name`; `fields` `date_local`.

### 3. **Merge this PR** (yours)
- GUI-only; `gui-ci` and `python-ci` should both be green.
- **Acceptance:** `gh issue view 421 --json state` is `CLOSED`.

### 4. **Next small slice: #430** (a `Date:` header with an overflowing year poison-pills sync)
- `parse_date_header` already catches `OverflowError` for `date_local`
  (#428); sync still reads `msg.get("Date")`, which raises out of the
  stdlib header parser.
- **Acceptance:**
  - A RED test: a message whose `Date:` has a year past 9999 goes through
    `process_one_message` and is stored (with `date_sent` NULL), not
    recorded in `failed_messages`.
  - The fix, at the header read (the #314 `_header_text` seam is the
    likely place).
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

1. **#421: reuse the typed spelling rather than invent a quoted flag** (my
   call). Mirroring the server's `raw` keeps every quote where it was typed
   (`"-acme"` vs `-"acme"`, `to:"bob smith"`), which a boolean could not.
2. **`tokenize` keeps its `string[]` shape** (my call); only `joinTokens`
   changed signature, and its two callers are in the same module.

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
git log --oneline main..origin/fix/421-gui-requote-quoted-tokens   # empty once merged
gh pr list --state all --limit 3
gh issue view 421 --json state --jq .state
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
unset VIRTUAL_ENV && uv run pytest -q              # 4392 at session 71 (not re-run in 72)
uv run pytest -q tests/test_search_attachment_name_filter.py tests/test_search_date_local.py tests/test_date_header.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 165 files
cd gui && npm test -- --run && npm run check   # 530 at session 72
```

The latest migration is **`0037_messages_attachment_names_idx.sql`**; the next
free slot is `0038_*.sql`.
