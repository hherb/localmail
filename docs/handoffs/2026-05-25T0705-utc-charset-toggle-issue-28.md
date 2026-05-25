# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-25T0705 UTC (post-session).**
> **Issue #28 implemented** (charset toggle / sniff for `RawBodyView`).
> Pure helper module + dropdown + auto-sniff hint. **303 vitest tests
> passing** (276 → +27: 23 in the new helper module, 4 new in the
> component test). `svelte-check` reports 0 errors / 0 warnings / 335
> files (was 333; added `charset_helpers.ts` + `charset_helpers.test.ts`).
>
> **Working tree is NOT yet committed.** `main` HEAD remains `ecf7e34`.
> The session leaves a feature-branch-shaped change in the worktree
> awaiting maintainer review + branch/PR decision (see "Open decisions"
> below). The two earlier handoff snapshots from PR #94 housekeeping
> and the #91 smoke session are also still untracked from prior
> `/nextsession` runs that did not commit.
>
> **Visual smoke is still TODO** — the implementation is fully
> headless-doable (tests prove the dropdown changes the rendered
> `<pre>` content live) but eyeballing a real Latin-1 / Windows-1252 /
> Shift_JIS message in the actual Tauri WebView wasn't done in this
> session. Recommended next-session opener: 5-minute Tauri-dev smoke
> against a known-Latin-1 message.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read
DB + attachment tree directly or via the `localmail serve` HTTPS API.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and
[docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What we shipped this session

**Issue #28** — encoding toggle / charset detection for `RawBodyView`.

### Files added

- [`gui/src/lib/charset_helpers.ts`](gui/src/lib/charset_helpers.ts)
  — pure module, no DOM / Tauri imports. Public API:
  - `AUTO_CHARSET`, `DEFAULT_CHARSET` — string constants (no magic
    numbers anywhere in the search/dropdown wiring).
  - `SUPPORTED_CHARSETS` — readonly array of `{label, value}` driving
    the dropdown order (`Auto` first so it's the natural default).
  - `parseCharsetFromHeaders(bytes)` — finds the first `Content-Type:`
    header in the RFC822 header block (bytes before the `\r\n\r\n` /
    `\n\n` separator), unfolds RFC 5322 continuation lines, and
    extracts the `charset=` parameter. Quoted (`"…"`, `'…'`),
    whitespace-padded, folded, and case-insensitive variants all
    normalise to lower-case. Inner-MIME-part `Content-Type` headers
    past the body separator are deliberately ignored. Header bytes
    are decoded as Latin-1 (a 1:1 byte→codepoint map) so non-ASCII
    bytes in the header block never throw.
  - `decodeWithLabel(bytes, label)` — wraps `new TextDecoder(label,
    {fatal: false}).decode(bytes)`, falls back to UTF-8 when the
    WebView rejects the label.
  - `resolveCharset(bytes, selected)` — returns `selected` verbatim
    unless it's `AUTO_CHARSET`, in which case it sniffs from headers
    with `DEFAULT_CHARSET` as the last-resort fallback.
- [`gui/src/lib/charset_helpers.test.ts`](gui/src/lib/charset_helpers.test.ts)
  — 23 vitest cases covering header-end detection (CRLF + LF
  variants), case-insensitive header name + parameter, quote
  stripping (double + single), whitespace tolerance around `=`,
  folded headers, MIME-bleed protection, multi-Content-Type pick-the-
  first, empty-quoted-value → null, non-ASCII byte tolerance,
  decoder fallback on bad labels, decoder non-fatality on invalid
  bytes, AUTO sniff + default-fallback resolution.

### Files modified

- [`gui/src/components/RawBodyView.svelte`](gui/src/components/RawBodyView.svelte)
  — bytes are now held in state (was: decoded string), so the
  encoding dropdown can re-decode live without re-fetching. Added
  the dropdown wired to `SUPPORTED_CHARSETS`, default `Auto`, with a
  `(detected: <charset>)` hint that surfaces only when AUTO + a
  charset was actually sniffed (not when AUTO falls back to UTF-8
  default). `decodeWithLabel` + `resolveCharset` drive the
  `$derived` text computation.
- [`gui/src/components/RawBodyView.test.ts`](gui/src/components/RawBodyView.test.ts)
  — kept the two existing cases (load + error) and added four:
  - Dropdown renders with default `auto` after load.
  - Detected-charset hint appears when the message declares a
    charset.
  - No hint when the message declares no charset (regression guard
    against showing `(detected: utf-8)` from the default fallback).
  - **Live re-decode**: Latin-1 body bytes mojibake under default
    UTF-8 (U+FFFD) and decode cleanly when the dropdown is flipped
    to `iso-8859-1` — proves the `$derived` chain reactively
    re-renders without a refetch.

### Verification

```bash
cd gui && npm test
# Test Files  36 passed (36)
#      Tests  303 passed (303)

cd gui && npm run check
# COMPLETED 335 FILES 0 ERRORS 0 WARNINGS
```

Backend Python tests and Rust tests were not touched this session;
no need to re-verify those baselines (no shared surface).

### Docs review

- **README.md** — only mention of "encoding" is the unrelated
  poison-pill-encoding line in the failure-handling section; no
  RawBodyView / charset documentation exists for end users. The
  raw view remains a debug/diagnostic feature, so the dropdown is
  self-explanatory in-app. **Not updated.**
- **ROADMAP.md** — does not exist in this repo.
- **CLAUDE.md** — the helper-module pattern (`*_helpers.ts` under
  `gui/src/lib/`) is already documented from PR #93. No new
  load-bearing invariant to capture.

## What's next

### 1. **Visual smoke for #28** *(needs Tauri dev session — ~5 min)*

The unit tests prove the decoder swap re-renders the `<pre>` live,
but the WebView's actual `TextDecoder` should be eyeballed in the
real app. Cheap and self-contained.

**Acceptance**:
- `npm run tauri dev` launches, no panic.
- Open a UTF-8 message → text renders correctly with
  `(detected: utf-8)` (or no hint if no charset header).
- Open a Latin-1 message → with AUTO + declared charset, decodes
  correctly. Without declared charset → default UTF-8 mojibakes the
  Latin-1 bytes (U+FFFD). Flip the dropdown to `Latin-1` → live
  re-render shows clean text, no refetch.
- Flip back to UTF-8 → mojibake returns, proving the dropdown
  binding is the only thing driving the change.

If no real Latin-1 message is at hand, fabricate one via
`tests/_eml.py` patterns on the backend and resync. Alternatively
the unit test's `[0x63, 0x61, 0x66, 0xe9]` byte pattern is enough
to verify the visual mojibake/un-mojibake flip in isolation.

### 2. **#24 — macOS to gui-ci.yml OS matrix** *(headless-doable; spends CI minutes)*

Unchanged from the prior handoff. Convert `tauri-rust` job to
`strategy.matrix.os: [ubuntu-latest, macos-latest]`. Push as DRAFT
first to validate macOS-minute consumption.

**Acceptance**: gui-ci.yml runs both `ubuntu-latest` +
`macos-latest`; both report SUCCESS on a clean PR.

### 3. **#38 — `/v1/changes` semantics decision** *(needs user input)*

Unchanged — three options in the issue body, needs maintainer
decision before code can move.

### 4. **#87 — CI-gated at-scale folder-filter regression coverage**

Unchanged — substantial work, scale-tuning is empirical.

### Blocked / deferred (unchanged from prior handoff)

- **#90** glib Cargo alert — upstream-blocked.
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#47** Third-party transient classes — needs ops data.
- **#5** Search batch INSERT — deferred until measured.
- **#2** Migration 0006 GIN CONCURRENT — deferred until live-upgrade
  scenario hits.

## Open decisions & risks

1. **Commit / branch / PR shape is up to the maintainer.** The
   work is uncommitted on `main`. The natural shape:
   `feature/gui-charset-toggle-#28` branch with one code commit
   (the four touched files) and a follow-up docs commit (this
   `NEXT_SESSION.md` + the archive snapshot in `docs/handoffs/`).
   The prior session's untracked snapshots
   (`2026-05-25T0522-utc-housekeeping-post-pr-94.md`,
   `2026-05-25T0541-utc-tauri-smoke-issue-91.md`) could be bundled
   into the same docs commit. The session did not commit because
   (a) the user did not explicitly ask, (b) the visual smoke is
   still TODO and may surface an issue worth one more iteration
   before opening a PR.

2. **Detected-charset hint shows the SNIFFED value, not the resolved
   one.** When AUTO is selected and no header charset is declared,
   the `effectiveCharset` (= `DEFAULT_CHARSET = utf-8`) is what
   actually drives the decoder, but the hint is suppressed. This
   was a deliberate choice — saying "(detected: utf-8)" when the
   default kicks in would be misleading. The downside: the user
   has no surfacing of "we fell back to utf-8 because nothing was
   declared". If that ever surfaces as a support issue, swap to
   "(detected: utf-8 — default)" with two distinct branches.

3. **Charset list is fixed at 5 entries.** UTF-8 / Latin-1 /
   Windows-1252 / Shift_JIS covers the 99% case but won't help an
   archive heavy in GB18030, Big5, EUC-JP, or KOI8-R. The
   `decodeWithLabel` helper already falls back gracefully on
   unknown labels (no exceptions), and extending
   `SUPPORTED_CHARSETS` is a single-line addition with no test
   churn — so the cost of waiting for actual user feedback before
   broadening is zero.

4. **`HEADER_SCAN_CAP_BYTES = 65_536` caps pathological inputs.**
   No real-world RFC 5322 header block approaches 64 KiB, but a
   crafted message could. The cap prevents `parseCharsetFromHeaders`
   from doing a 100 MB-scan-per-keystroke on a malicious body. The
   constant is at module scope and unit-test-visible should anyone
   want to tune it.

5. **Carried-forward invariants (still load-bearing)** — same list
   as prior handoffs; not duplicated. Full enumeration in
   [`docs/handoffs/2026-05-25T0541-utc-tauri-smoke-issue-91.md`](docs/handoffs/2026-05-25T0541-utc-tauri-smoke-issue-91.md)
   "Known gotchas". Nothing in that list changed this session.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git status                           # expect: 5 files dirty
                                     #   M  NEXT_SESSION.md
                                     #   ?? .claude/settings.local.json
                                     #   ?? docs/handoffs/2026-05-25T0522-…
                                     #   ?? docs/handoffs/2026-05-25T0541-…
                                     #   ?? docs/handoffs/2026-05-25T0705-…
                                     # plus the #28 work in:
                                     #   M  gui/src/components/RawBodyView.svelte
                                     #   M  gui/src/components/RawBodyView.test.ts
                                     #   ?? gui/src/lib/charset_helpers.ts
                                     #   ?? gui/src/lib/charset_helpers.test.ts

# Re-verify the gates this session pinned:
cd gui && npm test                   # expect 303 passed across 36 files
cd gui && npm run check              # expect 0/0/335

# Visual smoke (the only TODO from this session):
unset VIRTUAL_ENV && uv run localmail serve \
  --bind 127.0.0.1 --port 8443 \
  --tls-cert ~/.config/localmail/tls/cert.pem \
  --tls-key ~/.config/localmail/tls/key.pem
cd gui && npm run tauri dev
# Walk through the acceptance checklist in "What's next §1" above.

# Backend sanity (untouched this session — should still be clean):
unset VIRTUAL_ENV && uv sync
unset VIRTUAL_ENV && LOCALMAIL_TEST_DSN='postgresql://localmail:local%40%40mail@localhost:5532/localmail_test' \
  uv run pytest -q                   # expect 805 passed
unset VIRTUAL_ENV && uv run mypy src/localmail   # 4 pre-existing parser.py errors

# When ready to ship: bundle the worktree into a feature branch and PR.
#   git checkout -b feature/gui-charset-toggle-28
#   git add gui/src/lib/charset_helpers.ts gui/src/lib/charset_helpers.test.ts \
#           gui/src/components/RawBodyView.svelte gui/src/components/RawBodyView.test.ts
#   git commit -m "feat(gui): charset toggle + auto-sniff for RawBodyView (#28)"
#   git add NEXT_SESSION.md docs/handoffs/2026-05-25T*-utc-*.md
#   git commit -m "docs: NEXT_SESSION + archive snapshot for #28"
#   git push -u origin feature/gui-charset-toggle-28
#   gh pr create --fill   # or with the standard summary/test-plan template

gh issue list --state open --limit 40
```

## Known gotchas (still load-bearing)

Same enumeration as the prior handoff
([`docs/handoffs/2026-05-25T0541-utc-tauri-smoke-issue-91.md`](docs/handoffs/2026-05-25T0541-utc-tauri-smoke-issue-91.md)).
Plus newly load-bearing from this session:

- **`charset_helpers` parses only the top-level header block.**
  MIME inner-part `Content-Type` headers (past the `\r\n\r\n` /
  `\n\n` separator) are intentionally ignored. If a multipart
  message has no top-level charset but an inner part declares one,
  AUTO will resolve to the UTF-8 default — flip the dropdown
  manually. Documenting this explicitly because the bleed-into-
  inner-parts case is a tempting "improvement" that would silently
  pick up the wrong part's charset on multipart/mixed where the
  inner parts disagree.
- **`bytes` (Uint8Array) is held in component state.** This is by
  design — the dropdown re-decodes live without re-fetching.
  Memory cost is one raw-message worth of bytes per open RawBodyView
  instance; raw messages are bounded (Postgres `bytea` storage,
  attachments live in the blob tree, not inline), so this is fine.
  Don't refactor back to "store decoded text only" to save bytes —
  it kills the dropdown UX.

## File map (post-session, uncommitted)

```
src/localmail/                              # unchanged this session
migrations/                                 # unchanged
tests/                                      # unchanged
gui/src-tauri/                              # unchanged
gui/src/lib/
  charset_helpers.ts                        # NEW (#28)
  charset_helpers.test.ts                   # NEW (#28; 23 cases)
  format_error.ts / change_helpers.ts / …   # unchanged
gui/src/components/
  RawBodyView.svelte                        # MODIFIED (#28; dropdown + sniff)
  RawBodyView.test.ts                       # MODIFIED (#28; +4 cases)
docs/handoffs/
  2026-05-25T0705-utc-charset-toggle-issue-28.md   # THIS session's snapshot
  2026-05-25T0541-utc-tauri-smoke-issue-91.md      # prior (still untracked)
  2026-05-25T0522-utc-housekeeping-post-pr-94.md   # prior (still untracked)
  2026-05-24T2312-utc-attachment-error-pr-94.md    # prior (committed)
  …                                                # earlier
NEXT_SESSION.md                            # this file (post-session)
```

End of #28 implementation session. **Code complete, tests green
(303 / 0 / 335), visual smoke deferred.** Recommended next-session
opener: ~5 min Tauri-dev visual smoke against the acceptance
checklist in "What's next §1". Open issue count: **8** (after #28
closes via PR).
