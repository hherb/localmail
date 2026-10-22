# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-10-22 (session 56).** `main` was **`7761530`** at
> session start (#403 merged), and is unchanged.
>
> This session:
> - **deployed #403 on both hosts.** `/v1/version` → `build_hash: 7761530`
>   on the Mac and the DGX; certifi 2026.10.19 and pypdf 6.21.2 on both;
>   DGX units `NRestarts=0`; `init-db` reports the schema up to date.
> - **Dependabot #136, #137, #138 and #148 are fixed** (`fixed_at`
>   2026-10-22T11:26Z). Open: **#71** (accelerate, no fix exists) plus the
>   **29 Pillow alerts** (#139–#167, minus #148), still blocked on
>   fastembed#830, which is **open**.
> - **removed the `localmail-sec` worktree** and its local branch. The
>   remote branch was already gone.
> - **fixed #400: PR on `fix/400-docling-converter-fallback`.** By operator
>   decision it **fails closed** rather than taking the issue's "log and
>   wrap the fallback" route. See the ruling below.
>
> **Open issues: 41, which becomes 40 once the PR merges** (`Closes #400`).
>
> **Things to know before you touch anything.**
>
> **(1) The Mac deployment tree (`~/src/localmail`) is on the PR branch,
> not `main`.** The running daemon imports docling lazily per extraction,
> and this branch changes `extractor.py`, so a scanned PDF arriving before
> the merge would run branch code. Its behaviour is identical on a healthy
> install (the probe below shows it), but `git checkout main` after merging
> is step 0 either way. Then `kickstart -k` both agents so the running
> processes match the tree.
>
> **(2) Still no scanned PDF on the Mac since the docling 2.141 deploy.**
> The newest `docling%` row is `docling@2.97.0` from 2026-10-01, and
> `transient_extractions` is 0. #402's acceptance is unobserved, not failed.
>
> **(3) A leftover worktree, `.claude/worktrees/bottie-bearer-token-dbc203`**
> (detached at `5b85180`), predates this session. I did not touch it; ask
> before removing it.

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

### PR: `fix/400-docling-converter-fallback` (opened on `main`)

| SHA | What |
|---|---|
| `8f66b97` | #400: one docling converter, PDF-only and configured, or an `ExtractorConfigurationError`; tests, CLAUDE.md, README |
| handoff | this file + `docs/handoffs/2026-10-22T1150Z-docling-converter-fail-closed.md` |

- **The defect.** `DoclingExtractor.extract` built a configured converter
  inside `except Exception: converter = None`, then fell back to a bare
  `DocumentConverter(**pdf_only)`. Any construction failure discarded the
  OCR engine, languages and `FULL_PAGE` with no log line. If the bare
  construction failed too, the raw exception escaped every classifying
  `try`, and the worker recorded each docling-bound PDF as an
  `'unexpected'` poison pill.
- **The fix.** `_pdf_only_converter_kwargs()` is now `_converter_kwargs(cfg)`,
  returning `allowed_formats` and `format_options` together.
  - It fails closed on an unimportable `InputFormat` or `PdfFormatOption`.
  - `_build_pdf_pipeline_options` raises on an unimportable
    `PdfPipelineOptions`; it used to return `None`.
  - A raising `DocumentConverter(...)` is wrapped too.
  - All three raise `ExtractorConfigurationError`.
- **No new log line.** The worker already logs every transient or
  configuration error as a per-blob WARNING with the exception text.
- **Tests (`tests/test_docling_input_formats.py`).**
  - The wiring test now also asserts `format_options`.
  - Each unimportable class fails closed (3 cases).
  - An unbuildable converter is held, constructed once, with its cause
    kept.
  - The 8 fakes that stubbed `_pdf_only_converter_kwargs` stub
    `_converter_kwargs`.

### Outside the PR (operational)

- Deployed #403 on the Mac and the DGX.
- Removed the `localmail-sec` worktree.
- Checked #2a: emSign ECC Root CA - G3 is **still present** in both OS
  stores (`/etc/ssl/cert.pem` 2 matches on the Mac, 1 file on the DGX).

## Verification (Mac, `~/src/localmail` venv, all extras, at `8f66b97`)

| gate | result |
|---|---|
| pytest | **4058 passed, 0 failed**, 6 warnings, 272 s (+2 net over `main`'s 4056) |
| mypy | Success, **160** files (`uvx mypy --python-executable .venv/bin/python`) |
| docling blocked on `sys.meta_path` (CI's shape) | the 4 affected files: 86 passed, 8 skipped (the real-docling tests) |
| mutations | restoring the fallback, `return None` for missing pipeline options, and dropping `format_options` each fail exactly the test written for it |
| `probe_docling_ocr.py --engine auto` | exit 0; `Auto OCR model selected ocrmac.`, `FULL_PAGE`, all three fixtures complete |
| CI | see `gh pr checks`; I stopped at the push |

- **Load** was 17–26, mostly Godot and a rustc build, so no timing is
  comparable.
- **GUI untouched**, so cargo and vitest were not re-run.

## What's next

### 0. **Merge the PR, return the Mac to `main`** *(acceptance below)*
- **You merge**, once both CI legs are green.
- **Mac:**
  1. `git checkout main && git pull`;
  2. `kickstart -k` both agents (no sync needed, as no dependency changed);
  3. `git branch -d fix/400-docling-converter-fallback`.
- **DGX:** `git pull --ff-only`, then restart both units.
- **Acceptance:**
  - `/v1/version` = the merge commit on both hosts;
  - `gh issue view 400` is closed;
  - `git log --oneline main..origin/fix/400-docling-converter-fallback`:
    after a squash merge, compare trees rather than expecting this to be
    empty.

### 1. **Pillow 13, once fastembed lifts its cap** (carried)
- Watch qdrant/fastembed#830 (`gh pr view 830 --repo qdrant/fastembed`).
- **When it merges:**
  1. Move `[tool.uv.sources] fastembed`'s `rev` to a commit that has it.
  2. Raise the dev group's `Pillow>=10.3` to `>=13.0.0` and re-lock.
  3. Run the full suite and `probe_docling_ocr.py` on **both** hosts
     (`--engine auto --engine rapidocr` on the Mac).
- **Acceptance:** the lock holds Pillow 13.x, both probes exit 0, and the 29
  Pillow alerts close.

### 2. **Watch the first scanned PDF on the Mac** (carried; Things to know, (2))
- **Acceptance:** a new `attachment_text` row with `extractor LIKE
  'docling@2.141%'` and non-empty text, and `transient_extractions` still 0.

### 2a. **emSign G3 in the OS trust stores** (carried)
- Still present on both hosts as of today. Re-check after each OS or
  `ca-certificates` update (commands below).

### 3. **kastellan** (carried, yours)
- Ask whether it still wants slice C (`/problems/token-expired`); its API
  key never expires.
- Tell it about `?headers=list` (api_minor ≥ 1), attachment index paging
  (≥ 2; never compute the offset from `text.length`), and `fields` /
  `snippet_chars` (≥ 3).

### 4. **Carried issues**
- **Next small slice:** #401 (opaque-bearer `/mcp` does no RFC 8707 check
  on resource-bound tokens). Note the opaque path mints no bound tokens, so
  first measure whether a bound token can reach it at all.
- **Then:** #394, #395, #386, #389, #380, #383, #384, #377, #378, #369,
  #373, #372, #370, #371, #368, #365.
- **Not filed yet:** the `snippet_source="header"` mislabel on a
  whole-message lexical win.
- **Larger work:**
  - #375 (Mac build-hash timeout cache);
  - #374 + #305 (the `cli.py` refactor, 2177 lines);
  - #358/#357, #330/#327/#328, #319/#318/#320 (the next migration slot is
    `0037_*.sql`), #340, #363, #285 (ruff in CI);
  - lifting the rapidocr cap, with its own probe;
  - admin GUI phase 5, the Users panel;
  - robustness: #218, #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **#400 fails closed: the fallback is deleted, not logged** (operator
   decision). The `docling>=2.132` floor guarantees the option classes, so
   the fallback was reachable only from a broken install. There it stored an
   extraction made with docling's defaults, which is never redone. *Cost if
   wrong:* a docling that renames `PdfFormatOption` or `PdfPipelineOptions`
   holds every scanned PDF on the transient budget until the code catches
   up. That is loud, and `retry-failed-extractions` recovers it.
2. **No extra log line for #400.** The worker's per-blob WARNING already
   carries the exception. *Cost if wrong:* a WARNING per scanned PDF during
   such an outage rather than one per process. Scanned PDFs are rare (none
   in three weeks on the Mac).
3. **Deploying #403 went ahead without a fresh confirmation**, following the
   handoff's step 0 and the precedent of sessions 54 and 55.
4. **Code and handoff are in one PR**, per the convention.

## Open decisions & risks

1. **29 Pillow alerts stay open**, 3 of them critical, until fastembed#830
   ships. The no-reach claim rests on one measurement of docling 2.141's
   default pipeline.
2. **The Mac's extraction path is still unexercised by real mail** under
   docling 2.141 (Things to know, (2)).
3. **The Mac tree is on the PR branch until step 0.**
4. **`api_minor` stays 3.** Nothing on the wire changed.
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
git status && git branch --show-current  # fix/400-docling-converter-fallback until step 0
git log --oneline -1 origin/main         # THE authority
git worktree list

# Is the PR merged?
gh pr list --state all --head fix/400-docling-converter-fallback --json number,state,mergeCommit
gh issue list --limit 100 --json number --jq length       # 40 after the merge
gh api --paginate repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | "\(.number)\t\(.dependency.package.name)"'
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
#   launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
#   launchctl kickstart -k gui/$(id -u)/com.localmail.serve
#   git branch -d fix/400-docling-converter-fallback
# DGX:
#   ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && systemctl --user restart localmail-daemon localmail-serve'

# Load before trusting any timing.
uptime; ps -A -o pid,pcpu,etime,comm -r | head -6

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4058 passed (macOS, all extras)
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 160 files
cd gui && npm test -- --run && npm run check && npm audit
cd gui/src-tauri && cargo test --locked && cargo clippy --locked --all-targets -- -D warnings

# This change directly:
unset VIRTUAL_ENV && uv run pytest -q tests/test_docling_input_formats.py tests/test_extractor.py tests/test_extract_worker.py
unset VIRTUAL_ENV && uv run python tests/acceptance/probe_docling_ocr.py --engine auto
```

`main` was **`7761530`** at session start and is unchanged. This session left
**one PR** on `fix/400-docling-converter-fallback`: commit `8f66b97` plus this
handoff.

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`. **Open issues: 41 (40 after the merge).**
