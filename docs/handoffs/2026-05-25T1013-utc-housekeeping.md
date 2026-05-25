# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-25T1013 UTC (post-session).**
> **Housekeeping session.** No code touched. Out-of-band before this
> session, **PR #95** (`feat(gui): charset toggle + auto-sniff for
> RawBodyView`) was merged to `main` as commit **`37cc506`** — closing
> **issue #28**. This session committed the three dangling handoff
> snapshots that earlier `/nextsession` runs created but never staged
> (from PR #94 housekeeping, the #91 Tauri smoke session, and the #28
> charset-toggle session) plus a refreshed NEXT_SESSION.md.
>
> **Working tree is clean after the housekeeping commit.** `main` HEAD
> moves to the new docs commit (SHA recorded in the commit log; not
> embedded here because this file is written *before* the commit, by
> design — `/nextsession` writes NEXT_SESSION.md first, copies it to
> `docs/handoffs/<timestamp>.md`, then commits both atomically).
>
> **Visual smoke for #28 is still TODO** (carried over from the prior
> handoff) but the feature is *shipped*, not in-flight. It's a
> verification, not a gating step. Recommended next-session opener
> if no higher-priority work appears: ~5-minute Tauri-dev smoke
> against a known-Latin-1 message — checklist below.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What we shipped this session

Pure housekeeping — no code, no tests, no migrations. **One docs
commit** containing:

- [`NEXT_SESSION.md`](NEXT_SESSION.md) — rewritten to reflect
  post-PR-#95 reality (was stale, still described #28 as
  "not yet committed" even though `37cc506` had landed on `main`).
- [`docs/handoffs/2026-05-25T0522-utc-housekeeping-post-pr-94.md`](docs/handoffs/2026-05-25T0522-utc-housekeeping-post-pr-94.md)
  — snapshot from the PR #94 housekeeping session.
- [`docs/handoffs/2026-05-25T0541-utc-tauri-smoke-issue-91.md`](docs/handoffs/2026-05-25T0541-utc-tauri-smoke-issue-91.md)
  — snapshot from the #91 Tauri smoke session.
- [`docs/handoffs/2026-05-25T0705-utc-charset-toggle-issue-28.md`](docs/handoffs/2026-05-25T0705-utc-charset-toggle-issue-28.md)
  — snapshot from the #28 charset-toggle implementation session
  (the work itself shipped via PR #95 / `37cc506`).
- [`docs/handoffs/2026-05-25T1013-utc-housekeeping.md`](docs/handoffs/2026-05-25T1013-utc-housekeeping.md)
  — snapshot of *this* file (the live ephemeral version is
  `NEXT_SESSION.md`; the handoff is the frozen archive).

`.claude/settings.local.json` is also dirty but stays untracked
by convention — the `*.local.json` suffix is the signal even
though it isn't in `.gitignore`.

### Verification

Pure docs commit — no test runs gated this commit. The last
known-green gates are from PR #95's CI run (commit `37cc506`,
`gui-ci` both jobs SUCCESS) and remain the canonical baseline:

```bash
# GUI (was green at 37cc506 / PR #95):
cd gui && npm test           # 303 passed / 36 files
cd gui && npm run check      # 0 errors / 0 warnings / 335 files

# Backend (untouched since the last green run):
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q           # expect 805 passed
unset VIRTUAL_ENV && uv run mypy src/localmail
                             # expect 4 pre-existing parser.py errors
```

Re-run them only if the next session intends to touch the
corresponding surface.

### Docs review

- **README.md** — only mention of "encoding" is the unrelated
  poison-pill line at [README.md:180](README.md#L180); no
  end-user-facing charset documentation exists. Raw view remains
  a debug/diagnostic feature, dropdown is self-explanatory in-app.
  **Not updated.**
- **ROADMAP.md** — does not exist in this repo. **Not created.**
- **CLAUDE.md** — no new load-bearing invariant from PR #95 (the
  `*_helpers.ts` pattern under `gui/src/lib/` was already documented
  by PR #93). **Not updated.**

## What's next

### 1. **Visual smoke for #28** *(optional verification, ~5 min Tauri dev)*

Carried over from the prior handoff. The unit tests prove the
decoder swap re-renders the `<pre>` live, but the real WebView's
`TextDecoder` hasn't been eyeballed against a non-UTF-8 message
in the actual app. Now that #28 is *merged*, this is a
verification exercise — no code unless a bug surfaces.

**Acceptance**:
- `npm run tauri dev` launches, no panic.
- Open a UTF-8 message → text renders correctly with
  `(detected: utf-8)` (or no hint if no charset header).
- Open a Latin-1 message → AUTO + declared charset decodes
  correctly. Without declared charset → default UTF-8 mojibakes
  the bytes (U+FFFD). Flip dropdown to `Latin-1` → clean text,
  no refetch. Flip back to UTF-8 → mojibake returns.

If no real Latin-1 message is at hand, fabricate one via
`tests/_eml.py` patterns on the backend and resync. Alternatively
the unit test's `[0x63, 0x61, 0x66, 0xe9]` byte pattern is enough
to verify the visual mojibake / un-mojibake flip in isolation.

### 2. **#24 — macOS to gui-ci.yml OS matrix** *(headless-doable; spends CI minutes)*

Unchanged. Convert the `tauri-rust` job to
`strategy.matrix.os: [ubuntu-latest, macos-latest]`. Push as
DRAFT first to validate macOS-minute consumption.

**Acceptance**: gui-ci.yml runs both `ubuntu-latest` +
`macos-latest`; both report SUCCESS on a clean PR.

### 3. **#38 — `/v1/changes` semantics decision** *(needs user input)*

Unchanged — three options in the issue body, needs maintainer
decision before code can move.

### 4. **#87 — CI-gated at-scale folder-filter regression coverage**

Unchanged — substantial work; scale-tuning is empirical.

### Blocked / deferred (unchanged)

- **#90** glib Cargo alert — upstream-blocked.
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#47** Third-party transient classes — needs ops data.
- **#5** Search batch INSERT — deferred until measured.
- **#2** Migration 0006 GIN CONCURRENT — deferred until live-upgrade
  scenario hits.

**Open issue count: 8** (no change this session; #28 closed at
PR #95 merge time, before this session).

## Open decisions & risks

1. **No in-flight code.** The repo is in a clean,
   between-features state. No half-shipped feature branch, no
   pending review feedback, no failing gates. The next session
   is free to pick from "What's next" without inherited context.

2. **`.claude/settings.local.json` stays untracked.** The
   `*.local.json` naming convention signals "local-only" and
   matches Claude Code's own convention (`settings.json` =
   shared, `settings.local.json` = per-machine). It is *not*
   in `.gitignore` so a future contributor might wonder; if
   that ever surfaces as a question, add an explicit ignore
   rule rather than accidentally committing it. The file
   does not contain secrets — only tool-allow rules — but it
   is per-machine noise.

3. **Carried-forward invariants** — same list as the prior
   handoffs; not duplicated. Full enumeration in
   [`docs/handoffs/2026-05-25T0705-utc-charset-toggle-issue-28.md`](docs/handoffs/2026-05-25T0705-utc-charset-toggle-issue-28.md)
   "Known gotchas". Nothing changed this session because no
   code was touched.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git status                           # expect: clean working tree
                                     #   (only `.claude/settings.local.json`
                                     #    untracked, by design)
git log --oneline -5                 # expect: housekeeping commit on top
                                     #   then 37cc506 (PR #95 / #28)
                                     #   then ecf7e34 (PR #94 / #22)
                                     #   then 06121c6 (PR #93 / #27)
                                     #   then 8ed59e9 (docs for PR #93)

# Pick from "What's next" above, then re-verify the relevant gates:
cd gui && npm test                   # if touching gui/src or gui/src-tauri
cd gui && npm run check
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q                   # if touching src/localmail or migrations/

# If picking option 1 (visual smoke for #28):
unset VIRTUAL_ENV && uv run localmail serve \
  --bind 127.0.0.1 --port 8443 \
  --tls-cert ~/.config/localmail/tls/cert.pem \
  --tls-key ~/.config/localmail/tls/key.pem
cd gui && npm run tauri dev
# Walk the acceptance checklist in "What's next §1".

gh issue list --state open --limit 40
```

## File map (post-session, post-commit)

```
NEXT_SESSION.md                                           # MODIFIED this session
docs/handoffs/
  2026-05-25T1013-utc-housekeeping.md                     # NEW (this session's snapshot)
  2026-05-25T0705-utc-charset-toggle-issue-28.md          # NEW (now committed; #28 / PR #95)
  2026-05-25T0541-utc-tauri-smoke-issue-91.md             # NEW (now committed; #91 smoke)
  2026-05-25T0522-utc-housekeeping-post-pr-94.md          # NEW (now committed; PR #94 housekeeping)
  …                                                       # earlier (already committed)

src/localmail/                                            # unchanged this session
migrations/                                               # unchanged
tests/                                                    # unchanged
gui/                                                      # unchanged
```

End of housekeeping session. **Working tree clean,
8 open issues, no in-flight work, ready for next pick.**
