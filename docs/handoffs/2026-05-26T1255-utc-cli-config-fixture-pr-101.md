# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-26T1255 UTC (post-session).**
> **#100 shipped** as PR [#101](https://github.com/hherb/localmail/pull/101)
> (`issue-100-cli-test-fixture-cleanup`). 1 commit.
>
> Replaces the `LOCALMAIL_CONFIG` workaround that PR #99 left behind in
> `.github/workflows/python-ci.yml`. The eight CLI tests that previously
> depended on `~/.config/localmail/config.toml` existing now opt into a
> per-test `cli_config` fixture that writes a stub `config.toml` to
> `tmp_path` and `monkeypatch.setenv("LOCALMAIL_CONFIG", …)`. Global
> filesystem state no longer leaks into test setup.
>
> Verification: reproduced the 8 failures locally with
> `HOME=/tmp/nonexistent uv run pytest -q tests/test_cli_*.py` (no
> `LOCALMAIL_CONFIG`); after the fix all 13 in those files pass and the
> full suite is `809 passed, 3 warnings in 36.89s` under the same
> clean-runner simulation. With a developer's real `$HOME` it's
> `809 passed, 4 warnings in 36.82s` — fixture does not conflict with
> an existing config.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What we shipped this session

### Issues + PR

- **Issue [#100](https://github.com/hherb/localmail/issues/100)** —
  `test: CLI tests depend on $HOME/.config/localmail/config.toml on a clean runner`.
  Filed by the previous session when standing up PR #99.
  Closed by PR #101 (`Closes #100` in the commit body).
- **PR [#101](https://github.com/hherb/localmail/pull/101)** —
  `test(cli): replace LOCALMAIL_CONFIG CI workaround with per-test fixture (#100)`.
  Single commit; +36 / -23 lines across 6 files.

### Commits (1)

```
180f68f test(cli): inject load_config via cli_config fixture; remove CI workaround (#100)
```

### Headline changes

- **`tests/conftest.py`** *(+18 lines)* — new `cli_config` fixture.
  Writes a minimal stub `config.toml` (only the mandatory
  `[database].dsn` key) to `tmp_path` and points `LOCALMAIL_CONFIG`
  at it via `monkeypatch.setenv`. Tests opt in by adding the
  fixture parameter; the fixture is **not autouse** so tests that
  don't hit `load_config` (e.g. `--help` smoke tests) stay
  filesystem-independent.
- **8 CLI tests** in `test_cli_embed_backfill.py`,
  `test_cli_extract.py`, `test_cli_lang_backfill.py`,
  `test_cli_search.py` — each gains the `cli_config` fixture
  parameter. The DSN inside the stub config is `db_dsn`, but the
  tests still monkeypatch `localmail.cli._dsn`, so the stub's DSN
  is opaque — it only has to make the config file *parse*.
- **`.github/workflows/python-ci.yml`** — drops the `LOCALMAIL_CONFIG`
  env var and the "Write stub localmail config for CLI tests"
  step. Workflow shrinks from 75 to 60 lines.

### Verification

- Reproduced #100 locally: `HOME=/tmp/nonexistent
  LOCALMAIL_CONFIG=/tmp/does_not_exist uv run pytest -q
  tests/test_cli_*.py` → **8 failed, 5 passed** (exactly the eight
  named in the issue body).
- After the fix, same command: **13 passed in 0.83s**.
- Full suite under clean-runner simulation
  (`HOME=/tmp/nonexistent uv run pytest -q`): **809 passed,
  3 warnings in 36.89s**.
- Full suite with real `$HOME`: **809 passed, 4 warnings in
  36.82s** — fixture does not conflict with an existing developer
  config file.
- **`python-ci` workflow: green at `180f68f`** (run
  [`26449218412`](https://github.com/hherb/localmail/actions/runs/26449218412),
  1m33s).

### Docs

- **README.md** — not updated. The "~800 tests" line stays accurate
  (count unchanged at 809). The CI paragraph already documents
  `python-ci.yml`; removing the workaround did not change behaviour
  visible from the README's altitude.
- **CLAUDE.md** — not updated. Production code is unchanged; the new
  fixture lives in `tests/conftest.py` alongside the existing
  `memory_keyring` / `db_conn` / `api_user` fixtures, no
  load-bearing invariant moved.
- **ROADMAP.md** — does not exist in this repo. **Not created.**

## What's next

### 1. **Maintainer: merge PR #101** *(blocks closing #100)*

PR is ready-for-review (single commit, full test suite green
locally, `python-ci` green at `180f68f` — run
[`26449218412`](https://github.com/hherb/localmail/actions/runs/26449218412),
1m33s).

**Acceptance**: PR #101 merged to `main`, issue #100 auto-closes via
`Closes #100` in the commit body.

### 2. **#28 visual smoke** *(carried over; optional, ~5 min Tauri dev)*

Unchanged from prior handoffs — verify the charset toggle eyeballs
correctly against a real Latin-1 message in `npm run tauri dev`.

### 3. **#38 — `/v1/changes` semantics decision** *(needs user input)*

Conversation-first design call on what the wire contract should be
for initial backfill (since-cursor semantics, safe-horizon
interaction). No code until aligned.

### 4. **Carried-forward deferred items** *(unchanged)*

- **#90** glib Cargo alert — upstream-blocked.
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#47** Third-party transient classes — needs ops data.
- **#5** Search batch INSERT — deferred until measured.
- **#2** Migration 0006 GIN CONCURRENT — deferred until live-upgrade
  scenario hits.

**Open issue count: 7** (was 8; #100 will close on PR #101 merge).

## Open decisions & risks

1. **Fixture is opt-in, not autouse.** Each of the 8 affected tests
   gains a `cli_config` parameter. Future CLI tests that exercise a
   subcommand calling `load_config()` will silently regress on a
   clean runner unless their author remembers to add the fixture.
   Mitigations considered: making the fixture autouse (overkill —
   would write a stub config for every test in the suite, including
   pure-Python tests that don't touch the CLI). The current opt-in
   shape is the right tradeoff but is worth flagging: if this
   class of regression starts recurring, the next escalation is
   either an autouse session-scoped fixture or a CLI-layer change
   that accepts an injectable `Config` (option 3 from the issue body —
   bigger blast radius, deferred unless motivated by other pain).

2. **Stub DSN is `db_dsn` but is opaque.** Tests still monkeypatch
   `localmail.cli._dsn` so the actual SQL routing goes to the test
   DB. The stub DSN matches `db_dsn` mostly for readability if a
   future test forgets to monkeypatch `_dsn` — it would still work
   (since `db_dsn` *is* the test DSN), but that's coincidence, not
   design. If we ever want to enforce that tests monkeypatch
   `_dsn`, the fixture could write a deliberately-broken DSN like
   `postgresql://localhost:1/no_such_db` — punted for now since
   it's needlessly user-hostile.

3. **`.claude/settings.local.json` + `.claude/scheduled_tasks.lock`
   stay untracked.** Same as prior handoffs — by-convention
   local-only files. Not in `.gitignore`; if a future contributor
   wonders, add explicit ignore rules rather than committing.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail

# Verify state:
git status                           # expect: only .claude/* untracked (by design)
git log --oneline -5                 # tip: 180f68f on
                                     #   issue-100-cli-test-fixture-cleanup
gh pr view 101                       # status: OPEN
gh pr checks 101                     # python-ci: pass (1m33s)

# If picking option 1 (merge PR #101):
gh pr merge 101 --squash             # squash-merge (matches recent style)
git checkout main && git pull        # sync local
# Issue #100 auto-closes via `Closes #100` in commit body.

# If picking option 2 (#28 visual smoke):
unset VIRTUAL_ENV && uv run localmail serve \
  --bind 127.0.0.1 --port 8443 \
  --tls-cert ~/.config/localmail/tls/cert.pem \
  --tls-key ~/.config/localmail/tls/key.pem
cd gui && npm run tauri dev
# Walk the acceptance checklist from earlier handoffs.

# If picking option 3 (#38 semantics decision):
gh issue view 38                     # read the design context; conversation-first.

gh issue list --state open --limit 40
```

## File map (post-session)

```
NEXT_SESSION.md                                                 # MODIFIED this session
docs/handoffs/
  2026-05-26T1255-utc-cli-config-fixture-pr-101.md              # NEW (this session's snapshot)
  2026-05-26T0827-utc-at-scale-regression-pr-99.md              # prior session
  2026-05-26T0004-utc-node24-action-bumps-pr-98.md              # earlier
  …

.github/workflows/
  python-ci.yml                                                 # MODIFIED — stub-config workaround removed

tests/
  conftest.py                                                   # MODIFIED — cli_config fixture added
  test_cli_embed_backfill.py                                    # MODIFIED — 2 tests opt into cli_config
  test_cli_extract.py                                           # MODIFIED — 2 tests opt into cli_config
  test_cli_lang_backfill.py                                     # MODIFIED — 3 tests opt into cli_config
  test_cli_search.py                                            # MODIFIED — 1 test opts into cli_config

src/localmail/                                                  # unchanged this session
migrations/                                                     # unchanged
README.md                                                       # unchanged
CLAUDE.md                                                       # unchanged
```

Branch `issue-100-cli-test-fixture-cleanup` is up-to-date with
origin at `180f68f`. PR #101 is OPEN. Working tree clean (only
`.claude/scheduled_tasks.lock` + `.claude/settings.local.json`
untracked, by design).
