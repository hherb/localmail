# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-25T1205 UTC (post-session).**
> **#24 shipped** as DRAFT PR [#96](https://github.com/hherb/localmail/pull/96)
> (`issue-24-gui-ci-macos-matrix`). Two bundled commits:
> - **`e1b3ab3`** — ci(gui): add macos-latest to gui-ci.yml tauri-rust
>   matrix (#24).
> - **`5a0b87e`** — ci(gui): bump Node 20 → 22 + add codepoint-annotated
>   decode diagnostic.
>
> **All three CI jobs green on the second run** (Node 22 + cache hit):
> `tauri-rust (ubuntu-latest)` 1m4s, `tauri-rust (macos-latest)` 50s,
> `svelte-check + vitest` 29s. **PR is still DRAFT** pending the
> maintainer's audit of macOS-minute consumption in the Actions billing
> summary before promoting to ready-for-review.
>
> Side effect of the Node bump: it incidentally repairs a pre-existing
> red on `main` since `37cc506` (PR #95) — the windows-1252 unit test
> was failing on CI's Node 20 (returned `U+0080` for byte `0x80`
> instead of `U+20AC €`). PR #95's own pre-merge check on this job was
> already red at merge time; the prior NEXT_SESSION's "both jobs
> SUCCESS" claim was wrong. Fix surfaced while debugging #24's CI.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What we shipped this session

### Commits

- **`e1b3ab3`** — `ci(gui): add macos-latest to gui-ci.yml tauri-rust matrix (#24)`
  - `strategy.fail-fast: false` + `matrix.os: [ubuntu-latest, macos-latest]`
  - `runs-on: ${{ matrix.os }}`; job name embeds OS for the GH UI
  - Linux apt-get install step gated `if: matrix.os == 'ubuntu-latest'`
    (macOS Tauri toolchain needs no system deps)
  - `Swatinem/rust-cache` `key: ${{ matrix.os }}` so Linux/macOS
    caches don't collide on different target triples + system libs
  - Frontend job stays on `ubuntu-latest` (issue #24 marks frontend
    matrix as optional; pure JS, no platform divergence)

- **`5a0b87e`** — `ci(gui): bump Node 20 -> 22 + add codepoint-annotated decode diagnostic`
  - `actions/setup-node@v4 with: node-version: '22'` (was `'20'`)
  - `gui/src/lib/charset_helpers.test.ts`: new `expectDecodedEquals`
    + `annotateCodepoints` helpers render every char as
    `"<glyph>" (U+XXXX)` so failure diagnostics can't be hidden by
    visually-collapsing glyphs (e.g. `"€"` vs the U+0080 control-glyph
    that renders as `"^@"` or empty depending on terminal). Applied
    to the three `decodeWithLabel` assertions; other tests unchanged
    (their strings are not glyph-ambiguous).

### Verification

Local (Node 26.0.0):
```bash
cd gui && npm test            # 316 passed / 36 files
cd gui && npm run check       # 0 errors / 0 warnings / 335 files
```

CI on PR #96 (post-Node-22 bump, second run, with rust-cache hit):
```
cargo test + clippy (ubuntu-latest)   PASS   1m4s
cargo test + clippy (macos-latest)    PASS    50s
svelte-check + vitest                 PASS    29s
```

Backend (untouched this session — no need to re-run):
```bash
# Last known-green:
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q            # expect 805 passed
unset VIRTUAL_ENV && uv run mypy src/localmail
                              # expect 4 pre-existing parser.py errors
```

### Docs review

- **README.md** — CI infra is internal, not end-user concern. **Not updated.**
- **ROADMAP.md** — does not exist in this repo. **Not created.**
- **CLAUDE.md** — no new load-bearing invariant. The macOS-leg in CI
  is a meta-fact (build matrix), not a code architecture invariant.
  The `expectDecodedEquals` helper is localised to one test file;
  no broader pattern to register yet. **Not updated.**

## What's next

### 1. **Maintainer: audit + merge PR #96** *(blocks closing #24)*

PR is DRAFT. Outstanding checklist item from the PR body:
- Inspect Actions runner billing summary on the PR; confirm macOS-minute
  consumption matches expectations before marking non-draft.
- Public repos on the free tier: no minute charge. Private repos: 10×
  multiplier. This repo is currently public, so the cost on PR pushes
  should be a single full macOS run per push (~50s warm, ~3:55 cold).

**Acceptance**: PR #96 promoted to ready-for-review, merged to `main`,
issue #24 closed.

### 2. **Optional: bump action versions to opt out of Node 20 deprecation**

CI still emits the warning *"Node.js 20 actions are deprecated …
removal Sept 16, 2026"* because `actions/checkout@v4` and
`actions/setup-node@v4` internally still run on Node 20. The bump
to Node 22 we did is for the **npm runtime**; the actions themselves
need either a newer action version (when published) or
`FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true` set in the workflow env.

**Acceptance**: no Node 20 deprecation warning in CI annotations.
Likely just a one-line workflow env addition. File as a small issue
if you want it tracked separately.

### 3. **Visual smoke for #28** *(carried over; optional, ~5 min Tauri dev)*

Unchanged from prior handoff — verify the charset toggle eyeballs
correctly against a real Latin-1 message in `npm run tauri dev`.
Now that the test diagnostic actually shows codepoints when it fails,
local debugging during the smoke is easier too.

**Acceptance**:
- `npm run tauri dev` launches, no panic.
- Open a UTF-8 message → text renders correctly with
  `(detected: utf-8)` (or no hint if no charset header).
- Open a Latin-1 message → AUTO + declared charset decodes
  correctly. Without declared charset → default UTF-8 mojibakes the
  bytes (U+FFFD). Flip dropdown to `Latin-1` → clean text, no
  refetch. Flip back to UTF-8 → mojibake returns.

### 4. **#38 — `/v1/changes` semantics decision** *(needs user input)*

Unchanged.

### 5. **#87 — CI-gated at-scale folder-filter regression coverage**

Unchanged.

### Blocked / deferred (unchanged)

- **#90** glib Cargo alert — upstream-blocked.
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#47** Third-party transient classes — needs ops data.
- **#5** Search batch INSERT — deferred until measured.
- **#2** Migration 0006 GIN CONCURRENT — deferred until live-upgrade
  scenario hits.

**Open issue count: 8** (no change this session; #24 will close on
PR #96 merge).

## Open decisions & risks

1. **PR #96 scope expansion.** The matrix work was bundled with a
   Node version bump and a test diagnostic improvement, all under
   the "CI/test infra" umbrella. Keeping them separate would have
   left `main` red on `svelte-check + vitest` until a follow-up PR.
   The commit message of `5a0b87e` documents the bundling decision.
   If the reviewer prefers strict one-concern-per-PR, the Node bump
   + test diagnostic could be lifted into a separate PR — both
   changes are independent of the matrix expansion.

2. **Node 20 vs Node 22 in local dev.** Contributors running Node 20
   locally will still hit the `windows-1252` test failure (now with
   a clear codepoint diagnostic). The fix is to bump local Node to
   22+. No `engines` field in `gui/package.json` to enforce this;
   if it becomes a recurring footgun, add one. Not yet warranted.

3. **Pre-existing PR-time-CI vs push-time-CI drift.** PR #95's PR-time
   check on `svelte-check + vitest` was red yet the PR was merged
   anyway. That points to a process gap — either branch protection
   wasn't required for that check, or the maintainer overrode the
   red. Worth a moment of thought on whether to require all gui-ci
   jobs as branch protection on `main`. Out of scope for this PR.

4. **`.claude/settings.local.json` stays untracked.** Same as prior
   handoffs — by-convention local-only file (`*.local.json` suffix).
   Not in `.gitignore`; if a future contributor wonders, add an
   explicit ignore rule rather than committing.

5. **Carried-forward invariants** — same list as the prior handoffs;
   not duplicated. Full enumeration in earlier handoffs. Nothing
   changed this session in `src/localmail/` or `migrations/`.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail

# Verify state:
git status                           # expect: only .claude/settings.local.json
                                     #   untracked (by design)
git log --oneline -8                 # current branch: issue-24-gui-ci-macos-matrix
gh pr view 96                        # status: DRAFT, 3/3 checks green

# If picking option 1 (audit + merge):
#   1. Visit https://github.com/hherb/localmail/actions/usage (or the
#      org billing page) and confirm macOS-minute consumption.
#   2. gh pr ready 96                # promote to ready-for-review
#   3. gh pr merge 96 --squash       # squash-merge (matches recent style)
#   4. git checkout main && git pull # sync local
#   5. gh issue close 24             # closes when PR merges if `Closes #24`
                                     #   syntax was honoured (it was)

# If picking option 2 (deprecation warning followup):
git checkout main && git pull
git checkout -b fix-node20-action-deprecation-warning
# Edit .github/workflows/gui-ci.yml — add `env: FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true`
# at the workflow level, OR update actions/checkout and actions/setup-node to
# whatever version they publish that runs on Node 24 natively.

# If picking option 3 (#28 visual smoke):
unset VIRTUAL_ENV && uv run localmail serve \
  --bind 127.0.0.1 --port 8443 \
  --tls-cert ~/.config/localmail/tls/cert.pem \
  --tls-key ~/.config/localmail/tls/key.pem
cd gui && npm run tauri dev
# Walk the acceptance checklist in "What's next §3".

gh issue list --state open --limit 40
```

## File map (post-session)

```
NEXT_SESSION.md                                                 # MODIFIED this session
docs/handoffs/
  2026-05-25T1205-utc-gui-ci-macos-matrix-pr-96.md              # NEW (this session's snapshot)
  2026-05-25T1013-utc-housekeeping.md                           # prior session
  2026-05-25T0705-utc-charset-toggle-issue-28.md                # #28 / PR #95
  …                                                             # earlier (already committed)

.github/workflows/gui-ci.yml                                    # MODIFIED — matrix + Node 22
gui/src/lib/charset_helpers.test.ts                             # MODIFIED — codepoint diagnostic

src/localmail/                                                  # unchanged this session
migrations/                                                     # unchanged
tests/                                                          # unchanged
```

Branch `issue-24-gui-ci-macos-matrix` is up-to-date with origin.
PR #96 is DRAFT pending billing audit. Working tree on the branch
is clean (only `.claude/settings.local.json` untracked).
