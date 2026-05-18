# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-18 evening (local).** All GUI client sub-plans
> (1–5) have merged to `main`. The CI fix for Linux Secret Service has
> merged. The next strategic question is no longer "what's left on
> Sub-plan 5" — it's **"when does `worktree-phase2-hybrid-search` merge
> back to `main`?"** That branch carries the entire GUI API server (79
> commits) which the merged GUI client depends on at runtime.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Search Phase 1 (hybrid
lexical + vector) is on `main`. Phase 2 (attachment text + GUI API
server) lives on `worktree-phase2-hybrid-search` and has not merged.
GUI client (Tauri 2 + Svelte 5) is on `main` through Sub-plan 5.
See [CLAUDE.md](CLAUDE.md) and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What's done (cumulative, on `main` unless noted)

| Component | Status |
|---|---|
| **Server**: `localmail.api` + FastAPI `localmail serve` + migrations through `0014_api_users.sql` | ✅ on `worktree-phase2-hybrid-search` (not yet on main) |
| **Sub-plan 1**: Tauri 2 + Svelte 5 scaffolding | ✅ PR #14 → `main` |
| **Sub-plan 2**: Connection core | ✅ PR #19 → `main` |
| **Sub-plan 3**: 3-pane main view shell | ✅ PR #20 → `main` |
| **Sub-plan 4**: GUI search + HTML body + attachments | ✅ PR #21 → `main` (Phase A on phase2 branch) |
| **PR #23 fixissues** | ✅ → `main` |
| **Sub-plan 5**: polish + packaging (settings, polling, splitter, version gate, bundle, icons) | ✅ **PR #26 → `main`** |
| **CI: Linux secret-service feature** | ✅ **PR #29 → `main`** |
| **`change-password` route** (resolved prior Open Decision #2) | ✅ commit `4e3a7b1` on `worktree-phase2-hybrid-search` |
| **CSS sanitizer hardening** | ✅ commit `8002038` on `worktree-phase2-hybrid-search` |

## What this session shipped (delta since prior handoff `7db9e06`)

The prior NEXT_SESSION.md was written immediately after the
`change-password` route landed and listed B24 (Tauri bundle + icons)
and B25 (manual smoke + PR) as pending. Both shipped in the subsequent
session(s), along with several review/cleanup commits.

### On `main`

| SHA | What |
|---|---|
| `267cd4f` | `test(gui): accept day-first locale ordering in formatRelativeDate` — locale-flake fix |
| `b926e50` | `fix(gui): add standard line-clamp alongside -webkit-line-clamp` — non-webkit fallback |
| `7e8a4b8` | `fix(gui-bundle): add macOS DMG layout` — symmetric icon positions, window size |
| `1320ae4` | `fix(gui): closable FilterPopover + Settings button in MainView top bar` |
| `58b0719` | `chore(gui-bundle): branded app icons (envelope + database silhouette)` — B24 icon work |
| `94e9f76` | `review(gui): address PR #26 findings (version gate, polling backoff, error UX)` |
| `0271b60` | **Merge PR #26** — Sub-plan 5 lands on main (+7243 / -55 across 121 files) |
| `c08df12` | **Merge PR #29** — `ci(gui): enable secret-service rt-tokio-crypto-rust feature on Linux` |
| `2573ee8` | `icon added` — final asset commit |

### On `worktree-phase2-hybrid-search`

| SHA | What |
|---|---|
| `8002038` | `fix(api): wire bleach CSSSanitizer to enforce CSS property allowlist` — hardens HTML sanitiser to actually filter inline CSS, not just markup |

Net effect: **GUI v1 feature surface is complete and merged.** The
client is shippable as soon as a host can build the bundle, but the
server it talks to still lives on a feature branch.

## What's next — the phase2 → main merge

This is now the single biggest open item. `worktree-phase2-hybrid-search`
is 79 commits ahead of `main` and carries:

- The entire HTTPS API server (`src/localmail/api/`, `src/localmail/serve/`).
- The `localmail serve`, `add-api-user`, `remove-api-user`,
  `list-api-users`, `rotate-tls` CLI commands.
- Phase 2 search (attachment text extraction, `extract-worker`,
  `extract-backfill`, `list/retry-failed-extractions`,
  `search-status`).
- Migrations `0011`–`0015` (failed_extractions, attachments_text,
  api_users, messages_body_lang).
- `account_ids` / `folder_ids` / `lang` filter forwarding + DSL tokens.
- The `change-password` route.
- CSS sanitiser hardening (`8002038`).

**Concrete acceptance criteria for the merge session:**

1. **No regressions** — full server pytest suite green on the merge
   commit (target 371+ passing, currently 371 on phase2 HEAD).
2. **`uv run localmail init-db`** applies migrations 0011–0015 cleanly
   on an existing Phase-1 archive (i.e. against a DB that already has
   0001–0010 applied). No data loss, no drop columns.
3. **`uv run localmail serve --bind 127.0.0.1 --port 8443 --tls-cert …
   --tls-key …`** starts; `/v1/health`, `/v1/capabilities`,
   `/v1/version` return 200; `/v1/auth/login` round-trips with a
   created API user.
4. **GUI client (already on main) connects** to the freshly-merged
   server and search/messages/attachments work end-to-end.
5. **GitHub issues closable on merge:** **#11** (filters wired:
   account_ids, folder_ids, date_from, date_to, lang) — verify it lists
   as closed once the merge lands.

**Likely strategy** (open for discussion): open a single large PR
`worktree-phase2-hybrid-search → main` rather than trying to
cherry-pick. The branch is internally coherent; splitting it now would
be costly and gain little.

## Open decisions & risks

1. **`messages.body_lang` is NULL for every existing row.** Migration
   0015 (on phase2) adds the column but no embed-worker change
   populates it. The `lang:` filter is plumbed end-to-end but returns
   0 results until either (a) a follow-up embed worker change detects
   language per-message, or (b) a one-shot backfill script runs.
   Defer until after the phase2 merge so we can iterate on `main`.

2. **Many open follow-up issues queued** (see `gh issue list`). The
   high-signal ones to triage after the phase2 merge:
   - **#11** auto-closes on merge (filters wired).
   - **#13** Migrate HTML sanitisation from bleach to nh3 — `8002038`
     hardened bleach; nh3 migration is the longer-term plan.
   - **#10 / #12** Persist Content-ID on attachments (inline `cid:`
     image rendering).
   - **#28** Encoding toggle / charset detection for `RawBodyView`.
   - **#27** Rename `change_poller.ts` → `change_helpers` (or fold
     `setInterval` back into it).
   - **#25** `websockets.legacy` DeprecationWarning in uvicorn.
   - **#24** Add macOS to the `gui-ci.yml` OS matrix.
   - **#22** Split `AuthError::Io` into a dedicated `AttachmentError`.
   - **#7 / #8 / #9** Auth hardening (rate limit, per-user ACL,
     pool sizing).

3. **VersionGate mounted twice** (Router + MainView). Harmless because
   both branches render only on `api_major` mismatch, but a single
   mount point would be cleaner. Defer.

4. **Stale worktrees on disk.** `git worktree list` shows finished
   feature worktrees (gui-client-2, gui-client-3, gui-client-4,
   gui-client-5, phase1-hybrid-search, ci-secret-service-fix) whose
   PRs have all merged. Safe to `git worktree remove` after the
   phase2 merge so the next session starts from a clean slate. Do
   not remove `phase2-hybrid-search` until that branch is merged.

5. **README.md still doesn't mention the GUI.** The Tauri client is
   now on main but the README has zero `gui` references. Update when
   the phase2 merge lands so the GUI install/usage section can also
   cover the server it requires — these belong together.

## Exact commands to resume

```bash
# Land in the repo root on the live main:
cd /Users/hherb/src/localmail
git fetch origin
git status                                        # should show clean main

# Verify main is green before doing anything:
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && uv run pytest -q             # full suite; needs Postgres at LOCALMAIL_TEST_DSN

# Inspect the phase2 → main diff before opening the merge PR:
git log --oneline origin/main..origin/worktree-phase2-hybrid-search | wc -l   # ~79
git diff --stat origin/main..origin/worktree-phase2-hybrid-search | tail -20

# When ready to merge phase2 → main, open the PR:
gh pr create --base main --head worktree-phase2-hybrid-search \
  --title "feat: GUI server + Phase 2 search (attachment text + filter forwarding)" \
  --body-file docs/superpowers/specs/2026-05-17-localmail-gui-design.md
# (or hand-write a summary body referencing the spec + Phase 2 plan)

# To smoke the server locally from its worktree:
cd /Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search
unset VIRTUAL_ENV && uv run pytest -q             # should be 371 passed
unset VIRTUAL_ENV && uv run localmail init-db
unset VIRTUAL_ENV && uv run localmail serve --bind 127.0.0.1 --port 8443 \
                                            --tls-cert PATH --tls-key PATH

# To smoke the GUI client (lives on main now):
cd /Users/hherb/src/localmail/gui
npm install
npm test -- --run                                 # vitest
(cd src-tauri && cargo test)
npm run check
npm run tauri dev                                 # launches against the local server
```

## Known gotchas (still load-bearing)

- **`unset VIRTUAL_ENV && uv run …`** — shells frequently have a stale
  `VIRTUAL_ENV` pointing at some other pyenv venv; `uv run --active`
  will pick the wrong interpreter without this.
- **`LOCALMAIL_TEST_DSN` defaults to `postgresql:///localmail_test`** —
  tests must not touch the live `localmail` DB. The conftest enforces
  this but the env var still has to be reachable for DB tests to run;
  otherwise they skip.
- **`@testing-library/jest-dom` is NOT installed in `gui/`.** Tests use
  `toBeTruthy()` / `.textContent` / `querySelector`. Never
  `toBeInTheDocument()`.
- **jsdom in `gui/` lacks `window.localStorage` and `PointerEvent`** by
  default — tests that need them install shims in `beforeAll`. See
  `settings.test.ts`, `Splitter.test.ts` for templates.
- **Rust command argument structs** in `gui/src-tauri/` need
  `#[derive(Deserialize)]`.
- **Phase 2 migrations are additive.** Re-running `init-db` on a
  Phase-1 archive should be safe (idempotent), but back up first if
  the archive is non-trivial.
- **The `gui/` directory is on `main` but the server it talks to is
  not** — running `npm run tauri dev` without `localmail serve` (from
  the phase2 worktree) will land you on the connection screen with no
  reachable backend.

## File map (current `main`)

```
src/localmail/                       # sync + parser + search Phase 1
  search/                            # Phase 1 hybrid search (lexical + vector)
gui/                                 # Tauri 2 + Svelte 5 client (Sub-plans 1–5)
  src/                               # Svelte components, stores, API wrappers
  src-tauri/                         # Rust commands, bundle config, icons
migrations/                          # 0001 … 0010 on main
docs/superpowers/specs/              # design specs (GUI, search, Phase 2)
docs/superpowers/plans/              # implementation plans (Sub-plans 1–5)
docs/handoffs/                       # frozen NEXT_SESSION snapshots per session
NEXT_SESSION.md                      # this file — the live handoff
```

```
.claude/worktrees/
  phase2-hybrid-search/              # 79 commits ahead of main — GUI server + Phase 2 search
  gui-client-2…5/                    # merged; safe to remove after phase2 lands
  phase1-hybrid-search/              # merged; safe to remove
  ci-secret-service-fix/             # merged; safe to remove
```

End of session. The next session's first decision: open the
`worktree-phase2-hybrid-search → main` PR, or split the merge into
narrower pieces first.
