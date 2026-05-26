# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-26T0827 UTC (post-session).**
> **#87 shipped** as PR [#99](https://github.com/hherb/localmail/pull/99)
> (`issue-87-at-scale-folder-filter-regression-coverage`). 15 commits.
>
> **`python-ci` workflow green** on the at-scale-test-bearing commit
> `5efba1f` (run `26440823381`, 1m26s). The branch tip is
> `6bb97d8` (README + workflow-comment polish only). Final code review:
> **APPROVED for merge** with two minor nits addressed (`# Tracked by
> #100` ref + README test-count bump). PR #99 is **ready-for-review**.
>
> **First Python CI in this repo.** Up to now only the Tauri/Svelte GUI
> had CI. The new workflow runs the full pytest suite (now 809) against
> a `pgvector/pgvector:pg18` service container on every push to `main`
> and every PR touching `src/`, `tests/`, `migrations/`,
> `pyproject.toml`, `uv.lock`, or the workflow itself.
>
> **Issue [#100](https://github.com/hherb/localmail/issues/100) filed**
> for a pre-existing latent bug surfaced by the new CI — eight CLI
> tests rely on `~/.config/localmail/config.toml` existing.
> Worked around in `python-ci.yml` by writing a stub config and
> setting `LOCALMAIL_CONFIG`; the right fix (per-test fixture / mock
> `load_config`) is the next session's lead candidate.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What we shipped this session

### Issues + PR

- **Issue [#87](https://github.com/hherb/localmail/issues/87)** —
  `browse: consider CI-gated at-scale regression coverage for the folder-filter plan family`.
  Filed by a previous session during `/fixall` review of PR #86.
  Closed by PR #99.
- **PR [#99](https://github.com/hherb/localmail/pull/99)** —
  `test: at-scale folder-filter regression coverage + python-ci (#87)`.
  15 commits, ~+2300 / -800 net lines (most of the deletion is the
  operator-CLI gut, replaced by `import` lines).
- **Issue [#100](https://github.com/hherb/localmail/issues/100)** *(filed)* —
  `test: CLI tests depend on $HOME/.config/localmail/config.toml on a clean runner`.
  Tracking the right fix for the workaround currently in `python-ci.yml`.

### Commits (15)

```
6bb97d8 docs(readme): bump test count, document python-ci workflow (#87)
696a0b9 ci(python): reference #100 in LOCALMAIL_CONFIG workaround comment (#87)
5efba1f ci(python): stub LOCALMAIL_CONFIG so CLI tests work on a clean runner (#87)
73cb757 ci(python): add python-ci.yml with pgvector pg18 service (#87)
43fd3cf test: calibrate DEFAULT_REGRESSION_ROWS for the broad-folder probe (#87)
6547b39 test: explain non-obvious choices in test_browse_at_scale (#87)
52d9319 test: at-scale folder-filter regression coverage (#87)
0694146 test(acceptance): switch run_browse_explain.py to import from lib (#87)
4ecc33c test(acceptance): extract pure primitives into browse_explain_lib (#87)
894b382 docs(plan): use canonical tests.acceptance.* imports (#87)
673958e test(acceptance): address review on has_unique_node (#87)
693fafc test(acceptance): track has_unique_node in PlanSummary (#87 prep)
ed60e15 docs(plan): fix Task 3 baseline-diff to extract JSON from harness output (#87)
b1e22f0 docs(plan): implementation plan for at-scale folder-filter regression (#87)
29a470f docs(spec): design for at-scale folder-filter regression coverage (#87)
```

### Headline changes

- **`tests/acceptance/run_browse_explain.py`** shrinks from 1021 → 264
  lines. Operator-CLI JSON output is bit-identical pre/post-refactor
  (verified by diffing against `/tmp/baseline_before_refactor.json`,
  stripping perf-counter jitter).
- **`tests/acceptance/browse_explain_lib.py`** *(new, 565 lines)* —
  pure library extracted from the harness. Public surface:
  `SeedConfig`, `PlanSummary` (with new `has_unique_node` field),
  `ProbeSpec`, `FolderMailboxes`, `classify_plan`, `seed_accounts`,
  `seed_messages`, `seed_folder_filter_mailboxes`, `run_explain`, etc.
  Both the CLI and the new pytest test consume it; production-SQL
  drift now lands in CI automatically (the #77/#85 invariant).
- **`tests/test_browse_at_scale.py`** *(new, 163 lines)* — CI-gated
  pytest test that catches the DISTINCT-regression class (`Unique`
  node + full-projection Sort on the messages projection).
  Calibration gate runs first (the planner must pick the date-ordered
  walk at the chosen scale), then the structural signature is
  asserted. Scale tunable via `LOCALMAIL_REGRESSION_ROWS`.
- **`tests/test_browse_explain_classifier.py`** *(new, 60 lines)* —
  pure-Python unit tests for the new `has_unique_node` heuristic.
- **`.github/workflows/python-ci.yml`** *(new)* — repo's first Python
  CI. `pgvector/pgvector:pg18` service, `uv sync --frozen`, `uv run
  pytest -q`. One pinned PG version and one Python version; matrix
  scaffolding in place so future contributors can widen either
  dimension in a one-line change.

### Calibration result

Empirical sweep against PostgreSQL 18.1 on macOS aarch64
(developer machine):

| N | 5/5 PASS? |
|---|---|
| 50,000 | ✅ |
| 7,500 – 40,000 | ✅ |
| 5,000 | ✅ |
| **4,500** | **✅ (smallest stable)** |
| 4,000 | ❌ (5/5 fail) |
| 3,000 | ❌ |

Selected `DEFAULT_REGRESSION_ROWS = 7000` (= ceil(4500 × 1.5 / 1000) × 1000).
The headroom multiplier absorbs PG planner cost-model jitter run-to-run.
At N=7000 the test takes ~0.22s locally.

### Verification

- Local `uv run pytest -q`: **809 passed, 4 warnings** (4 = the 3
  pre-existing psycopg-pool / websockets / hf_xet deprecation
  noises + 1 transient resource warning).
- Operator-harness JSON diff vs pre-refactor baseline:
  **empty after stripping perf-counter jitter**.
- Calibration gate: `plan_family='index-walk (option 1)'` at N=7000.
- `python-ci` workflow: **green at `5efba1f`** (run `26440823381`,
  1m26s); same workflow re-fired on subsequent doc-only pushes
  (`696a0b9`, `6bb97d8`) since they touched the workflow file or
  the workflow's path-filter coverage of the workflow itself.

### Docs

- **README.md** — updated `~400 tests` → `~800` and added a paragraph
  documenting `python-ci.yml`.
- **CLAUDE.md** — not updated. The new tests live in `tests/`, not in
  `src/localmail/`, and the production code is unchanged; no
  load-bearing invariant moved.
- **ROADMAP.md** — does not exist in this repo. **Not created.**
- **Spec + plan committed**: `docs/superpowers/specs/2026-05-26-browse-at-scale-regression-design.md`
  and `docs/superpowers/plans/2026-05-26-browse-at-scale-regression.md`.

## What's next

### 1. **Maintainer: merge PR #99** *(blocks closing #87)*

PR is ready-for-review (not draft). `python-ci` green, final code
review APPROVED, all spec sections delivered.

**Acceptance**: PR #99 merged to `main`, issue #87 auto-closes via
`Closes #87` in the squash-commit message.

### 2. **#100 — eliminate the `LOCALMAIL_CONFIG` workaround** *(recommended next)*

Once PR #99 merges, the cleanest follow-up is making the eight
failing CLI tests inject `load_config` (or use a `tmp_path`-scoped
config fixture) so the workaround in `python-ci.yml` can be
deleted. Estimated 1-2 hours, mostly mechanical edits across
four `tests/test_cli_*.py` files.

**Acceptance** (from issue #100):
- 8 named tests pass on a clean runner with no `LOCALMAIL_CONFIG`.
- `python-ci.yml` no longer carries the env var or the stub-config step.
- Total pytest count unchanged (809).

### 3. **#28 visual smoke** *(carried over; optional, ~5 min Tauri dev)*

Unchanged from prior handoffs — verify the charset toggle eyeballs
correctly against a real Latin-1 message in `npm run tauri dev`.

### 4. **#38 — `/v1/changes` semantics decision** *(needs user input)*

Conversation-first design call on what the wire contract should be
for initial backfill (since-cursor semantics, safe-horizon
interaction). No code until aligned.

### 5. **Carried-forward deferred items** *(unchanged)*

- **#90** glib Cargo alert — upstream-blocked.
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#47** Third-party transient classes — needs ops data.
- **#5** Search batch INSERT — deferred until measured.
- **#2** Migration 0006 GIN CONCURRENT — deferred until live-upgrade
  scenario hits.

**Open issue count: 8** (no change from the deferred set; +1 for
#100; #87 will close on PR #99 merge → 8 open).

## Open decisions & risks

1. **CLI test fixture design (#100).** Two viable approaches:
   per-test `tmp_path` config fixture, or per-test
   `monkeypatch.setattr("localmail.cli.load_config", ...)`. Both work;
   pick the one that reads better when the fix lands. The third option
   (refactor the click group to accept an injectable `Config`) is the
   cleanest but probably out of proportion for the impact.

2. **PG version matrix in `python-ci.yml`.** Currently pinned to
   `pgvector/pgvector:pg18`. The matrix syntax is in place so a
   future contributor can widen to `["pg17", "pg18"]` in one line.
   Not warranted until we have a customer pinned on PG 17 — at
   which point the calibration gate in `test_browse_at_scale.py`
   might need re-running on the older version.

3. **Calibration value (`DEFAULT_REGRESSION_ROWS = 7000`).** Measured
   on macOS aarch64 / PG 18.1 — the documented headroom multiplier
   (1.5×) is designed to absorb the runner-vs-laptop cost-model
   delta on GH Actions' ubuntu-latest x86_64. If a future PG version
   bump or a runner switch tightens the cost model, the calibration
   gate will fail loud with a `bump LOCALMAIL_REGRESSION_ROWS` hint
   rather than silently letting the regression assertions go vacuous.

4. **`_scan_actual_rows` private leak in
   `tests/test_browse_explain_harness.py:24`.** Pre-existing
   (`tests.acceptance.run_browse_explain._scan_actual_rows` before
   the refactor) and intentionally tested as a private helper. The
   final review flagged it as a minor nit; out of scope to fix here.

5. **`.claude/settings.local.json` stays untracked.** Same as prior
   handoffs — by-convention local-only file (`*.local.json` suffix).
   Not in `.gitignore`; if a future contributor wonders, add an
   explicit ignore rule rather than committing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail

# Verify state:
git status                           # expect: only .claude/settings.local.json
                                     #   untracked (by design)
git log --oneline -5                 # tip: 6bb97d8 on
                                     #   issue-87-at-scale-folder-filter-regression-coverage
gh pr view 99                        # status: OPEN, ready-for-review
gh pr checks 99                      # python-ci: pass (1m26s)

# If picking option 1 (merge PR #99):
gh pr merge 99 --squash              # squash-merge (matches recent style)
git checkout main && git pull        # sync local
gh issue close 87                    # auto-closes if `Closes #87` syntax is in the squash body

# If picking option 2 (#100 CLI test fixture cleanup):
gh issue view 100                    # read the full design context
git checkout main && git pull        # start from a clean main
git checkout -b issue-100-cli-test-fixture-cleanup
# Edit tests/test_cli_extract.py, tests/test_cli_lang_backfill.py,
#   tests/test_cli_embed_backfill.py, tests/test_cli_search.py to inject
#   load_config (or use a tmp_path config fixture in tests/conftest.py).
unset VIRTUAL_ENV && uv run pytest -q   # expect: 809 still passes locally
# Push, then in .github/workflows/python-ci.yml delete the
#   "Write stub localmail config" step + the LOCALMAIL_CONFIG env var.

# If picking option 3 (#28 visual smoke):
unset VIRTUAL_ENV && uv run localmail serve \
  --bind 127.0.0.1 --port 8443 \
  --tls-cert ~/.config/localmail/tls/cert.pem \
  --tls-key ~/.config/localmail/tls/key.pem
cd gui && npm run tauri dev
# Walk the acceptance checklist from earlier handoffs.

# If picking option 4 (#38 semantics decision):
gh issue view 38                     # read the design context; conversation-first.

gh issue list --state open --limit 40
```

## File map (post-session)

```
NEXT_SESSION.md                                                 # MODIFIED this session
docs/handoffs/
  2026-05-26T0827-utc-at-scale-regression-pr-99.md              # NEW (this session's snapshot)
  2026-05-26T0004-utc-node24-action-bumps-pr-98.md              # prior session
  2026-05-25T1205-utc-gui-ci-macos-matrix-pr-96.md              # earlier
  …

docs/superpowers/
  specs/2026-05-26-browse-at-scale-regression-design.md         # NEW
  plans/2026-05-26-browse-at-scale-regression.md                # NEW

.github/workflows/
  python-ci.yml                                                 # NEW

tests/
  test_browse_at_scale.py                                       # NEW
  test_browse_explain_classifier.py                             # NEW
  acceptance/
    browse_explain_lib.py                                       # NEW
    run_browse_explain.py                                       # REFACTORED (264 lines)

README.md                                                       # MODIFIED — CI paragraph + test count
src/localmail/                                                  # unchanged this session
migrations/                                                     # unchanged
```

Branch `issue-87-at-scale-folder-filter-regression-coverage` is
up-to-date with origin at `6bb97d8`. PR #99 is OPEN
(ready-for-review), `python-ci` green at `5efba1f`. Working tree
clean (only `.claude/settings.local.json` untracked).
