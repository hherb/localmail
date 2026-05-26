# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-26T0004 UTC (post-session).**
> **#97 shipped** as PR [#98](https://github.com/hherb/localmail/pull/98)
> (`issue-97-bump-ci-actions-node24`). One commit:
> - **`74a1999`** — `ci(gui): bump checkout + setup-node to v6 (node24) (#97)`.
>
> **All three CI jobs green and the Node 20 deprecation annotation is
> gone.** Before/after evidence collected via
> `gh api repos/.../check-runs/<id>/annotations`:
>
> | Run | ubuntu-latest | macos-latest | svelte-check |
> |---|---|---|---|
> | `cbe89f0` (main HEAD, pre-PR) | warning ×1 | warning ×1 | warning ×1 |
> | `74a1999` (PR #98) | 0 annotations | 0 annotations | 0 annotations |
>
> PR #98 is **non-draft, ready for review**. Awaiting maintainer
> merge to close #97. Previous session's PR #96 merged as `cbe89f0`,
> issue #24 closed 2026-05-25T12:07 UTC.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What we shipped this session

### Issue + PR

- **Issue [#97](https://github.com/hherb/localmail/issues/97)** —
  `chore(ci): bump GH Actions to Node 24 (silence Node 20 deprecation warning)`.
  Filed at the start of the session to follow project convention of
  PR ↔ issue pairing.
- **PR [#98](https://github.com/hherb/localmail/pull/98)** —
  `ci(gui): bump checkout + setup-node to v6 (silence Node 20 deprecation) (#97)`.

### Commit

- **`74a1999`** — `ci(gui): bump checkout + setup-node to v6 (node24) (#97)`
  - `.github/workflows/gui-ci.yml`: 3 line bumps total
    - `actions/checkout@v4` → `@v6` (2 occurrences: frontend job + tauri-rust job)
    - `actions/setup-node@v4` → `@v6` (1 occurrence: frontend job)
  - **Verified before bumping**, via `curl raw.githubusercontent.com`
    of each action's `action.yml`:
    - `actions/checkout@v6` declares `runs.using: node24` ✓
    - `actions/setup-node@v6` declares `runs.using: 'node24'` ✓
    - `Swatinem/rust-cache@v2` already declares `runs.using: "node24"`
      (current v2.9.1) — no change required.
    - `dtolnay/rust-toolchain@stable` is `runs.using: composite` —
      no JS host, not affected.
  - **No input changes**: the workflow only uses inputs
    (`node-version`, `cache`, `cache-dependency-path`) that have been
    stable across v3/v4/v5/v6 of `setup-node`. `checkout` had no
    inputs configured.

### Verification

CI on PR #98 (run `26424775413`, commit `74a1999`):
```
cargo test + clippy (ubuntu-latest)   PASS   56s
cargo test + clippy (macos-latest)    PASS   39s
svelte-check + vitest                 PASS   31s
```

Annotation diff (the acceptance criterion):
```bash
# Pre-PR (cbe89f0 on main, run 26421322438):
$ gh api repos/hherb/localmail/check-runs/77776457314/annotations
[{"annotation_level":"warning","message":"Node.js 20 actions are
  deprecated. The following actions are running on Node.js 20…
  actions/checkout@v4, actions/setup-node@v4…"}]
# (same warning on the other two jobs)

# Post-PR (74a1999, run 26424775413):
$ gh api repos/hherb/localmail/check-runs/77786105735/annotations
[]
# (zero annotations on all three jobs)
```

No local test run needed — the change is a workflow-only edit and
the CI itself is the test surface.

### Docs review

- **README.md** — CI infra is internal, not end-user concern. **Not updated.**
- **ROADMAP.md** — does not exist in this repo. **Not created.**
- **CLAUDE.md** — no new load-bearing invariant. Action versions are
  meta-facts about the build matrix, not code-architecture invariants;
  no permanent reference is warranted. **Not updated.**

## What's next

### 1. **Maintainer: merge PR #98** *(blocks closing #97)*

PR is ready for review (not draft). All three CI jobs green, zero
annotations, three-line workflow-only diff.

**Acceptance**: PR #98 merged to `main`, issue #97 auto-closes via
the `Closes #97` syntax in the commit body.

### 2. **#28 visual smoke** *(carried over; optional, ~5 min Tauri dev)*

Unchanged from prior handoffs — verify the charset toggle eyeballs
correctly against a real Latin-1 message in `npm run tauri dev`.

**Acceptance**:
- `npm run tauri dev` launches, no panic.
- Open a UTF-8 message → text renders correctly with
  `(detected: utf-8)` (or no hint if no charset header).
- Open a Latin-1 message → AUTO + declared charset decodes
  correctly. Without declared charset → default UTF-8 mojibakes the
  bytes (U+FFFD). Flip dropdown to `Latin-1` → clean text, no
  refetch. Flip back to UTF-8 → mojibake returns.

### 3. **#38 — `/v1/changes` semantics decision** *(needs user input)*

Unchanged. Initial-backfill window semantics — needs a conversation
about what the wire contract should be before code.

### 4. **#87 — CI-gated at-scale folder-filter regression coverage**

Unchanged. Wire a CI job that seeds a 200k-row synthetic archive and
runs `tests/acceptance/run_browse_explain.py` so the planner doesn't
silently regress on the folder-filter path. Multi-step (image / build
matrix / synthetic-data fixture).

### 5. **Add a Node `engines` field to `gui/package.json`** *(filed if needed)*

Carried-forward from PR #96. Contributors on Node 20 still hit the
windows-1252 test failure (now with a clean codepoint diagnostic).
Adding `"engines": {"node": ">=22"}` to `gui/package.json` would
have npm warn / fail on the wrong version. Not yet warranted, but
file a small chore if it becomes a recurring footgun.

### Blocked / deferred (unchanged)

- **#90** glib Cargo alert — upstream-blocked.
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#47** Third-party transient classes — needs ops data.
- **#5** Search batch INSERT — deferred until measured.
- **#2** Migration 0006 GIN CONCURRENT — deferred until live-upgrade
  scenario hits.

**Open issue count: 8** (no change to the deferred set; #97 will
close on PR #98 merge).

## Open decisions & risks

1. **Action major-version pin policy.** The bumps go straight to
   `@v6` rather than to a pinned commit SHA. Public-action pinning
   to a commit SHA is the more paranoid (supply-chain-safe) option
   used by some security-sensitive projects. This repo has so far
   used floating major tags (`@v2`, `@v4`, `@v6`) for all
   GitHub-owned + `Swatinem/rust-cache` actions — that convention
   is preserved here. If you decide to harden against
   compromised-tag risk, the natural target is all of
   `.github/workflows/gui-ci.yml` at once, not just the lines
   touched by this PR.

2. **`dtolnay/rust-toolchain@stable` is a rolling tag.** It still
   resolves on every CI run to whatever commit `stable` points at
   today. The action is composite (no Node host), so it does not
   participate in the Node 20 deprecation. But the same pinning
   conversation applies if you ever decide to tighten supply-chain
   posture across the workflow.

3. **`Swatinem/rust-cache@v2` was already on node24 (v2.9.1).** No
   change was required for that line; if you ever see a future Node
   deprecation cycle, that's the one place that *won't* need a v3 —
   the maintainer has been keeping v2 floating to the latest LTS.

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
git log --oneline -5                 # current branch: issue-97-bump-ci-actions-node24
                                     #   tip: 74a1999
gh pr view 98                        # status: OPEN (not draft), 3/3 checks green

# If picking option 1 (merge PR #98):
gh pr merge 98 --squash              # squash-merge (matches recent style)
git checkout main && git pull        # sync local
gh issue close 97                    # auto-closes if `Closes #97` syntax honoured
                                     #   (it was)

# If picking option 2 (#28 visual smoke):
unset VIRTUAL_ENV && uv run localmail serve \
  --bind 127.0.0.1 --port 8443 \
  --tls-cert ~/.config/localmail/tls/cert.pem \
  --tls-key ~/.config/localmail/tls/key.pem
cd gui && npm run tauri dev
# Walk the acceptance checklist in "What's next §2".

# If picking option 3 (#38 semantics decision):
gh issue view 38                     # read the design context
# Conversation-first; no code yet.

# If picking option 4 (#87 at-scale CI):
gh issue view 87                     # full scope; multi-step
# Likely needs a separate workflow file with a docker postgres,
# checked-in seed script, and a runtime-bounded CI job (it's
# substantial — see issue body).

gh issue list --state open --limit 40
```

## File map (post-session)

```
NEXT_SESSION.md                                                 # MODIFIED this session
docs/handoffs/
  2026-05-26T0004-utc-node24-action-bumps-pr-98.md              # NEW (this session's snapshot)
  2026-05-25T1205-utc-gui-ci-macos-matrix-pr-96.md              # prior session
  2026-05-25T1013-utc-housekeeping.md
  2026-05-25T0705-utc-charset-toggle-issue-28.md
  …                                                             # earlier (already committed)

.github/workflows/gui-ci.yml                                    # MODIFIED — 3-line action bumps

src/localmail/                                                  # unchanged this session
gui/                                                            # unchanged
migrations/                                                     # unchanged
tests/                                                          # unchanged
```

Branch `issue-97-bump-ci-actions-node24` is up-to-date with origin.
PR #98 is OPEN (not draft), 3/3 checks green, zero annotations.
Working tree on the branch is clean (only `.claude/settings.local.json`
untracked).
