# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-10-23 (session 59).** `main` was **`e1900b8`** at
> session start (#407 merged, closing #405), and is unchanged.
>
> This session:
> - **deployed #407 on both hosts.** `/v1/version` → `build_hash: e1900b8`
>   on the Mac and the DGX; DGX units `active`, `NRestarts=0`. Local branch
>   `fix/405-broken-docling-import` deleted (its diff against `main` was
>   empty).
> - **fixed #377: PR on `fix/377-nul-in-search-query`.**
>
> **Open issues: 39, 38 once the PR merges** (`Closes #377`).
>
> **Things to know before you touch anything.**
>
> **(1) The Mac deployment tree (`~/src/localmail`) is on the PR branch,
> not `main`.** The branch touches `search/query.py`, which **serve** runs
> (and the daemon imports, but never parses a query). Neither agent was
> restarted, so the running code is still `e1900b8`'s. `git checkout main`
> after the merge is step 0.
>
> **(2) Still no scanned PDF on the Mac since the docling 2.141 deploy.**
> The newest `docling%` row is still `docling@2.97.0` from 2026-10-01, and
> `transient_extractions` is 0.
>
> **(3) A leftover worktree, `.claude/worktrees/bottie-bearer-token-dbc203`**
> (detached at `5b85180`), predates session 56. Untouched; ask before
> removing it.

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

### PR: `fix/377-nul-in-search-query` (opened on `main`)

| SHA | What |
|---|---|
| `ca24cbf` | #377: a NUL (U+0000) in the search query or a text filter is refused with a 400 / `ToolError` naming the field; tests, CLAUDE.md, README |
| handoff | this file + `docs/handoffs/2026-10-23T1158Z-nul-in-search-query.md` |

- **The defect.** A NUL in `query`, `from`, `to`, `subject` or `lang`
  reached psycopg as `DataError` ("PostgreSQL text fields cannot contain
  NUL"): an unhandled 500 on `/v1/search`, an unmapped exception on MCP.
  For a caller granted nothing it was a 200 empty page instead (the
  empty-ACL short-circuit never touches SQL).
- **The fix (`search/query.py`).** `_reject_nul(tok)`, called per token at
  the top of `parse_query`'s loop, raises `QueryParseError("<field>: contains
  a NUL character (U+0000), which cannot be searched")`. The field is the
  token's operator if it is one of `_OPERATORS`, else `query`.
  - **Why the parser, not `build_query_string`:** every path parses before
    any IO. `_gate_query` already maps `QueryParseError` → `ValidationFailed`
    ahead of the empty-ACL short-circuit; `Searcher.search` parses too, so
    library/CLI callers get a named error instead of `DataError`; and the
    structured filters arrive composed into that same string.
  - **Complete by construction:** the tokenizer drops only whitespace and
    quote characters, so every NUL lands in some token. Operators typed in
    the query (`label:`, `account:`, `folder:`) are covered too.
- **Tests (`tests/test_search_nul.py`, new, 33 tests).** Parser naming (12
  shapes incl. quoted values, non-operators, `account_id:`), a positive
  control, `run_search` × 5 fields × {empty ACL, granted}, the HTTP route
  (400 problem+json, `detail` names the field) and MCP through
  `server.call_tool` with the real `tool_search` (`ToolError`).
- **Mutation:** replacing the `_reject_nul(tok)` call with `pass` fails 32
  of 33 (the positive control is the survivor, as it should be).

## Verification (Mac, `~/src/localmail` venv, all extras, `ca24cbf`)

| gate | result |
|---|---|
| pytest | **4113 passed, 0 failed**, 6 warnings, 254 s |
| mypy | Success, **160** files |
| focused (`test_search_nul`, `test_query_*`, `test_api_search*`, `test_serve_search_route`, `test_mcp_server_build`, `test_searcher*`) | 731 passed |
| CI | see `gh pr checks`; I stopped at the push |

- **Load** was 13–29 during the run (bmlibrarian's pytest was running
  against its own database), so no timing is comparable.
- **GUI untouched**, so cargo and vitest were not re-run.
- **No review round was run** (`/review-pr`). Consider one before merging.

## What's next

### 0. **Merge the PR, return the Mac to `main`** *(acceptance below)*
- **You merge**, once both CI legs are green.
- **Mac:**
  1. `git checkout main && git pull`;
  2. restart **serve** (the change is on the search path; restarting the
     daemon too is harmless and keeps both on one commit):
     `launchctl kickstart -k gui/$(id -u)/com.localmail.serve` and the same
     for `com.localmail.daemon`;
  3. delete the local branch. After a squash merge, `git branch -d` refuses,
     so first check `git diff --stat main fix/377-nul-in-search-query` is
     empty, then use `-D`.
- **DGX:** `git pull --ff-only`, then restart both units.
- **Acceptance:**
  - `/v1/version` reports the merge commit on both hosts;
  - `gh issue view 377` is closed.

### 1. **Pillow 13, once fastembed lifts its cap** (carried)
- qdrant/fastembed#830 was still **open** on 2026-10-23 (session 59).
- **When it merges:**
  1. Move `[tool.uv.sources] fastembed`'s `rev` to a commit that has it.
  2. Raise the dev group's `Pillow>=10.3` to `>=13.0.0` and re-lock.
  3. Run the full suite and `probe_docling_ocr.py` on **both** hosts
     (`--engine auto --engine rapidocr` on the Mac).
- **Acceptance:** the lock holds Pillow 13.x, both probes exit 0, and the
  Pillow alerts close.

### 2. **Watch the first scanned PDF on the Mac** (carried)
- **Acceptance:** a new `attachment_text` row with `extractor LIKE
  'docling@2.141%'` and non-empty text, and `transient_extractions` still 0.

### 2a. **emSign G3 in the OS trust stores** (carried)
- Re-check after each OS or `ca-certificates` update (commands below).

### 3. **kastellan** (carried, yours)
- Ask whether it still wants slice C (`/problems/token-expired`); its API
  key never expires.
- Tell it about `?headers=list` (api_minor ≥ 1), attachment index paging
  (≥ 2; never compute the offset from `text.length`), and `fields` /
  `snippet_chars` (≥ 3).

### 4. **Carried issues**
- **Next small slice:** #394 — `fields` on `/v1/search` is unbounded
  (`len(fields)` × `len(HIT_FIELDS)` per hit; 20,000 names cost 212 ms on
  200 hits).
  - **Acceptance:** a `fields` list over a stated cap (or with duplicates,
    if the decision is to refuse them) is a 400 naming the cap, decided
    before the empty-ACL short-circuit; `fields_error` membership tested
    against a `frozenset`, not the tuple. It is a wire-contract change, so
    decide the cap (e.g. `len(HIT_FIELDS)` after dedup) first and say so in
    README.
- **Then:** #395, #386, #389, #380, #383, #384, #378, #369, #373, #372,
  #370, #371, #368, #365.
- **Not filed yet:** the `snippet_source="header"` mislabel on a
  whole-message lexical win.
- **Larger work:**
  - #375 (Mac build-hash timeout cache);
  - #374 + #305 (the `cli.py` refactor, 2177 lines) — note the CLI's search
    still does not catch `QueryParseError`, so a NUL there is now a named
    traceback rather than a `DataError` one;
  - #358/#357, #330/#327/#328, #319/#318/#320 (the next migration slot is
    `0037_*.sql`), #340, #363, #285 (ruff in CI);
  - lifting the rapidocr cap, with its own probe;
  - admin GUI phase 5, the Users panel;
  - robustness: #218, #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **A NUL is refused, not stripped.** The issue left it open; refusal is
   the #364 rule, and `pgtext.strip_nuls` is the rule for *stored* text.
2. **The rule is in `parse_query`, not the api composer**, so it covers
   HTTP, MCP, library and CLI callers with one check.
3. **`account_id:1\0` is named `account_id`, not `query`**: the refusal
   runs before an unparseable id would fall back to free text.
4. **Deploying #407 went ahead without a fresh confirmation**, following
   the handoff's step 0 and the precedent of sessions 54–58.
5. **Code and handoff are in one PR**, per the convention.

## Open decisions & risks

1. **Pillow alerts stay open**, some critical, until fastembed#830 ships.
2. **The Mac's extraction path is still unexercised by real mail** under
   docling 2.141.
3. **The Mac tree is on the PR branch until step 0.**
4. **Other endpoints taking free text were not audited for NUL** (e.g.
   `/v1/messages` takes ids only; admin forms take names). Only search was
   in #377's scope.
5. **`api_minor` stays 3.** A request that used to 500 now 400s; nothing
   that worked changed.
6. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.**
7. **Never run two pytest sessions against one test database.**
8. **Restore a mutation from a file copy, never with `git checkout`.**
9. **The test DB is on port 5532**, the same cluster as the live archive.
10. **Always sync with `--extra mcp --extra extraction` on both hosts**, and
    use `~/.local/bin/uv` on the DGX.
11. **`uv sync --dry-run` is not read-only; `uv lock --dry-run` is.**
12. **No ROADMAP.md.** That `/nextsession` step is a no-op.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status && git branch --show-current  # fix/377-nul-in-search-query until step 0
git log --oneline -1 origin/main         # THE authority
git worktree list

# Is the PR merged?
gh pr list --state all --head fix/377-nul-in-search-query --json number,state,mergeCommit
gh issue list --limit 200 --json number --jq length       # 38 after the merge
gh api --paginate repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | .dependency.package.name' | sort | uniq -c
gh pr view 830 --repo qdrant/fastembed --json state,mergedAt   # the Pillow unblock

# Deployment health
launchctl list | grep localmail
curl -sk https://127.0.0.1:8443/v1/version
tail -3 ~/localmail-probe/tunnel-probe.log
ssh -o ConnectTimeout=8 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'
psql -p 5532 -d localmail -Atc "select extractor, count(*), max(extracted_at) from attachment_text where extractor like 'docling%' group by 1"
psql -p 5532 -d localmail -Atc "select count(*) from transient_extractions"
grep -c "emSign ECC Root CA - G3" /etc/ssl/cert.pem
ssh 10.0.0.3 'ls /etc/ssl/certs | grep -ic emsign_ecc_root_ca_-_g3'

# After merge — Mac:
#   git checkout main && git pull
#   launchctl kickstart -k gui/$(id -u)/com.localmail.serve
#   launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
#   git diff --stat main fix/377-nul-in-search-query   # empty ⇒ safe
#   git branch -D fix/377-nul-in-search-query
# DGX:
#   ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && systemctl --user restart localmail-daemon localmail-serve'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4113 passed (macOS, all extras)
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 160 files
cd gui && npm test -- --run && npm run check && npm audit
cd gui/src-tauri && cargo test --locked && cargo clippy --locked --all-targets -- -D warnings

# This change directly:
unset VIRTUAL_ENV && uv run pytest -q tests/test_search_nul.py tests/test_query_parser.py tests/test_api_search_malformed_query.py
```

`main` was **`e1900b8`** at session start and is unchanged. This session left
**one PR** on `fix/377-nul-in-search-query`: commit `ca24cbf` plus this
handoff.

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`. **Open issues: 39 (38 after the merge).**
