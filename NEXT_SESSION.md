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
>   session**: **29 new alerts** (#81–#114), against the one the last handoff
>   expected. Fixed in **PR #397** (see below), which leaves only #71 open.
>
> **Open issues: 39** (37 + #394, #395 filed by the #393 review). **Dependabot:
> 30 open → 1 after #397 merges** (#71 `accelerate`, left open on purpose).
>
> **Three things to know before you touch anything.**
>
> **(1) The production venv already carries #397's dependencies.** This Mac's
> `.venv` *is* the deployment, and testing the bump meant syncing it. The
> running agents still hold the old modules in memory, so nothing has changed
> yet. But the tree is on `chore/security-floors-2026-10`, and **any restart
> picks up the new dependencies**. That is safe (the Python code is `main`'s,
> and the probe below proves the dependency behaviour), but it means "deploy"
> on the Mac is a restart once #397 merges, not a sync.
>
> **(2) pypdf is the one that matters.** Eight HIGH DoS advisories on the
> emailed-PDF extraction path. Everything else is hygiene: urllib3, oauthlib,
> PyJWT, docling and devalue are not on a mail-borne path.
>
> **(3) docling is pinned at 2.118.1 on purpose.** A bare `uv lock --upgrade`
> takes 2.138, which moves `docling-ibm-models` 3 → 4 (new layout models). Do
> that only as its own change, with its own OCR probe.

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

Plus this handoff, in the same PR.

| package | lock | alerts | reach |
|---|---|---|---|
| **pypdf** `>=6.19.0` | 6.16.2 → **6.20.1** | #105–#112, 8 HIGH | **attacker-controlled**: `_extract_pdf` over an emailed PDF |
| urllib3 `>=2.8.0` (new) | 2.7.0 → 2.8.0 | #94–#96, 2 HIGH | hygiene: talks to Google and the HF hub only |
| oauthlib `>=4.0.0` (new) | 3.3.1 → **4.0.0** (major) | #84, #85 | hygiene: both are server-endpoint advisories; we are the client |
| pyjwt `>=2.15.0` (new, `[mcp]`) | 2.13.0 → 2.15.1 | #81–#83, #86–#93, #97, #98 (one CRITICAL) | hygiene: only the MCP SDK's *client* credentials extension imports it |
| docling `>=2.118.1` | 2.97.0 → **2.118.1** | #113, #114 | hygiene: HTML browser mode; `DoclingExtractor` is PDF-only |
| devalue (npm, lock-only) | 5.8.1 → 5.9.4 | #99, #101, #103, #104 | hygiene: Svelte's *server* render; the GUI is a client-only SPA |

Notes on the table:
- **"(new)"** means newly *declared*. The package arrived transitively with
  no floor, which is the `transformers` lesson.
- **The docling lock moved more than docling**: docling-parse 6 → 7, an
  opencv → opencv-python-headless swap, rapidocr 3.8 → 3.10, and docling-core.
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
`"Invoice 4711 total 250 EUR"` on both sides.

**One behaviour change:** docling 2.118 `torch.compile`s its layout model by
default. The first extraction in a process now logs ~35 lines of
`torch._dynamo` graph-break warnings, once per process. Output is unchanged;
it is noise in `daemon.err.log`. Recorded in CLAUDE.md.

| gate | result |
|---|---|
| pytest | **4035 passed, 0 failed**, 5 warnings, 292 s |
| mypy | Success, **160** files |
| `gui/` vitest | **512 passed**, 49 files |
| `svelte-check` | 326 files, **0 errors, 0 warnings** |
| `npm audit` | **0 vulnerabilities** |
| `uv lock --check` | clean |
| CI | see `gh pr checks 397`; I stopped at the push |

- **The 4035 equals the last handoff's post-review count**, and that is the
  right comparison: this PR changes no test.
- **5 warnings, not 3.** The known three are the #25 websockets pair and
  anyio's `BlockingPortal`. The extra two are an `hf_xet` deprecation from
  `test_fastembed_backend_real_model_smoke`, which only shows when the model
  download goes to the network. huggingface_hub did not change in this bump.
- **Load:** average ~8–10 with an unrelated rustc build running. No timing
  here is comparable.

## What's next

### 0. **Merge #397, then deploy** *(acceptance: Dependabot 30 → 1)*
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
- **Most recent:** #394 (`fields` unbounded; a wire-contract change) and
  #395 (a rule module forbidding new `{@html}` sinks).
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

## Open decisions & risks

1. **#397 is open and yours to merge.** After it, Dependabot is **1**.
2. **The Mac tree is on the PR branch and the venv is synced to it.** A
   restart before the merge runs the branch, which is the same Python code
   with the new dependencies. If #397 is *rejected*, run
   `git checkout main && uv sync --frozen --extra mcp --extra extraction`
   before the next restart.
3. **oauthlib 4.0 is a major release.** If `oauth-login` or the admin Connect
   flow misbehaves after deploy, suspect it first.
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
git status && git branch --show-current
git log --oneline -1 origin/main         # THE authority

# Is #397 merged?
gh pr view 397 --json state,mergeCommit --jq '"\(.state) \(.mergeCommit.oid // "-")"'
gh pr checks 397
git log --oneline main..origin/chore/security-floors-2026-10   # non-empty after merge = stranded commits
gh issue list --limit 100 --json number --jq length      # 39
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
unset VIRTUAL_ENV && uv run pytest -q              # 4035 passed, 0 skipped (macOS)
unset VIRTUAL_ENV && uv run mypy src/localmail     # Success, 160 files
cd gui && npm test -- --run && npm run check && npm audit   # 512 / 0 errors / 0 vulns
```

`main` was **`4912799`** at session start and is unchanged. This session left
**one PR**: **#397** on `chore/security-floors-2026-10`, commits
`c956678..77c75d8`, plus this handoff.

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`. **Open issues: 39. Dependabot: 30 open → 1 after #397.**
