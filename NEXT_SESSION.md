# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-03 (session 81).** `main` is **`7bd1448`** (#445:
> #440, closed).
>
> This session has **one open PR** on `fix/373-apostrophe-is-literal`. Its
> body says **`Closes #373`**. It carries the #373 code and this handoff.
>
> **Both hosts are deployed at `7bd1448`**; nothing from the #373 PR is
> deployed.
>
> **Open issues: 33** (32 once the #373 PR merges).
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

### Deploy of `7bd1448` (#445, #440) to both hosts (no commit)

- **Mac:** `uv sync --frozen --extra mcp --extra extraction` (no change),
  kickstarted `com.localmail.serve` only.
- **DGX:** pulled → `7bd1448`, synced (reinstalled `nvidia-cusparselt-cu13`
  again, lock unchanged), restarted `localmail-serve`. Both units `active`.
- Both `/v1/version`: `api_minor` 6, `build_hash` `7bd1448`.

### PR on `fix/373-apostrophe-is-literal` (based on `main` at `7bd1448`)

| SHA | What |
|---|---|
| `2edbae3` | #373: an apostrophe is literal text, `"` the only quote |
| handoff | this file + `docs/handoffs/2026-11-03T0345Z-apostrophe-is-literal.md` |

- **The defect.** `query._tokenize` opened a quote at `'` and dropped it, so
  `O'Brien` became `OBrien`, which no tsvector holds (`'o' 'brien'`): no
  lexical match, nothing on `sort=date`, and the vector arm and rewriter saw
  `OBrien` too.
- **The fix (the issue's option 2).** `'` is literal; `"` is the only quote.
  The GUI's tokenizer already did this, so client and server now agree.
  `websearch_to_tsquery('simple', "O'Brien")` is `'o' <-> 'brien'` (checked
  in Postgres), which matches. Option 1 (`'` quotes only at a token's start)
  was not taken: it keeps an undocumented second quote syntax.
- **Cost:** `from:'Anna Schmidt'` (undocumented) now splits; use `"`.
  `has:'attachment` is now a 400 (unknown `has:` value).
- **Tests:** 3 new in `test_query_parser.py`; the #367 end-to-end test now
  seeds `O'Brien` (it seeded `OBrien` to dodge this) and was RED on all 4
  cases before the fix; `test_search_websearch_syntax.py`'s `it's late`
  case pinned the bug and was updated; one open-quote case swapped `'` for
  `"`. GUI `joinTokens` code unchanged (its `'` wrapping is harmless here
  and still right against a pre-#373 server); comments updated. README,
  CLAUDE.md, `build_query_string`'s docstring updated. No migration, no
  wire change.

## Verification

| check | result |
|---|---|
| RED first | 10 failed (6 parser, 4 end-to-end) before the fix |
| `uv run pytest -q` | 4524 passed + 1 failed (the stale `it's late` pin), then that file 57 passed → **4525** |
| mypy | Success, 165 files |
| GUI `npm test` | 530 passed |
| load | 7–13 (another project's pytest running, not localmail's) |
| CI | **check on the PR** (`gh pr checks`) |

## What's next

### 1. **Merge the #373 PR** (yours), once CI is green
- **Acceptance:** `gh pr checks <n>` all pass; after the merge
  `gh issue view 373` is `CLOSED`.

### 2. **Deploy #373 once merged**
- Pure Python, no migration. Search runs in **serve**, so restarting serve
  is enough.
- **Acceptance:** `build_hash` on `/v1/version` equals the merge SHA on both
  hosts; units `active`; a search for a name with an apostrophe returns
  lexical hits.

### 3. **Next small slice: pick with the operator**
- Candidates: #374 (CLI composer: filters after free text; an unclosed `"`
  still swallows its flags; pairs with #305), #369 (silent last-token-wins
  on conflicting scalar operators; needs refuse-vs-keep call), #378 (an
  operator with an empty value becomes free text), #383 (drop write-only
  `messages.headers` + GIN in `0039`; one-way door), #365 (product call).
- **Acceptance (any):** a RED test first, then full `uv run pytest -q`
  green and mypy clean.
- **Tell kastellan** (yours; carried): problem+json 400s on `/v1/search`,
  `filters.lang` as a list, #419 syntax, #415 dates, `api_minor` 5 and 6
  features, and now #373 (`'` is literal).

### 4. **Carried**
- **Migrations:** `0038` is taken. #319/#318/#320 need `0039`.
- **#414 watchdog:** waits for a stack dump (SIGUSR1 works on the DGX).
- **Dependabot #71 / #185:** left open on purpose.
- **docling > 2.144:** probe the next release when it lands.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **Mac `failed_messages`:** one `iso2022_jp` decode failure from
  2026-07-25 (`hherb-dmc/[Gmail]/Important` uid 5263). Not investigated.
- **Issues:** #383, #378, #369, #372, #368, #365; #375; #374 + #305;
  #358/#357; #330/#327/#328; #340, #363, #285; robustness #218, #226,
  #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **The operator asked for `7bd1448` on both hosts and picked #373** (over
   #369/#383/#378).
2. **#373: option 2** (drop single-quote quoting, match the GUI) — my call,
   recorded in the PR; revert is one line if you prefer option 1.

## Open decisions & risks

0. **Since #438, `localmail import` exits 1 on a failed job** (behaviour
   change for scripts; none known on either host). Since #441, a contended
   poison-pill record also fails the job / aborts the sync pass. Since #442,
   an import whose final status write fails on all 5 attempts (~40 s) also
   exits 1, and the row stays `running` until serve restarts. Since #440,
   a re-imported pill bumps its existing `failed_messages` row; duplicates
   made before #440 are not merged (none exist on either host, checked
   2026-11-03). Since #373, `'` in a search query is literal text:
   an undocumented `from:'Anna Schmidt'` now splits, and `has:'attachment`
   is a 400 (unknown `has:` value).
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
git diff main origin/fix/373-apostrophe-is-literal --stat   # empty once merged (squash)
gh pr list --state all --limit 3
gh issue view 373 --json state --jq .state
gh issue list --limit 200 --json number --jq length

# Deploy — Mac (the repo is the install; no migration for #373; serve only)
unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
launchctl kickstart -k gui/$(id -u)/com.localmail.serve
curl -sk https://127.0.0.1:8443/v1/version     # api_minor 6, build_hash = merge SHA

# Deploy — DGX
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user restart localmail-serve && systemctl --user is-active localmail-daemon localmail-serve'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'

# Load before trusting any timing; no other LOCALMAIL pytest may be running
# (check the cwd: other projects' pytest runs are harmless).
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4525 at session 81
uv run pytest -q tests/test_query_parser.py tests/test_api_search_filters_before_free_text.py tests/test_search_websearch_syntax.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 165 files
cd gui && npm test -- --run && npm run check   # 530 at session 81
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
