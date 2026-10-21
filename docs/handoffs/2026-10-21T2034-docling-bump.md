# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-10-21 (session 54).** `main` started this session at
> **`0b05382`**: #399 had been merged at 10:16Z, just before the session began.
>
> This session:
> - **deployed #399 on the Mac.** `/v1/version` → `build_hash: 0b05382`, and
>   `init-db` reports the schema up to date.
> - **dismissed the 15 docling alerts** (#116, #118–#131) as `not_used`,
>   citing #399's PDF-only restriction.
> - **found the DGX unreachable again.** The tunnel was up 06:58–09:55Z today
>   and has failed since. The Mac's end is up (`utun_rx` is a number), so it
>   is the DGX side or the path. **#397 and #399 are still not deployed
>   there.**
> - **did the docling bump: PR #402**, verified by an OCR probe that is now
>   kept in the tree.
> - **review round on #402 (2026-10-22):** the first probe's `auto` cells
>   were *rapidocr*, not ocrmac. Under the old `["en"]` language default,
>   docling 2.141's `auto` skips ocrmac on the Mac and logs it only at INFO,
>   so the deploy would have moved the Mac's OCR engine silently. Fixed by
>   defaulting `extractor_ocr_languages` to `["iso:en"]`, which also closes
>   #398. The probe now records which engine was selected and exits 1 on a
>   failed or incomplete cell.
>
> **Open issues: 42. Dependabot: 4 open** — #71 (accelerate, no fix exists)
> plus #115, #117 and #132. **Those three are fixed on `main`** (rustls
> 0.23.46, fsspec 2026.9.0, mcp 1.30.0 in the locks) and were still open 15
> minutes after the merge. That is Dependabot's rescan lag; re-check before
> doing anything about them.
>
> **Three things to know before you touch anything.**
>
> **(1) The Mac deployment tree (`~/src/localmail`) is on `main`; #402 is in
> a separate worktree, `~/src/localmail-docling`, with its own venv.**
> docling is imported lazily per extraction, so syncing a new docling into
> the venv the daemon runs from changes what a *running* daemon imports at
> its next scanned PDF, with no restart. Build dependency bumps in a
> worktree. Remove it once #402 merges (`git worktree remove
> ../localmail-docling`).
>
> **(2) MCP is not enabled on the Mac** (no `[mcp]` section), so the
> `validate_token_resource` check from #399 ("`/mcp` still accepts a
> bearer") can only be done on the DGX, if it is enabled there.
>
> **(3) The OCR probe lives in the tree now:**
> `tests/acceptance/probe_docling_ocr.py`. It is not a `run_*.py` on
> purpose: those are DB harnesses and must take the session lock, and a
> no-DB `run_*.py` fails `test_the_db_entry_calls_are_the_ones_the_harnesses_actually_use`.

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

### PR #402: `deps/docling-2.132` (opened on `main`)

| SHA | What |
|---|---|
| `824c342` | `docling>=2.132.0` → lock 2.141.0, `docling-ibm-models` 3.13.2 → 4.0.3; the OCR probe and its fixture test; README (how to run the probe) and CLAUDE.md (the deferred bump) |
| review round | `extractor_ocr_languages` default `["iso:en"]` (closes #398) + `tests/test_ocr_language_default.py`; the probe records engine selection, OCR options, platform and more versions, and exits 1 on a failed or incomplete cell; doc corrections |

Plus this handoff, in the same PR.

**The probe result** (Mac, `main` and branch in separate venvs, re-run
after the review with the hardened probe; `ocr_selection` is `Auto OCR model
selected ocrmac.` on both sides):

| cell | main (2.118.1) | branch (2.141.0) |
|---|---|---|
| all three fixtures, `auto` | invoice text, full tables | identical |
| scanned invoice + table, `rapidocr` | invoice text, full table | identical |
| native_table, `rapidocr` | **Qty column empty** | **full table** |
| all three fixtures, explicit `ocrmac` | **conversion fails** (#398) | full output |

Exit status: `main` 1 (four failed cells), branch 0. The first table in
this handoff labelled its `auto` column "ocrmac"; on the branch it had been
rapidocr, which is what the review found.

**Two caps re-examined, both kept:**
- **rapidocr `<3.10`** is now conservative rather than required. docling
  2.132+ reads `list_supported_langs`, per its own source, and tolerates
  3.10. Lifting it swaps OpenCV for `opencv-python-headless`, so it needs its
  own probe.
- **OpenCV `<5`** is still required: `docling-ibm-models` 4.0.3's
  TableFormer still imports cv2, and the package declares `<5` only inside
  its opencv extras, which docling does not request.

**The lock moved more than the first write-up said:** it gains `langcodes`,
`python-oxmsg` and `olefile` and drops `jsonlines`. `python-oxmsg` is an
Outlook `.msg` backend that #399's PDF-only restriction keeps away from mail.

### Outside the PR (operational)

- Mac deploy of `0b05382` (both agents kickstarted).
- 15 Dependabot dismissals.

## Verification (Mac, worktree venv, all extras, at `824c342`)

| gate | result |
|---|---|
| pytest | **4050 passed, 0 failed**, 6 warnings, 326 s |
| mypy | Success, **160** files (`uv run --with mypy`; the worktree venv has no mypy) |
| `uv lock --check` | clean |
| `tests/test_docling_input_formats.py` | passes against real docling 2.141 |
| CI | see `gh pr checks 402`; I stopped at the push |

- **After the review round:** **4056 passed, 0 failed**, 6 warnings, 294 s;
  mypy clean, 160 files (`uvx mypy --python-executable .venv/bin/python`,
  which does not re-sync the venv the way `uv run --with` can). +11 over
  `main`: 8 in `tests/test_docling_ocr_probe.py`, 3 in
  `tests/test_ocr_language_default.py` (real docling; they skip on CI, which
  installs no `[extraction]`).
- **+5 tests** (first commit), measured with `--collect-only`: `main` at `0b05382`
  collects **4045** and the branch **4050**, all 5 in
  `tests/test_docling_ocr_probe.py`. (Session 53's handoff said 4044 for
  `main`; its review round added one before merge.)
- **Warnings 18 → 6:** docling 2.141 emits far fewer torch/docling
  deprecations from the real-docling tests.
- **GUI untouched**, so cargo/vitest were not re-run.
- **Load** was ~8 all session. No timing is comparable.

## What's next

### 0. **Merge #402, deploy** *(acceptance below)*
- **You merge**, once `gh pr checks 402` is green on both legs.
- **Mac:** `git checkout main && git pull`, then `uv sync --frozen --extra
  mcp --extra extraction` (this one installs the new docling), then
  `kickstart -k` both agents.
- **Then remove the worktree** (Things to know, (1)).
- **Acceptance:**
  - `/v1/version` = the merge commit;
  - `uv pip show docling` → 2.141.0;
  - **the first scanned PDF after the restart extracts** — check
    `transient_extractions` stays at 0 and a new `attachment_text` row has
    `extractor` starting `docling`;
  - Dependabot lists **#71 only** (#115/#117/#132 should have closed by
    then; if not, inspect each alert's `fixed_at`).

### 1. **The DGX: restore reachability, then deploy #397 + #399 + #402**
- **Triage per the `dgx-network-drops` memory:** `journalctl --list-boots`
  first, then the persistent probe logs. Its LAN IP moves (DHCP), and this
  Mac was on a different network (10.3.10.x) this session, so the LAN path
  was not available either.
- **Today's tunnel transitions** (from `~/localmail-probe/tunnel-probe.log`):
  FAIL from 2026-10-20 01:49Z to 22:16Z, again 22:21Z → 2026-10-21 06:44Z,
  up 06:58–09:55Z, then FAIL. **Read the DGX's own probe log for those
  windows before hypothesising** — the memory lists five refuted theories.
- **Acceptance:**
  - both units report `NRestarts=0` and `build_hash` = the `main` merge
    commit;
  - `uv pip show pypdf docling` → 6.20.1 / 2.141.0;
  - **`uv run python tests/acceptance/probe_docling_ocr.py --engine auto`**
    returns the invoice and both tables with text (`auto` = rapidocr there).
    This is the first aarch64 check of the rapidocr/OpenCV caps *and* of
    ibm-models 4;
  - if `[mcp]` is enabled there, `/mcp` `tools/list` with an API key
    returns the five tools (mint one with `add-api-key`, remove it after).

### 2. **kastellan** (carried, yours)
- Ask whether it still wants slice C (`/problems/token-expired`); its API
  key never expires.
- Tell it about `?headers=list` (api_minor ≥ 1), attachment index paging
  (≥ 2; never compute the offset from `text.length`), and `fields` /
  `snippet_chars` (≥ 3).

### 3. **Carried issues**
- **Newest:** #401 (opaque-bearer `/mcp` does no RFC 8707 check on
  resource-bound tokens), #400 (docling converter-construction fallback drops
  the OCR config silently; the probe now records the OCR options per engine,
  which would show it). #398 is closed by #402. #400 is the natural next
  extraction slice.
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

1. **Locked at 2.141.0, not 2.132.0.** 2.141 was the newest release at lock
   time (a day old); the next docling release will move a bare `uv lock
   --upgrade`, so re-run the probe then. *Cost if wrong:* nine more docling
   releases of untested-by-the-suite change. The probe is the mitigation,
   and it found only an improvement.
2. **The rapidocr cap stays**, though no longer required. *Cost if wrong:*
   none; we just do not get rapidocr 3.10.
3. **The probe is in the tree, not the scratchpad**, and not named `run_*`.
4. **The bump was built in a worktree**, to keep the live venv on `main`.
5. **The 15 dismissals went ahead** without a fresh confirmation: the
   operator chose dismissal in session 53, conditional on #399 being on
   `main`, which it was.
6. **Code and handoff are in one PR**, per the convention.
7. **The fix is a new default (`["iso:en"]`), not code that adds `iso:` to
   bare values.** A bare value now means "engine-native" in docling, and a
   user who set one (tesserocr's `eng`, say) meant exactly that. *Cost if
   wrong:* a `config.toml` that sets a bare `extractor_ocr_languages` keeps
   the ocrmac skip; the Mac's sets none, the DGX's is unchecked.

## Open decisions & risks

1. **#402 is open and yours to merge** (step 0).
2. **The DGX has not received #397, #399 or #402.** Its aarch64 OCR is
   unverified for all three. Deploy them together, then run the probe.
3. **ibm-models 4 is new layout models.** The probe covers three synthetic
   PDFs. Real scanned mail is more varied, so watch the first day of
   extractions on the Mac (`attachment_text` with `extractor LIKE
   'docling%'`, and `transient_extractions`).
4. **oauthlib 4.0 and docling-parse 7 (from #397) are still lightly
   exercised.**
5. **`api_minor` stays 3.** Nothing on the wire changed.
6. **THIS FILE IS NOT THE AUTHORITY; `git` and `gh` are.**
7. **Never run two pytest sessions against one test database.** That
   includes the worktree: it shares `localmail_test`, and the session lock
   would make a second run wait.
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
git status && git branch --show-current  # expect main
git log --oneline -1 origin/main         # THE authority
git worktree list                        # ../localmail-docling until #402 merges

# Is #402 merged?
gh pr view 402 --json state,mergeCommit --jq '"\(.state) \(.mergeCommit.oid // "-")"'
gh pr checks 402
git log --oneline main..origin/deps/docling-2.132   # non-empty after merge = stranded
gh issue list --limit 100 --json number --jq length      # 42
gh api --paginate repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | "\(.number)\t\(.dependency.package.name)"'

# Deployment health
launchctl list | grep localmail
curl -sk https://127.0.0.1:8443/v1/version
tail -3 ~/localmail-probe/tunnel-probe.log     # tunnel FAIL + utun_rx=<number> = DGX side
ssh -o ConnectTimeout=8 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'

# Deploy after merge — Mac:
#   git checkout main && git pull
#   unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
#   unset VIRTUAL_ENV && uv run localmail init-db      # expect "schema already up to date"
#   launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
#   launchctl kickstart -k gui/$(id -u)/com.localmail.serve
#   git worktree remove ../localmail-docling && git branch -D deps/docling-2.132
# DGX:
#   ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra extraction --extra mcp'
#   ssh 10.0.0.3 'systemctl --user restart localmail-daemon localmail-serve'
#   ssh 10.0.0.3 'cd ~/src/localmail && ~/.local/bin/uv run python tests/acceptance/probe_docling_ocr.py --engine auto'

# Load before trusting any timing.
uptime; ps -A -o pid,pcpu,etime,comm -r | head -6

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4050 passed (macOS, all extras)
unset VIRTUAL_ENV && uv run --with mypy mypy src/localmail     # Success, 160 files
cd gui && npm test -- --run && npm run check && npm audit
cd gui/src-tauri && cargo test --locked && cargo clippy --locked --all-targets -- -D warnings

# This change directly:
unset VIRTUAL_ENV && uv run pytest -q tests/test_docling_ocr_probe.py tests/test_docling_input_formats.py
unset VIRTUAL_ENV && uv run python tests/acceptance/probe_docling_ocr.py --engine auto --engine rapidocr
```

`main` was **`0b05382`** at session start and is unchanged. This session left
**one PR**: **#402** on `deps/docling-2.132`, commit `824c342`, plus this
handoff.

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`. **Open issues: 42. Dependabot: #71, plus #115/#117/#132
awaiting rescan.**
