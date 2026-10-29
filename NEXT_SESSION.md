# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-10-29 (session 61).** `main` was **`9154672`** at
> session start (#409 merged, closing #394), and is unchanged.
>
> This session:
> - **deployed #409 on both hosts.** `/v1/version` → `build_hash: 9154672`
>   on the Mac and the DGX; DGX units `active`. Local branch
>   `fix/394-cap-search-fields` deleted (diff against `main` empty).
> - **closed carried item 2:** two `docling@2.141.0` rows exist on the Mac
>   (2818 and 1660 chars of text, 2026-10-27/28); `transient_extractions` is 0.
> - **fastembed#830 merged and shipped in fastembed 0.9.1**, so the Pillow
>   bump went ahead: **PR #410 on `chore/pillow13-fastembed-091`.**
>
> **Open issues: 37** (#410 closes Dependabot alerts, not issues).
>
> **Things to know before you touch anything.**
>
> **(1) The PR was built in a worktree, `../localmail-pillow13`, with its own
> venv.** The Mac deployment tree (`~/src/localmail`) is still on `main` at
> `9154672`, and its venv still holds docling 2.141 / Pillow 12. After the
> merge you **must** `uv sync` there (step 0). A `git pull` alone changes
> nothing the daemon imports.
>
> **(2) docling is deliberately NOT the newest release.** It is locked at
> 2.142.1 because 2.143.x/2.144.0 fail the OCR probe (below), and
> `pyproject.toml` caps `docling<2.143` so a bare `uv lock --upgrade` cannot
> undo that. Lift the cap only with a passing probe on both hosts.
>
> **(3) Leftover worktree `.claude/worktrees/bottie-bearer-token-dbc203`**
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

### PR #410: `chore/pillow13-fastembed-091` (opened on `main`)

| SHA | What |
|---|---|
| `8f9b169` | fastembed `>=0.9.1` from PyPI (git rev dropped); `pillow>=13.0.0`; docling locked 2.142.1; `filelock>=4.2.1`, `setuptools>=84.1.0`; CLAUDE.md round |
| `250555e` | CLAUDE.md: the aarch64 probe result |
| handoff | this file + `docs/handoffs/2026-10-28T2158Z-pillow13-fastembed-091.md` |

- **Why docling moved.** docling 2.141's whole stack (slim, core, parse,
  ibm-models) declares `pillow<13`, which the third round's "nothing else caps
  it" missed. 2.142 is the first release to lift it.
- **Why 2.142.1 and not 2.144.0.** On 2.143.x and 2.144.0,
  `probe_docling_ocr.py`'s `native_table` under **rapidocr** comes back with
  the Qty column empty. That reproduced 2 of 2 times.
  - **Bisect:** Pillow 12 vs 13, docling-parse 7.22.3 vs 7.24.1 and
    ibm-models 4.0.3 vs 4.0.4 each make no difference. docling 2.142.0 and
    2.142.1 pass, and 2.143.1 fails with everything else held at the new
    versions.
  - **It is not a production path today:** `DoclingExtractor` only sees PDFs
    with no text layer, and the scanned fixtures pass on every version.
- **Lock moves:** docling/-slim 2.141.0 → 2.142.1, docling-core 2.101.1 →
  2.105.0, docling-parse 7.22.3 → 7.24.1, ibm-models 4.0.3 → 4.0.4, doclang
  0.7.3 → 0.8.0, fastembed 0.8.0 (git) → 0.9.1, filelock 3.29.0 → 4.2.4,
  pillow 12.3.0 → 13.0.0, setuptools 83.0.0 → 84.2.1. Nothing else.

## Verification

| check | Mac | DGX (aarch64) |
|---|---|---|
| `probe_docling_ocr.py`, `main` vs branch | 9/9 cells byte-identical (`auto`=ocrmac, `rapidocr`, `ocrmac`), exit 0 | 6/6 byte-identical (`auto`=rapidocr, `rapidocr`), exit 0 |
| embedding + rerank probe (scratch, not kept) | full-vector hashes + scores bit-identical | bit-identical |
| `/DCTDecode` JPEG reaching `PIL.Image.open` (scratch spy, not kept) | 0 on 2.141 and 2.142.1 | 0 |
| pytest (branch venv) | **4170 passed**, 6 warnings, 275 s | — |
| mypy | Success, 160 files | — |
| CI | see `gh pr checks 410`; I stopped at the push | |

- Load was 10–14 throughout (a bmlibrarian suite was running alongside on
  its own DB); no timing is comparable.
- The `rapid_ocr_model.py:658 DeprecationWarning` is pre-existing: `main`
  emits it too.
- GUI untouched, so cargo and vitest were not re-run.

## What's next

### 0. **Merge #410, then sync both deployments**
- **You merge**, once both CI legs are green. On merge, Dependabot should
  close #139–#184 (pillow, filelock, setuptools).
- **Mac:**
  - `git pull`, then
    `uv sync --frozen --extra mcp --extra extraction` in `~/src/localmail`.
  - Restart **both** agents. docling is imported lazily, so a daemon left
    running would mix old modules in memory with new ones from disk.
  - Remove the worktree: `git worktree remove ../localmail-pillow13`, then
    `git branch -D chore/pillow13-fastembed-091`.
- **DGX:** `git pull --ff-only`,
  `~/.local/bin/uv sync --frozen --extra mcp --extra extraction`, then
  restart both units.
- **Acceptance:**
  - `/v1/version` reports the merge commit on both hosts.
  - `.venv/bin/python -c "import PIL, docling; print(PIL.__version__)"`
    prints 13.0.0 on both hosts.
  - The open-alert listing shows only accelerate (#71) and rustls (#115).

### 1. **Dismiss Dependabot #115 (rustls) as stale** (yours: outward action)
- It reports `= 0.23.40`; `gui/src-tauri/Cargo.lock` holds 0.23.46.
- **Acceptance:** alert closed. If it re-opens, check `cargo tree -i rustls`
  for a second copy.

### 2. **Watch for docling > 2.144** (new)
- Re-run the probe under rapidocr against any new release. If `native_table`
  passes, move the lock forward. Consider filing upstream: native PDF + a
  `FULL_PAGE` rapidocr pass drops single-digit table cells from 2.143 on.
  That issue would need a repro outside localmail.
- **Acceptance:** lock on the newest release whose probe exits 0 on both
  hosts.

### 2a. **emSign G3 in the OS trust stores** (carried)
- Re-check after each OS or `ca-certificates` update (commands below). Mac
  `/etc/ssl/cert.pem` still names it (count 2 on 2026-10-29).

### 3. **kastellan** (carried, yours)
- Ask whether it still wants slice C (`/problems/token-expired`).
- Tell it about `?headers=list` (api_minor ≥ 1), attachment index paging
  (≥ 2), `fields` / `snippet_chars` (≥ 3), and the 48-entry `fields` cap.

### 4. **Carried issues**
- **Next small slice:** #395, which forbids a new `{@html}` sink in
  `gui/src`.
  - **Acceptance:**
    - A pure rule (the `_rerank_wiring_rules.py` shape) scans every `.svelte`
      file under `gui/src` and reports any `{@html`.
    - A test runs it (in vitest or pytest), with a positive control proving
      it finds one.
    - Today's tree has zero hits.
- **Then:** #386, #389, #380, #383, #384, #378, #369, #373, #372, #370,
  #371, #368, #365.
- **Not filed yet:** the `snippet_source="header"` mislabel on a
  whole-message lexical win.
- **Larger work:**
  - #375; #374 + #305 (the `cli.py` refactor).
  - #358/#357, #330/#327/#328, #319/#318/#320 (next migration `0037_*.sql`).
  - #340, #363, #285.
  - Lifting the rapidocr cap; admin GUI phase 5 (Users panel).
  - Robustness: #218, #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **The Pillow bump went ahead without asking**, because the third round
   recorded the trigger ("when #830 ships, bump"). It needed one deviation
   the handoff did not foresee: dropping the git source for PyPI rather
   than moving the rev.
2. **docling locked at 2.142.1, not newest.** This follows the probe's
   exit status, the rule the probe was kept for. Say so before merging if
   you would rather take 2.144 and accept the native-table difference.
3. **filelock and setuptools were folded in** because they were open on the
   same lock and cost no extra verification.
4. **#395 deferred** to keep this PR one concern (dependencies) for review.
5. **Deploying #409 went ahead without a fresh confirmation**, per the
   handoff's step 0.

## Open decisions & risks

1. **A deployment left on `git pull` without `uv sync` silently keeps
   Pillow 12 / docling 2.141.** Step 0 is two commands per host, not one.
2. **The native-table regression in docling ≥ 2.143 is unexplained**, only
   localised. If it reaches scanned tables in a later release, the probe's
   `scanned_table` cell is what will catch it.
3. **accelerate #71 has no fix**; unchanged.
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
git status && git branch --show-current  # main
git log --oneline -1 origin/main         # THE authority
git worktree list                        # ../localmail-pillow13 until step 0

# Is the PR merged?
gh pr view 410 --json state,mergeCommit
gh issue list --limit 200 --json number --jq length       # 37
gh api --paginate repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | .dependency.package.name' | sort | uniq -c

# After merge — Mac:
#   git pull && unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
#   launchctl kickstart -k gui/$(id -u)/com.localmail.serve
#   launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
#   .venv/bin/python -c "import PIL; print(PIL.__version__)"     # 13.0.0
#   git worktree remove ../localmail-pillow13 && git branch -D chore/pillow13-fastembed-091
# DGX:
#   ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra mcp --extra extraction && systemctl --user restart localmail-daemon localmail-serve'

# Deployment health
launchctl list | grep localmail
curl -sk https://127.0.0.1:8443/v1/version
tail -3 ~/localmail-probe/tunnel-probe.log
ssh -o ConnectTimeout=8 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'
psql -p 5532 -d localmail -Atc "select extractor, count(*), max(extracted_at) from attachment_text where extractor like 'docling%' group by 1"
psql -p 5532 -d localmail -Atc "select count(*) from transient_extractions"
grep -c "emSign ECC Root CA - G3" /etc/ssl/cert.pem
ssh 10.0.0.3 'ls /etc/ssl/certs | grep -ic emsign_ecc_root_ca_-_g3'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4170 passed (macOS, all extras)
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 160 files
cd gui && npm test -- --run && npm run check && npm audit
cd gui/src-tauri && cargo test --locked && cargo clippy --locked --all-targets -- -D warnings

# This change directly (probe; exits 1 on any failed cell):
.venv/bin/python tests/acceptance/probe_docling_ocr.py --engine auto --engine rapidocr --engine ocrmac
```

`main` was **`9154672`** at session start and is unchanged. This session left
**one PR**, #410 on `chore/pillow13-fastembed-091`: commits `8f9b169` and
`250555e`, plus this handoff.

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`. **Open issues: 37.**
