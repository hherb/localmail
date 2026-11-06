---
name: finalcheck
description: final pre-merge pass on the current PR — review, fix everything, then drive GitHub CI to green
disable-model-invocation: false
allowed-tools: Bash(git *), Bash(gh *), Bash(cargo *), Bash(uv *), Bash(uvx *), Bash(npm *), Bash(docker *)
---
Run these steps in order on the PR for the current branch. First run
`pwd && git branch --show-current && git worktree list` and `cd` into the PR's worktree if it is not the
main checkout. Then find the PR with `gh pr view --json number,url,headRefOid,baseRefName`. If there is
none, stop and tell me. If the PR's base is not `main`, stop and tell me: a PR stacked on another branch
is stranded silently once that branch merges (see the Conventions section in CLAUDE.md).

**Before running anything that touches the test DB:** `uptime; pgrep -fl pytest`. No other localmail
pytest may be running (the test DB shares a cluster with the live archive), and timings taken at high load
are not evidence. Prefix every `uv` command with `unset VIRTUAL_ENV &&`.

1. **Review.** Invoke the `pr-review-toolkit:review-pr` skill via the Skill tool and let it finish.
2. **Fix.** First bring the branch up to date: `git fetch origin && git merge origin/main` (resolve any
   conflicts, then re-run the affected tests). Then invoke the `fixall` skill via the Skill tool on that
   review's findings. It ends by committing and pushing into the PR. Before moving on, confirm the push
   landed: `git fetch origin && git status -sb` must show no `ahead`, and the PR's `headRefOid` must equal
   `git rev-parse HEAD`. If the fixes change what the session shipped, update `NEXT_SESSION.md` **and**
   re-copy it over its `docs/handoffs/` snapshot, so the two stay byte-identical.
3. **Drive CI to green.** Loop:
   - Watch the checks for the pushed head commit with `gh pr checks --watch --fail-fast`. Checks can take
     a minute to register, so wait if it reports none yet. Both workflows are path-gated: `python-ci` runs
     only for `src/`, `tests/`, `migrations/`, `pyproject.toml`, `uv.lock` and a few `gui/` manifests, and
     `gui-ci` only for `gui/**`. A docs-only PR posts no CI check (only Cloudflare Pages), which is normal,
     not stuck.
   - All green → done. Report the PR URL and the final check list. `main` has no required status checks
     (its ruleset only blocks deletion and force-push, and `branches/main/protection` returns 404), so
     "all green" means **every check the PR reports passed**, not just a required subset.
   - A check failed → read the cause: `gh run list --branch "$(git branch --show-current)" --limit 5`,
     then `gh run view <run-id> --log-failed`. Reproduce it locally with the command the failing job runs
     (see `.github/workflows/`):
     - `pytest (PG pg18, Python 3.13)` → `unset VIRTUAL_ENV && uv run pytest -q` (the venv is 3.13 via
       `.python-version`)
     - `pytest (… Python 3.12)` → **only in a throwaway worktree with its own venv**:
       `git worktree add ../localmail-py312 HEAD && cd ../localmail-py312 && uv sync --locked --python 3.12 --extra mcp && uv run --python 3.12 pytest -q`.
       Never pass `--python 3.12` in the main checkout: that venv is the Mac deployment's install, and
       `uv` would rebuild it. Remove the worktree afterwards.
     - `svelte-check + vitest` → `cd gui && npm ci && npm run check && npm test -- --run`
     - `cargo test + clippy` → `cd gui/src-tauri && cargo test --locked && cargo clippy --locked -- -D warnings`
       (also run `cargo clippy --all-targets -- -D warnings`: CI does not lint test modules, but a
       regression there is still one)
     - mypy is not a CI job, but run it on any Python change:
       `uvx mypy --python-executable .venv/bin/python src/localmail`

     Judge each command by its **exit code**, never by filtered output: `cmd | tail` reports `tail`'s
     status. Fix the root cause, using TDD where it is a code bug. Confirm the fix passes locally, commit
     with a message naming the CI failure, push, and loop again. A sync that drops extras breaks the
     deployment: any local `uv sync` in the main checkout must carry `--extra mcp --extra extraction`.
   - **Linux-only failures** (CI is ubuntu-latest and macOS passes locally): reproduce in Docker
     (`ubuntu:24.04`, with a `pgvector/pgvector:pg18` container for Postgres; start the daemon with
     `open -a Docker`). Don't guess. Note CI installs `--extra mcp` only, so tests needing `[extraction]`
     are skipped there; a skip count that differs from macOS is expected (see Testing notes in CLAUDE.md).
   - **A job that is CANCELLED with no log** means the runner was killed, not that a test was slow.
     Treat it as a code bug: find what killed the runner. Do not rerun it as a flake.
   - If the failure is not caused by the code (runner/infra outage, network flake, a model download that
     timed out), rerun once with `gh run rerun <run-id> --failed`. If it fails the same way again, stop
     and report rather than changing code to work around it. The Actions API returns empty or stale data
     during an outage, so re-query before concluding a run never happened.
   - Never "fix" CI by skipping, `xfail`-ing or deleting tests, by loosening a `filterwarnings = error`
     entry, by weakening lint/CI config or a structural rule (`tests/_*_rules.py`, `tests/_pool_leaks.py`,
     `tests/acceptance/_harness_lock.py`), or by adding an allowlist entry (e.g. `ALLOWED_SINKS`, `COVERED_LIBRARIES`),
     without my explicit permission. If the same failure survives three fix attempts, stop and report
     what you tried.
4. **Not covered by CI.** Run these locally when the PR touches them and include the results in the report:
   - `tests/acceptance/` harnesses: no CI job runs them. For a harness change, run
     `uv run pytest -q tests/test_acceptance_harness_lock.py` and the harness itself against the test DB.
   - docling / OCR or an `[extraction]` dependency bump: CI does not install `[extraction]`, so run
     `uv run python tests/acceptance/probe_docling_ocr.py` in a worktree venv on `main` and on the branch,
     and diff the JSON (see the docling notes in CLAUDE.md).
   - Deployment: a merged change only reaches the hosts on deploy. Do not deploy from this skill. Just
     report whether the PR needs a deploy, a migration, or a `uv sync`.
