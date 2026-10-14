# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-10-14 (session 52).** `main` was **`4912799`** at
> session start: the operator merged #393 and its review round #396 between
> sessions. That round is described in the previous handoff
> (`docs/handoffs/2026-09-20T0707-slice-e-review-followups.md`).
>
> This session:
> - **confirmed the Mac deployment is up**: both launchd agents running
>   since 2026-10-01, `/v1/version` → `api_minor: 3`, `build_hash: 4912799`,
>   all seven heartbeats current. The IDLE `BrokenPipeError` tracebacks in
>   `daemon.err.log` are ordinary reconnects.
> - **cleared the Dependabot backlog that had built up since the last
>   session**: **32 new alerts** (#81–#114), against the one the last handoff
>   expected. Fixed in **PR #397** (see below), which leaves only #71 open.
> - **reviewed #397 and fixed what the review found.** Two of the findings were
>   real breakage the first probe could not see: rapidocr 3.10 broke docling's
>   OCR outright (the DGX's engine), and docling 2.118 changed ocrmac output on
>   scanned tables (the Mac's engine). See "The review round" below.
>
> **Open issues: 40** (37 + #394, #395 filed by the #393 review, + #398 filed
> by the #397 review). **Dependabot: 33 open → 1 after #397 merges** (#71
> `accelerate`, left open on purpose).
>
> **Three things to know before you touch anything.**
>
> **(1) The production venv already carries #397's dependencies.** This Mac's
> `.venv` *is* the deployment, and testing the bump meant syncing it. The
> running agents still hold the old modules in memory, so nothing has changed
> yet. But the tree is on `chore/security-floors-2026-10`, and **any restart
> picks up the new dependencies** and the branch's one code change (the
> `FULL_PAGE` OCR mode). That is safe: the second probe below compares the
> branch with `main` directly. But it means "deploy" on the Mac is a restart
> once #397 merges, not a sync.
>
> **(2) pypdf is the one that matters.** Eight HIGH DoS advisories on the
> emailed-PDF extraction path. Everything else is hygiene: urllib3, oauthlib,
> PyJWT, docling and devalue are not on a mail-borne path.
>
> **(3) docling is pinned at 2.118.1 on purpose.** A bare `uv lock --upgrade`
> takes 2.138, which moves `docling-ibm-models` 3 → 4 (new layout models). Do
> that only as its own change, with its own OCR probe, **under every engine a
> host uses** (rapidocr and ocrmac), against `main` in its own worktree venv.

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

### PR #397: `chore/security-floors-2026-10` (opened on `main`)

| SHA | What |
|---|---|
| `c956678` | Five floors in `pyproject.toml` + `uv.lock` |
| `6d830b7` | `gui/package-lock.json`: `npm audit fix` |
| `77c75d8` | CLAUDE.md: the October 2026 round |
| *(review round)* | rapidocr and OpenCV caps, `FULL_PAGE` OCR mode + test, doc corrections |

Plus this handoff, in the same PR.

| package | lock | alerts | reach |
|---|---|---|---|
| **pypdf** `>=6.19.0` | 6.16.2 → **6.20.1** | #105–#112, 8 HIGH | **attacker-controlled**: `_extract_pdf` over an emailed PDF |
| urllib3 `>=2.8.0` (new) | 2.7.0 → 2.8.0 | #94–#96, 2 HIGH | hygiene: talks to Google and the HF hub only |
| oauthlib `>=4.0.0` (new) | 3.3.1 → **4.0.0** (major) | #84, #85 | hygiene: both are server-endpoint advisories; we are the client |
| pyjwt `>=2.15.0` (new, `[mcp]`) | 2.13.0 → 2.15.1 | #81–#83, #86–#93, #97, #98 (one CRITICAL) | hygiene: only the MCP SDK's *client* credentials extension imports it |
| docling `>=2.118.1` (raised from `>=2.94.0`) | 2.97.0 → **2.118.1** | #113 (docling-slim), #114 | hygiene: HTML browser mode; `DoclingExtractor` is PDF-only |
| rapidocr `<3.10` (new cap) | 3.8.1 → 3.9.2 | none | **breakage**: 3.10.0 removed symbols docling 2.118.1 imports |
| opencv-python `<5` (new cap) | 4.13.0.92 → 4.14.0.94 | none | docling-ibm-models (TableFormer) declares `<5` |
| devalue (npm, lock-only) | 5.8.1 → 5.9.4 | #99, #101, #103, #104 | hygiene: Svelte's *server* render; the GUI is a client-only SPA |

Notes on the table:
- **"(new)"** means newly *declared*. The package arrived transitively with
  no floor, which is the `transformers` lesson.
- **The docling lock moved more than docling**: docling-parse 6.2.0 → 7.22.3,
  docling-core 2.78.0 → 2.101.1, and a new `doclang` 0.7.3. The first lock
  also carried rapidocr 3.10 and an opencv-python-headless 5.0 swap; the
  review round capped both back out.
- **#100 and #102 (devalue) were auto-dismissed by Dependabot**, which is why
  #81–#114 holds 34 numbers and 32 alerts.
- **`npm audit fix` also moved source-map-js** 1.2.1 → 1.2.2. It is
  build-time, and Dependabot had not raised it yet.

### Verification: a before/after probe, not the suite

The suite cannot see this change: every docling test mocks the converter, and
every OAuth test mocks the flow. So a scratchpad probe (not kept, like its
predecessors) recorded three things before syncing and again after:

- **pypdf:** `LightweightExtractor` over seven PDF shapes: plain, FlateDecode
  multipage, outlined, page-labelled, embedded files, all of those combined,
  and a scanned page.
- **oauthlib:** the consent URL and the token-exchange request that **both**
  production flows build (`InstalledAppFlow` for `oauth-login`, `Flow` for the
  admin Connect flow), with the HTTP transport mocked and the PKCE nonce
  masked.
- **docling:** a **real** `DoclingExtractor` pass over a scanned PDF (OCR via
  ocrmac) and a native two-page PDF (the layout and docling-parse path).

**All three sections were byte-identical.** The OCR string is
`"Invoice 4711 total 250 EUR"` on both sides. **That docling evidence was too
narrow**: it ran only `auto` (ocrmac on the Mac) over a one-line page. The
review round below found two defects it could not see.

**One behaviour change:** docling 2.118 `torch.compile`s its layout model by
default. The first extraction in a process now logs ~35 lines of
`torch._dynamo` graph-break warnings, once per process. Output is unchanged;
it is noise in `daemon.err.log`. Recorded in CLAUDE.md.

### The review round

A five-aspect review (code and comments agents, findings checked by hand)
raised three Important findings and five doc inaccuracies. While fixing them,
a second probe found the real defects:

- **rapidocr 3.10.0 broke OCR.** docling 2.118.1's `rapid_ocr_model` imports
  `COMMON_LANG_ALIASES` and `PP_OCRV6_LANGS` from
  `rapidocr.utils.model_resolver`. 3.10.0 (2026-10-08) removed both, and
  docling's range (`>=3.9.1,<4`) admits it. Checked against the 3.9.1, 3.9.2
  and 3.10.0 wheels. Every rapidocr pass raised `ImportError`. `auto` picks
  rapidocr on Linux, so **every scanned PDF on the DGX** would have been given
  up on. Capped at `<3.10`.
- **OpenCV 5 had come in with it.** rapidocr 3.10 required
  opencv-python-headless (uncapped), which the lock took at 5.0.0.93, while
  docling-ibm-models 3.13.2 declares `<5`. With 3.9.2 the dependency is
  `opencv-python` again, capped at `<5` (lock: 4.14.0.94).
- **docling 2.118 changed ocrmac output.** Its new default OCRs layout regions
  (`OcrMode.DEFAULT`). On a scanned three-row table ocrmac returned loose
  lines, with the table gone and the `3` dropped. 2.97 OCRed the whole
  page. `extractor._ocr_the_whole_page` sets `OcrMode.FULL_PAGE` for every
  engine. That is right for docling's input anyway, which is only PDFs with
  no text layer. Pinned by `test_pdf_pipeline_options_ocr_the_whole_page`,
  and mutation-checked: deleting the call fails all three cases.
- **Second probe:** `main` in its own worktree venv against the branch, a real
  `DoclingExtractor` over a scanned invoice, a scanned table and a native
  table, under `rapidocr` **and** `auto`. **All six byte-identical to `main`**
  after the fixes. Before them, three were `ImportError`s and two differed.
- **Filed, not fixed: #398.** An explicit `extractor_ocr_engine = "ocrmac"`
  fails every pass (`en` is not a region tag ocrmac accepts). It is the same
  on `main`, and both deployments run `auto`.
- **Doc corrections** (CLAUDE.md, pyproject comments): docling was a raised
  floor, not a new one; the alert count; `mcp` requires `pyjwt[crypto]`
  directly; urllib3 also talks to modelscope.cn; a reason for devalue that
  covers #99; and the stale "Linux gets no OCR engine" note.

| gate | result |
|---|---|
| pytest | **4038 passed, 0 failed**, 3 warnings, 224 s (after the review round) |
| mypy | Success, **160** files |
| `gui/` vitest | **512 passed**, 49 files |
| `svelte-check` | 326 files, **0 errors, 0 warnings** |
| `npm audit` | **0 vulnerabilities** |
| `uv lock --check` | clean |
| CI | see `gh pr checks 397`; I stopped at the push |

- **The count is the last handoff's 4035 plus the three new parametrised
  `FULL_PAGE` cases.** They skip in CI, which does not install `[extraction]`.
- **Warnings: 5 on the first run, 3 after the review round.** The known three
  are the #25 websockets pair and anyio's `BlockingPortal`. The first run's
  extra two were an `hf_xet` deprecation from
  `test_fastembed_backend_real_model_smoke`, which only shows when the model
  download goes to the network. huggingface_hub did not change in this bump.
- **Load:** average ~8–10 with an unrelated rustc build running. No timing
  here is comparable.

## What's next

### 0. **Merge #397, then deploy** *(acceptance: Dependabot 33 → 1)*
- **You merge**, once `gh pr checks 397` is green on both legs (3.12 + 3.13).
- **Mac:** `git checkout main && git pull`, then
  `uv sync --frozen --extra mcp --extra extraction`. That should be a no-op,
  since the venv is already synced; `init-db` should be a no-op too. Then
  `kickstart -k` both agents.
- **DGX:** same as the Mac, and expect the usual cusparselt re-resolve.
- **Acceptance:**
  - both hosts report `build_hash` = the merge commit;
  - `uv pip show pypdf` → 6.20.1 on both;
  - `gh api …/dependabot/alerts` lists **#71 only**;
  - the first PDF extraction after the restart works. Look for the dynamo
    warning block in `daemon.err.log`, then an `attachment_text` row.
  - **on the DGX, a scanned PDF extracts with text** (`extractor = 'docling'`,
    non-empty `extracted_text`). It is the only check of rapidocr and OpenCV on
    aarch64 wheels, and the defect the cap fixes would have shown up there as
    `transient_extractions` rows naming `COMMON_LANG_ALIASES`.
- **Re-query Dependabot a few minutes after the merge.** Alerts read *open*
  for a while (seen last time).

### 1. **Ask kastellan about slice C, then build it or close it out** (carried)
- **Agreed 2026-09-14:**
  - a 401 with `/problems/token-expired` and `WWW-Authenticate: Bearer
    error="invalid_token"`, **only** when the token's `expires_at` has passed;
  - revoked, disabled and unknown tokens keep the generic answer;
  - `whoami` gains `credential: {kind, expires_at}`;
  - `api_minor` goes to **4**.
- **Ask first:** kastellan uses an API key, and API keys never expire.

### 2. **Telling kastellan** (carried, still yours)
- `?headers=list` (gate on `api_minor >= 1`).
- Attachment index addressing and paging (`>= 2`). **Never** compute the
  next offset from `text.length`.
- `fields` and `snippet_chars` (`>= 3`).

### 3. **Candidate issue (still not filed): `snippet_source` mislabel**
- A whole-message lexical win builds its snippet from `body_text` but
  reports `snippet_source="header"`. Not on the wire.

### 4. **Open issues from recent review rounds**
- **Most recent:** #398 (explicit `ocrmac` engine fails on language codes),
  #394 (`fields` unbounded; a wire-contract change) and #395 (a rule module
  forbidding new `{@html}` sinks).
- **Earlier:** #386, #389, #380, #383, #384, #377, #378, #369, #373, #372,
  #370, #371, #368, #365.

### 5. **Carried**
- **#375:** Mac build-hash probe timeout cached. It did not recur on the
  current deployment.
- **#374 + #305:** the `cli.py` refactor (2177 lines).
- **Others:** #358/#357, #330/#327/#328, #319/#318/#320 (the next migration
  slot is `0037_*.sql`), #340, #363, #285 (ruff in CI).
- **Admin GUI phase 5:** the Users panel.
- **Robustness:** #218, #226, #225/#227, #200/#211/#208, #206, #204, #25.

## Rulings made this session

1. **The Dependabot backlog came before the carried plan.** pypdf was eight
   HIGH advisories on attacker-controlled input, and the plan's next item
   (slice C) waits on an answer from kastellan anyway. *Cost if wrong:* none;
   nothing else was blocked.
2. **docling was pinned at the first patched release, not the newest.** The
   newest changes the layout models. *Cost if wrong:* the major upgrade is
   deferred, not avoided.
3. **Hygiene floors are declared rather than left lock-only**: the
   `transformers` and anyio precedent. *Cost if wrong:* four more lines to
   maintain in `pyproject.toml`.
4. **oauthlib 4.0 was accepted on probe evidence alone.** The consent URL and
   the token request are byte-identical, and token *refresh* (the daemon's
   path) goes through google-auth, not oauthlib. *Cost if wrong:* a broken
   `oauth-login` or admin Connect, i.e. an interactive re-auth that fails
   loudly. The daemon is not on that path.
5. **The torch.compile warnings were accepted as noise.** docling exposes no
   localmail-facing switch for the layout model's compile, and the output is
   identical. *Cost if wrong:* ~35 log lines per daemon start.
6. **Code and handoff are in one PR**, per the convention.
7. **The two new caps are ceilings, not floors, and are meant to be lifted.**
   Lift `rapidocr<3.10` with a docling bump that tolerates its new layout, and
   `opencv-python<5` when docling-ibm-models does. *Cost if wrong:* a stale
   cap holds back a fix; the comments beside them say when to remove them.
8. **`FULL_PAGE` rather than leaving docling's new default.** It restores the
   pre-2.118 output and matches the input docling gets. *Cost if wrong:* a PDF
   with both a text layer pypdf missed and images is OCRed whole. That is
   slower, but it was the behaviour until this PR.

## Open decisions & risks

1. **#397 is open and yours to merge.** After it, Dependabot is **1**.
2. **The Mac tree is on the PR branch and the venv is synced to it.** A
   restart before the merge runs the branch: the new dependencies plus the
   one `FULL_PAGE` change in `extractor.py`. If #397 is *rejected*, run
   `git checkout main && uv sync --frozen --extra mcp --extra extraction`
   before the next restart.
3. **oauthlib 4.0 is a major release.** If `oauth-login` or the admin Connect
   flow misbehaves after deploy, suspect it first.
4. **docling-parse 7 is a major move the probe covers thinly.** The second
   probe's native-table PDF is the same on both sides. If native-PDF fallback
   extraction looks different after deploy, look there.
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
git status && git branch --show-current
git log --oneline -1 origin/main         # THE authority

# Is #397 merged?
gh pr view 397 --json state,mergeCommit --jq '"\(.state) \(.mergeCommit.oid // "-")"'
gh pr checks 397
git log --oneline main..origin/chore/security-floors-2026-10   # non-empty after merge = stranded commits
gh issue list --limit 100 --json number --jq length      # 40
gh api --paginate repos/hherb/localmail/dependabot/alerts \
  --jq '.[] | select(.state=="open") | "\(.number)\t\(.dependency.package.name)"'   # only 71 after #397

# Deployment health
launchctl list | grep localmail
curl -sk https://127.0.0.1:8443/v1/version
ssh 10.0.0.3 'curl -sk https://10.0.0.3:8443/v1/version'

# Deploy after merge — Mac:
#   git checkout main && git pull
#   unset VIRTUAL_ENV && uv sync --frozen --extra mcp --extra extraction
#   unset VIRTUAL_ENV && uv run localmail init-db      # expect "schema already up to date"
#   launchctl kickstart -k gui/$(id -u)/com.localmail.daemon
#   launchctl kickstart -k gui/$(id -u)/com.localmail.serve
# DGX:
#   ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && ~/.local/bin/uv sync --frozen --extra extraction --extra mcp'
#   ssh 10.0.0.3 'systemctl --user restart localmail-daemon localmail-serve'
#   ssh 10.0.0.3 'cd ~/src/localmail && ~/.local/bin/uv pip show pypdf | head -2'   # 6.20.1

# Load before trusting any timing.
uptime; ps -A -o pid,pcpu,etime,comm -r | head -6

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4038 passed, 0 skipped (macOS)
unset VIRTUAL_ENV && uv run mypy src/localmail     # Success, 160 files
cd gui && npm test -- --run && npm run check && npm audit   # 512 / 0 errors / 0 vulns
```

`main` was **`4912799`** at session start and is unchanged. This session left
**one PR**: **#397** on `chore/security-floors-2026-10`, commits
`c956678..77c75d8` plus the review-round commit, and this handoff.

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`. **Open issues: 40. Dependabot: 33 open → 1 after #397.**
