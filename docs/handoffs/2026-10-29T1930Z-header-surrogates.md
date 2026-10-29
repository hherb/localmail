# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-10-29 (session 63).** `main` was **`a8aa2b5`** at
> session start (#411 merged, with its `bind:innerHTML` review fix; #395
> closed).
>
> This session:
> - **#380 fixed: PR on `fix/380-header-surrogates`** (commit `c644187`).
> - **DGX still NOT synced.** The auto-mode classifier refused the remote
>   `git pull` + `uv sync` + restart again ("Remote Shell Writes"). It still
>   reports `build_hash: 9154672` with Pillow 12. **Step 0 is yours.**
> - **Mac untouched**: still `b6fd32f`. #411 and this PR change nothing the
>   running services execute that matters (#411 is test-only; #380 is an
>   unreachable path), so no redeploy was done.
>
> **Open issues: 36** (35 once this PR merges).
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

### PR on `fix/380-header-surrogates` (based on `main` at `a8aa2b5`)

| SHA | What |
|---|---|
| `c644187` | `pgtext.replace_surrogates` + its use in `header_block.entries_from_message`; 6 tests; CLAUDE.md |
| handoff | this file + `docs/handoffs/2026-10-29T1930Z-header-surrogates.md` |

- **The defect (#380):** the #314 fallback keeps a header's raw text when
  the stdlib cannot parse it. For 8-bit bytes that text is
  surrogate-escaped (`\udcfc`), and Starlette's `JSONResponse` raises
  `UnicodeEncodeError` on it (verified: `surrogates not allowed`). That is a
  500 after the handler succeeded.
- **Not reachable today**: the sync-time JSONB insert rejects such a
  message. #379 moved the guarantee off the database onto `header_block`.
- **The rule:** `pgtext.replace_surrogates`, beside `strip_nuls`. It
  replaces each surrogate code point with U+FFFD. That matches what the
  parser's own success path renders for the same bytes: on 3.13,
  `X-A: \xfc\xfd` parses to `"��"`.
- **Applied to every value**, after the `try/except`, so the success path is
  covered too. Header *names* are not passed through: the feedparser only
  admits printable ASCII there.
- **Replaced, not stripped**: a dropped byte would join its neighbours into
  a different word.
- **Not done:** recovering UTF-8 from the escaped bytes (`caf\udcc3\udca9`
  → `café`). The fallback now shows `caf��` where a successful
  parse would show `café`. That costs fidelity only, on a path that is
  unreachable today.

## Verification

| check | result |
|---|---|
| RED first | 6 new tests fail (ImportError / assertion) before the fix |
| mutation: drop `replace_surrogates(...)` in `header_block.py` (restored from a scratch copy, run with `python -B`) | `test_a_raw_fallback_with_8bit_bytes_still_renders_as_json` fails, 35 pass |
| pytest (macOS, all extras) | **4186 passed**, 6 warnings, 263 s |
| mypy `src/localmail` | Success, 160 files |
| CI | see `gh pr checks`; I stopped at the push |

- Load was 13–24 during the run; timings are not comparable.
- No GUI or Rust change, so vitest/cargo were not re-run.

## What's next

### 0. **Sync the DGX** (yours: the classifier blocks remote writes)
```bash
ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user restart localmail-daemon localmail-serve'
```
- **Acceptance:**
  - `curl -sk https://10.0.0.3:8443/v1/version` reports the current
    `main` hash.
  - `.venv/bin/python -c "import PIL; print(PIL.__version__)"` prints
    13.0.0 there.
  - Both units are `active`.
- Or allow it with a Bash permission rule for `ssh 10.0.0.3 …`, and the next
  session runs it. This is the second session it has been refused.

### 1. **Merge the #380 PR** (yours, once CI is green)
- `Closes #380` is in the PR body; check with `gh issue view 380` after the
  merge.
- **Optional:** redeploy the Mac (`git pull`, `uv sync --frozen --extra mcp
  --extra extraction`, kickstart both agents). The change is unreachable
  today, so this is not urgent.

### 2. **#389 needs YOUR decision** (it is a wire change)
- **The question:** should `fields` be typed `Any` like `snippet_chars`?
  - **Yes:** `{"fields": "subject"}` and `{"fields": [5]}` move from 422
    (an array `detail`) to 400 (problem+json from `fields_error`).
  - **No:** keep the 422 and close #389 as "won't fix". It is honest, only
    inconsistent.
- **If yes:**
  - Write `fields: Any = Field(default=None, …)`.
  - Add two rows to `_REFUSALS` in `tests/test_serve_search_projection.py`.
  - Mutation: restore `list[str] | None`, and the two rows must fail.
  - Tell kastellan, since it renders `detail`.

### 3. **Next small slice: #386** (blob streaming follow-ups, 5 items)
- **Item 1 is the real one:** the sha route opens the file before
  `get_attachment_filename`, so a DB error leaks the handle until GC.
  - **Fix:** look up the filename before the open. Both steps already come
    after `not_modified`, which keeps the #62 ordering.
  - **Acceptance:**
    - A RED test makes `get_attachment_filename` raise and asserts the
      handle is closed (spy `_open_blob_file_at` and return a tracked file).
    - The #62 spies still pass.
- **Items 2–4** are logging (an OSError mid-stream, a file overrunning
  `Content-Length`, message id and index in the truncation WARNING).
- **Item 5** is a type annotation. Do them in the same PR only if each one
  stays small.

### 4. **Dependabot** — two open, both left open on purpose
- **#71 accelerate:** no fix exists.
- **#185 deepmerge:** no patched release; dev-scope only.
  - **Acceptance:** bump when a fix exists (`npm audit fix`, then
    `npm test -- --run && npm run check`).

### 5. **Watch for docling > 2.144** (carried)
- Re-run the probe under rapidocr against any new release. If
  `native_table` passes, move the lock forward and lift the `docling<2.143`
  cap.
- **Acceptance:** the lock is on the newest release whose probe exits 0 on
  both hosts.

### 5a. **emSign G3 in the OS trust stores** (carried)
- Re-check after each OS or `ca-certificates` update (commands below).

### 6. **kastellan** (carried, yours)
- Ask whether it still wants slice C (`/problems/token-expired`).
- Tell it about `?headers=list` (api_minor ≥ 1), attachment index paging
  (≥ 2), and `fields` / `snippet_chars` (≥ 3) with the 48-entry cap.

### 7. **Carried issues**
- **Then:** #383, #384, #378, #369, #373, #372, #370, #371, #368, #365.
- **Not filed yet:** the `snippet_source="header"` mislabel on a
  whole-message lexical win.
- **Larger work:**
  - #375; #374 + #305 (the `cli.py` refactor).
  - #358/#357, #330/#327/#328, #319/#318/#320 (next migration `0037_*.sql`).
  - #340, #363, #285.
  - Lifting the rapidocr cap; admin GUI phase 5 (Users panel).
  - Robustness: #218, #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **#380 over #386 and #389**: #389 is a wire decision (yours), and #386
   has five parts. #380 was one pure rule at one call site.
2. **U+FFFD per surrogate, not UTF-8 recovery.** It is one line and total.
   It matches the success path's rendering of undecodable bytes. Recovery
   would need a second branch for surrogates outside `DC80–DCFF`, on a path
   that is unreachable today.
3. **No Mac redeploy**: nothing it would run changes behaviour on any
   reachable input.

## Open decisions & risks

1. **The DGX is still on `9154672` / Pillow 12** until step 0 runs. It is
   self-consistent, only out of date.
2. **#389** is waiting on your call (step 2).
3. **The native-table regression in docling ≥ 2.143 is unexplained**, only
   localised.
4. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.**
5. **Never run two pytest sessions against one test database.**
6. **Restore a mutation from a file copy, never with `git checkout`.**
7. **The test DB is on port 5532**, the same cluster as the live archive.
8. **Always sync with `--extra mcp --extra extraction` on both hosts**, and
   use `~/.local/bin/uv` on the DGX.
9. **`uv sync --dry-run` is not read-only; `uv lock --dry-run` is.**
10. **No ROADMAP.md.** That `/nextsession` step is a no-op.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git switch main && git pull --ff-only
git log --oneline -1 origin/main         # THE authority
git worktree list

gh pr list --head fix/380-header-surrogates --state all --json number,state,mergeCommit
gh issue list --limit 200 --json number --jq length       # 35 after the #380 PR
gh api --paginate repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | .dependency.package.name' | sort | uniq -c   # accelerate, deepmerge

# Deployment health
launchctl list | grep localmail
curl -sk https://127.0.0.1:8443/v1/version
tail -3 ~/localmail-probe/tunnel-probe.log
ssh -o ConnectTimeout=8 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'
grep -c "emSign ECC Root CA - G3" /etc/ssl/cert.pem
ssh 10.0.0.3 'ls /etc/ssl/certs | grep -ic emsign_ecc_root_ca_-_g3'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4186 passed (macOS, all extras)
uv run pytest -q tests/test_pgtext.py tests/test_header_block.py   # 56 passed — this change
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 160 files
cd gui && npm test -- --run && npm run check && npm audit
cd gui/src-tauri && cargo test --locked && cargo clippy --locked --all-targets -- -D warnings
```

`main` was **`a8aa2b5`** at session start and is unchanged. This session left
**one PR**, on `fix/380-header-surrogates`: commit `c644187`, plus this
handoff.

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`.
