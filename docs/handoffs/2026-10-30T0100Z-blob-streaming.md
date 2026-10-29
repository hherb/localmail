# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-10-30 (session 64).** `main` was **`162422d`** at
> session start (#412 merged; #380 and #389 closed; #370 open for its `lang`
> question).
>
> This session: **PR on `fix/386-blob-streaming`**, one commit
> (`90d0733`) closing all five items of #386.
>
> **Neither host was redeployed.** Mac runs `b6fd32f`, DGX `a8aa2b5`
> (checked at session end via `/v1/version`). Both lag #412's wire change
> (422 → 400 on `/v1/search`), so last session's step 1 is still open.
>
> **Open issues: 35** (34 once this PR merges).
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

### PR on `fix/386-blob-streaming` (based on `main` at `162422d`)

| SHA | What |
|---|---|
| `90d0733` | #386 items 1–5: `blob_response._stream_slice`, open-after-connection in both routes, `log_context`, `str \| None` mime; 8 tests; CLAUDE.md |
| handoff | this file + `docs/handoffs/2026-10-30T0100Z-blob-streaming.md` |

### #386: blob streaming follow-ups

- **Item 1 (handle leak):** both routes call `_open_blob_file_at` **after**
  the `with pool.connection()` block. The sha route used to open before
  `get_attachment_filename`, so a DB error there (or the commit on exit)
  left the file open until GC. It still runs after `not_modified`, so the
  #62 spies are unaffected.
- **Items 2 + 3:** both streamers go through one `_stream_slice`.
  - It never reads past its slice. A file longer than `size_bytes` is cut
    at the size and logged as `attachment stream overran`. Before, it
    overran `Content-Length` and h11 aborted the response silently.
  - An `OSError` mid-read is logged at ERROR (`attachment stream read
    failed … sent=N`) and re-raised.
- **Item 4:** `blob_response(log_context=…)`. The index route passes
  `message_id=… index=…`, so every stream log line says which request
  hit a shared blob. The sha route passes nothing.
- **Item 5:** `mime_type` is `str | None` on `_lookup_blob_row`,
  `get_attachment_blob_info` and `open_attachment_bytes`. The third was not
  named in the issue; mypy found the lie propagating there.

## Verification

| check | result |
|---|---|
| RED first | 5 new tests fail before the fix; 3 controls pass |
| mutation: open before the filename lookup | `test_a_filename_lookup_failure_leaves_no_blob_file_open` fails |
| mutation: overrun check disabled | the overrun test fails |
| mutation: `except OSError` → `LookupError` | both read-error cases fail |
| mutation: read to EOF (`while True`) | 5 fail, incl. the #58 no-warn pins |
| mutation: drop `log_context` on the index route | the index truncation test fails |
| pytest (macOS, all extras) | **4221 passed**, 6 warnings, 372 s |
| mypy `src/localmail` | Success, 161 files |
| CI | see `gh pr checks` on the PR |

- Mutations were restored from scratchpad copies and run with `python -B`.
- No baseline was taken on `main` this session, so 4221 is not compared
  with anything. This session added 8 tests.
- No GUI or Rust change, so vitest and cargo were not re-run.

## What's next

### 1. **Merge this PR, then redeploy both hosts** (yours; carried from #412)
- Check `gh issue view 386` after the merge; it should be closed.
- **Redeploy.** #412 changed the wire, and this PR changes logging only.
  - Mac: `git pull`, `uv sync --frozen --extra mcp --extra extraction`,
    then kickstart both agents.
  - DGX: the ssh one-liner from an earlier handoff.
- **Acceptance:**
  - `/v1/version` on each host reports the merge commit.
  - `POST /v1/search` with `{"limit": 500}` answers a 400 problem+json.

### 2. **Tell kastellan** (yours; carried)
- Every schema error on `/v1/search` is now a problem+json 400 with a
  string `detail` (`loc: msg; …`). `api_minor` was not bumped.

### 3. **Next small slice: #370's remainder (`lang` as a list)**
- `api.search._filter_tokens` already accepts a list, while
  `SearchFiltersModel.lang` is `str | None`. Decide whether to widen it.
  This is your call.
- **Acceptance, if widened:**
  - `{"filters": {"lang": ["en", "de"]}}` unions both.
  - A list member with whitespace or a quote is a 400.
  - The MCP `search` tool's `lang` gets the same type.
  - The GUI wire test (`test_serve_search_request_keys.py`) still passes.
  - Close #370.
- **If not widened:** close #370 with that ruling.

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
- **Not filed yet:** the `snippet_source="header"` mislabel on a
  whole-message lexical win.
- **Larger work:**
  - #375; #374 + #305 (the `cli.py` refactor).
  - #358/#357, #330/#327/#328, #319/#318/#320 (next migration `0037_*.sql`).
  - #340, #363, #285.
  - Lifting the rapidocr cap; admin GUI phase 5 (Users panel).
  - Robustness: #218, #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **Open after releasing the connection, rather than closing `fp` on the
   exception path.** It removes the handle from every DB failure window,
   including the commit on exit that the issue names for the index route.
   There is no new `try`.
2. **One `_stream_slice` for both streamers.** A full GET is the slice
   `[0, size)`. Only the full path checks for overrun: a range is a slice,
   and bytes past it are not an error.
3. **A read error is logged at ERROR; truncation and overrun at WARNING.**
   A failed read is an I/O fault, while a size mismatch is a data
   inconsistency.
4. **All five items in one PR.** Each stayed small, as the issue required.

## Open decisions & risks

1. **Both hosts lag `main`**: the Mac runs `b6fd32f` and the DGX
   `a8aa2b5`. Each is self-consistent until it is redeployed.
2. **The `fp.read(1)` overrun probe is outside the `OSError` logging.** An
   EIO on exactly that one-byte read would propagate without the
   "read failed" line. It is a narrow window, and accepted.
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

gh pr list --head fix/386-blob-streaming --state all --json number,state,mergeCommit
gh issue view 386 --json state
gh issue list --limit 200 --json number --jq length       # 34 after the merge
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
unset VIRTUAL_ENV && uv run pytest -q              # 4221 passed (macOS, all extras)
uv run pytest -q tests/test_blob_response_streaming.py tests/test_serve_attachments_routes.py tests/test_serve_message_attachment_routes.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 161 files
cd gui && npm test -- --run && npm run check && npm audit
cd gui/src-tauri && cargo test --locked && cargo clippy --locked --all-targets -- -D warnings
```

`main` was **`162422d`** at session start and is unchanged. This session left
**one PR** on `fix/386-blob-streaming`: commit `90d0733`, plus this handoff.

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`.
