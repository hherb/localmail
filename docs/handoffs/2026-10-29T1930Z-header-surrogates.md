# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-10-29 (session 63).** `main` was **`a8aa2b5`** at
> session start (#411 merged, with its `bind:innerHTML` review fix; #395
> closed).
>
> This session:
> - **PR #412 on `fix/380-header-surrogates`, two fixes:**
>   - **#380** (commit `c644187`).
>   - **#370's envelope half + #389** (commit `eaf3139`): every schema error
>     on `/v1/search` is a problem+json 400, as you ruled.
> - **DGX synced by you** to `a8aa2b5` (Pillow 13.0.0), after the classifier
>   refused it for me. Step 0 is done.
> - **Mac untouched**: still `b6fd32f`. **#412 changes the wire** (422 → 400
>   on `/v1/search`), so redeploy both hosts after the merge (step 1).
>
> **Open issues: 36** (34 once #412 merges; #370 stays open for its `lang`
> question).
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

### PR #412 on `fix/380-header-surrogates` (based on `main` at `a8aa2b5`)

| SHA | What |
|---|---|
| `c644187` | `pgtext.replace_surrogates` + its use in `header_block.entries_from_message`; 6 tests; CLAUDE.md |
| `eaf3139` | `serve/request_validation.py` (route class + pure `validation_detail`); search router uses it; 16 tests; README, CLAUDE.md |
| handoff | this file + `docs/handoffs/2026-10-29T1930Z-header-surrogates.md` |

### #370 (envelope half) and #389: schema errors on `/v1/search` → 400

- **Your ruling:** fix #370's option 1 rather than #389 as written.
- **What changed:**
  - The search router's `route_class` is `ProblemJsonValidationRoute`. It
    re-raises FastAPI's `RequestValidationError` as `ValidationFailed`, and
    the existing `APIError` handler renders that.
  - `detail` is `validation_detail`: one `loc: msg` per error, with the
    `body.` prefix dropped, joined by `; ` (for example
    `fields.1: Input should be a valid string`).
- **Wire change:** every schema error on `/v1/search` moves from 422 with an
  array `detail` to a problem+json 400 with a string `detail`. Other routes
  keep 422, and a test pins that scope.
- **`fields` stays `list[str]`.** `snippet_chars` stays `Any`, because
  coercion is a silent 200 that no handler sees. The code comments say
  this.
- **Left open in #370:** whether `lang` should accept a list on the wire.
  The PR body says "Closes #389" and only refers to #370.

- **The defect (#380):** the #314 fallback keeps a header's raw text when
  the stdlib cannot parse it. For 8-bit bytes that text is
  surrogate-escaped (`\udcfc`), and Starlette's `JSONResponse` raises
  `UnicodeEncodeError` on it (verified: `surrogates not allowed`). That is a
  500 after the handler succeeded.
- **Not reachable today**: the sync-time JSONB insert rejects such a
  message. #379 moved the guarantee off the database onto `header_block`.
  *(Wrong as a description of the fix: the same walk feeds sync, so the
  fix also turns that poison pill into a stored message. See "Review
  round" below.)*
- **The rule:** `pgtext.replace_surrogates`, beside `strip_nuls`. It
  replaces each surrogate code point with U+FFFD. That matches what the
  parser's own success path renders for the same bytes: on 3.13,
  `X-A: \xfc\xfd` parses to `"��"`. *(True only for bytes invalid one
  by one; superseded, see "Review round".)*
- **Applied to every value**, after the `try/except`, so the success path is
  covered too. Header *names* are not passed through: the feedparser only
  admits printable ASCII there.
- **Replaced, not stripped**: a dropped byte would join its neighbours into
  a different word.
- **Not done:** recovering UTF-8 from the escaped bytes (`caf\udcc3\udca9`
  → `café`). The fallback now shows `caf��` where a successful
  parse would show `café`. That costs fidelity only, on a path that is
  unreachable today.

## Review round (PR #412)

Four reviewers (code, tests, errors, comments). Fixed on the branch:

- **UTF-8 recovery after all.** The fallback mangled raw UTF-8 (`café` →
  `caf��`), and since `parser._headers_dict` is the same walk it would
  have *stored* that. The fallback now decodes as `email.utils._sanitize`
  does (`header_block._decode_raw`); `replace_surrogates` stays as the
  backstop. Pinned against the stdlib's own rendering of the same bytes.
- **Sync changes too**: such a message is now ingested, not a poison pill.
  Pinned at `process_one_message`; HTTP `list`/`full` and the MCP
  serializer are pinned end to end. The three fail without the fix.
- **Malformed JSON** rendered `1: JSON decode error` (a char offset read
  as a field); now `body: JSON decode error at position 1: <reason>`.
- **A non-UTF-8 body** was FastAPI's bare `HTTPException(400)`, not
  problem+json; the route class maps it too.
- Stale "422" test comments fixed; the `snippet_chars` non-integer needles
  now use `snippet_width_error`'s own wording, since a retyped `int` would
  also have answered a 400 naming the field.

## Verification

| check | result |
|---|---|
| RED first | 6 new tests fail (ImportError / assertion) before the fix |
| mutation: drop `replace_surrogates(...)` in `header_block.py` (restored from a scratch copy, run with `python -B`) | `test_a_raw_fallback_with_8bit_bytes_still_renders_as_json` fails, 35 pass |
| pytest (macOS, all extras) | **4186 passed**, 6 warnings, 263 s |
| mypy `src/localmail` | Success, 160 files |
| mutation: `router = APIRouter()` (no route class) | 11 fail (10 new + the updated sort test) |
| pytest after `eaf3139` (macOS, all extras) | **4202 passed** (4186 + 16), 344 s |
| mypy after `eaf3139` | Success, 161 files |
| CI | `c644187` green; see `gh pr checks 412` for `eaf3139` |

- Load was 13–24 during the run; timings are not comparable.
- No GUI or Rust change, so vitest/cargo were not re-run.

## What's next

### 0. ~~Sync the DGX~~ — done by you (`a8aa2b5`, Pillow 13.0.0)

### 1. **Merge #412, then redeploy both hosts** (yours)
- Check `gh issue view 380` and `gh issue view 389` after the merge; both
  should be closed.
- **Redeploy, because the wire changed:**
  - Mac: `git pull`, `uv sync --frozen --extra mcp --extra extraction`,
    then kickstart both agents.
  - DGX: the ssh one-liner from the last handoff.
- **Acceptance:** `/v1/version` on each host reports the merge commit, and
  `POST /v1/search` with `{"limit": 500}` answers 400 problem+json there.

### 2. **Tell kastellan** (yours)
- Every schema error on `/v1/search` is now a problem+json 400 with a
  string `detail` (`loc: msg; …`).
- `api_minor` was not bumped: only malformed requests change answer.

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

### 6. **kastellan, slice C** (carried, yours)
- Ask whether it still wants slice C (`/problems/token-expired`).
- Tell it about `?headers=list` (api_minor ≥ 1), attachment index paging
  (≥ 2), and `fields` / `snippet_chars` (≥ 3) with the 48-entry cap.

### 7. **Carried issues**
- **#370's remainder:** should `lang` accept a list on the wire?
  `api.search._filter_tokens` already supports one, while
  `SearchFiltersModel.lang` is `str | None`. Close #370 once that is
  decided.
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
2. *(Reversed in the review round.)* **U+FFFD per surrogate, not UTF-8
   recovery.** It is one line and total.
   It matches the success path's rendering of undecodable bytes. Recovery
   would need a second branch for surrogates outside `DC80–DCFF`, on a path
   that is unreachable today.
3. **No Mac redeploy mid-session.** This was decided before `eaf3139`,
   which does change the wire, so redeploy after the merge (step 1).
4. **#389 is fixed through #370's option 1, not by typing `fields` as
   `Any`** (your call):
   - It covers every schema error on the route, not two.
   - It keeps the OpenAPI type.
   - It adds no second type check.
   - It is scoped to `/v1/search`.

## Open decisions & risks

1. **Both hosts lag `main` after #412 merges**: the Mac runs `b6fd32f`
   and the DGX `a8aa2b5`. Each is self-consistent until redeployed.
2. **The 422 → 400 change is visible to any client that branches on the
   status code** for `/v1/search`. Only kastellan is known; the GUI sends
   typed structs and treats a 400 on a paging request as final, which is
   correct here too.
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

gh pr view 412 --json state,mergeCommit
gh issue list --limit 200 --json number --jq length       # 34 after #412
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
unset VIRTUAL_ENV && uv run pytest -q              # 4202 passed (macOS, all extras)
uv run pytest -q tests/test_pgtext.py tests/test_header_block.py tests/test_request_validation.py tests/test_serve_search_schema_errors.py
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 161 files
cd gui && npm test -- --run && npm run check && npm audit
cd gui/src-tauri && cargo test --locked && cargo clippy --locked --all-targets -- -D warnings
```

`main` was **`a8aa2b5`** at session start and is unchanged. This session left
**one PR**, #412 on `fix/380-header-surrogates`: commits `c644187` and
`eaf3139`, plus this handoff.

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`.
