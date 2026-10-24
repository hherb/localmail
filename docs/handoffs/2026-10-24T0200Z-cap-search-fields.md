# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-10-24 (session 60).** `main` was **`ff0eddc`** at
> session start (#408 merged, closing #377), and is unchanged.
>
> This session:
> - **deployed #408 on both hosts.** `/v1/version` → `build_hash: ff0eddc`
>   on the Mac and the DGX; DGX units `active`. Local branch
>   `fix/377-nul-in-search-query` deleted (diff against `main` empty).
> - **fixed #394: PR on `fix/394-cap-search-fields`.**
>
> **Open issues: 38, 37 once the PR merges** (`Closes #394`).
>
> **Things to know before you touch anything.**
>
> **(1) The Mac deployment tree (`~/src/localmail`) is on the PR branch,
> not `main`.** The change is in `api/search_projection.py` (serve only).
> Neither agent was restarted after the branch switch, so the running code
> is `ff0eddc`'s. `git checkout main` after the merge is step 0.
>
> **(2) Still no scanned PDF on the Mac since the docling 2.141 deploy.**
> Newest `docling%` row is still `docling@2.97.0` (2026-10-01);
> `transient_extractions` is 0.
>
> **(3) fastembed#830 is still open** (checked 2026-10-24), so the Pillow
> alerts stay open.
>
> **(4) Leftover worktree `.claude/worktrees/bottie-bearer-token-dbc203`**
> (detached at `5b85180`). Untouched; ask before removing it.

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

### PR: `fix/394-cap-search-fields` (opened on `main`)

| SHA | What |
|---|---|
| `32b06b7` | #394: `fields` over 48 entries is a 400 naming the cap; frozenset membership; tests, CLAUDE.md, README |
| handoff | `NEXT_SESSION.md` + this file |

- **The defect.** `fields` had no length bound and accepts duplicates, so
  the caller chose `len(fields)`, and `project_hit`'s per-hit re-check
  (#392) cost that × up to 200 hits (20,000 names: 212 ms).
- **The fix (`api/search_projection.py`).** `MAX_FIELDS = 48` (4 × the 12
  hit fields, a literal so a new hit field does not move a documented
  limit). `fields_error` refuses a longer list **before reading any
  element**: `fields has N entries; at most 48 are accepted (12 hit
  fields exist)`. Membership is a `frozenset`. Duplicates stay accepted.
  - **Why `fields_error`, not `Field(max_length=…)`:** pydantic answers that
    with a 422 array `detail` (#370); the rule answers problem+json, ahead of
    the empty-ACL short-circuit like the rest of `fields_error`.
  - **Why not hoist the re-check out of `project_hit`:** it reopens #392.
  - **`api_minor` stays 3**: only a list no honest caller sends changes.
- **Tests:** 4 unit (cap pinned to literal 48, at-cap accepted, over-cap
  message names count and cap, cap decided before element walk) + one
  `_REFUSALS` row → both the granted and grant-nothing route tests.
- **Mutation:** `if len(fields) > MAX_FIELDS:` → `if False:` fails exactly
  the 4 cap tests (2 unit, 2 route).
- **No review round was run.** Small diff; consider `/review-pr` anyway.

## Verification (Mac, `~/src/localmail` venv, all extras, `32b06b7`)

| gate | result |
|---|---|
| pytest | **4170 passed, 0 failed**, 6 warnings, 314 s |
| mypy | Success, 160 files |
| CI | see `gh pr checks`; I stopped at the push |

- Load was 15–28 at the start of the run; no timing is comparable.
- GUI untouched, so cargo and vitest were not re-run.

## What's next

### 0. **Merge the PR, return the Mac to `main`**
- **You merge**, once both CI legs are green.
- **Mac:** `git checkout main && git pull`; restart serve (and the daemon,
  to keep both on one commit); then check
  `git diff --stat main fix/394-cap-search-fields` is empty and
  `git branch -D` it.
- **DGX:** `git pull --ff-only`, restart both units.
- **Acceptance:** `/v1/version` reports the merge commit on both hosts;
  `gh issue view 394` is closed.

### 1. **Pillow 13, once fastembed lifts its cap** (carried)
- qdrant/fastembed#830 still **open** on 2026-10-24.
- When it merges: move `[tool.uv.sources] fastembed`'s `rev`, raise the dev
  group's `Pillow>=10.3` to `>=13.0.0`, re-lock, run the full suite and
  `probe_docling_ocr.py` on **both** hosts (`--engine auto --engine
  rapidocr` on the Mac).
- **Acceptance:** lock holds Pillow 13.x, both probes exit 0, alerts close.

### 2. **Watch the first scanned PDF on the Mac** (carried)
- **Acceptance:** a new `attachment_text` row with `extractor LIKE
  'docling@2.141%'` and non-empty text; `transient_extractions` still 0.

### 2a. **emSign G3 in the OS trust stores** (carried)
- Re-check after each OS or `ca-certificates` update (commands below).

### 3. **kastellan** (carried, yours)
- Ask whether it still wants slice C (`/problems/token-expired`).
- Tell it about `?headers=list` (api_minor ≥ 1), attachment index paging
  (≥ 2), `fields` / `snippet_chars` (≥ 3) — and now the 48-entry `fields`
  cap.

### 4. **Carried issues**
- **Next small slice:** #395 — forbid a new `{@html}` sink in `gui/src`.
  - **Acceptance:** a pure rule (the `_rerank_wiring_rules.py` shape) scans
    every `.svelte` file under `gui/src` and reports any `{@html`; a test
    runs it (in vitest or pytest) with a positive control proving it finds
    one; zero hits on today's tree.
- **Then:** #386, #389, #380, #383, #384, #378, #369, #373, #372, #370,
  #371, #368, #365.
- **Not filed yet:** the `snippet_source="header"` mislabel on a
  whole-message lexical win.
- **Larger work:** #375; #374 + #305 (the `cli.py` refactor);
  #358/#357, #330/#327/#328, #319/#318/#320 (next migration `0037_*.sql`),
  #340, #363, #285; lifting the rapidocr cap; admin GUI phase 5 (Users
  panel); robustness: #218, #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **The cap is 48, the issue's own suggestion** (`len(HIT_FIELDS) * 4`).
   Not asked of the operator: loosening later is compatible, and no honest
   caller is near it. Say so if you disagree before merging.
2. **Duplicates stay accepted**; refusing them was the other option and
   would change answers for callers that do nothing wrong.
3. **Deploying #408 went ahead without a fresh confirmation**, per the
   handoff's step 0 and sessions 54–59.
4. **Code and handoff are in one PR.**

## Open decisions & risks

1. **Pillow alerts stay open**, some critical, until fastembed#830 ships.
2. **The Mac's extraction path is still unexercised by real mail** under
   docling 2.141.
3. **The Mac tree is on the PR branch until step 0.**
4. **The cap is a wire-contract change** (a 49-entry `fields` that was a 200
   is now a 400). Documented in README; `api_minor` unchanged.
5. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.**
6. **Never run two pytest sessions against one test database.**
7. **Restore a mutation from a file copy, never with `git checkout`.**
8. **The test DB is on port 5532**, the same cluster as the live archive.
9. **Always sync with `--extra mcp --extra extraction` on both hosts**, and
   use `~/.local/bin/uv` on the DGX.
10. **`uv sync --dry-run` is not read-only; `uv lock --dry-run` is.**
11. **No ROADMAP.md.** That `/nextsession` step is a no-op.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status && git branch --show-current  # fix/394-cap-search-fields until step 0
git log --oneline -1 origin/main         # THE authority
git worktree list

# Is the PR merged?
gh pr list --state all --head fix/394-cap-search-fields --json number,state,mergeCommit
gh issue list --limit 200 --json number --jq length       # 37 after the merge
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
#   git diff --stat main fix/394-cap-search-fields   # empty ⇒ safe
#   git branch -D fix/394-cap-search-fields
# DGX:
#   ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && systemctl --user restart localmail-daemon localmail-serve'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4170 passed (macOS, all extras)
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 160 files
cd gui && npm test -- --run && npm run check && npm audit
cd gui/src-tauri && cargo test --locked && cargo clippy --locked --all-targets -- -D warnings

# This change directly:
unset VIRTUAL_ENV && uv run pytest -q tests/test_search_projection.py tests/test_serve_search_projection.py
```

`main` was **`ff0eddc`** at session start and is unchanged. This session left
**one PR** on `fix/394-cap-search-fields`: commit `32b06b7` plus this
handoff.

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`. **Open issues: 38 (37 after the merge).**
