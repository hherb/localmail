# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-10-23 (session 58).** `main` was **`1707e88`** at
> session start (#406 merged, closing #401), and is unchanged.
>
> This session:
> - **deployed #406 on both hosts.** `/v1/version` → `build_hash: 1707e88`
>   on the Mac and the DGX; DGX units `active`, `NRestarts=0`. The Mac tree
>   was already on `main` and the #401 branch already deleted.
> - **fixed #405: PR on `fix/405-broken-docling-import`.**
>
> **Open issues: 40, 39 once the PR merges** (`Closes #405`).
>
> **Things to know before you touch anything.**
>
> **(1) The Mac deployment tree (`~/src/localmail`) is on the PR branch,
> not `main`.** The branch touches `search/extractor.py` and
> `search/extract_worker.py`, which the **daemon** runs (the extract
> worker). The daemon has not been restarted, so the code that runs is
> still `1707e88`'s. `git checkout main` after the merge is step 0.
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

### PR: `fix/405-broken-docling-import` (opened on `main`)

| SHA | What |
|---|---|
| `0245f2e` | #405: a docling that is installed but will not import is held as an `ExtractorConfigurationError`; the `convert()` ImportError warning reworded; tests, CLAUDE.md, README |
| handoff | this file + `docs/handoffs/2026-10-23T0552Z-broken-docling-import.md` |

- **The defect.** `_try_import_docling` caught every `ImportError` and
  returned `None` ("not installed"). A partial `uv sync` that dropped one of
  docling's dependencies therefore made the worker store
  `lightweight-empty` for every scanned PDF, and a blob with an
  `attachment_text` row is never claimed again.
- **The fix (`search/extractor.py`).**
  - `importlib.util.find_spec("docling") is None` is the only "absent".
  - Any failure after that is an `ExtractorConfigurationError`, held on the
    #153 transient budget and cleared by `retry-failed-extractions`. That
    includes a non-`ImportError` (a native `.so` raising `OSError`, which
    used to escape as an `'unexpected'` poison pill) and a `find_spec` that
    itself raises.
  - The worker's outer handler already routes that error to
    `transient_extractions` with no `attachment_text` row, so no worker
    change was needed for the hold itself.
- **`search/extract_worker.py`:** `_try_import_docling` is called **only for
  PDFs** now. Without that gate, a broken docling would have held every
  empty *text* file too.
- **Part 2 of #405 (wording).** Any `ImportError` out of `convert()` was
  logged as "OCR engine … is not usable". The warning now says "a module
  docling needs", names the engine as the likely cause, and names the
  `uv sync --extra extraction` repair. The function name
  `warn_ocr_engine_unavailable` was kept.
- **Tests (`tests/test_docling_import_state.py`, new, 7 tests; +1 assertion
  in `test_extractor.py`).**
  - They fake presence (patched `importlib.util.find_spec`) and failure
    (`sys.modules["docling.document_converter"]`), so they mean the same
    with or without `[extraction]`.
  - Covered: absent → `None` with no import attempted; present but
    `ModuleNotFoundError` / `OSError` → configuration error; `find_spec`
    raising → held; worker end to end (no row, `transient_count` 1, no
    poison pill); the absent-docling control (still `lightweight-empty`);
    and an empty text file under a broken docling (still the sentinel).
- **Mutations, each caught by its own test:** restoring
  `except ImportError: return None`, dropping the worker's PDF gate, and
  hoisting `find_spec` out of the `try`.

## Verification (Mac, `~/src/localmail` venv, all extras, `0245f2e`)

| gate | result |
|---|---|
| pytest | **4080 passed, 0 failed**, 6 warnings, 340 s |
| mypy | Success, **160** files |
| focused (`test_docling_import_state`, `test_extractor`, `test_extract_worker`, `test_docling_input_formats`, `test_extract_worker_allowlist`) | 110 passed |
| CI | see `gh pr checks`; I stopped at the push |

- **Load** was 15–19 during the run (another project's pytest was running
  against its own database), so no timing is comparable.
- **GUI untouched**, so cargo and vitest were not re-run.
- **No review round was run** (`/review-pr`). Consider one before merging.

## What's next

### 0. **Merge the PR, return the Mac to `main`** *(acceptance below)*
- **You merge**, once both CI legs are green.
- **Mac:**
  1. `git checkout main && git pull`;
  2. restart **both** agents: the change is in the daemon's extract worker.
     `launchctl kickstart -k gui/$(id -u)/com.localmail.daemon` and the same
     for `com.localmail.serve` (so `/v1/version` reports the merge);
  3. delete the local branch. After a squash merge, `git branch -d` refuses,
     so first check `git diff --stat main fix/405-broken-docling-import` is
     empty, then use `-D`.
- **DGX:** `git pull --ff-only`, then restart both units.
- **Acceptance:**
  - `/v1/version` reports the merge commit on both hosts;
  - `gh issue view 405` is closed.

### 1. **Pillow 13, once fastembed lifts its cap** (carried)
- qdrant/fastembed#830 was still **open** on 2026-10-23.
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
- **Next small slice:** #377 — a NUL byte in the search query or a text
  filter is an unhandled 500 (psycopg `DataError`).
  - **Acceptance:** a NUL in `query`, `from`, `to`, `subject` or `lang` is a
    400 problem+json on HTTP and a `ToolError` on MCP, decided before the
    empty-ACL short-circuit (the #348 placement rule), with a test per
    field.
  - Decide first whether a NUL is *refused* or *stripped*
    (`pgtext.strip_nuls` is the one NUL rule); refusal matches the
    "honoured or refused, never dropped" rule of #364.
- **Then:** #394, #395, #386, #389, #380, #383, #384, #378, #369, #373,
  #372, #370, #371, #368, #365.
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

1. **`find_spec` decides absence, not `ModuleNotFoundError.name`.** Both
   work; `find_spec` is what the last handoff named and does not depend on
   which module the failing import happened to name.
2. **A failed docling import is retried per PDF rather than cached.** The
   cost is one failed import attempt per scanned PDF while the install is
   broken, bounded by the transient budget. The gain is that a repaired
   install is usually picked up without restarting the daemon. Not after a
   half-upgrade: only the failed modules leave `sys.modules`, so the retry
   mixes new files with old in-memory modules. Restart after repairing one.
3. **Deploying #406 went ahead without a fresh confirmation**, following
   the handoff's step 0 and the precedent of sessions 54–57.
4. **Code and handoff are in one PR**, per the convention.

## Open decisions & risks

1. **Pillow alerts stay open**, some critical, until fastembed#830 ships.
   The claim that mail cannot reach them rests on one measurement of
   docling 2.141's default pipeline.
2. **The Mac's extraction path is still unexercised by real mail** under
   docling 2.141.
3. **The Mac tree is on the PR branch until step 0.**
4. **`run_attachment_eval.py` now raises on a broken docling** instead of
   reporting it absent. That is intended (a harness should fail loudly),
   but it is a behaviour change in a file this PR did not edit.
5. **`api_minor` stays 3.** Nothing on the wire changed.
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
git status && git branch --show-current  # fix/405-broken-docling-import until step 0
git log --oneline -1 origin/main         # THE authority
git worktree list

# Is the PR merged?
gh pr list --state all --head fix/405-broken-docling-import --json number,state,mergeCommit
gh issue list --limit 200 --json number --jq length       # 39 after the merge
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
#   launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
#   launchctl kickstart -k gui/$(id -u)/com.localmail.serve
#   git diff --stat main fix/405-broken-docling-import   # empty ⇒ safe
#   git branch -D fix/405-broken-docling-import
# DGX:
#   ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && systemctl --user restart localmail-daemon localmail-serve'

# Load before trusting any timing; no other localmail pytest may be running.
uptime; pgrep -fl pytest

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4080 passed (macOS, all extras)
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 160 files
cd gui && npm test -- --run && npm run check && npm audit
cd gui/src-tauri && cargo test --locked && cargo clippy --locked --all-targets -- -D warnings

# This change directly:
unset VIRTUAL_ENV && uv run pytest -q tests/test_docling_import_state.py tests/test_extractor.py tests/test_extract_worker.py
```

`main` was **`1707e88`** at session start and is unchanged. This session left
**one PR** on `fix/405-broken-docling-import`: commit `0245f2e` plus this
handoff.

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`. **Open issues: 40 (39 after the merge).**
