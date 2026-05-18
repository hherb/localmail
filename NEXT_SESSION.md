# NEXT_SESSION.md — localmail GUI client handoff

> **Delete this file once Sub-plan 5 ships and the GUI is feature-complete.**

You're picking up after **Sub-plan 5 has been fully planned** in the `gui-client-5` worktree (single commit, 27 tasks). The plan itself is the only deliverable of this session — no Sub-plan 5 code has been written yet.

Open PR #21 and PR #23 from the previous handoff have both **merged to `main`** (commits `c768107` and `f5667b8` respectively). Sub-plan 5 is the only remaining work to make the GUI feature-complete per the design spec.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres. **Strictly read-only with respect to IMAP**. Hybrid search (Phases 1 + 2 incl. attachment text) shipped. GUI server (`localmail serve`, migration 0014) shipped. GUI client Sub-plans 1–4 shipped and merged to `main`. See [CLAUDE.md](CLAUDE.md) and [docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

**Server vs. main branch quirk:** the GUI server code (`src/localmail/api/`, `src/localmail/serve/`, migration `0014_api_users.sql`) lives on the long-lived `worktree-phase2-hybrid-search` branch. Main has the **client** only. Sub-plan 5 Phase A commits land on `worktree-phase2-hybrid-search` directly (no PR, matching the Sub-plan 4 Phase A convention); Sub-plan 5 Phase B lands on `gui-client-5` and PRs into `main`.

## What's done

| Component | Status |
|---|---|
| **Server**: `localmail.api` + FastAPI `localmail serve` + migration 0014 + CLI commands | ✅ shipped — PR #6 into `worktree-phase2-hybrid-search` |
| **Sub-plan 1**: Tauri 2 + Svelte 5 scaffolding | ✅ shipped — PR #14 → `main` |
| **Sub-plan 2**: Connection core (TOFU pin + keyring + Connect/Login/AuthShell) | ✅ shipped — PR #19 → `main` |
| **Sub-plan 3**: 3-pane main view shell (plain-text bodies) | ✅ shipped — PR #20 → `main` (`653c445`) |
| **Sub-plan 4 — Phase A**: Server filter wiring (`account_ids` / `folder_ids`) | ✅ shipped — 4 commits on `worktree-phase2-hybrid-search` (`c046744`…`fa6c6d5`) |
| **Sub-plan 4 — Phase B**: GUI search + HTML body + attachments | ✅ shipped — PR #21 → `main` (`c768107`) |
| **PR #23 fixissues**: post-merge clean-ups | ✅ shipped — PR #23 → `main` (`f5667b8`) |
| **Sub-plan 5 plan** | ✅ committed on `gui-client-5` (`50901fe`) — 27 tasks (A1–A2 + B1–B25), 128 steps |
| **Sub-plan 5 execution** | 🟡 **not started** — pick up in `.claude/worktrees/gui-client-5/` |

## What this session did

Drafted [docs/superpowers/plans/2026-05-18-localmail-gui-client-5-polish-packaging.md](docs/superpowers/plans/2026-05-18-localmail-gui-client-5-polish-packaging.md) (3185 lines, 27 tasks, 128 TDD steps) via `superpowers:writing-plans`. The plan is on `gui-client-5` branch; it will be visible on `main` once Sub-plan 5's PR merges.

**Commits this session:**

| SHA | Branch | What |
|---|---|---|
| `50901fe` | `gui-client-5` | `docs(gui-client): Sub-plan 5 plan — polish + packaging` |

**Worktree set up:**

```
.claude/worktrees/gui-client-5/   # branch gui-client-5, based off main (f5667b8)
.claude/worktrees/phase2-hybrid-search/   # pre-existing — Sub-plan 5 Phase A lands here
```

## What remains — Sub-plan 5 (execution)

The plan is the spec. Acceptance criteria for the sub-plan as a whole:

- `_KNOWN_UNSUPPORTED_FILTER_KEYS` in `src/localmail/api/search.py` is **empty** (date_from, date_to, lang fully wired through).
- All eight GUI screens from the design spec (`docs/.../localmail-gui-design.md#screens`) are implemented and have at least one component test.
- `npm test -- --run` is green on `gui-client-5` (target: ~150+ passing tests; Sub-plan 4 left 101).
- `(cd gui/src-tauri && cargo test)` is green (target: ~60+ passing tests; Sub-plan 4 left 49).
- `npm run check` reports zero TypeScript/svelte-check errors.
- `npm run tauri build -- --bundles dmg` produces a runnable bundle on macOS; build commands for `.msi` / `.AppImage` are documented in `docs/superpowers/notes/2026-05-18-bundle-smoke.md`.
- Manual smoke (B25 step 2) passes — splitter, polling, version gate, settings, headers-full, raw body, debug pane, PDF multi-page.

### How to start execution

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-5
git status   # should show clean tree on gui-client-5
# Open the plan:
cat docs/superpowers/plans/2026-05-18-localmail-gui-client-5-polish-packaging.md | less

# Recommended: invoke superpowers:subagent-driven-development to dispatch one
# fresh sonnet subagent per task (same approach that shipped Sub-plan 4).
```

Phase A (server) executes in the `phase2-hybrid-search` worktree:

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search
git checkout worktree-phase2-hybrid-search
git pull origin worktree-phase2-hybrid-search
# Tasks A1 + A2 go here.
```

### Test commands by phase

Phase A (server):
```bash
cd /Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search
unset VIRTUAL_ENV && uv run pytest tests/ -v
```

Phase B (client):
```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-5/gui
npm run check
npm test -- --run
(cd src-tauri && cargo test)
```

## Open decisions & risks

1. **`messages.body_lang` vs. `message_chunks.lang`** (Task A1, step 6a). The plan defers the schema decision to execution time — the subagent runs `\d messages` first and picks the predicate accordingly. If neither column exists, the `lang:` filter won't have a SQL-level predicate; A1 step 6b's fallback will fail and the engineer will need to add a migration. **Pre-flight check before starting A1:** confirm the column exists.

2. **`/v1/auth/change-password` may not exist on the server yet** (Task B16). The plan tells the engineer to grep for it and add the route on `worktree-phase2-hybrid-search` if missing. This is a small server-side side-quest that lands as a separate commit alongside Phase A's A1/A2.

3. **`matched_chunks` may not be in `/v1/search` response** (Task B21). If absent, the debug pane shows only `matched_arms` chips (no chunk text) — DebugChunks degrades gracefully. Don't add a new server endpoint for chunk text; defer to v1.x if needed.

4. **Brand icon is a placeholder** (Task B24). The plan generates a simple SVG envelope+database silhouette via `rsvg-convert` / ImageMagick. A real designer asset is post-v1. The build will work either way.

5. **Tauri 2 bundle field names** (Task B24, step 3). The exact `tauri.conf.json` schema differs across Tauri 1 → 2 minor versions. If a config field is rejected, run `npx @tauri-apps/cli info` to see the active schema and adapt. The plan's JSON is the most-common 2.x shape; minor field-name drift is expected.

6. **`MessageDetail.matched_chunks` field is not in any current type definition.** The plan adds it conditionally in B21 step 3, but the type may already need a discriminated union. Engineer must verify it doesn't break any downstream consumer in `MessageList` / search rendering.

7. **Polling cursor reset on scope change.** The plan's `mail.pollOnce()` keeps the cursor across account/folder switches, which means a switch from "All Mail" to a specific account may surface a message that wasn't in the new scope but was in the old. Acceptable for v1 (mail is read-only and dedup-by-message_id prevents duplicates), but if it's surprising during smoke, the fix is to reset `changeCursor` in `mail.setSelection()`.

## Exact commands to resume

```bash
# Get to the worktree:
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-5

# Open the plan, then either:
#  (a) dispatch a subagent per task via superpowers:subagent-driven-development, or
#  (b) work through tasks inline via superpowers:executing-plans.

# Pre-flight check for Task A1 (run in the OTHER worktree):
cd /Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search
unset VIRTUAL_ENV && uv run python -c "
import os, psycopg
dsn = os.environ.get('LOCALMAIL_TEST_DSN', 'postgresql:///localmail_test')
with psycopg.connect(dsn) as conn, conn.cursor() as cur:
    cur.execute(\"SELECT column_name FROM information_schema.columns WHERE table_name = 'messages'\")
    cols = sorted(r[0] for r in cur.fetchall())
    print('body_lang' in cols, '— body_lang column present' if 'body_lang' in cols else '— body_lang column NOT present')
"

# Test commands during execution:
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-5/gui
npm run check
npm test -- --run
(cd src-tauri && cargo test)
```

## Known gotchas (still load-bearing — don't repeat them)

All gotchas from prior handoffs still apply. Highlights worth re-stating:

- **`@testing-library/jest-dom` is NOT installed in `gui/`.** Use `toBeTruthy()` / `toBeFalsy()` / `.textContent` checks. Never `toBeInTheDocument()`.
- **Rust structs used as Tauri command arguments need `#[derive(Deserialize)]`** even if they look like output types.
- **Plan-snippet schema may not match real schema.** A1 step 6a is the explicit verify-against-`\d` checkpoint — apply the same discipline elsewhere if a task supplies seed SQL.
- **`unset VIRTUAL_ENV && uv run …`** — required for ad-hoc Python commands so the wrong venv doesn't get picked up.
- **Per-iframe CSP in `HtmlBody.svelte`** must NOT be relaxed to support attachment-image policy — image-policy belongs to the iframe loader (which sets the `cid:` rewriting), not to the CSP itself.
- **Sub-plan 4's `bodyMode` is sticky across messages; `externalImagesAllowed` resets per-message.** Sub-plan 5's `SettingsDisplay → imagePolicy = allow` should be applied only on first-load of each message, not as a permanent override that bypasses the per-message reset.
- **Polling thread vs. UI thread:** `mail.pollOnce()` runs on the JS event loop — no worker thread. Don't add one in Sub-plan 5.

## File map (after Sub-plan 4, unchanged by this session)

```
docs/superpowers/specs/2026-05-17-localmail-gui-design.md                       # design spec (all 5 sub-plans)
docs/superpowers/plans/2026-05-17-localmail-gui-client-{1,2,3,4}-*.md           # prior plans (on main)
docs/superpowers/plans/2026-05-18-localmail-gui-client-5-polish-packaging.md    # NEW — on gui-client-5 only until PR merges

.claude/worktrees/
  phase2-hybrid-search/    # Sub-plan 5 Phase A lands here (A1 + A2 + change-password route if missing)
  gui-client-{2,3,4,5}/    # Sub-plan worktrees (5 is the fresh one — only the plan committed so far)
```

Good luck. The plan is exhaustive enough that a subagent-driven execution should ship the whole sub-plan over the course of a single long session or two short ones; budget ~3500 input tokens per task × 27 tasks for context, plus per-task tool churn.
