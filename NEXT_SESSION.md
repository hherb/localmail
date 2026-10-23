# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-10-23 (session 57).** `main` was **`9023642`** at
> session start (#404 merged, closing #400), and is unchanged.
>
> This session:
> - **deployed #404 on both hosts.** `/v1/version` → `build_hash: 9023642`
>   on the Mac and the DGX; DGX units `active`, `NRestarts=0`. The local
>   `fix/400-docling-converter-fallback` branch was deleted after checking
>   that its tree equals `main` (squash merge).
> - **fixed #401: PR on `fix/401-opaque-bearer-resource-check`.**
>
> **Open issues: 41, 40 once the PR merges** (`Closes #401`).
>
> **Things to know before you touch anything.**
>
> **(1) The Mac deployment tree (`~/src/localmail`) is on the PR branch,
> not `main`.** The branch touches only `mcp/`. `serve` runs with
> `[mcp]` enabled, so the code that runs depends on whether `serve` has
> been restarted, and it has not been. `git checkout main` after the merge
> is step 0 either way.
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

### PR: `fix/401-opaque-bearer-resource-check` (opened on `main`)

| SHA | What |
|---|---|
| `954b856` | #401: the opaque-bearer `/mcp` verifier enforces the RFC 8707 binding through `access.load_access`; tests, CLAUDE.md, README, `docs/mcp-usage.md` |
| handoff | this file + `docs/handoffs/2026-10-23T0425Z-opaque-bearer-resource-check.md` |

- **Reachability, measured by reading the code (the last handoff asked for
  it first).** The opaque path mints no bound tokens. But turning
  `authorization_server_enabled` off only swaps the verifier: the
  `api_tokens` rows the AS minted with `oauth_resource` set stay valid for
  their TTL (`oauth_access_token_ttl_s`, default 1 h, no upper bound). `LocalmailTokenVerifier` called
  `verify_token` alone, so those tokens were accepted with their audience
  unchecked. The flaw is narrow but real.
- **The fix.**
  - `LocalmailTokenVerifier(pool, *, accepted_resources)` now verifies
    through `access.load_access`, the AS path's own check, so both
    branches use one predicate.
  - `accepted_resources` is keyword-only with no default, because `None`
    means "skip the check" (#234's shape).
  - The accepted set comes from the new
    `discovery.accepted_resources_for(config)`. `LocalmailASProvider.__init__`
    now uses that helper instead of its own inline copy.
  - The `CLIENT_ID` constant in `mcp/auth.py` is gone. `load_access`
    returns the token's real `oauth_client_id`, or the `"localmail"`
    sentinel when there is none. On the opaque path no SDK route reads
    `client_id`, since revocation is mounted only with the AS.
  - **Side effect:** an all-malformed `resource_indicators` now also fails
    the opaque build. `create_app` already closes the pool on that raise.
- **Tests (`tests/test_mcp_auth.py`, +5):**
  - a token bound to another resource is refused;
  - a token bound to an accepted resource is honoured;
  - an unbound login token stays unrestricted (positive control);
  - omitting `accepted_resources` raises `TypeError`;
  - end to end through `build_mcp_server`, the opaque server's verifier
    refuses a token bound elsewhere and accepts one bound to its derived
    audience.
- **Mutations, each caught by the intended test:** the verifier passing
  `None`, the server passing `[]`, and the server hard-coding a wrong
  audience.
- **Review round (`/review-pr`, then fixed on the branch).**
  - Those mutations pinned the *derived* audience only. No test configured
    `resource_indicators`, so three bypasses of `accepted_resources_for`
    (discovery, server, provider) survived. Added: configured indicators
    through the opaque server and the AS provider, and the all-malformed
    `ValueError` on the opaque build. Each bypass now fails its own test.
  - `load_access` failed open when its binding read found no row (deleted
    after `verify_token`). It returns `None` now, pinned by a test that
    deletes the row in between.
  - `load_access`'s `accepted_resources` has no default now; the bridge
    tests write `accepted_resources=None`.
  - Also added: an unbound OAuth-minted token stays unrestricted.
  - Wording: `user_id_from_access_token`'s docstring and error, the
    `create_app` comment, the TTL claim, and a "corrected by #401" note in
    the 2026-07-22 resource-indicator spec.

## Verification (Mac, `~/src/localmail` venv, all extras, review-round commit)

| gate | result |
|---|---|
| pytest (after the review round) | **4073 passed, 0 failed**, 6 warnings, 336 s |
| mypy | Success, **160** files |
| focused (`test_mcp_auth`, `test_api_key_verify`, `test_oauth_*`, `test_mcp_server_build`, `test_mcp_discovery`, `test_mcp_integration`) | 123 passed |
| CI | see `gh pr checks`; I stopped at the push |

- **Load** was 8–14 during the run, so no timing is comparable.
- **GUI untouched**, so cargo and vitest were not re-run.

## What's next

### 0. **Merge the PR, return the Mac to `main`** *(acceptance below)*
- **You merge**, once both CI legs are green.
- **Mac:**
  1. `git checkout main && git pull`;
  2. `launchctl kickstart -k gui/$(id -u)/com.localmail.serve`. This
     changes only `mcp/`; restarting the daemon too is harmless;
  3. delete the local branch. After a squash merge, `git branch -d` refuses,
     so first check `git diff --stat main fix/401-opaque-bearer-resource-check`
     is empty, then use `-D`.
- **DGX:** `git pull --ff-only`, then restart both units.
- **Acceptance:**
  - `/v1/version` reports the merge commit on both hosts;
  - `gh issue view 401` is closed.

### 1. **Pillow 13, once fastembed lifts its cap** (carried)
- qdrant/fastembed#830 was still **open** on 2026-10-23.
- **When it merges:**
  1. Move `[tool.uv.sources] fastembed`'s `rev` to a commit that has it.
  2. Raise the dev group's `Pillow>=10.3` to `>=13.0.0` and re-lock.
  3. Run the full suite and `probe_docling_ocr.py` on **both** hosts
     (`--engine auto --engine rapidocr` on the Mac).
- **Acceptance:** the lock holds Pillow 13.x, both probes exit 0, and the
  Pillow alerts close (28 open today: 21 `Pillow` + 7 `pillow`).

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
- **Next small slice:** #405. `_try_import_docling` reads a *broken*
  docling import as "not installed", so a partial `uv sync` writes scanned
  PDFs off as `lightweight-empty`, a one-way door.
  - **Acceptance:** an `ImportError` from a *present* docling is held as an
    `ExtractorConfigurationError` on the #153 transient budget, and no
    `attachment_text` row is written for it.
  - A genuinely absent docling still takes the lightweight path.
  - Distinguish the two with `importlib.util.find_spec("docling")`, not by
    reading the message.
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

1. **The opaque path reuses `access.load_access` rather than getting its own
   check.** Two verifiers with one predicate is the
   `credential_valid_sql` / `login_eligible_sql` call. *Cost if wrong:* one
   extra primary-key SELECT per opaque `/mcp` request, which the AS path
   already pays.
2. **Deploying #404 went ahead without a fresh confirmation**, following
   the handoff's step 0 and the precedent of sessions 54–56.
3. **Code and handoff are in one PR**, per the convention.

## Open decisions & risks

1. **28 Pillow alerts stay open**, some critical, until fastembed#830 ships.
   The claim that mail cannot reach them rests on one measurement of
   docling 2.141's default pipeline.
2. **The Mac's extraction path is still unexercised by real mail** under
   docling 2.141.
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
git status && git branch --show-current  # fix/401-opaque-bearer-resource-check until step 0
git log --oneline -1 origin/main         # THE authority
git worktree list

# Is the PR merged?
gh pr list --state all --head fix/401-opaque-bearer-resource-check --json number,state,mergeCommit
gh issue list --limit 200 --json number --jq length       # 40 after the merge
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
#   launchctl kickstart -k gui/$(id -u)/com.localmail.serve
#   git diff --stat main fix/401-opaque-bearer-resource-check   # empty ⇒ safe
#   git branch -D fix/401-opaque-bearer-resource-check
# DGX:
#   ssh 10.0.0.3 'cd ~/src/localmail && git pull --ff-only && systemctl --user restart localmail-daemon localmail-serve'

# Load before trusting any timing.
uptime; ps -A -o pid,pcpu,etime,comm -r | head -6

# Suites.
unset VIRTUAL_ENV && uv run pytest -q              # 4073 passed (macOS, all extras)
uvx mypy --python-executable .venv/bin/python src/localmail   # Success, 160 files
cd gui && npm test -- --run && npm run check && npm audit
cd gui/src-tauri && cargo test --locked && cargo clippy --locked --all-targets -- -D warnings

# This change directly:
unset VIRTUAL_ENV && uv run pytest -q tests/test_mcp_auth.py tests/test_api_key_verify.py tests/test_oauth_provider.py tests/test_mcp_server_build.py
```

`main` was **`9023642`** at session start and is unchanged. This session left
**one PR** on `fix/401-opaque-bearer-resource-check`: commit `954b856` plus
this handoff.

The latest migration is still **`0036_api_keys.sql`**; the next free slot is
`0037_*.sql`. **Open issues: 41 (40 after the merge).**
