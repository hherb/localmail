# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-11-04 (session 84).** `main` is **`82540f9`** (#448:
> #305's search-catch half; #305 stays open for the import half).
>
> This session has **one open PR** on `fix/378-refuse-empty-fixed-operators`
> (closes #378). It carries the code and this handoff.
>
> **Mac is deployed at `82540f9`; the DGX is still at `08d329c`** — my ssh
> deploy was refused by the permission classifier and the operator said they
> would run it. #305 is CLI-only, so only the DGX `build_hash` lags.
>
> **Open issues: 31** (30 once #378 closes).
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

### Deploy of `82540f9` (#448) — Mac only (no commit)

- `uv sync --frozen --extra mcp --extra extraction` (no change), kickstarted
  `com.localmail.serve`; `/v1/version` reports `api_minor` 6, `build_hash`
  `82540f9`.
- **Acceptance met:** `localmail search 'has:attachments'` exits 2 with
  `Error: has: expected 'attachment' or 'no-attachment', got 'attachments'`.
- **DGX not deployed** (classifier refused remote shell writes; operator to run).

### PR on `fix/378-refuse-empty-fixed-operators` (based on `main` at `82540f9`)

| SHA | What |
|---|---|
| `98cf39b` | #378: refuse an empty or unparsable fixed-shape operator |
| handoff | this file + `docs/handoffs/2026-11-04T1000Z-refuse-empty-fixed-operators.md` |

- **The defect.** `parse_query` kept an operator with an empty value, and an
  `account_id:`/`folder_id:` whose value is not an int, as free text. `has:"`
  therefore searched for the word "has" and flipped the query to rankable.
- **The fix.** `query._FIXED_SHAPE_OPERATORS` = `after`, `before`, `has`,
  `account_id`, `folder_id`: an empty value raises `QueryParseError("<op>:
  empty value not allowed")`; a non-int id raises `"<op>: expected an integer
  id, got …"` (`_parse_id`). Reaches the wire as a 400 via `_gate_query`, the
  CLI as a usage error (#448).
- **Ruling (mine, per the issue's caution):** text operators (`from`, `to`,
  `subject`, `label`, `account`, `folder`, `attachment_name`) keep an empty
  value as free text — `subject: meeting`, `reply to: bob` are ordinary
  queries. Pinned as the deliberate half.
- **Tests:** new `tests/test_query_operator_values.py` (20: 12 RED first,
  8 controls); `MALFORMED` in `test_api_search_malformed_query.py` gained
  three shapes (all three `run_search` branches); `has:"` moved out of
  `_OPEN_QUOTE_TEXT`; `test_account_id_non_integer_value_treated_as_free_text`
  became `…_is_refused`.
- README (the "honoured or refused" paragraph) and CLAUDE.md (#364 section)
  updated.

## Verification

| check | result |
|---|---|
| RED first | 12 of 20 new tests failed against `main` |
| `uv run pytest -q` | **4592 passed**, 6 warnings (first run caught the old free-text pin, since rewritten) |
| mypy | Success, 166 files |
| GUI | untouched |
| CI | **check on the PR** (`gh pr checks`) |

## What's next

### 1. **Merge the #378 PR** (yours), once CI is green
- **Acceptance:** `gh pr checks <n>` all pass; `gh issue view 378` CLOSED.

### 2. **Deploy** (DGX needs `82540f9` anyway; then the #378 merge on both)
- `/v1/search` changes, so restart `serve` on both hosts.
- **Acceptance:** both `/v1/version` show the merge SHA;
  `curl` a `POST /v1/search` with `{"query":"has:\""}` → 400 problem+json
  `has: empty value not allowed`.

### 3. **Next small slice: pick with the operator**
- Candidates: #305's other half (defer `cli.py`'s module-scope imports so
  `--version` survives a missing dependency; touches many commands), #369
  (last-token-wins; needs a refuse-vs-keep call), #383 (drop write-only
  `messages.headers` + GIN in `0039`; one-way door), #365 (product call).
- **Acceptance (any):** a RED test first, then full `uv run pytest -q`
  green and mypy clean.
- **Tell kastellan** (yours; carried): problem+json 400s on `/v1/search`,
  `filters.lang` as a list, #419 syntax, #415 dates, `api_minor` 5 and 6
  features, #373 (`'` is literal), and now #378 (an empty
  `after:`/`before:`/`has:` or a non-int id in `query` is a 400).

### 4. **Carried**
- **Migrations:** `0038` is taken. #319/#318/#320 need `0039`.
- **#414 watchdog:** waits for a stack dump (SIGUSR1 works on the DGX).
- **Dependabot #71 / #185:** left open on purpose.
- **docling > 2.144:** probe the next release when it lands.
- **emSign G3:** re-check the trust stores after OS updates.
- **kastellan slice C:** ask whether it is still wanted.
- **Mac `failed_messages`:** one `iso2022_jp` decode failure from
  2026-07-25 (`hherb-dmc/[Gmail]/Important` uid 5263). Not investigated.
- **Issues:** #383, #369, #372, #368, #365; #375; #305 (imports);
  #358/#357; #330/#327/#328; #340, #363, #285; robustness #218, #226,
  #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **The operator picked #378** and said they would deploy the DGX themselves.
2. **#378:** refuse only the fixed-shape operators; empty text operators stay
   free text (my call, following the issue's own caution).

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
   is a 400 (unknown `has:` value). Since #374, in `localmail search` a
   query operator out-votes the matching flag, and `--from '""'` exits 2.
   Since #305's half, a malformed `localmail search` query exits 2 with a
   one-line error instead of 1 with a traceback. Since #378, an empty
   `after:`/`before:`/`has:`/`account_id:`/`folder_id:`, or a non-int id,
   in a query is a 400 / exit 2 (it was served as free text). The GUI
   shows it as an error banner.
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
10. **Load on the Mac was ~11–14 this session** (full suite 5½ min). Check
    `uptime` before trusting any timing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git diff main origin/fix/378-refuse-empty-fixed-operators --stat   # empty once merged (squash)
gh pr list --state all --limit 3
gh issue view 378 --json state --jq .state   # CLOSED once merged
gh issue list --limit 200 --json number --jq length

# Deploy — Mac (the repo is the install; no migration for #378; restart serve)
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
unset VIRTUAL_ENV && uv run pytest -q              # 4592 at session 84
uv run pytest -q tests/test_query_operator_values.py tests/test_api_search_malformed_query.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 166 files
cd gui && npm test -- --run && npm run check   # 530 at session 81
```

The latest migration is **`0038_messages_attachment_names_strict_idx.sql`**;
the next free slot is `0039_*.sql`.
