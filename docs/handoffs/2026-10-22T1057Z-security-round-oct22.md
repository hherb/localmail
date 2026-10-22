# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-10-22 (session 55).** `main` was **`e2deed9`** at
> session start (#402 merged), and is unchanged.
>
> This session:
> - **confirmed #402 is deployed on both hosts.** The operator had already
>   deployed it before the session. `/v1/version` → `build_hash: e2deed9`
>   on the Mac and the DGX; docling 2.141.0 on both; DGX units
>   `NRestarts=0`.
> - **the DGX is reachable again** (tunnel `ok(3/3)` all session). That
>   closed handoff step 1: **#397, #399 and #402 are all deployed there.**
> - **ran the first aarch64 OCR probe:** `probe_docling_ocr.py --engine
>   auto` on the DGX → exit 0. `auto` = rapidocr (onnxruntime). The invoice
>   and both tables came back complete, with ibm-models 4.0.3 and OpenCV
>   4.14.0.94.
> - **`/mcp` on the DGX** lists all five tools with an API key. The key
>   (`mcp-probe-s55`) was minted and removed, and `list-api-keys` confirms
>   it is gone.
> - **a new Dependabot round arrived: 34 alerts (#136–#167).** It is handled
>   by **PR #403** on `deps/security-round-oct22`, except Pillow,
>   which is blocked upstream (see Open decisions, 1).
>
> **Open issues: 41. Dependabot: 31 open after this PR merges** — #71
> (accelerate, no fix exists) plus the 29 Pillow alerts and #148's rescan.
> Re-check after the rescan lag.
>
> **Things to know before you touch anything.**
>
> **(1) The PR was built in a worktree, `~/src/localmail-sec`, with its own
> venv and `gui/node_modules`.** The Mac deployment tree (`~/src/localmail`)
> stays on `main`. Remove the worktree after the merge.
>
> **(2) No scanned PDF has arrived on the Mac since the docling 2.141
> deploy.** The extraction queue is empty (`blobs_claimable 0`) and the
> newest `docling%` row is from 2026-10-01 (`docling@2.97.0`). So #402's
> "first scanned PDF extracts" acceptance is still unobserved, not failed.
>
> **(3) The stale local branches `deps/docling-2.132` and
> `fix/docling-pdf-only-and-floors` were deleted** (both PRs merged). The
> remote branches still exist; delete them on GitHub if you want.

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

### PR #403: `deps/security-round-oct22` (opened on `main`)

| SHA | What |
|---|---|
| `456cb17` | `certifi>=2026.10.19` (#138, newly declared), `pypdf>=6.21.1` (#148), vitest `^4.1.12` (#136, #137); CLAUDE.md records the round and the Pillow block |
| handoff | this file + `docs/handoffs/2026-10-22T1057Z-security-round-oct22.md` |

Lock: certifi 2026.4.22 → 2026.10.19, pypdf 6.20.1 → 6.21.2. npm: the vitest
family 4.1.11 → 4.1.12, chai 6.2.2 → 6.3.0, tinyrainbow 3.1.1 → 3.2.0.

**Reachability, measured:**
- **Pillow.** docling-parse's default config hands embedded JPEG/JP2 bytes
  to `PIL.Image.open`, so I spied on it during a real `DoclingExtractor`
  pass over a PDF with a `/DCTDecode` image. Under `auto` (ocrmac) and
  `rapidocr`, **zero** calls received JPEG bytes. The one call is docling
  re-reading its own temp file of the rendered page; pdfium decodes the
  embedded JPEG.
- **pypdf #148** needs `page.images`, which `_extract_pdf` never calls.
- **certifi** is the outbound TLS trust store; vitest is dev-only.

### Outside the PR (operational)

- DGX aarch64 OCR probe, `/mcp` check, and the throwaway API key (removed).
- Deleted two merged local branches.

## Verification (Mac, worktree venv, all extras, at `456cb17`)

| gate | result |
|---|---|
| pytest | **4056 passed, 0 failed**, 6 warnings, 265 s (= `main`'s count; no tests added) |
| mypy | Success, **160** files (`uvx mypy --python-executable .venv/bin/python`) |
| `uv lock --check` | clean |
| `uv export --all-extras --all-groups`, old vs new lock | differs **only** in certifi and pypdf (the nvidia marker rewrite is equivalent) |
| pypdf on 300 real archive PDFs | `LightweightExtractor` output **byte-identical** 6.20.1 vs 6.21.2; the 17 that raise raise the same type |
| vitest | **512 passed** |
| svelte-check | 0 errors, 0 warnings |
| `npm audit` | 0 vulnerabilities |
| CI | see `gh pr checks`; I stopped at the push |

- **cargo was not re-run**: no Rust changed.
- **Load** was 14–27 all session, so no timing is comparable.

## What's next

### 0. **Merge the PR, deploy** *(acceptance below)*
- **You merge**, once both CI legs are green.
- **Mac:** `git checkout main && git pull`, then `uv sync --frozen --extra
  mcp --extra extraction`, then `kickstart -k` both agents. Remove the
  worktree.
- **DGX:** `git pull --ff-only`, sync with both extras, restart both units.
- **Acceptance:**
  - `/v1/version` = the merge commit on both hosts;
  - `uv pip show certifi pypdf` → 2026.10.19 / 6.21.2 on both;
  - Dependabot no longer lists #136, #137, #138 or #148 (allow for the
    rescan lag; check each alert's `fixed_at`).

### 1. **Pillow 13, once fastembed lifts its cap**
- Watch qdrant/fastembed#830 (`gh pr view 830 --repo qdrant/fastembed`).
- **When it merges:** move `[tool.uv.sources] fastembed`'s `rev` to a commit
  that has it, raise the dev group's `Pillow>=10.3` to `>=13.0.0`, re-lock,
  and run the full suite plus `probe_docling_ocr.py` on **both** hosts
  (`--engine auto --engine rapidocr` on the Mac). Pillow 13 has API
  removals, and docling, torchvision and rapidocr all import it.
- **Acceptance:** the lock holds Pillow 13.x, both probes exit 0, and the 29
  Pillow alerts close.

### 2. **Watch #402's first scanned PDF on the Mac** (Things to know, (2))
- **Acceptance:** a new `attachment_text` row with `extractor LIKE
  'docling@2.141%'` and non-empty text, and `transient_extractions` still 0.

### 3. **kastellan** (carried, yours)
- Ask whether it still wants slice C (`/problems/token-expired`); its API
  key never expires.
- Tell it about `?headers=list` (api_minor ≥ 1), attachment index paging
  (≥ 2; never compute the offset from `text.length`), and `fields` /
  `snippet_chars` (≥ 3).

### 4. **Carried issues**
- **Next extraction slice:** #400 (docling converter-construction fallback
  drops the OCR config silently). #401 (opaque-bearer `/mcp` does no RFC
  8707 check on resource-bound tokens).
- **Then:** #394, #395, #386, #389, #380, #383, #384, #377, #378, #369,
  #373, #372, #370, #371, #368, #365.
- **Not filed yet:** `snippet_source="header"` mislabel on a whole-message
  lexical win.
- **Larger work:**
  - #375 (Mac build-hash timeout cache);
  - #374 + #305 (the `cli.py` refactor, 2177 lines);
  - #358/#357, #330/#327/#328, #319/#318/#320 (the next migration slot is
    `0037_*.sql`), #340, #363, #285 (ruff in CI);
  - lifting the rapidocr cap, with its own probe;
  - admin GUI phase 5, the Users panel;
  - robustness: #218, #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **Pillow waits for fastembed** (operator decision). The alternatives
   were a uv override past fastembed's cap (it resolves cleanly) or
   dismissing the 29 alerts. *Cost if wrong:* if a later docling starts
   decoding embedded images with Pillow, the critical JPEG/TIFF decoder
   bugs become mail-reachable. Re-measure on any docling bump.
2. **certifi is declared** rather than left transitive: the `transformers`
   reasoning, a floor the resolver cannot drop below.
3. **The pypdf check used real archive PDFs**, not synthetic fixtures. The
   script was not kept. The sample is the first 300 by sha256, so it is
   arbitrary rather than chosen.
4. **Code and handoff are in one PR**, per the convention.

## Open decisions & risks

1. **29 Pillow alerts stay open**, 3 of them critical, until fastembed#830
   ships. The no-reach claim rests on one measurement of docling 2.141's
   default pipeline.
2. **#402's Mac extraction path is still unexercised by real mail** (Things
   to know, (2)).
3. **`api_minor` stays 3.** Nothing on the wire changed.
4. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.**
5. **Never run two pytest sessions against one test database.** That
   includes the worktree: it shares `localmail_test`.
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
git status && git branch --show-current  # expect main
git log --oneline -1 origin/main         # THE authority
git worktree list                        # ../localmail-sec until the PR merges

# Is the PR merged?
gh pr view 403 --json state,mergeCommit --jq '"\(.state) \(.mergeCommit.oid // "-")"'
git log --oneline main..origin/deps/security-round-oct22   # non-empty after a squash merge is expected; compare trees instead
gh issue list --limit 100 --json number --jq length       # 41
gh api --paginate repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | "\(.number)\t\(.dependency.package.name)"'
gh pr view 830 --repo qdrant/fastembed --json state,mergedAt   # the Pillow unblock

# Deployment health
launchctl list | grep localmail
curl -sk https://127.0.0.1:8443/v1/version
tail -3 ~/localmail-probe/tunnel-probe.log
ssh -o ConnectTimeout=8 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'
psql -p 5532 -d localmail -Atc "select extractor, count(*), max(extracted_at) from attachment_text where extractor like 'docling%' group by 1"

# Deploy after merge — Mac:
#   git checkout main && git pull
#   unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
#   launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
#   launchctl kickstart -k gui/$(id -u)/com.localmail.serve
#   git worktree remove ../localmail-sec && git branch -D deps/security-round-oct22
# DGX:
#   ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra extraction --extra mcp'
#   ssh 10.0.0.3 'systemctl --user restart localmail-daemon localmail-serve'

# Load before trusting any timing.
uptime; ps -A -o pid,pcpu,etime,comm -r | head -6

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4056 passed (macOS, all extras)
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 160 files
cd gui && npm test -- --run && npm run check && npm audit     # 512 passed, 0 vulns
cd gui/src-tauri && cargo test --locked && cargo clippy --locked --all-targets -- -D warnings
```

`main` was **`e2deed9`** at session start and is unchanged. This session left
**one PR**, **#403** on `deps/security-round-oct22`: commit `456cb17` plus this handoff.

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`. **Open issues: 41.**
