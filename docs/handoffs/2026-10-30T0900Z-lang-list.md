# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-10-30 (session 65).** `main` was **`661112a`** at
> session start (#413 merged, #386 closed).
>
> This session:
> - **Redeployed both hosts to `661112a`.**
> - Opened a **PR on `fix/370-lang-list`** with one code commit
>   (`05c6320`), which closes the rest of #370.
>
> **Open issues: 37** (36 once this PR merges and closes #370).
>
> **Leftover worktree `.claude/worktrees/bottie-bearer-token-dbc203`**
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

### Redeploy (no commit)

| host | before | after | check |
|---|---|---|---|
| Mac | `b6fd32f` | `661112a` | `/v1/version` `build_hash` = `661112a`; `pending_migrations` empty; docling/mcp/ocrmac import |
| DGX | `a8aa2b5` | `661112a` | `build_hash` = `661112a`; `NRestarts=0` on both units; docling/mcp/rapidocr import |

- **Mac:** `uv sync --frozen --extra mcp --extra extraction` was a no-op.
  Both agents were restarted with `launchctl kickstart -k`.
- **DGX:** the sync reinstalled one package (`nvidia-cusparselt-cu13`).
- **Not checked live:** `{"limit":500}` → 400. An unauthenticated POST gets
  a **401 first**, because auth runs before body validation, and I had no
  credential. The suite pins the 400 (`test_serve_search_schema_errors.py`).

### PR on `fix/370-lang-list` (based on `main` at `661112a`)

| SHA | What |
|---|---|
| `05c6320` | `lang` is `str \| list[str]` on `SearchFiltersModel` and on the MCP `search` tool. Adds 6 tests, updates 1 stale case, README, `docs/mcp-usage.md`, CLAUDE.md |
| review fixes | `lang: []` is a 400 (it used to drop the filter silently); MCP `ToolError` pin for a bad member; this file corrected |
| handoff | this file + `docs/handoffs/2026-10-30T0900Z-lang-list.md` |

- **What changed:** `api.search._filter_tokens` always unioned a list, so
  only the two transport types changed. The MCP description now says "a
  list matches any of them".
- **Tests:** `tests/test_search_lang_list.py`.
  - HTTP: a list reaches the searcher as two `lang:` tokens, and a single
    string still works.
  - A bad member (`"e n"`) is a 400 with the existing wording, and the
    searcher is never called. So is a non-string member (`3`).
  - MCP: a list goes through `call_tool` to `filters`, and the published
    `inputSchema` admits `array<string>`. A bad member reaches the agent as
    a `ToolError`.
  - **Review fix:** `lang: []` used to compose no token, i.e. a 200 over
    every language, once the list type let it through. It is refused now,
    like `""`. Reverting the refusal fails exactly the two tests added for it.
- **Stale case:** `test_serve_search_schema_errors.py` used `lang: ["en","de"]`
  as a schema error. It now uses `lang: 5`, whose location is
  `filters.lang.str`.

## Verification

| check | result |
|---|---|
| RED first | 4 of the 6 original new tests fail before the fix. Of the 2 that pass, `test_a_single_lang_string_still_works` is a control; `test_a_non_string_list_member_is_a_400` passed before for the old reason (every list was a 400), so it guards a regression and pins nothing about the fix |
| pytest (macOS, all extras) | **4230 passed**, 6 warnings, 209 s (machine load ~48 from other projects) |
| mypy `src/localmail` | Success, 161 files |
| CI | see `gh pr checks` on the PR |

- The mutation is the RED run: reverting either transport type fails that
  transport's tests.
- No baseline was taken on `main`, so 4230 is not compared with anything.
- No GUI or Rust change, so vitest and cargo were not run. The GUI still
  drops `lang` at the Tauri hop (#371).

## What's next

### 1. **Merge this PR, then redeploy** (yours)
- After the merge, check `gh issue view 370`. It should be closed by
  `Closes #370` in the PR body.
- **Redeploy both hosts.** This is a wire widening: a request that was a
  400 is now a 200. Use the same recipe as this session (commands below).
- **Acceptance:** `/v1/version` on each host reports the merge commit.

### 2. **Tell kastellan** (yours; carried)
- Every schema error on `/v1/search` is a problem+json 400 with a string
  `detail` (`loc: msg; …`).
- `filters.lang` now takes a list; an empty list is a 400.
- `api_minor` was **not** bumped for either change. Whether a widening
  deserves a bump is open; I kept to #412's precedent.

### 3. **Next small slice: pick from the carried issues**
- **#371** (the GUI drops `lang`/`date_from`/`date_to` at the Tauri hop) is
  the natural follow-on now that `lang` is a list. It is a Rust + TS change.
  - **Acceptance:** the Rust `SearchFiltersWire` carries the three fields,
    and a lang chip reaches the server.
  - `test_serve_search_request_keys.py` must still pass.
  - Run `cargo test` and the vitest suite.
- **#368** (MCP drops unknown tool args) needs an SDK hook that does not
  exist. Leave it.

### 4. **Dependabot** — two open, both on purpose
- **#71 accelerate:** no fix exists.
- **#185 deepmerge:** no patched release; dev-scope only.

### 5. **Watch for docling > 2.144** (carried)
- Re-run the probe under rapidocr against any new release. If
  `native_table` passes, move the lock forward and lift the `docling<2.143`
  cap.

### 5a. **emSign G3 in the OS trust stores** (carried)
- Re-check after each OS or `ca-certificates` update (commands below).

### 6. **kastellan, slice C** (carried, yours)
- Ask whether it still wants slice C (`/problems/token-expired`).

### 7. **Carried issues**
- #383, #384, #378, #369, #373, #372, #371, #368, #365.
- **New, filed 2026-10-29:** #414 (daemon workers stall on unbounded pool connections), #415 (INTERNALDATE local time labelled UTC), #416 (Gmail IDLE drop logged as a crash).
- **Not filed yet:** the `snippet_source="header"` mislabel on a
  whole-message lexical win.
- **Larger work:**
  - #375; #374 + #305 (the `cli.py` refactor).
  - #358/#357, #330/#327/#328, #319/#318/#320 (next migration `0037_*.sql`).
  - #340, #363, #285.
  - Lifting the rapidocr cap; admin GUI phase 5 (Users panel).
  - Robustness: #218, #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **Widen `lang` (operator decision).** The api layer already unioned a
   list, so a narrower wire type only hid a working feature.
2. **A bad list member refuses the whole request.** This is the existing
   `_filter_tokens` behaviour, and it matches "honoured or refused, never
   dropped".
3. **Pydantic's union branch tag stays in the error location**
   (`filters.lang.str`, `filters.lang.list[str].1`). It still names the
   field, and stripping it would add code to `validation_detail` for
   cosmetics.
4. **Redeploy before the PR**, to `661112a`, as the operator asked. This PR
   needs one more redeploy after it merges.

## Open decisions & risks

1. **Should a wire widening bump `api_minor`?** Not done, following #412.
   A client cannot discover the list form except by trying it.
2. **The native-table regression in docling ≥ 2.143 is unexplained**, only
   localised.
3. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.**
4. **Never run two pytest sessions against one test database.**
5. **Restore a mutation from a file copy, never with `git checkout`.**
6. **The test DB is on port 5532**, the same cluster as the live archive.
7. **Always sync with `--extra mcp --extra extraction` on both hosts**, and
   use `~/.local/bin/uv` on the DGX.
8. **`uv sync --dry-run` is not read-only; `uv lock --dry-run` is.**
9. **No ROADMAP.md.** That `/nextsession` step is a no-op.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git worktree list

gh pr list --head fix/370-lang-list --state all --json number,state,mergeCommit
gh issue view 370 --json state
gh issue list --limit 200 --json number --jq length       # 36 after the merge
gh api --paginate repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | .dependency.package.name' | sort | uniq -c   # accelerate, deepmerge

# Redeploy — Mac (the repo is the install)
unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
.venv/bin/python -c "from localmail.config import load_config; from localmail.db import pending_migrations; print(pending_migrations(load_config().database.dsn))"
launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
launchctl kickstart -k gui/$(id -u)/com.localmail.serve
curl -sk https://127.0.0.1:8443/v1/version

# Redeploy — DGX
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user restart localmail-daemon localmail-serve'
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version; systemctl --user show -p NRestarts localmail-daemon localmail-serve'

# Deployment health
launchctl list | grep localmail
tail -3 ~/localmail-probe/tunnel-probe.log
grep -c "emSign ECC Root CA - G3" /etc/ssl/cert.pem
ssh 10.0.0.3 'ls /etc/ssl/certs | grep -ic emsign_ecc_root_ca_-_g3'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4230 passed (macOS, all extras)
uv run pytest -q tests/test_search_lang_list.py tests/test_serve_search_schema_errors.py tests/test_api_search_lang_dates.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 161 files
cd gui && npm test -- --run && npm run check && npm audit
cd gui/src-tauri && cargo test --locked && cargo clippy --locked --all-targets -- -D warnings
```

`main` was **`661112a`** at session start and is unchanged. This session left
**one PR** on `fix/370-lang-list`: commit `05c6320`, plus this handoff. Both
hosts run `661112a`.

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`.
