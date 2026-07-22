# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-07-22.** This session shipped **RFC 8707 resource
> indicators** for the MCP OAuth AS (validate + bind + `/mcp`-only enforcement)
> as **PR #195** (open, branch `feat/oauth-resource-indicators`). Full Python
> suite **1720 passed** (14 deselected), `mypy` clean (122 files). Built via the
> full brainstorm → spec → plan → subagent-driven-development pipeline: 11 TDD
> tasks, each two-stage (spec + code-quality) reviewed, plus a final
> whole-branch review (opus) — **Ready to merge: Yes**, no Critical/Important.
>
> `origin/main` HEAD is `e6e3676` (PR #193 access-token family containment **and**
> PR #194 AGPL-3.0 license both merged since the last handoff). PR #195 is the
> only open PR.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The database is canonical for accounts.
Daemon: hot-reload account set, heartbeats, DB command queue, two-plane
supervision, non-blocking lifecycle + admin panel. Admin UI: account CRUD, user
management, archive imports, daemon control. Hybrid search (Phases 1+2) + an
HTTPS GUI server + a remote MCP server + the opt-in `--smart` LLM query rewriter
are all shipped. The MCP server can act as an **OAuth 2.1 authorization server**
(opt-in) with sliding refresh-token rotation, family revocation on reuse, access-
token family containment, **and (this session) RFC 8707 resource-indicator
validation + audience binding + `/mcp` enforcement**. A Tauri 2 + Svelte 5 GUI
lives under `gui/`. Licensed AGPL-3.0-or-later (per-file SPDX headers). See
[CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we did this session

### RFC 8707 resource indicators (PR #195)

Validate the client's OAuth `resource` at `/authorize` against a configurable
accepted set, carry the bound audience through consent → authorization code →
minted access + refresh tokens, and enforce audience membership at the `/mcp`
resource server. Opaque tokens (`api_tokens`); single RS today; **NULL bound
resource = unrestricted** (so `/v1/auth/login` + legacy tokens are immune).

- **Migration `0031_oauth_resource_indicator.sql`** — three nullable `TEXT`
  columns: `oauth_authorization_codes.resource`, `oauth_refresh_tokens.resource`,
  `api_tokens.oauth_resource`. No backfill, no index.
- **New pure module** `src/localmail/mcp/oauth/resource_indicator.py` —
  `canonicalize_resource` (RFC 8707 §2), `resolve_accepted_resources`
  (config-or-derived; all-malformed config → `ValueError` at construction),
  `decide_resource` (accept/bind/reject table).
- **Config** (`McpConfig`): `resource_indicators: list[AnyHttpUrl] | None = None`
  (default `[mcp_resource_url(resource_server_url)]`) +
  `oauth_require_resource_indicator: bool = False`.
- **Flow**: `authorize` validates+binds → signed consent blob
  (`ConsentPayload.resource`) → `codes.mint_code(resource=…)` →
  `AuthorizationCode.resource` → `mint_access`/`mint_refresh(resource=…)` (both
  the code-exchange and rotation paths) → enforced at `/mcp` in
  `access.load_access(accepted_resources=self._accepted)`.
- **Accepted SDK limits (by design):** the SDK swallows the token-endpoint
  `resource` (validated at authorize time only); no `invalid_target` code (bad
  resource → `invalid_request`). Enforcement is `/mcp`-only; `/v1` REST unchanged.

Design: [docs/superpowers/specs/2026-07-22-oauth-resource-indicators-design.md](docs/superpowers/specs/2026-07-22-oauth-resource-indicators-design.md)
· Plan: [docs/superpowers/plans/2026-07-22-oauth-resource-indicators.md](docs/superpowers/plans/2026-07-22-oauth-resource-indicators.md)

Commits on `feat/oauth-resource-indicators` (pushed; PR #195), base `e6e3676`:

| SHA | what |
|---|---|
| `11240cf` | docs(mcp): design — RFC 8707 resource indicators (validate + bind) |
| `f716b97` | docs(mcp): implementation plan |
| `34e5a86` | feat(mcp): pure RFC 8707 resource-indicator canonicalize + decide |
| `471c366` | fix(mcp): canonicalize_resource returns None on malformed port; harden derived invariant |
| `58660e2` | feat(mcp): migration 0031 — resource columns on codes/refresh/api_tokens |
| `919a681` | feat(mcp): McpConfig resource_indicators + oauth_require_resource_indicator |
| `d0365d5` | feat(mcp): carry resource on the authorization-code store |
| `6f4050a` | feat(mcp): bind resource on access mint + enforce audience on load |
| `6805b6c` | test(mcp): make canonicalization load-bearing + pin fail-closed on corrupt bound resource |
| `914bc46` | feat(mcp): carry resource through refresh mint/load/rotate |
| `8a761dc` | feat(mcp): carry resource on the consent-state blob |
| `b15100e` | feat(mcp): validate + bind resource at /authorize |
| `95e8f26` | feat(mcp): bind resource onto exchanged tokens; enforce at /mcp load |
| `bb87c06` | feat(mcp): forward resource from consent to the authorization code |
| `02cd8d4` | docs: RFC 8707 resource indicators shipped (CLAUDE.md + README) |
| `b2e7b1a` | docs(mcp): mcp-usage.md — RFC 8707 resource indicators now shipped |
| `49f7ddb` | docs(spec): note AS-mode-toggle audience-enforcement edge case (final review) |

**Verification:** `unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/
--deselect tests/test_daemon_control_socket.py` → **1720 passed, 14 deselected**
(+50 over the 1670 baseline). `uv run mypy src/localmail` → clean, 122 files.

## What's next

### 0. **Merge PR #195** *(immediate — once CI green)*
   Check CI, squash-merge, advance `origin/main`; migration `0031` then goes live
   (operators apply it with `localmail init-db`). **Acceptance:** PR #195
   squash-merged; `origin/main` past `e6e3676`; `localmail init-db` applies `0031`.
```bash
gh pr checks 195
gh pr merge 195 --squash --delete-branch
git checkout main && git fetch --prune origin && git pull
unset VIRTUAL_ENV && uv run localmail init-db     # applies migration 0031
```

### 1. **Dependabot alerts (new, unblocking but worth a look)**
   The push to #195 reported **20 vulnerabilities on the default branch (13 high,
   6 moderate, 1 low)** — these accrued since the last handoff (which reported all
   alerts fixed/dismissed). Not related to #195. **Acceptance:** triage
   `gh api repos/hherb/localmail/dependabot/alerts --jq '.[] | select(.state=="open")'`,
   bump/patch or dismiss each with a rationale, as in prior dep-bump PRs (#188–#192).

### 2. **(Deferred / not filed)** candidate future work, none blocking:
   - **RFC 8707 minor polish (recorded, not fixed):** `canonicalize_resource`
     drops IPv6-host brackets and doesn't reject a trailing bare `#` (empty
     fragment). Both are cosmetic and *self-consistent for matching* (request and
     accepted-set entries canonicalize identically), so they don't affect access
     decisions. Resources are DNS origins in practice. Only worth touching for
     strict RFC 8707 §2 literalism.
   - **Second resource server / `invalid_target` / token-endpoint resource:** the
     `resource_indicators` list is the forward seam for a 2nd RS; the two SDK
     limitations (swallowed token-endpoint resource, no `invalid_target`) only
     matter with a 2nd RS or a spec-strict client that inspects the error code.
   - **httpx2 test migration** — Starlette TestClient `httpx`-deprecation warning;
     `#192` added `httpx2` to the dev group but tests still use `httpx`. Cosmetic.

## Open decisions & risks
1. **PR #195 is open, not merged.** First action next session is §0.
2. **Enforcement scope (by design):** audience is enforced only on the AS
   provider's `/mcp` path (`load_access`). NULL-resource tokens are unrestricted.
   `/v1` REST is out of RFC 8707 scope. If an operator runs the AS (minting
   `oauth_resource`-bound tokens) then switches to opaque-bearer mode
   (`authorization_server_enabled = false`), those tokens revert to unrestricted
   (verified by `LocalmailTokenVerifier`, which does no resource check) — a
   privileged-admin edge, outside the threat model; documented in the spec's
   "Accepted consequences".
3. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails locally
   on macOS (`AF_UNIX path too long`); deselect locally, Linux CI is the real
   signal. Also carried: psycopg_pool teardown `ResourceWarning`s, the websockets
   `DeprecationWarning` (issue #25), the Starlette TestClient `httpx`
   `DeprecationWarning`.
4. **No ROADMAP.md** *(carried)* — the `/nextsession` ROADMAP step is a no-op;
   slice status lives in NEXT_SESSION/handoffs + the specs.
5. **`.claude/` local files** stay untracked (incl. `.claude/scheduled_tasks.lock`).
   The SDD progress ledger lives at `.superpowers/sdd/progress.md` (git-ignored).

## Exact commands to resume
```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # expect clean (only untracked .claude lock + .superpowers)
git --no-pager log --oneline -5
gh pr list --state open                  # expect PR #195 (merge it — §0)
gh pr view 195
gh pr checks 195

# §0 — merge the feature PR (once CI green):
gh pr merge 195 --squash --delete-branch
git checkout main && git fetch --prune origin && git pull
unset VIRTUAL_ENV && uv run localmail init-db    # applies migration 0031

# Python suite + types (use --extra mcp so the MCP/OAuth tests actually run):
unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/ \
  --deselect tests/test_daemon_control_socket.py            # expect 1720 passed
unset VIRTUAL_ENV && uv run mypy src/localmail              # expect clean, 122 files

# gui frontend checks (run inside gui/, unchanged this session):
cd gui && npm ci && npm run check && npm test && npm run build && cd ..
```

`origin/main` at `e6e3676`; feature branch `feat/oauth-resource-indicators` is
PR #195. Latest migration `0031_oauth_resource_indicator.sql`; next free slot
`0032_*.sql`.
