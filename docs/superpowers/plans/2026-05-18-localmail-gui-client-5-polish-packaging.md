# localmail GUI Client — Sub-plan 5: Polish + Packaging

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close out the GUI client by (a) wiring the last three server-side search filters (`date_from`, `date_to`, `lang`); (b) adding background change polling, a resizable splitter, a version-mismatch hard modal, a header-unfold widget, a raw RFC822 view, a Settings screen, a search debug pane, and multi-page PDF preview; (c) producing distributable `.dmg` / `.msi` / `.AppImage` bundles with branded icons via `npm run tauri build`. Done when (1) all spec-listed v1 GUI screens are implemented and tested; (2) `npm run tauri build` produces a runnable bundle on macOS and the build steps for Windows/Linux are documented; (3) `_KNOWN_UNSUPPORTED_FILTER_KEYS` on the server is empty.

**Architecture:** Two worktrees, two PRs (same model as Sub-plan 4). **Phase A** (server, `worktree-phase2-hybrid-search`) finishes the filter table: `lang:` DSL token in `parse_query`, `languages` predicate in `_filter_sql`, and removes `date_from` / `date_to` / `lang` from `_KNOWN_UNSUPPORTED_FILTER_KEYS` in `api/search.py` with the corresponding emit logic. **Phase B** (client, `gui-client-5` off `main`) adds 9 components, 4 Rust commands, the mail-store poller, a tab-based Settings screen, and the Tauri bundle config. No new server-side migrations; no new client npm dependencies (PDF.js already shipped in Sub-plan 4 — we add prev/next page controls; the existing `@tauri-apps/plugin-dialog` is the only system-API touch).

**Tech Stack:** Server: Python + psycopg + tsvector + pgvector — no new deps. Client: Svelte 5 runes + TypeScript + vitest + cargo test + Tauri 2. Bundle: `tauri-apps/cli` already pinned in `gui/package.json`; macOS produces `.dmg` via the built-in bundler, Linux `.AppImage` via `appimage` feature flag, Windows `.msi` via `wix`.

**Base branches:**
- Phase A branches off `worktree-phase2-hybrid-search` (existing server-side branch — same as Sub-plan 4 Phase A).
- Phase B branches off `main` (which now includes the merged Sub-plan 4 — PR #21, commit `c768107`).

**Worktree locations (already created at planning time):**
- Phase A: `/Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search` (re-use; already exists).
- Phase B: `/Users/hherb/src/localmail/.claude/worktrees/gui-client-5` (branch `gui-client-5`, created off `main`).

**Out of scope (deferred past v1):**
- SMTP send + compose UI (capability gated; not enabled).
- Threading (separate spec).
- Multi-archive federation.
- SSE / real-time push.
- Per-user account ACL.
- Code signing / notarisation for distributables (bundles ship unsigned in v1; users get a Gatekeeper / SmartScreen prompt).
- Auto-update (not in v1).

**Acknowledged tech debt that lands later:**
- `/v1/folders/{id}/messages` is still not added. The GUI keeps using `/v1/search` with an empty query plus `folder_ids` filter — works end-to-end as of Sub-plan 4 Phase A, just slightly more expensive than a dedicated list endpoint.
- Per-arm score breakdown for the debug pane uses the existing `SearchResult.matched_arms` + `SearchResult.score`. A per-arm score (vs. fused score) is not on the API surface; the debug pane shows the fused score plus the matched-arms list, not individual arm contributions.
- The Settings → Server "change password" flow assumes an existing `/v1/auth/change-password` route. If the server doesn't have one yet, Task B-Settings-Server creates the server route as a sub-task (see B16).

---

## File structure

### Phase A — Server (worktree: `phase2-hybrid-search`)

#### Created

```
tests/test_query_lang_token.py                       # NEW — DSL parser tests for lang:
tests/test_arms_languages_filter.py                  # NEW — _filter_sql languages predicate tests
tests/test_api_search_lang_dates.py                  # NEW — end-to-end positive tests for date_from/date_to/lang
```

#### Modified

```
src/localmail/search/query.py                        # parse_query: add `lang:` token → SearchFilters.languages
src/localmail/search/arms.py                         # _filter_sql: add languages predicate
src/localmail/api/search.py                          # remove date_from/date_to/lang from _KNOWN_UNSUPPORTED_FILTER_KEYS
                                                     # _filter_tokens: emit `after:` / `before:` / `lang:` from those keys
tests/test_query_parser.py                           # extend existing tests with lang: round-trip
tests/test_api_search.py                             # adjust unsupported-keys assertion (now empty)
```

### Phase B — Client (worktree: `gui-client-5`)

#### Created

```
gui/src-tauri/src/commands/
  version.rs                                         # NEW — GET /v1/version wrapper + cmd (for VersionGate)
  raw_message.rs                                     # NEW — GET /v1/messages/{id}/raw → Vec<u8>
  full_headers.rs                                    # NEW — GET /v1/messages/{id}?headers=full → MessageDetail w/ headers
  auth_change_password.rs                            # NEW — POST /v1/auth/change-password wrapper

gui/src/
  lib/api/
    version.ts                                       # NEW — VersionInfo type + getVersion()
    raw_message.ts                                   # NEW — getRawMessage(id): Promise<Uint8Array>
    full_headers.ts                                  # NEW — getMessageFullHeaders(id): Promise<MessageDetail>
    change_password.ts                               # NEW — changePassword(old, new): Promise<void>
  lib/
    splitter.ts                                      # NEW — pure helper: clampPaneWidths, parseStoredWidths, serializeWidths
    splitter.test.ts                                 # vitest
    change_poller.ts                                 # NEW — pure helper: dedupNewMessages, parseCursor
    change_poller.test.ts                            # vitest
    version_check.ts                                 # NEW — pure helper: isMajorCompatible
    version_check.test.ts                            # vitest
    stores/
      settings.svelte.ts                             # NEW — settings singleton (density, lang, page size, debug, image policy)
      settings.test.ts                               # vitest
      version.svelte.ts                              # NEW — version gate state
      version.test.ts                                # vitest
  components/
    Splitter.svelte                                  # NEW — vertical drag handle + width persistence
    Splitter.test.ts                                 # vitest
    VersionGate.svelte                               # NEW — hard modal on api_major mismatch
    VersionGate.test.ts                              # vitest
    HeaderUnfold.svelte                              # NEW — “Show full headers” toggle inside ReadingPane
    HeaderUnfold.test.ts                             # vitest
    RawBodyView.svelte                               # NEW — raw RFC822 in <pre> + copy button
    RawBodyView.test.ts                              # vitest
    DebugBadges.svelte                               # NEW — per-result score + matched_arms chips
    DebugBadges.test.ts                              # vitest
    DebugChunks.svelte                               # NEW — reading-pane chunk listing for debug mode
    DebugChunks.test.ts                              # vitest
  screens/
    SettingsScreen.svelte                            # NEW — overlay screen w/ Server / Display / Search / About tabs
    SettingsScreen.test.ts                           # vitest
    settings/
      SettingsServer.svelte                          # NEW — server URL, username, change password, re-trust cert, log out
      SettingsServer.test.ts
      SettingsDisplay.svelte                         # NEW — density, date format, default HTML-image policy
      SettingsDisplay.test.ts
      SettingsSearch.svelte                          # NEW — page size, default language, debug toggle
      SettingsSearch.test.ts
      SettingsAbout.svelte                           # NEW — versions + “view logs” button
      SettingsAbout.test.ts
gui/src-tauri/icons/                                 # NEW — branded icon set (master + Tauri-generated sizes)
  icon.png                                           # 1024×1024 master PNG
  icon.icns                                          # macOS
  icon.ico                                           # Windows
  32x32.png  64x64.png  128x128.png  128x128@2x.png  # Linux + dev
```

#### Modified

```
gui/src-tauri/src/
  commands/mod.rs                                    # add: pub mod version; pub mod raw_message; pub mod full_headers; pub mod auth_change_password;
  lib.rs                                             # register the four new cmds in generate_handler!
  Cargo.toml                                         # already has tokio/reqwest — no new crates

gui/src-tauri/tauri.conf.json                        # bundle.icon paths + bundle.targets + bundle.identifier check + windows.wix + macOS.minimumSystemVersion

gui/src/screens/MainView.svelte                      # mount Splitter ×2 + VersionGate; start change-poller on mount; stop on unmount
gui/src/screens/AuthShell.svelte                     # mount VersionGate before delegating to MainView
gui/src/components/ReadingPane.svelte                # HeaderUnfold + RawBodyView + DebugChunks (gated by settings.debug)
gui/src/components/MessageList.svelte                # DebugBadges per result row (gated by settings.debug)
gui/src/components/FilterPopover.svelte              # surface date_from / date_to / lang form fields
gui/src/components/AttachmentPreviewModal.svelte     # extend PDF preview with prev/next page controls + page counter

gui/src/lib/api/search.ts                            # SearchFiltersUI: add dateFrom, dateTo, language
gui/src/lib/filter_parse.ts                          # round-trip the three new fields
gui/src/lib/filter_parse.test.ts                     # extend existing tests
gui/src/lib/stores/mail.svelte.ts                    # add: changeCursor, startPolling, stopPolling, mergeNewMessages
gui/src/lib/stores/mail.test.ts                      # extend existing tests with poller + merge

gui/package.json                                     # bump only if a missing peer dep is found; otherwise unchanged
```

#### File-size pressure

`ReadingPane.svelte` is at 172 LOC pre-Sub-plan-5. After adding `HeaderUnfold` + `RawBodyView` + `DebugChunks`, it stays under 350 LOC because the new pieces are imported components, not inline markup. `MainView.svelte` stays under 250 LOC. If any file approaches 500 LOC during execution, split before continuing.

---

## Phase A — Server

> Worktree: `/Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search`
> Branch: `worktree-phase2-hybrid-search` (long-lived). Push to it directly; no PR (same pattern as Sub-plan 4 Phase A).

### Task A1: `lang:` DSL token + `SearchFilters.languages` predicate

**Files:**
- Modify: `src/localmail/search/query.py`
- Modify: `src/localmail/search/arms.py`
- Create: `tests/test_query_lang_token.py`
- Create: `tests/test_arms_languages_filter.py`

- [ ] **Step 1: Write the failing parser test**

`tests/test_query_lang_token.py`:

```python
"""DSL parser tests for the `lang:` token.

`lang:en` populates SearchFilters.languages as a list; multiple tokens append.
The value is lowercased and stripped — case-insensitive matching downstream.
"""
from __future__ import annotations

import pytest

from localmail.search.query import parse_query, QueryParseError


def test_single_lang_token():
    parsed = parse_query("invoice lang:en")
    assert parsed.free_text == "invoice"
    assert parsed.filters.languages == ["en"]


def test_multiple_lang_tokens_accumulate():
    parsed = parse_query("lang:de lang:en")
    assert parsed.free_text == ""
    assert parsed.filters.languages == ["de", "en"]


def test_lang_token_is_lowercased():
    parsed = parse_query("lang:EN")
    assert parsed.filters.languages == ["en"]


def test_lang_token_empty_value_raises():
    with pytest.raises(QueryParseError):
        parse_query("lang:")


def test_lang_token_strips_whitespace_in_quoted_value():
    parsed = parse_query('lang:" en "')
    assert parsed.filters.languages == ["en"]
```

- [ ] **Step 2: Run and verify failure**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search
unset VIRTUAL_ENV && uv run pytest tests/test_query_lang_token.py -v
```
Expected: 5 failures — `lang` is not in `_OPERATORS` yet.

- [ ] **Step 3: Add `lang` to operators + populate filters**

In `src/localmail/search/query.py`:

```python
_OPERATORS = {
    "from", "to", "subject", "after", "before", "has", "label",
    "account", "folder", "account_id", "folder_id", "lang",
}
```

Then in `parse_query`, after `f_folder_ids` declaration add `f_languages: list[str] = []` and handle the token. Find the block that handles `op_l == "folder_id"` and add right after it:

```python
            elif op_l == "lang":
                normalized = value.strip().lower()
                if not normalized:
                    raise QueryParseError(f"lang: empty value not allowed")
                f_languages.append(normalized)
```

And in the `SearchFilters(...)` construction at the bottom of `parse_query`, add `languages=f_languages or None,`.

- [ ] **Step 4: Run and verify the parser tests pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_query_lang_token.py -v
```
Expected: 5 PASS.

- [ ] **Step 5: Write the failing `_filter_sql` test**

`tests/test_arms_languages_filter.py`:

```python
"""_filter_sql languages predicate — only messages whose primary language
matches the supplied list are kept.

Per Phase 1 + 2 design, `messages.body_lang` (text, nullable) is the column
populated by the embed worker. The predicate is `m.body_lang = ANY(%s)`.
Messages with NULL body_lang are excluded when a filter is set — opt-in.
"""
from __future__ import annotations

from datetime import datetime

import psycopg
import pytest

from localmail.search.arms import _filter_sql
from localmail.search.query import SearchFilters


def test_languages_filter_emits_predicate():
    where, params = _filter_sql(SearchFilters(languages=["en", "de"]))
    assert "m.body_lang = ANY(" in where
    assert ["en", "de"] in params


def test_no_languages_filter_emits_nothing():
    where, params = _filter_sql(SearchFilters())
    assert "body_lang" not in where


def test_languages_with_other_filters_combine():
    where, params = _filter_sql(SearchFilters(languages=["en"], from_substr="alice"))
    assert "m.body_lang = ANY(" in where
    assert "m.from_addr ILIKE" in where or "m.from_name ILIKE" in where
```

Note: if `messages.body_lang` does not exist as a column yet, the test for `_filter_sql` will still pass at the string level — but a real query will then fail. The engineer must verify with `\d messages` against `localmail_test` that the column exists; if not, fall back to filtering by `chunks.lang` joined through `message_chunks` — the chunking step populates `lang` per chunk. The fallback SQL is documented in step 6b.

- [ ] **Step 6a: Inspect the actual schema**

```bash
unset VIRTUAL_ENV && uv run python -c "
import os, psycopg
dsn = os.environ.get('LOCALMAIL_TEST_DSN', 'postgresql:///localmail_test')
with psycopg.connect(dsn) as conn, conn.cursor() as cur:
    cur.execute('SELECT column_name FROM information_schema.columns WHERE table_name = %s', ('messages',))
    print(sorted(r[0] for r in cur.fetchall()))
"
```
Expected: a column list. Look for `body_lang`. If present, proceed with step 6b. If absent, look for `lang` on `message_chunks` (or similar); the predicate will instead be `EXISTS (SELECT 1 FROM message_chunks mc WHERE mc.message_id = m.id AND mc.lang = ANY(%s))`.

- [ ] **Step 6b: Implement the languages predicate**

In `src/localmail/search/arms.py`, locate `_filter_sql` and add the languages handler near other field predicates. If `messages.body_lang` exists:

```python
    if f.languages:
        where_parts.append("m.body_lang = ANY(%s)")
        params.append(list(f.languages))
```

If only `message_chunks.lang` exists (fallback):

```python
    if f.languages:
        where_parts.append(
            "EXISTS (SELECT 1 FROM message_chunks mc "
            "WHERE mc.message_id = m.id AND mc.lang = ANY(%s))"
        )
        params.append(list(f.languages))
```

Pick whichever the schema supports. Document the choice in a one-line comment immediately above the block (the WHY is non-obvious — it's a schema-pinning decision).

- [ ] **Step 7: Run and verify the filter tests pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_arms_languages_filter.py -v
```
Expected: 3 PASS.

- [ ] **Step 8: Run the full search test suite**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_query_parser.py tests/test_arms.py tests/test_arms_id_filters.py tests/test_query_account_folder_id_tokens.py -v
```
Expected: all pass, no regressions.

- [ ] **Step 9: Commit**

```bash
git add src/localmail/search/query.py src/localmail/search/arms.py \
        tests/test_query_lang_token.py tests/test_arms_languages_filter.py
git commit -m "feat(search): lang: DSL token + languages predicate in _filter_sql"
```

### Task A2: Forward `date_from` / `date_to` / `lang` from API → DSL; clear unsupported list

**Files:**
- Modify: `src/localmail/api/search.py`
- Create: `tests/test_api_search_lang_dates.py`
- Modify: `tests/test_api_search.py`

- [ ] **Step 1: Write the failing positive test**

`tests/test_api_search_lang_dates.py`:

```python
"""End-to-end: the three formerly-unsupported keys round-trip through
`build_query_string` into the right DSL tokens."""
from __future__ import annotations

import pytest

from localmail.api.search import build_query_string


def test_date_from_emits_after_token():
    q = build_query_string(free_text="", filters={"date_from": "2024-01-15"})
    assert "after:2024-01-15" in q


def test_date_to_emits_before_token():
    q = build_query_string(free_text="", filters={"date_to": "2024-12-31"})
    assert "before:2024-12-31" in q


def test_lang_single_emits_lang_token():
    q = build_query_string(free_text="", filters={"lang": "en"})
    assert "lang:en" in q


def test_lang_list_emits_one_token_per_value():
    q = build_query_string(free_text="", filters={"lang": ["en", "de"]})
    assert "lang:en" in q
    assert "lang:de" in q


def test_invalid_date_from_raises():
    from localmail.api.errors import ValidationFailed
    with pytest.raises(ValidationFailed):
        build_query_string(free_text="", filters={"date_from": "2024/01/01"})


def test_invalid_lang_raises():
    from localmail.api.errors import ValidationFailed
    with pytest.raises(ValidationFailed):
        build_query_string(free_text="", filters={"lang": ""})
    with pytest.raises(ValidationFailed):
        build_query_string(free_text="", filters={"lang": ["en", ""]})
```

- [ ] **Step 2: Run and verify failure**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search
unset VIRTUAL_ENV && uv run pytest tests/test_api_search_lang_dates.py -v
```
Expected: all 6 fail — the keys are still in `_KNOWN_UNSUPPORTED_FILTER_KEYS`.

- [ ] **Step 3: Remove from unsupported + add to supported + emit tokens**

In `src/localmail/api/search.py`:

```python
_SUPPORTED_FILTER_KEYS = frozenset({
    "from", "to", "subject", "after", "before", "has_attachment",
    "account_ids", "folder_ids",
    "date_from", "date_to", "lang",
})

_KNOWN_UNSUPPORTED_FILTER_KEYS: frozenset[str] = frozenset()
```

Then in `_filter_tokens`, add — after the existing `before` block:

```python
    if (v := filters.get("date_from")):
        _validate_date(v, "date_from")
        out.append(f"after:{v}")
    if (v := filters.get("date_to")):
        _validate_date(v, "date_to")
        out.append(f"before:{v}")
    if (v := filters.get("lang")) not in (None, "", []):
        values = v if isinstance(v, list) else [v]
        for one in values:
            s = str(one).strip().lower()
            if not s:
                raise ValidationFailed("lang: empty value not allowed")
            out.append(f"lang:{s}")
```

- [ ] **Step 4: Run and verify positive tests pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_search_lang_dates.py -v
```
Expected: 6 PASS.

- [ ] **Step 5: Update the unsupported-key assertion**

In `tests/test_api_search.py`, find the test that asserts `_KNOWN_UNSUPPORTED_FILTER_KEYS == {...}` (or that supplying `date_from` raises `ValidationFailed`) and update it. The new assertion should be that the set is empty and that `_SUPPORTED_FILTER_KEYS` now includes the three keys. If the existing test sends `date_from` expecting a 400 — flip it to expect 200 with the corresponding DSL token in the constructed query.

- [ ] **Step 6: Run the whole API test module**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_search.py tests/test_serve_search_route.py -v
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/localmail/api/search.py tests/test_api_search.py tests/test_api_search_lang_dates.py
git commit -m "feat(api): forward date_from/date_to/lang filters; clear unsupported list"
```

- [ ] **Step 8: Push the integration branch**

```bash
git push origin worktree-phase2-hybrid-search
```

---

## Phase B — Client

> Worktree: `/Users/hherb/src/localmail/.claude/worktrees/gui-client-5`
> Branch: `gui-client-5` off `main`. PR target: `main`.

All `cd` paths below assume `cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-5/gui` unless otherwise stated.

### Task B1: Splitter helper module (pure)

**Files:**
- Create: `gui/src/lib/splitter.ts`
- Create: `gui/src/lib/splitter.test.ts`

- [ ] **Step 1: Write the failing tests**

`gui/src/lib/splitter.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import {
  clampPaneWidths,
  parseStoredWidths,
  serializeWidths,
  DEFAULT_LEFT_WIDTH_PX,
  DEFAULT_MIDDLE_WIDTH_PX,
  MIN_PANE_WIDTH_PX,
} from "./splitter";

describe("clampPaneWidths", () => {
  it("returns defaults when total < 3 * MIN", () => {
    const out = clampPaneWidths(
      { left: 220, middle: 340 },
      { containerWidth: MIN_PANE_WIDTH_PX * 2 },
    );
    expect(out.left).toBe(DEFAULT_LEFT_WIDTH_PX);
    expect(out.middle).toBe(DEFAULT_MIDDLE_WIDTH_PX);
  });

  it("clamps left to >= MIN", () => {
    const out = clampPaneWidths({ left: 10, middle: 340 }, { containerWidth: 1200 });
    expect(out.left).toBeGreaterThanOrEqual(MIN_PANE_WIDTH_PX);
  });

  it("clamps middle so that right pane is >= MIN", () => {
    const out = clampPaneWidths({ left: 200, middle: 9000 }, { containerWidth: 1200 });
    expect(1200 - out.left - out.middle).toBeGreaterThanOrEqual(MIN_PANE_WIDTH_PX);
  });

  it("passes valid widths through unchanged", () => {
    const out = clampPaneWidths({ left: 240, middle: 360 }, { containerWidth: 1200 });
    expect(out.left).toBe(240);
    expect(out.middle).toBe(360);
  });
});

describe("parseStoredWidths / serializeWidths round-trip", () => {
  it("serializes and deserializes a config", () => {
    const cfg = { left: 240, middle: 360 };
    const s = serializeWidths(cfg);
    expect(parseStoredWidths(s)).toEqual(cfg);
  });

  it("returns null on invalid JSON", () => {
    expect(parseStoredWidths("not json")).toBeNull();
  });

  it("returns null on missing keys", () => {
    expect(parseStoredWidths(JSON.stringify({ left: 100 }))).toBeNull();
  });

  it("returns null on non-finite numbers", () => {
    expect(parseStoredWidths(JSON.stringify({ left: NaN, middle: 100 }))).toBeNull();
    expect(parseStoredWidths(JSON.stringify({ left: 100, middle: Infinity }))).toBeNull();
  });
});
```

- [ ] **Step 2: Run, expect failure**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-5/gui
npm test -- --run src/lib/splitter.test.ts
```
Expected: file not found error.

- [ ] **Step 3: Implement**

`gui/src/lib/splitter.ts`:

```ts
export const MIN_PANE_WIDTH_PX = 160;
export const DEFAULT_LEFT_WIDTH_PX = 220;
export const DEFAULT_MIDDLE_WIDTH_PX = 340;

export interface PaneWidths {
  left: number;
  middle: number;
}

export interface ClampContext {
  containerWidth: number;
}

export function clampPaneWidths(input: PaneWidths, ctx: ClampContext): PaneWidths {
  if (ctx.containerWidth < MIN_PANE_WIDTH_PX * 3) {
    return { left: DEFAULT_LEFT_WIDTH_PX, middle: DEFAULT_MIDDLE_WIDTH_PX };
  }
  const left = Math.max(MIN_PANE_WIDTH_PX, Math.min(input.left, ctx.containerWidth - 2 * MIN_PANE_WIDTH_PX));
  const middleMax = ctx.containerWidth - left - MIN_PANE_WIDTH_PX;
  const middle = Math.max(MIN_PANE_WIDTH_PX, Math.min(input.middle, middleMax));
  return { left, middle };
}

export function serializeWidths(w: PaneWidths): string {
  return JSON.stringify({ left: w.left, middle: w.middle });
}

export function parseStoredWidths(raw: string): PaneWidths | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object") return null;
  const obj = parsed as Record<string, unknown>;
  const left = obj.left;
  const middle = obj.middle;
  if (typeof left !== "number" || !Number.isFinite(left)) return null;
  if (typeof middle !== "number" || !Number.isFinite(middle)) return null;
  return { left, middle };
}
```

- [ ] **Step 4: Tests pass**

```bash
npm test -- --run src/lib/splitter.test.ts
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add gui/src/lib/splitter.ts gui/src/lib/splitter.test.ts
git commit -m "feat(gui-client): pure splitter helpers with clamping + storage round-trip"
```

### Task B2: `Splitter.svelte` component

**Files:**
- Create: `gui/src/components/Splitter.svelte`
- Create: `gui/src/components/Splitter.test.ts`

- [ ] **Step 1: Failing component test**

`gui/src/components/Splitter.test.ts`:

```ts
import { render, fireEvent } from "@testing-library/svelte";
import { describe, it, expect, vi } from "vitest";
import Splitter from "./Splitter.svelte";

describe("Splitter", () => {
  it("renders a vertical drag handle with role=separator", () => {
    const { getByRole } = render(Splitter, { props: { onResize: vi.fn() } });
    expect(getByRole("separator")).toBeTruthy();
  });

  it("calls onResize with deltaX on pointer drag", async () => {
    const onResize = vi.fn();
    const { getByRole } = render(Splitter, { props: { onResize } });
    const handle = getByRole("separator");
    await fireEvent.pointerDown(handle, { clientX: 100, pointerId: 1 });
    await fireEvent.pointerMove(window, { clientX: 130, pointerId: 1 });
    await fireEvent.pointerUp(window, { clientX: 130, pointerId: 1 });
    expect(onResize).toHaveBeenCalled();
    const totalDelta = onResize.mock.calls.reduce((sum, [d]) => sum + d, 0);
    expect(totalDelta).toBe(30);
  });

  it("ignores pointer events when disabled", async () => {
    const onResize = vi.fn();
    const { getByRole } = render(Splitter, { props: { onResize, disabled: true } });
    const handle = getByRole("separator");
    await fireEvent.pointerDown(handle, { clientX: 100, pointerId: 1 });
    await fireEvent.pointerMove(window, { clientX: 130, pointerId: 1 });
    await fireEvent.pointerUp(window, { clientX: 130, pointerId: 1 });
    expect(onResize).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run, expect failure**

```bash
npm test -- --run src/components/Splitter.test.ts
```

- [ ] **Step 3: Implement**

`gui/src/components/Splitter.svelte`:

```svelte
<script lang="ts">
  /**
   * Vertical drag handle between two flex children. Emits cumulative
   * pointer-move deltaX through `onResize`. The parent owns the width state
   * — Splitter is stateless apart from drag-in-progress tracking, which
   * isolates pointer capture to the lifetime of one gesture.
   */
  interface Props {
    onResize: (deltaX: number) => void;
    disabled?: boolean;
  }
  let { onResize, disabled = false }: Props = $props();

  let dragging: boolean = $state(false);
  let lastX: number = $state(0);
  let pointerId: number | null = $state(null);

  function onPointerDown(e: PointerEvent): void {
    if (disabled) return;
    dragging = true;
    lastX = e.clientX;
    pointerId = e.pointerId;
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
  }

  function onPointerMove(e: PointerEvent): void {
    if (!dragging || e.pointerId !== pointerId) return;
    const dx = e.clientX - lastX;
    lastX = e.clientX;
    onResize(dx);
  }

  function onPointerUp(e: PointerEvent): void {
    if (e.pointerId !== pointerId) return;
    dragging = false;
    pointerId = null;
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", onPointerUp);
  }
</script>

<div
  class="splitter"
  class:dragging
  class:disabled
  role="separator"
  aria-orientation="vertical"
  tabindex="-1"
  onpointerdown={onPointerDown}
></div>

<style>
  .splitter {
    width: 6px;
    cursor: col-resize;
    background: transparent;
    flex: 0 0 auto;
  }
  .splitter:hover,
  .splitter.dragging {
    background: rgba(0, 0, 0, 0.08);
  }
  .splitter.disabled {
    cursor: default;
    pointer-events: none;
  }
</style>
```

- [ ] **Step 4: Tests pass**

```bash
npm test -- --run src/components/Splitter.test.ts
```

- [ ] **Step 5: Commit**

```bash
git add gui/src/components/Splitter.svelte gui/src/components/Splitter.test.ts
git commit -m "feat(gui-client): Splitter.svelte — vertical drag handle"
```

### Task B3: Mount splitters in `MainView.svelte` with localStorage persistence

**Files:**
- Modify: `gui/src/screens/MainView.svelte`

- [ ] **Step 1: Add pane-width state + handlers + Splitter mounts**

Edit `gui/src/screens/MainView.svelte`. Inside the `<script lang="ts">` block:

```ts
  import {
    DEFAULT_LEFT_WIDTH_PX,
    DEFAULT_MIDDLE_WIDTH_PX,
    clampPaneWidths,
    parseStoredWidths,
    serializeWidths,
    type PaneWidths,
  } from "../lib/splitter";
  import Splitter from "../components/Splitter.svelte";

  const PANE_WIDTHS_KEY = "localmail.gui.paneWidths";

  let widths: PaneWidths = $state(loadInitialWidths());
  let containerWidth: number = $state(window.innerWidth);

  function loadInitialWidths(): PaneWidths {
    const raw = window.localStorage.getItem(PANE_WIDTHS_KEY);
    if (raw === null) return { left: DEFAULT_LEFT_WIDTH_PX, middle: DEFAULT_MIDDLE_WIDTH_PX };
    return parseStoredWidths(raw) ?? { left: DEFAULT_LEFT_WIDTH_PX, middle: DEFAULT_MIDDLE_WIDTH_PX };
  }

  function persistWidths(w: PaneWidths): void {
    window.localStorage.setItem(PANE_WIDTHS_KEY, serializeWidths(w));
  }

  function onLeftResize(dx: number): void {
    const next = clampPaneWidths({ left: widths.left + dx, middle: widths.middle }, { containerWidth });
    widths = next;
    persistWidths(next);
  }

  function onMiddleResize(dx: number): void {
    const next = clampPaneWidths({ left: widths.left, middle: widths.middle + dx }, { containerWidth });
    widths = next;
    persistWidths(next);
  }

  function onWindowResize(): void {
    containerWidth = window.innerWidth;
    const clamped = clampPaneWidths(widths, { containerWidth });
    if (clamped.left !== widths.left || clamped.middle !== widths.middle) {
      widths = clamped;
      persistWidths(clamped);
    }
  }
```

In `onMount` (existing — extend it):

```ts
  onMount(async () => {
    window.addEventListener("resize", onWindowResize);
    await Promise.all([mail.loadAccounts(), mail.loadRecentMessages()]);
    return () => window.removeEventListener("resize", onWindowResize);
  });
```

In the template, replace the `<main class="panes">…</main>` block with:

```svelte
    <main class="panes" style="grid-template-columns: {widths.left}px auto {widths.middle}px auto 1fr;">
      <AccountTree />
      <Splitter onResize={onLeftResize} />
      <MessageList />
      <Splitter onResize={onMiddleResize} />
      <ReadingPane />
    </main>
```

And in the `<style>` block, remove any fixed `grid-template-columns` on `.panes` — it's now inline-driven.

- [ ] **Step 2: Manual smoke**

```bash
npm run check && npm test -- --run
```
Expected: 0 type errors; all component tests pass.

- [ ] **Step 3: Commit**

```bash
git add gui/src/screens/MainView.svelte
git commit -m "feat(gui-client): mount Splitter ×2 + localStorage-persisted pane widths"
```

### Task B4: Change-poller pure helper

**Files:**
- Create: `gui/src/lib/change_poller.ts`
- Create: `gui/src/lib/change_poller.test.ts`

- [ ] **Step 1: Failing tests**

`gui/src/lib/change_poller.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { dedupNewMessages, parseCursor, POLL_INTERVAL_MS } from "./change_poller";
import type { MessageSummary } from "./api/changes";

function ms(id: string): MessageSummary {
  return {
    message_id: id,
    subject: null,
    from: { address: null, name: null },
    to: [],
    date: null,
    account: { id: "1", name: null },
    folder: null,
    snippet_html: null,
    has_attachments: false,
  } as unknown as MessageSummary;
}

describe("dedupNewMessages", () => {
  it("returns input untouched when nothing overlaps", () => {
    const existing = [ms("1"), ms("2")];
    const incoming = [ms("3"), ms("4")];
    expect(dedupNewMessages(existing, incoming)).toEqual(incoming);
  });

  it("filters out messages already present", () => {
    const existing = [ms("1"), ms("2")];
    const incoming = [ms("2"), ms("3")];
    expect(dedupNewMessages(existing, incoming)).toEqual([ms("3")]);
  });

  it("returns [] when all incoming are duplicates", () => {
    const existing = [ms("1"), ms("2")];
    const incoming = [ms("1"), ms("2")];
    expect(dedupNewMessages(existing, incoming)).toEqual([]);
  });
});

describe("parseCursor", () => {
  it("treats null as no cursor", () => expect(parseCursor(null)).toBeNull());
  it("treats empty string as no cursor", () => expect(parseCursor("")).toBeNull());
  it("preserves a numeric string", () => expect(parseCursor("12345")).toBe("12345"));
});

describe("constants", () => {
  it("polls every 30s by default", () => expect(POLL_INTERVAL_MS).toBe(30000));
});
```

- [ ] **Step 2: Run, expect failure**

```bash
npm test -- --run src/lib/change_poller.test.ts
```

- [ ] **Step 3: Implement**

`gui/src/lib/change_poller.ts`:

```ts
import type { MessageSummary } from "./api/changes";

export const POLL_INTERVAL_MS = 30_000;

export function dedupNewMessages(
  existing: readonly MessageSummary[],
  incoming: readonly MessageSummary[],
): MessageSummary[] {
  const seen = new Set(existing.map((m) => m.message_id));
  return incoming.filter((m) => !seen.has(m.message_id));
}

export function parseCursor(raw: string | null): string | null {
  if (raw === null) return null;
  if (raw.trim() === "") return null;
  return raw;
}
```

Note: this imports `MessageSummary` from `./api/changes`. If `api/changes.ts` does not exist as a separate module yet, define `MessageSummary` in `gui/src/lib/api/changes.ts` as a re-export from the existing location (look for it in `gui/src/lib/api/` or, failing that, in `gui/src/lib/stores/mail.svelte.ts`'s imports). Move the type if it isn't already in a dedicated module — the goal is a stable import path.

- [ ] **Step 4: Tests pass**

```bash
npm test -- --run src/lib/change_poller.test.ts
```

- [ ] **Step 5: Commit**

```bash
git add gui/src/lib/change_poller.ts gui/src/lib/change_poller.test.ts gui/src/lib/api/changes.ts
git commit -m "feat(gui-client): change-poller pure helpers + dedup logic"
```

### Task B5: Wire change polling into `mail` store

**Files:**
- Modify: `gui/src/lib/stores/mail.svelte.ts`
- Modify: `gui/src/lib/stores/mail.test.ts` (or create if missing)

- [ ] **Step 1: Extend the store with polling state**

In `gui/src/lib/stores/mail.svelte.ts`:

```ts
  // … existing state …
  private changeCursor: string | null = null;
  private pollHandle: number | null = null;

  startPolling(): void {
    if (this.pollHandle !== null) return;
    this.pollHandle = window.setInterval(() => { void this.pollOnce(); }, POLL_INTERVAL_MS);
  }

  stopPolling(): void {
    if (this.pollHandle !== null) {
      window.clearInterval(this.pollHandle);
      this.pollHandle = null;
    }
  }

  async pollOnce(): Promise<void> {
    try {
      const resp = await listRecentMessages(this.changeCursor);
      this.changeCursor = parseCursor(resp.next_cursor) ?? this.changeCursor;
      const fresh = dedupNewMessages(this.#state.messages, resp.new_messages);
      if (fresh.length > 0) {
        this.#state.messages = [...fresh, ...this.#state.messages];
      }
    } catch (err: unknown) {
      this.#state.errorMessage = formatError(err);
    }
  }
```

Add the imports at the top of the file:

```ts
import { POLL_INTERVAL_MS, dedupNewMessages, parseCursor } from "../change_poller";
```

And update `listRecentMessages` to accept an optional `since` cursor:

```ts
// in gui/src/lib/api/changes.ts (or wherever the wrapper lives):
export async function listRecentMessages(since: string | null = null): Promise<ChangesResponse> {
  return invoke<ChangesResponse>("list_recent_messages_cmd", { since });
}
```

- [ ] **Step 2: Extend the Rust command to accept `since`**

Edit `gui/src-tauri/src/commands/changes.rs`:

```rust
pub async fn list_recent_messages(
    store: &KeyringStore,
    since: Option<&str>,
) -> Result<ChangesResponse, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = match since {
        Some(c) if !c.is_empty() => format!("{url}v1/changes?since={c}"),
        _ => format!("{url}v1/changes"),
    };
    let resp: ChangesResponse = http_get_json(&client, &endpoint, Some(&token)).await?;
    Ok(resp)
}

#[tauri::command]
pub async fn list_recent_messages_cmd(since: Option<String>) -> Result<ChangesResponse, AuthError> {
    let store = KeyringStore::new();
    list_recent_messages(&store, since.as_deref()).await
}
```

Update existing tests in `changes.rs` to pass `None` for the new `since` arg.

- [ ] **Step 3: Add a store test for polling**

`gui/src/lib/stores/mail.test.ts` — add or create:

```ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import { mail } from "./mail.svelte";

describe("mail.pollOnce", () => {
  beforeEach(() => { mail.reset(); });

  it("prepends fresh messages and drops duplicates", async () => {
    const a = { message_id: "1", subject: "A", from: { address: null, name: null }, to: [], date: null, account: { id: "1", name: null }, folder: null, snippet_html: null, has_attachments: false };
    const b = { message_id: "2", subject: "B", from: { address: null, name: null }, to: [], date: null, account: { id: "1", name: null }, folder: null, snippet_html: null, has_attachments: false };
    const c = { message_id: "3", subject: "C", from: { address: null, name: null }, to: [], date: null, account: { id: "1", name: null }, folder: null, snippet_html: null, has_attachments: false };
    mail.setMessagesForTest([a]);
    vi.spyOn(mail, "listRecentMessagesForTest").mockResolvedValue({ new_messages: [a, b, c], next_cursor: "10" });
    await mail.pollOnce();
    const seen = mail.snapshot.messages.map((m) => m.message_id);
    expect(seen).toEqual(["3", "2", "1"]);
  });
});
```

For the test to be possible, expose two test-only seams on the store (add to `mail.svelte.ts`):

```ts
  setMessagesForTest(list: MessageSummary[]): void { this.#state.messages = list; }
  async listRecentMessagesForTest(since: string | null): Promise<ChangesResponse> { return listRecentMessages(since); }
```

The `pollOnce` body must call `this.listRecentMessagesForTest(this.changeCursor)` instead of `listRecentMessages` directly so the spy intercepts. (Yes, this is a test-only seam — comment its existence inline with `// WHY: vitest spies on instance methods, not module-level functions.`)

- [ ] **Step 4: Run all store + cargo tests**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-5/gui
npm test -- --run && (cd src-tauri && cargo test)
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add gui/src/lib/stores/mail.svelte.ts gui/src/lib/stores/mail.test.ts \
        gui/src/lib/api/changes.ts gui/src-tauri/src/commands/changes.rs
git commit -m "feat(gui-client): poll /v1/changes since cursor + dedup-merge new messages"
```

### Task B6: Mount polling in `MainView.svelte`

**Files:**
- Modify: `gui/src/screens/MainView.svelte`

- [ ] **Step 1: Start polling on mount, stop on unmount**

Edit `onMount` in MainView:

```ts
  onMount(async () => {
    window.addEventListener("resize", onWindowResize);
    await Promise.all([mail.loadAccounts(), mail.loadRecentMessages()]);
    mail.startPolling();
    return () => {
      mail.stopPolling();
      window.removeEventListener("resize", onWindowResize);
    };
  });
```

Also ensure `onLogout` calls `mail.stopPolling()` before `mail.reset()`.

- [ ] **Step 2: Commit**

```bash
git add gui/src/screens/MainView.svelte
git commit -m "feat(gui-client): start change-poller on MainView mount; stop on unmount/logout"
```

### Task B7: Version-gate helpers + store + component

**Files:**
- Create: `gui/src-tauri/src/commands/version.rs`
- Create: `gui/src/lib/api/version.ts`
- Create: `gui/src/lib/version_check.ts`
- Create: `gui/src/lib/version_check.test.ts`
- Create: `gui/src/lib/stores/version.svelte.ts`
- Create: `gui/src/lib/stores/version.test.ts`
- Create: `gui/src/components/VersionGate.svelte`
- Create: `gui/src/components/VersionGate.test.ts`
- Modify: `gui/src-tauri/src/commands/mod.rs`, `gui/src-tauri/src/lib.rs`

The client expects `api_major === 1`. Mismatch shows a modal: "This client is incompatible with the server. Quit and update one of them." Single `[Quit]` button.

- [ ] **Step 1: Failing version_check tests**

`gui/src/lib/version_check.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { isMajorCompatible, EXPECTED_API_MAJOR } from "./version_check";

describe("isMajorCompatible", () => {
  it("returns true when major matches", () => {
    expect(isMajorCompatible({ api_major: EXPECTED_API_MAJOR, api_minor: 0 })).toBe(true);
  });
  it("returns false when major differs", () => {
    expect(isMajorCompatible({ api_major: EXPECTED_API_MAJOR + 1, api_minor: 0 })).toBe(false);
    expect(isMajorCompatible({ api_major: EXPECTED_API_MAJOR - 1, api_minor: 0 })).toBe(false);
  });
});
```

- [ ] **Step 2: Implement helper**

`gui/src/lib/version_check.ts`:

```ts
export const EXPECTED_API_MAJOR = 1;

export interface VersionInfo {
  api_major: number;
  api_minor: number;
}

export function isMajorCompatible(v: VersionInfo): boolean {
  return v.api_major === EXPECTED_API_MAJOR;
}
```

- [ ] **Step 3: Tests pass**

```bash
npm test -- --run src/lib/version_check.test.ts
```

- [ ] **Step 4: Rust command for `/v1/version` (no auth)**

`gui/src-tauri/src/commands/version.rs`:

```rust
//! GET /v1/version (unauthenticated). Used by VersionGate at startup and
//! optionally on poll-response error handling.

use serde::{Deserialize, Serialize};

use crate::commands::auth::AuthError;
use crate::commands::session::read_server_pin;
use crate::http::client::{build_pinned_client, http_get_json};
use crate::storage::keyring::KeyringStore;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct VersionInfo {
    pub api_major: u32,
    pub api_minor: u32,
    pub server_version: Option<String>,
    pub build_hash: Option<String>,
}

pub async fn get_version(store: &KeyringStore) -> Result<VersionInfo, AuthError> {
    let (url, pin) = read_server_pin(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/version");
    let resp: VersionInfo = http_get_json(&client, &endpoint, None).await?;
    Ok(resp)
}

#[tauri::command]
pub async fn get_version_cmd() -> Result<VersionInfo, AuthError> {
    let store = KeyringStore::new();
    get_version(&store).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::MemKeyring;

    fn fake_store() -> KeyringStore {
        KeyringStore::with_backend(MemKeyring::new())
    }

    #[tokio::test]
    async fn returns_not_connected_without_server_url() {
        let store = fake_store();
        let err = get_version(&store).await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }
}
```

Add `pub fn read_server_pin(store: &KeyringStore) -> Result<(String, String), AuthError>` in `gui/src-tauri/src/commands/session.rs` if it doesn't exist (returns server URL + cert pin without requiring a token):

```rust
pub fn read_server_pin(store: &KeyringStore) -> Result<(String, String), AuthError> {
    let url = store.get(Slot::ServerUrl)?.ok_or(AuthError::NotConnected)?;
    let pin = store.get(Slot::CertPin)?.ok_or(AuthError::NotConnected)?;
    Ok((url, pin))
}
```

Register the command in `gui/src-tauri/src/commands/mod.rs` (add `pub mod version;`) and in `gui/src-tauri/src/lib.rs`'s `tauri::generate_handler!` list (add `version::get_version_cmd`).

- [ ] **Step 5: TS wrapper**

`gui/src/lib/api/version.ts`:

```ts
import { invoke } from "@tauri-apps/api/core";
import type { VersionInfo as VersionShape } from "../version_check";

export interface ServerVersionInfo extends VersionShape {
  server_version: string | null;
  build_hash: string | null;
}

export async function getVersion(): Promise<ServerVersionInfo> {
  return invoke<ServerVersionInfo>("get_version_cmd");
}
```

- [ ] **Step 6: Version store**

`gui/src/lib/stores/version.svelte.ts`:

```ts
import { getVersion, type ServerVersionInfo } from "../api/version";
import { isMajorCompatible } from "../version_check";

interface VersionState {
  info: ServerVersionInfo | null;
  compatible: boolean | null;
  errorMessage: string | null;
  checking: boolean;
}

class VersionStore {
  #state: VersionState = $state({ info: null, compatible: null, errorMessage: null, checking: false });

  get snapshot(): VersionState { return this.#state; }

  async check(): Promise<void> {
    this.#state.checking = true;
    this.#state.errorMessage = null;
    try {
      const info = await getVersion();
      this.#state.info = info;
      this.#state.compatible = isMajorCompatible(info);
    } catch (err: unknown) {
      this.#state.errorMessage = String(err);
      this.#state.compatible = null;
    } finally {
      this.#state.checking = false;
    }
  }

  reset(): void {
    this.#state = { info: null, compatible: null, errorMessage: null, checking: false };
  }
}

export const version = new VersionStore();
```

`gui/src/lib/stores/version.test.ts`:

```ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import { version } from "./version.svelte";
import * as api from "../api/version";

describe("version.check", () => {
  beforeEach(() => { version.reset(); });

  it("sets compatible=true on matching api_major", async () => {
    vi.spyOn(api, "getVersion").mockResolvedValue({ api_major: 1, api_minor: 0, server_version: null, build_hash: null });
    await version.check();
    expect(version.snapshot.compatible).toBe(true);
  });

  it("sets compatible=false on mismatched api_major", async () => {
    vi.spyOn(api, "getVersion").mockResolvedValue({ api_major: 2, api_minor: 0, server_version: null, build_hash: null });
    await version.check();
    expect(version.snapshot.compatible).toBe(false);
  });

  it("sets errorMessage when getVersion throws", async () => {
    vi.spyOn(api, "getVersion").mockRejectedValue(new Error("boom"));
    await version.check();
    expect(version.snapshot.errorMessage).toContain("boom");
    expect(version.snapshot.compatible).toBeNull();
  });
});
```

- [ ] **Step 7: VersionGate component + test**

`gui/src/components/VersionGate.svelte`:

```svelte
<script lang="ts">
  /**
   * Hard modal shown when the server's api_major doesn't match what this
   * client was built against. Single action: [Quit]. The user must run
   * `localmail serve` of a compatible version, or download a matching
   * client release.
   */
  import { onMount } from "svelte";
  import { version } from "../lib/stores/version.svelte";
  import { invoke } from "@tauri-apps/api/core";

  onMount(() => { void version.check(); });

  async function onQuit(): Promise<void> {
    try {
      await invoke("quit_app_cmd");
    } catch {
      window.close();
    }
  }
</script>

{#if version.snapshot.compatible === false}
  <div class="overlay" role="dialog" aria-modal="true" aria-labelledby="vg-title">
    <div class="modal">
      <h2 id="vg-title">Incompatible server</h2>
      <p>
        This client expects API major 1; the server reports
        {version.snapshot.info?.api_major ?? "?"}.
        Update one of them, then retry.
      </p>
      <button onclick={onQuit}>Quit</button>
    </div>
  </div>
{/if}

<style>
  .overlay {
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.6);
    display: grid; place-items: center;
    z-index: 1000;
  }
  .modal {
    background: white; padding: 1.5rem; border-radius: 6px;
    max-width: 480px;
  }
</style>
```

Add Rust command `quit_app_cmd` in `gui/src-tauri/src/lib.rs`:

```rust
#[tauri::command]
fn quit_app_cmd(app: tauri::AppHandle) {
    app.exit(0);
}
```

…and register it in `generate_handler!`.

`gui/src/components/VersionGate.test.ts`:

```ts
import { render } from "@testing-library/svelte";
import { describe, it, expect } from "vitest";
import VersionGate from "./VersionGate.svelte";
import { version } from "../lib/stores/version.svelte";

describe("VersionGate", () => {
  it("renders nothing when compatible is null", () => {
    version.reset();
    const { container } = render(VersionGate);
    expect(container.querySelector("[role=dialog]")).toBeFalsy();
  });

  it("renders nothing when compatible=true", async () => {
    version.reset();
    Object.assign(version.snapshot, { compatible: true, info: { api_major: 1, api_minor: 0, server_version: null, build_hash: null } });
    const { container } = render(VersionGate);
    expect(container.querySelector("[role=dialog]")).toBeFalsy();
  });

  it("renders the dialog when compatible=false", async () => {
    version.reset();
    Object.assign(version.snapshot, { compatible: false, info: { api_major: 2, api_minor: 0, server_version: null, build_hash: null } });
    const { container } = render(VersionGate);
    expect(container.querySelector("[role=dialog]")).toBeTruthy();
  });
});
```

- [ ] **Step 8: All tests pass**

```bash
npm test -- --run && (cd src-tauri && cargo test)
```

- [ ] **Step 9: Commit**

```bash
git add gui/src-tauri/src/commands/version.rs gui/src-tauri/src/commands/mod.rs \
        gui/src-tauri/src/commands/session.rs gui/src-tauri/src/lib.rs \
        gui/src/lib/version_check.ts gui/src/lib/version_check.test.ts \
        gui/src/lib/api/version.ts \
        gui/src/lib/stores/version.svelte.ts gui/src/lib/stores/version.test.ts \
        gui/src/components/VersionGate.svelte gui/src/components/VersionGate.test.ts
git commit -m "feat(gui-client): VersionGate + /v1/version check + quit_app_cmd"
```

### Task B8: Mount `VersionGate` above `MainView`

**Files:**
- Modify: `gui/src/screens/MainView.svelte` (or `AuthShell.svelte` — whichever wraps the logged-in tree at the top)

- [ ] **Step 1: Locate the highest mount point for the logged-in tree**

```bash
grep -rn "phase === \"logged_in\"" gui/src/screens/
```

Pick whichever component renders unconditionally at app boot (likely `AuthShell.svelte` or the root `App.svelte`). Mount `VersionGate` *unconditionally* — it should also show before login, since major mismatch shouldn't even let the user attempt to authenticate.

- [ ] **Step 2: Add VersionGate import + mount**

Inside the `<script>` of the root component:

```ts
  import VersionGate from "../components/VersionGate.svelte";
```

In the template, as the FIRST element inside the root wrapper:

```svelte
<VersionGate />
```

- [ ] **Step 3: Manual smoke**

```bash
npm run check
npm test -- --run
```

- [ ] **Step 4: Commit**

```bash
git add <file>
git commit -m "feat(gui-client): mount VersionGate at app root"
```

### Task B9: `getMessageRaw` — Rust + TS

**Files:**
- Create: `gui/src-tauri/src/commands/raw_message.rs`
- Create: `gui/src/lib/api/raw_message.ts`
- Modify: `gui/src-tauri/src/commands/mod.rs`, `lib.rs`

- [ ] **Step 1: Rust command**

`gui/src-tauri/src/commands/raw_message.rs`:

```rust
//! GET /v1/messages/{id}/raw — returns the raw RFC822 bytes as a Vec<u8>.

use crate::commands::auth::AuthError;
use crate::commands::session::read_authenticated;
use crate::http::client::build_pinned_client;
use crate::storage::keyring::KeyringStore;

pub async fn get_message_raw(store: &KeyringStore, message_id: &str) -> Result<Vec<u8>, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/messages/{message_id}/raw");
    let resp = client
        .get(&endpoint)
        .bearer_auth(&token)
        .send()
        .await
        .map_err(|e| AuthError::Io(format!("network: {e}")))?;
    if !resp.status().is_success() {
        return Err(AuthError::Io(format!("server returned {}", resp.status())));
    }
    let body = resp.bytes().await.map_err(|e| AuthError::Io(format!("read body: {e}")))?;
    Ok(body.to_vec())
}

#[tauri::command]
pub async fn get_message_raw_cmd(message_id: String) -> Result<Vec<u8>, AuthError> {
    let store = KeyringStore::new();
    get_message_raw(&store, &message_id).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::MemKeyring;

    #[tokio::test]
    async fn returns_not_logged_in_without_token() {
        let store = KeyringStore::with_backend(MemKeyring::new());
        let err = get_message_raw(&store, "1").await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected | AuthError::NotLoggedIn));
    }
}
```

- [ ] **Step 2: TS wrapper**

`gui/src/lib/api/raw_message.ts`:

```ts
import { invoke } from "@tauri-apps/api/core";

export async function getRawMessage(messageId: string): Promise<Uint8Array> {
  const bytes = await invoke<number[]>("get_message_raw_cmd", { messageId });
  return new Uint8Array(bytes);
}
```

- [ ] **Step 3: Register + tests pass**

Add `pub mod raw_message;` to `commands/mod.rs`; add `raw_message::get_message_raw_cmd` to `generate_handler!`. Then:

```bash
cd src-tauri && cargo test
```

- [ ] **Step 4: Commit**

```bash
git add gui/src-tauri/src/commands/raw_message.rs gui/src-tauri/src/commands/mod.rs \
        gui/src-tauri/src/lib.rs gui/src/lib/api/raw_message.ts
git commit -m "feat(gui-client): Rust /v1/messages/{id}/raw command + TS wrapper"
```

### Task B10: `RawBodyView.svelte` component

**Files:**
- Create: `gui/src/components/RawBodyView.svelte`
- Create: `gui/src/components/RawBodyView.test.ts`

- [ ] **Step 1: Failing test**

`gui/src/components/RawBodyView.test.ts`:

```ts
import { render, screen } from "@testing-library/svelte";
import { describe, it, expect, vi } from "vitest";
import RawBodyView from "./RawBodyView.svelte";
import * as api from "../lib/api/raw_message";

describe("RawBodyView", () => {
  it("shows a Load button initially, fetches on click, then renders the decoded body", async () => {
    const enc = new TextEncoder();
    vi.spyOn(api, "getRawMessage").mockResolvedValue(enc.encode("From: a@b\r\nSubject: hi\r\n\r\nBody"));
    const { getByRole, findByText } = render(RawBodyView, { props: { messageId: "42" } });
    const btn = getByRole("button", { name: /load/i });
    btn.click();
    expect(await findByText(/From: a@b/)).toBeTruthy();
  });

  it("shows an error when the fetch fails", async () => {
    vi.spyOn(api, "getRawMessage").mockRejectedValue(new Error("nope"));
    const { getByRole, findByText } = render(RawBodyView, { props: { messageId: "42" } });
    getByRole("button", { name: /load/i }).click();
    expect(await findByText(/nope/)).toBeTruthy();
  });
});
```

- [ ] **Step 2: Implement**

`gui/src/components/RawBodyView.svelte`:

```svelte
<script lang="ts">
  /**
   * Raw RFC822 view. Renders only when `messageId` is set. Initial state is
   * empty (we don't want to refetch every time the user switches body mode);
   * a "Load raw bytes" button does the fetch on demand. Body is decoded as
   * UTF-8 with replacement — RFC822 isn't strictly UTF-8 but the use case
   * is debug/diagnostic, not byte-exact.
   */
  import { getRawMessage } from "../lib/api/raw_message";

  interface Props { messageId: string; }
  let { messageId }: Props = $props();

  let text: string | null = $state(null);
  let loading: boolean = $state(false);
  let errorMessage: string | null = $state(null);

  async function load(): Promise<void> {
    loading = true;
    errorMessage = null;
    try {
      const bytes = await getRawMessage(messageId);
      text = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
    } catch (err: unknown) {
      errorMessage = err instanceof Error ? err.message : String(err);
    } finally {
      loading = false;
    }
  }

  async function copy(): Promise<void> {
    if (text === null) return;
    await navigator.clipboard.writeText(text);
  }
</script>

{#if text === null}
  <div class="empty">
    <button onclick={load} disabled={loading}>{loading ? "Loading…" : "Load raw bytes"}</button>
    {#if errorMessage}<div class="error">{errorMessage}</div>{/if}
  </div>
{:else}
  <div class="bar">
    <button onclick={copy}>Copy</button>
  </div>
  <pre class="raw">{text}</pre>
{/if}

<style>
  .raw { white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, monospace; font-size: 12px; }
  .error { color: #b00020; margin-top: 0.5rem; }
</style>
```

- [ ] **Step 3: Tests pass + commit**

```bash
npm test -- --run src/components/RawBodyView.test.ts
git add gui/src/components/RawBodyView.svelte gui/src/components/RawBodyView.test.ts
git commit -m "feat(gui-client): RawBodyView — lazy-fetch + render RFC822 bytes"
```

### Task B11: Wire `RawBodyView` into `ReadingPane`

**Files:**
- Modify: `gui/src/components/ReadingPane.svelte`

- [ ] **Step 1: Replace the `raw` body-mode placeholder**

Find the `{:else if mail.snapshot.bodyMode === "raw"}` branch and replace its body with:

```svelte
      {:else if mail.snapshot.bodyMode === "raw"}
        <RawBodyView messageId={String(m.id)} />
```

Add `import RawBodyView from "./RawBodyView.svelte";` to the script block.

Note: pass `String(m.id)` — the API expects message ID as a string per the Sub-plan 4 wire convention.

- [ ] **Step 2: Manual smoke + commit**

```bash
npm run check && npm test -- --run
git add gui/src/components/ReadingPane.svelte
git commit -m "feat(gui-client): wire RawBodyView into ReadingPane raw body mode"
```

### Task B12: `getMessageFullHeaders` — Rust + TS

**Files:**
- Create: `gui/src-tauri/src/commands/full_headers.rs`
- Create: `gui/src/lib/api/full_headers.ts`
- Modify: `gui/src-tauri/src/commands/mod.rs`, `lib.rs`

- [ ] **Step 1: Rust command**

`gui/src-tauri/src/commands/full_headers.rs`:

```rust
//! GET /v1/messages/{id}?headers=full — returns MessageDetail with the
//! `headers` field populated (a flat JSON object of raw header name → value).

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::commands::auth::AuthError;
use crate::commands::session::read_authenticated;
use crate::http::client::{build_pinned_client, http_get_json};
use crate::storage::keyring::KeyringStore;

#[derive(Debug, Deserialize, Serialize)]
pub struct MessageDetailWithHeaders {
    #[serde(flatten)]
    pub other: Value,
    pub headers: Option<Value>,
}

pub async fn get_message_full_headers(
    store: &KeyringStore,
    message_id: &str,
) -> Result<MessageDetailWithHeaders, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/messages/{message_id}?headers=full");
    let resp: MessageDetailWithHeaders = http_get_json(&client, &endpoint, Some(&token)).await?;
    Ok(resp)
}

#[tauri::command]
pub async fn get_message_full_headers_cmd(
    message_id: String,
) -> Result<MessageDetailWithHeaders, AuthError> {
    let store = KeyringStore::new();
    get_message_full_headers(&store, &message_id).await
}
```

- [ ] **Step 2: TS wrapper**

`gui/src/lib/api/full_headers.ts`:

```ts
import { invoke } from "@tauri-apps/api/core";

export type RawHeaders = Record<string, string | string[]>;

export interface MessageFullHeaders {
  headers: RawHeaders | null;
}

export async function getMessageFullHeaders(messageId: string): Promise<MessageFullHeaders> {
  return invoke<MessageFullHeaders>("get_message_full_headers_cmd", { messageId });
}
```

- [ ] **Step 3: Register + commit**

Add to `commands/mod.rs` and `generate_handler!`. Then:

```bash
cd src-tauri && cargo test
git add ...
git commit -m "feat(gui-client): Rust /v1/messages/{id}?headers=full command + TS wrapper"
```

### Task B13: `HeaderUnfold.svelte` component

**Files:**
- Create: `gui/src/components/HeaderUnfold.svelte`
- Create: `gui/src/components/HeaderUnfold.test.ts`

- [ ] **Step 1: Failing test**

`gui/src/components/HeaderUnfold.test.ts`:

```ts
import { render } from "@testing-library/svelte";
import { describe, it, expect, vi } from "vitest";
import HeaderUnfold from "./HeaderUnfold.svelte";
import * as api from "../lib/api/full_headers";

describe("HeaderUnfold", () => {
  it("shows a 'Show full headers' button initially", () => {
    const { getByRole } = render(HeaderUnfold, { props: { messageId: "1" } });
    expect(getByRole("button", { name: /show full headers/i })).toBeTruthy();
  });

  it("fetches and renders headers on click", async () => {
    vi.spyOn(api, "getMessageFullHeaders").mockResolvedValue({
      headers: { "Message-Id": "<a@b>", "X-Spam-Status": "No" },
    });
    const { getByRole, findByText } = render(HeaderUnfold, { props: { messageId: "1" } });
    getByRole("button", { name: /show full headers/i }).click();
    expect(await findByText(/Message-Id/)).toBeTruthy();
    expect(await findByText(/X-Spam-Status/)).toBeTruthy();
  });
});
```

- [ ] **Step 2: Implement**

`gui/src/components/HeaderUnfold.svelte`:

```svelte
<script lang="ts">
  /**
   * Lazy-loads the full RFC822 headers via /v1/messages/{id}?headers=full.
   * Caches the result in component-local state so toggling Hide/Show doesn't
   * re-fetch.
   */
  import { getMessageFullHeaders, type RawHeaders } from "../lib/api/full_headers";

  interface Props { messageId: string; }
  let { messageId }: Props = $props();

  let headers: RawHeaders | null = $state(null);
  let visible: boolean = $state(false);
  let loading: boolean = $state(false);
  let errorMessage: string | null = $state(null);

  async function toggle(): Promise<void> {
    if (visible) {
      visible = false;
      return;
    }
    if (headers !== null) {
      visible = true;
      return;
    }
    loading = true;
    errorMessage = null;
    try {
      const resp = await getMessageFullHeaders(messageId);
      headers = resp.headers ?? {};
      visible = true;
    } catch (err: unknown) {
      errorMessage = err instanceof Error ? err.message : String(err);
    } finally {
      loading = false;
    }
  }

  function entries(h: RawHeaders): Array<[string, string]> {
    const out: Array<[string, string]> = [];
    for (const [k, v] of Object.entries(h)) {
      if (Array.isArray(v)) for (const one of v) out.push([k, one]);
      else out.push([k, v]);
    }
    return out;
  }
</script>

<button onclick={toggle} disabled={loading}>
  {#if loading}Loading…{:else if visible}Hide full headers{:else}Show full headers{/if}
</button>
{#if errorMessage}<div class="error">{errorMessage}</div>{/if}
{#if visible && headers}
  <dl class="hdrs">
    {#each entries(headers) as [name, value]}
      <dt>{name}</dt><dd>{value}</dd>
    {/each}
  </dl>
{/if}

<style>
  .hdrs { font-family: ui-monospace, monospace; font-size: 12px; margin-top: 0.5rem; }
  .hdrs dt { font-weight: bold; }
  .hdrs dd { margin: 0 0 0.25rem 0; word-break: break-all; }
  .error { color: #b00020; }
</style>
```

- [ ] **Step 3: Tests pass + commit**

```bash
npm test -- --run src/components/HeaderUnfold.test.ts
git add gui/src/components/HeaderUnfold.svelte gui/src/components/HeaderUnfold.test.ts
git commit -m "feat(gui-client): HeaderUnfold — lazy /v1/messages/{id}?headers=full"
```

### Task B14: Mount `HeaderUnfold` in `ReadingPane`

**Files:**
- Modify: `gui/src/components/ReadingPane.svelte`

- [ ] **Step 1: Mount under the compact header `<dl>`**

Inside the `<header>` block, after the closing `</dl>`:

```svelte
      <HeaderUnfold messageId={String(m.id)} />
```

Add `import HeaderUnfold from "./HeaderUnfold.svelte";` to the script block.

- [ ] **Step 2: Commit**

```bash
npm run check
git add gui/src/components/ReadingPane.svelte
git commit -m "feat(gui-client): mount HeaderUnfold inside ReadingPane header"
```

### Task B15: Settings store + screen scaffold

**Files:**
- Create: `gui/src/lib/stores/settings.svelte.ts`
- Create: `gui/src/lib/stores/settings.test.ts`
- Create: `gui/src/screens/SettingsScreen.svelte`
- Create: `gui/src/screens/SettingsScreen.test.ts`
- Modify: `gui/src/screens/MainView.svelte`

- [ ] **Step 1: Settings store with localStorage persistence**

`gui/src/lib/stores/settings.svelte.ts`:

```ts
export type Density = "comfortable" | "compact";
export type ImagePolicy = "block" | "allow";
export type DateFormat = "relative" | "absolute";

export interface SettingsSnapshot {
  density: Density;
  imagePolicy: ImagePolicy;
  dateFormat: DateFormat;
  pageSize: number;
  defaultLanguage: string | null;
  debug: boolean;
}

export const DEFAULTS: SettingsSnapshot = {
  density: "comfortable",
  imagePolicy: "block",
  dateFormat: "relative",
  pageSize: 50,
  defaultLanguage: null,
  debug: false,
};

const STORAGE_KEY = "localmail.gui.settings";

function loadInitial(): SettingsSnapshot {
  const raw = window.localStorage.getItem(STORAGE_KEY);
  if (raw === null) return { ...DEFAULTS };
  try {
    const parsed = JSON.parse(raw) as Partial<SettingsSnapshot>;
    return {
      density: parsed.density === "compact" ? "compact" : "comfortable",
      imagePolicy: parsed.imagePolicy === "allow" ? "allow" : "block",
      dateFormat: parsed.dateFormat === "absolute" ? "absolute" : "relative",
      pageSize: typeof parsed.pageSize === "number" && parsed.pageSize > 0 ? Math.min(parsed.pageSize, 200) : DEFAULTS.pageSize,
      defaultLanguage: typeof parsed.defaultLanguage === "string" && parsed.defaultLanguage.length > 0 ? parsed.defaultLanguage : null,
      debug: parsed.debug === true,
    };
  } catch {
    return { ...DEFAULTS };
  }
}

function persist(s: SettingsSnapshot): void {
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
}

class SettingsStore {
  #state: SettingsSnapshot = $state(loadInitial());
  get snapshot(): SettingsSnapshot { return this.#state; }
  setDensity(d: Density): void { this.#state.density = d; persist(this.#state); }
  setImagePolicy(p: ImagePolicy): void { this.#state.imagePolicy = p; persist(this.#state); }
  setDateFormat(d: DateFormat): void { this.#state.dateFormat = d; persist(this.#state); }
  setPageSize(n: number): void {
    if (!Number.isFinite(n) || n <= 0) return;
    this.#state.pageSize = Math.min(Math.floor(n), 200);
    persist(this.#state);
  }
  setDefaultLanguage(s: string | null): void {
    this.#state.defaultLanguage = s && s.trim().length > 0 ? s.trim().toLowerCase() : null;
    persist(this.#state);
  }
  setDebug(b: boolean): void { this.#state.debug = b; persist(this.#state); }
  resetForTest(): void { this.#state = { ...DEFAULTS }; persist(this.#state); }
}

export const settings = new SettingsStore();
```

`gui/src/lib/stores/settings.test.ts`:

```ts
import { describe, it, expect, beforeEach } from "vitest";
import { settings, DEFAULTS } from "./settings.svelte";

beforeEach(() => { settings.resetForTest(); });

describe("settings store", () => {
  it("starts with defaults", () => {
    expect(settings.snapshot).toEqual(DEFAULTS);
  });

  it("clamps page size to 200", () => {
    settings.setPageSize(1000);
    expect(settings.snapshot.pageSize).toBe(200);
  });

  it("rejects non-positive page size", () => {
    settings.setPageSize(0);
    expect(settings.snapshot.pageSize).toBe(DEFAULTS.pageSize);
    settings.setPageSize(-5);
    expect(settings.snapshot.pageSize).toBe(DEFAULTS.pageSize);
  });

  it("normalises defaultLanguage to lowercase or null", () => {
    settings.setDefaultLanguage("EN");
    expect(settings.snapshot.defaultLanguage).toBe("en");
    settings.setDefaultLanguage("");
    expect(settings.snapshot.defaultLanguage).toBeNull();
  });

  it("toggles debug", () => {
    settings.setDebug(true);
    expect(settings.snapshot.debug).toBe(true);
    settings.setDebug(false);
    expect(settings.snapshot.debug).toBe(false);
  });
});
```

- [ ] **Step 2: Settings screen scaffold (tabs)**

`gui/src/screens/SettingsScreen.svelte`:

```svelte
<script lang="ts">
  /**
   * Full-pane settings overlay. Mounted as an absolutely-positioned layer
   * over MainView; the parent toggles visibility via `open` prop. Tabs are
   * sub-components so each can have its own state + test isolation.
   */
  import SettingsServer from "./settings/SettingsServer.svelte";
  import SettingsDisplay from "./settings/SettingsDisplay.svelte";
  import SettingsSearch from "./settings/SettingsSearch.svelte";
  import SettingsAbout from "./settings/SettingsAbout.svelte";

  type Tab = "server" | "display" | "search" | "about";

  interface Props { open: boolean; onClose: () => void; }
  let { open, onClose }: Props = $props();

  let tab: Tab = $state("server");
</script>

{#if open}
  <div class="overlay" role="dialog" aria-modal="true" aria-labelledby="settings-title">
    <div class="modal">
      <header>
        <h2 id="settings-title">Settings</h2>
        <button class="close" onclick={onClose} aria-label="Close">×</button>
      </header>
      <nav class="tabs">
        <button class:active={tab === "server"} onclick={() => (tab = "server")}>Server</button>
        <button class:active={tab === "display"} onclick={() => (tab = "display")}>Display</button>
        <button class:active={tab === "search"} onclick={() => (tab = "search")}>Search</button>
        <button class:active={tab === "about"} onclick={() => (tab = "about")}>About</button>
      </nav>
      <section class="body">
        {#if tab === "server"}<SettingsServer />{/if}
        {#if tab === "display"}<SettingsDisplay />{/if}
        {#if tab === "search"}<SettingsSearch />{/if}
        {#if tab === "about"}<SettingsAbout />{/if}
      </section>
    </div>
  </div>
{/if}

<style>
  .overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.5); display: grid; place-items: center; z-index: 200; }
  .modal { background: white; width: min(800px, 90vw); height: min(600px, 90vh); display: flex; flex-direction: column; border-radius: 6px; }
  header { display: flex; justify-content: space-between; align-items: center; padding: 0.75rem 1rem; border-bottom: 1px solid #ddd; }
  .close { font-size: 1.25rem; background: none; border: none; cursor: pointer; }
  .tabs { display: flex; gap: 0.25rem; padding: 0 1rem; border-bottom: 1px solid #ddd; }
  .tabs button { padding: 0.5rem 0.75rem; background: none; border: none; cursor: pointer; }
  .tabs button.active { font-weight: bold; border-bottom: 2px solid #1a73e8; }
  .body { flex: 1; padding: 1rem; overflow: auto; }
</style>
```

`gui/src/screens/SettingsScreen.test.ts`:

```ts
import { render, fireEvent } from "@testing-library/svelte";
import { describe, it, expect, vi } from "vitest";
import SettingsScreen from "./SettingsScreen.svelte";

describe("SettingsScreen", () => {
  it("renders nothing when open=false", () => {
    const { container } = render(SettingsScreen, { props: { open: false, onClose: vi.fn() } });
    expect(container.querySelector("[role=dialog]")).toBeFalsy();
  });

  it("renders the dialog when open=true", () => {
    const { container } = render(SettingsScreen, { props: { open: true, onClose: vi.fn() } });
    expect(container.querySelector("[role=dialog]")).toBeTruthy();
  });

  it("calls onClose when × clicked", async () => {
    const onClose = vi.fn();
    const { getByLabelText } = render(SettingsScreen, { props: { open: true, onClose } });
    await fireEvent.click(getByLabelText("Close"));
    expect(onClose).toHaveBeenCalled();
  });
});
```

- [ ] **Step 3: Add a stub for each tab so the screen renders**

Create empty-but-valid Svelte files for the four sub-tabs (they will be filled in tasks B16–B19):

`gui/src/screens/settings/SettingsServer.svelte`:
```svelte
<p>Server settings (filled in B16).</p>
```

Same skeletal pattern for `SettingsDisplay.svelte`, `SettingsSearch.svelte`, `SettingsAbout.svelte`.

- [ ] **Step 4: Mount Settings entry point in MainView header bar**

In `gui/src/screens/MainView.svelte`, in the script block add:

```ts
  import SettingsScreen from "./SettingsScreen.svelte";
  let settingsOpen: boolean = $state(false);
```

In the template, in the header `.right` div, before the `Refresh token` button:

```svelte
        <button onclick={() => (settingsOpen = true)}>Settings</button>
```

And just before the closing `</div>` of `.app` (or wherever the top-level mount is appropriate), add:

```svelte
    <SettingsScreen open={settingsOpen} onClose={() => (settingsOpen = false)} />
```

- [ ] **Step 5: Tests pass + commit**

```bash
npm test -- --run
git add gui/src/lib/stores/settings.svelte.ts gui/src/lib/stores/settings.test.ts \
        gui/src/screens/SettingsScreen.svelte gui/src/screens/SettingsScreen.test.ts \
        gui/src/screens/settings/ gui/src/screens/MainView.svelte
git commit -m "feat(gui-client): settings store + Settings screen scaffold w/ tabs"
```

### Task B16: `SettingsServer.svelte` — change password + log out + re-trust + URL display

**Files:**
- Create: `gui/src-tauri/src/commands/auth_change_password.rs`
- Create: `gui/src/lib/api/change_password.ts`
- Create: `gui/src/screens/settings/SettingsServer.svelte` (replaces the stub)
- Create: `gui/src/screens/settings/SettingsServer.test.ts`

- [ ] **Step 1: Verify the server route exists**

```bash
grep -rn "change.password\|change_password\|/v1/auth/change" \
    /Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search/src/localmail/serve/routes/
```

If the route does not exist, add it before continuing:

`src/localmail/serve/routes/auth.py` — add (in the same router):

```python
class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    user=Depends(get_authenticated_user),
):
    pool = request.app.state.pool
    with pool.connection() as conn:
        verify_password(conn, user.id, body.old_password)
        update_password(conn, user.id, body.new_password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

Add tests + commit on the phase2 branch as a separate commit (subagent should report this side-quest in its summary).

- [ ] **Step 2: Rust + TS client wrappers**

`gui/src-tauri/src/commands/auth_change_password.rs`:

```rust
//! POST /v1/auth/change-password — token stays valid; password is replaced.

use serde::Serialize;

use crate::commands::auth::AuthError;
use crate::commands::session::read_authenticated;
use crate::http::client::build_pinned_client;
use crate::storage::keyring::KeyringStore;

#[derive(Serialize)]
struct Body<'a> {
    old_password: &'a str,
    new_password: &'a str,
}

pub async fn change_password(
    store: &KeyringStore,
    old_password: &str,
    new_password: &str,
) -> Result<(), AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/auth/change-password");
    let resp = client
        .post(&endpoint)
        .bearer_auth(&token)
        .json(&Body { old_password, new_password })
        .send()
        .await
        .map_err(|e| AuthError::Io(format!("network: {e}")))?;
    if !resp.status().is_success() {
        return Err(AuthError::Io(format!("server returned {}", resp.status())));
    }
    Ok(())
}

#[tauri::command]
pub async fn change_password_cmd(old_password: String, new_password: String) -> Result<(), AuthError> {
    let store = KeyringStore::new();
    change_password(&store, &old_password, &new_password).await
}
```

`gui/src/lib/api/change_password.ts`:

```ts
import { invoke } from "@tauri-apps/api/core";

export async function changePassword(oldPassword: string, newPassword: string): Promise<void> {
  await invoke("change_password_cmd", { oldPassword, newPassword });
}
```

Register the command in `commands/mod.rs` + `lib.rs`.

- [ ] **Step 3: Replace `SettingsServer.svelte` stub**

```svelte
<script lang="ts">
  /**
   * Server tab: shows current server URL + cert pin SHA-256 (read from auth
   * store), exposes change-password, log out, and a placeholder re-trust
   * action. Re-trust is intentionally non-destructive: it just shows the
   * current pin; the actual TOFU prompt happens on a fresh `/connect` flow.
   */
  import { changePassword } from "../../lib/api/change_password";
  import { auth } from "../../lib/stores/auth.svelte";

  let oldPassword: string = $state("");
  let newPassword: string = $state("");
  let busy: boolean = $state(false);
  let message: string | null = $state(null);

  async function onChange(): Promise<void> {
    busy = true;
    message = null;
    try {
      await changePassword(oldPassword, newPassword);
      message = "Password changed.";
      oldPassword = "";
      newPassword = "";
    } catch (err: unknown) {
      message = err instanceof Error ? err.message : String(err);
    } finally {
      busy = false;
    }
  }

  async function onLogout(): Promise<void> {
    busy = true;
    try { await auth.logout(); } finally { busy = false; }
  }
</script>

<section class="server">
  <h3>Server</h3>
  <dl>
    <dt>URL</dt><dd>{auth.snapshot.serverUrl ?? "(not connected)"}</dd>
    <dt>Username</dt><dd>{auth.snapshot.phase === "logged_in" ? auth.snapshot.username : "(logged out)"}</dd>
    <dt>Cert pin (SHA-256)</dt><dd class="mono">{auth.snapshot.certPin ?? "(unknown)"}</dd>
  </dl>
  <h3>Change password</h3>
  <form onsubmit={(e) => { e.preventDefault(); void onChange(); }}>
    <label>Current<input type="password" bind:value={oldPassword} disabled={busy} /></label>
    <label>New<input type="password" bind:value={newPassword} disabled={busy} /></label>
    <button type="submit" disabled={busy || oldPassword === "" || newPassword === ""}>Change password</button>
  </form>
  {#if message}<p class="msg">{message}</p>{/if}
  <h3>Session</h3>
  <button onclick={onLogout} disabled={busy}>Log out</button>
</section>

<style>
  .mono { font-family: ui-monospace, monospace; font-size: 12px; word-break: break-all; }
  form { display: flex; flex-direction: column; gap: 0.5rem; max-width: 320px; }
</style>
```

If `auth.snapshot.certPin` or `auth.snapshot.serverUrl` do not exist as public properties on the auth snapshot, add them as read-only getters in `gui/src/lib/stores/auth.svelte.ts` — the values are already in keyring; the snapshot is just a façade for component access.

- [ ] **Step 4: Test**

`gui/src/screens/settings/SettingsServer.test.ts`:

```ts
import { render, fireEvent } from "@testing-library/svelte";
import { describe, it, expect, vi } from "vitest";
import SettingsServer from "./SettingsServer.svelte";
import * as api from "../../lib/api/change_password";

describe("SettingsServer change-password", () => {
  it("calls changePassword with form values", async () => {
    const spy = vi.spyOn(api, "changePassword").mockResolvedValue();
    const { getAllByRole, getByText } = render(SettingsServer);
    const inputs = getAllByRole("textbox", { hidden: true }).concat(
      Array.from(document.querySelectorAll('input[type="password"]')) as unknown as HTMLElement[],
    );
    // Simpler: find by querySelector
    const pwd = Array.from(document.querySelectorAll('input[type="password"]')) as HTMLInputElement[];
    expect(pwd.length).toBe(2);
    await fireEvent.input(pwd[0], { target: { value: "old" } });
    await fireEvent.input(pwd[1], { target: { value: "new" } });
    await fireEvent.click(getByText("Change password"));
    expect(spy).toHaveBeenCalledWith("old", "new");
  });
});
```

- [ ] **Step 5: Tests pass + commit**

```bash
npm test -- --run src/screens/settings/SettingsServer.test.ts
git add ...
git commit -m "feat(gui-client): SettingsServer — change password + log out + cert pin display"
```

### Task B17: `SettingsDisplay.svelte`

**Files:**
- Create (replace stub): `gui/src/screens/settings/SettingsDisplay.svelte`
- Create: `gui/src/screens/settings/SettingsDisplay.test.ts`

- [ ] **Step 1: Implement**

```svelte
<script lang="ts">
  /** Density, date format, default HTML image policy. All persist to localStorage. */
  import { settings } from "../../lib/stores/settings.svelte";
</script>

<section class="display">
  <h3>Density</h3>
  <label><input type="radio" name="density" value="comfortable"
    checked={settings.snapshot.density === "comfortable"}
    onchange={() => settings.setDensity("comfortable")} /> Comfortable</label>
  <label><input type="radio" name="density" value="compact"
    checked={settings.snapshot.density === "compact"}
    onchange={() => settings.setDensity("compact")} /> Compact</label>

  <h3>Date format</h3>
  <label><input type="radio" name="dateFormat" value="relative"
    checked={settings.snapshot.dateFormat === "relative"}
    onchange={() => settings.setDateFormat("relative")} /> Relative (“yesterday”)</label>
  <label><input type="radio" name="dateFormat" value="absolute"
    checked={settings.snapshot.dateFormat === "absolute"}
    onchange={() => settings.setDateFormat("absolute")} /> Absolute</label>

  <h3>HTML images</h3>
  <label><input type="radio" name="img" value="block"
    checked={settings.snapshot.imagePolicy === "block"}
    onchange={() => settings.setImagePolicy("block")} /> Block by default</label>
  <label><input type="radio" name="img" value="allow"
    checked={settings.snapshot.imagePolicy === "allow"}
    onchange={() => settings.setImagePolicy("allow")} /> Always allow</label>
</section>

<style>
  label { display: block; margin: 0.25rem 0; }
  h3 { margin-top: 1rem; }
</style>
```

- [ ] **Step 2: Test**

```ts
import { render, fireEvent } from "@testing-library/svelte";
import { describe, it, expect, beforeEach } from "vitest";
import SettingsDisplay from "./SettingsDisplay.svelte";
import { settings } from "../../lib/stores/settings.svelte";

beforeEach(() => settings.resetForTest());

describe("SettingsDisplay", () => {
  it("flips density on click", async () => {
    const { getByLabelText } = render(SettingsDisplay);
    await fireEvent.click(getByLabelText(/Compact/));
    expect(settings.snapshot.density).toBe("compact");
  });

  it("flips image policy on click", async () => {
    const { getByLabelText } = render(SettingsDisplay);
    await fireEvent.click(getByLabelText(/Always allow/));
    expect(settings.snapshot.imagePolicy).toBe("allow");
  });
});
```

- [ ] **Step 3: Wire density + dateFormat into `MessageList` row rendering and `format.ts`**

In `gui/src/components/MessageList.svelte` (and `MessageListRow.svelte`), read `settings.snapshot.density` and add a class `class:compact={settings.snapshot.density === "compact"}` to the row.

In `gui/src/lib/format.ts`, add an exported `formatDate(d: string | null, mode: DateFormat): string` that switches between the existing `formatRelativeDate` and a new `formatAbsoluteDate` (using `Intl.DateTimeFormat`).

In `ReadingPane.svelte` (and any other date callsites), replace `formatRelativeDate(m.date)` with `formatDate(m.date, settings.snapshot.dateFormat)`.

In `HtmlBody.svelte`, read `settings.snapshot.imagePolicy` and pass it as the initial value of `externalImagesAllowed` only when the policy is `"allow"`; otherwise keep the per-message reset behaviour.

- [ ] **Step 4: Tests pass + commit**

```bash
npm test -- --run
git add ...
git commit -m "feat(gui-client): SettingsDisplay — density / date format / image policy + plumbing"
```

### Task B18: `SettingsSearch.svelte`

**Files:**
- Create (replace stub): `gui/src/screens/settings/SettingsSearch.svelte`
- Create: `gui/src/screens/settings/SettingsSearch.test.ts`

- [ ] **Step 1: Implement**

```svelte
<script lang="ts">
  /** Page size, default language for new searches, debug toggle. */
  import { settings } from "../../lib/stores/settings.svelte";

  let pageSizeText: string = $state(String(settings.snapshot.pageSize));
  let langText: string = $state(settings.snapshot.defaultLanguage ?? "");

  function applyPageSize(): void {
    const n = Number(pageSizeText);
    if (Number.isFinite(n) && n > 0) settings.setPageSize(n);
    pageSizeText = String(settings.snapshot.pageSize);
  }
  function applyLanguage(): void {
    settings.setDefaultLanguage(langText.trim() || null);
    langText = settings.snapshot.defaultLanguage ?? "";
  }
</script>

<section class="search">
  <h3>Page size</h3>
  <label>Results per search:
    <input type="number" min="1" max="200" bind:value={pageSizeText} onblur={applyPageSize} />
  </label>

  <h3>Default language</h3>
  <label>ISO 639-1 code (or empty for none):
    <input type="text" maxlength="5" bind:value={langText} onblur={applyLanguage} placeholder="e.g. en" />
  </label>

  <h3>Debug</h3>
  <label><input type="checkbox"
    checked={settings.snapshot.debug}
    onchange={(e) => settings.setDebug((e.currentTarget as HTMLInputElement).checked)} /> Show per-result scores, matched arms, and chunk highlights</label>
</section>

<style>
  label { display: block; margin: 0.25rem 0; }
</style>
```

- [ ] **Step 2: Test**

```ts
import { render, fireEvent } from "@testing-library/svelte";
import { describe, it, expect, beforeEach } from "vitest";
import SettingsSearch from "./SettingsSearch.svelte";
import { settings } from "../../lib/stores/settings.svelte";

beforeEach(() => settings.resetForTest());

describe("SettingsSearch", () => {
  it("toggles debug", async () => {
    const { getByRole } = render(SettingsSearch);
    const cb = getByRole("checkbox") as HTMLInputElement;
    await fireEvent.click(cb);
    expect(settings.snapshot.debug).toBe(true);
  });

  it("clamps page size at blur", async () => {
    const { getAllByRole } = render(SettingsSearch);
    const numInput = getAllByRole("spinbutton")[0] as HTMLInputElement;
    await fireEvent.input(numInput, { target: { value: "1000" } });
    await fireEvent.blur(numInput);
    expect(settings.snapshot.pageSize).toBe(200);
  });
});
```

- [ ] **Step 3: Plumb `pageSize` + `defaultLanguage` into the search store**

In `gui/src/lib/stores/search.svelte.ts`, when constructing the `SearchRequest`, use `settings.snapshot.pageSize` for `limit` and inject `settings.snapshot.defaultLanguage` into the filters if no explicit language filter is set.

- [ ] **Step 4: Commit**

```bash
git add ...
git commit -m "feat(gui-client): SettingsSearch — page size + default lang + debug toggle"
```

### Task B19: `SettingsAbout.svelte`

**Files:**
- Create (replace stub): `gui/src/screens/settings/SettingsAbout.svelte`
- Create: `gui/src/screens/settings/SettingsAbout.test.ts`
- Create: `gui/src-tauri/src/commands/open_logs.rs`
- Modify: `gui/src-tauri/src/commands/mod.rs`, `lib.rs`

- [ ] **Step 1: Rust command to open the log directory**

`gui/src-tauri/src/commands/open_logs.rs`:

```rust
//! Resolve the platform-specific log directory and open it in the OS file
//! manager. Path is the same one tracing writes to (set in lib.rs's
//! tracing init).

use std::path::PathBuf;
use tauri::AppHandle;
use tauri::Manager;

fn log_dir(app: &AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_log_dir()
        .map_err(|e| format!("resolve log dir: {e}"))
}

#[tauri::command]
pub async fn open_logs_cmd(app: AppHandle) -> Result<(), String> {
    let dir = log_dir(&app)?;
    if !dir.exists() {
        std::fs::create_dir_all(&dir).map_err(|e| format!("create log dir: {e}"))?;
    }
    opener::open(&dir).map_err(|e| format!("open: {e}"))?;
    Ok(())
}
```

Add `opener = "0.7"` to `gui/src-tauri/Cargo.toml` `[dependencies]` (if not already present). Register the command.

- [ ] **Step 2: `SettingsAbout.svelte`**

```svelte
<script lang="ts">
  import { version } from "../../lib/stores/version.svelte";
  import { invoke } from "@tauri-apps/api/core";

  const CLIENT_VERSION = "0.5.0";

  async function openLogs(): Promise<void> {
    try { await invoke("open_logs_cmd"); } catch (e) { console.error(e); }
  }
</script>

<section class="about">
  <h3>Versions</h3>
  <dl>
    <dt>Client</dt><dd>{CLIENT_VERSION}</dd>
    <dt>API major</dt><dd>{version.snapshot.info?.api_major ?? "?"}</dd>
    <dt>API minor</dt><dd>{version.snapshot.info?.api_minor ?? "?"}</dd>
    <dt>Server</dt><dd>{version.snapshot.info?.server_version ?? "?"}</dd>
    <dt>Server build</dt><dd>{version.snapshot.info?.build_hash ?? "?"}</dd>
  </dl>
  <h3>Logs</h3>
  <button onclick={openLogs}>Open log directory</button>
</section>
```

`CLIENT_VERSION` here must match `gui/src-tauri/Cargo.toml` `version` and `gui/package.json` `version`. It is hand-kept; a build-time injection is out of scope for v1.

- [ ] **Step 3: Test**

```ts
import { render } from "@testing-library/svelte";
import { describe, it, expect } from "vitest";
import SettingsAbout from "./SettingsAbout.svelte";
import { version } from "../../lib/stores/version.svelte";

describe("SettingsAbout", () => {
  it("renders version info", () => {
    Object.assign(version.snapshot, { info: { api_major: 1, api_minor: 0, server_version: "0.5.0", build_hash: "abc" } });
    const { getByText } = render(SettingsAbout);
    expect(getByText(/0\.5\.0/)).toBeTruthy();
  });
});
```

- [ ] **Step 4: Commit**

```bash
git add ...
git commit -m "feat(gui-client): SettingsAbout — versions + open log directory"
```

### Task B20: `DebugBadges.svelte` for `MessageList` rows

**Files:**
- Create: `gui/src/components/DebugBadges.svelte`
- Create: `gui/src/components/DebugBadges.test.ts`
- Modify: `gui/src/components/MessageListRow.svelte` (or `MessageList.svelte` — wherever the row template lives)

- [ ] **Step 1: Component**

```svelte
<script lang="ts">
  interface Props { score: number; matchedArms: readonly string[]; }
  let { score, matchedArms }: Props = $props();
</script>

<span class="debug">
  <span class="score" title="Fused score">{score.toFixed(3)}</span>
  {#each matchedArms as arm}
    <span class="arm">{arm}</span>
  {/each}
</span>

<style>
  .debug { font-family: ui-monospace, monospace; font-size: 11px; opacity: 0.75; }
  .score { background: #eef; padding: 0 4px; border-radius: 3px; margin-right: 4px; }
  .arm { background: #efe; padding: 0 4px; border-radius: 3px; margin-right: 2px; }
</style>
```

- [ ] **Step 2: Test**

```ts
import { render } from "@testing-library/svelte";
import { describe, it, expect } from "vitest";
import DebugBadges from "./DebugBadges.svelte";

describe("DebugBadges", () => {
  it("renders score and arms", () => {
    const { getByText, getByTitle } = render(DebugBadges, { props: { score: 0.7531, matchedArms: ["bm25_messages", "vector_chunks"] } });
    expect(getByTitle("Fused score").textContent).toBe("0.753");
    expect(getByText("bm25_messages")).toBeTruthy();
    expect(getByText("vector_chunks")).toBeTruthy();
  });
});
```

- [ ] **Step 3: Mount in row template (gated by `settings.debug`)**

In `MessageListRow.svelte` (or wherever the row is structured), import `DebugBadges` and the settings store; pass score + matched_arms as props (the props must be added too — Sub-plan 4's row was refactored to flat props). Add:

```svelte
  import DebugBadges from "./DebugBadges.svelte";
  import { settings } from "../lib/stores/settings.svelte";
  // … existing props …
  interface Props { /* … existing … */ score?: number; matchedArms?: readonly string[]; }
```

In the template, where the snippet renders:

```svelte
  {#if settings.snapshot.debug && score !== undefined && matchedArms !== undefined}
    <DebugBadges {score} {matchedArms} />
  {/if}
```

Update `MessageList.svelte` to forward `score` and `matchedArms` from the search-result objects to the row.

- [ ] **Step 4: Commit**

```bash
git add ...
git commit -m "feat(gui-client): DebugBadges — per-row fused score + matched arms"
```

### Task B21: `DebugChunks.svelte` for reading pane

**Files:**
- Create: `gui/src/components/DebugChunks.svelte`
- Create: `gui/src/components/DebugChunks.test.ts`
- Modify: `gui/src/components/ReadingPane.svelte`

- [ ] **Step 1: Decide data source**

The debug pane needs the matched chunks for the selected message. Two options:
- **Option A (preferred):** server returns chunk-level matches inside `SearchResult` (`matched_chunks: [{chunk_id, text, score}]`). If the server does this already (verify by inspecting `/v1/search` response JSON), we use it directly.
- **Option B (fallback):** the GUI fetches `/v1/search?q=…&include=chunks&for_message=<id>` separately. Not exposed today.

Inspect the search response:

```bash
grep -n "matched_chunks\|chunks" /Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search/src/localmail/api/search.py
```

If `matched_chunks` is present, attach it to the GUI's `SearchResult` type (`gui/src/lib/api/search.ts`) and proceed. If not, **scope this task to display only `matched_arms` text — no chunk text** and document the deferred work in NEXT_SESSION.md. Do NOT add a new server endpoint here; chunk-display is best-effort for the debug toggle.

- [ ] **Step 2: Implement (Option A path)**

```svelte
<script lang="ts">
  interface MatchedChunk { chunk_id: string; text: string; score: number; }
  interface Props { chunks: readonly MatchedChunk[]; }
  let { chunks }: Props = $props();
</script>

{#if chunks.length > 0}
  <details class="debug-chunks">
    <summary>{chunks.length} matched chunk{chunks.length === 1 ? "" : "s"}</summary>
    <ol>
      {#each chunks as c}
        <li>
          <span class="score">{c.score.toFixed(3)}</span>
          <pre>{c.text}</pre>
        </li>
      {/each}
    </ol>
  </details>
{/if}

<style>
  pre { white-space: pre-wrap; font-size: 12px; background: #fafafa; padding: 0.25rem; }
  .score { font-family: ui-monospace, monospace; color: #555; }
</style>
```

- [ ] **Step 3: Mount in ReadingPane**

```svelte
  {#if settings.snapshot.debug && mail.snapshot.selectedMessage?.matched_chunks}
    <DebugChunks chunks={mail.snapshot.selectedMessage.matched_chunks} />
  {/if}
```

Add `matched_chunks?: MatchedChunk[]` to `MessageDetail` in `gui/src/lib/api/messages.ts` (or wherever the type lives).

- [ ] **Step 4: Test (component-only, no integration)**

```ts
import { render } from "@testing-library/svelte";
import { describe, it, expect } from "vitest";
import DebugChunks from "./DebugChunks.svelte";

describe("DebugChunks", () => {
  it("renders nothing for empty chunks", () => {
    const { container } = render(DebugChunks, { props: { chunks: [] } });
    expect(container.querySelector("details")).toBeFalsy();
  });
  it("renders one <li> per chunk", () => {
    const { container } = render(DebugChunks, { props: { chunks: [
      { chunk_id: "1", text: "hello", score: 0.9 },
      { chunk_id: "2", text: "world", score: 0.8 },
    ]} });
    expect(container.querySelectorAll("li").length).toBe(2);
  });
});
```

- [ ] **Step 5: Commit**

```bash
git add ...
git commit -m "feat(gui-client): DebugChunks — matched-chunk listing in ReadingPane debug mode"
```

### Task B22: Multi-page PDF preview

**Files:**
- Modify: `gui/src/components/AttachmentPreviewModal.svelte`
- Modify: `gui/src/components/AttachmentPreviewModal.test.ts`

- [ ] **Step 1: Add page state + controls**

Inside the existing PDF render branch of `AttachmentPreviewModal.svelte`, replace the single-page render with:

```ts
  let pdfDoc: any | null = $state(null);
  let pageNum: number = $state(1);
  let pageCount: number = $state(0);

  async function loadPdf(): Promise<void> {
    const pdfjsLib = await import("pdfjs-dist");
    await import("pdfjs-dist/build/pdf.worker.mjs?url").then((u: any) => {
      pdfjsLib.GlobalWorkerOptions.workerSrc = u.default;
    });
    const bytes = await getAttachmentBytes(sha256);
    pdfDoc = await pdfjsLib.getDocument({ data: bytes }).promise;
    pageCount = pdfDoc.numPages;
    pageNum = 1;
    await renderPage();
  }

  async function renderPage(): Promise<void> {
    if (!pdfDoc) return;
    const page = await pdfDoc.getPage(pageNum);
    const canvas = document.getElementById("pdf-canvas") as HTMLCanvasElement | null;
    if (!canvas) return;
    const viewport = page.getViewport({ scale: 1.25 });
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    await page.render({ canvasContext: ctx, viewport }).promise;
  }

  function prev(): void { if (pageNum > 1) { pageNum -= 1; void renderPage(); } }
  function next(): void { if (pageNum < pageCount) { pageNum += 1; void renderPage(); } }
```

In the PDF render branch's template:

```svelte
  <div class="pdf-controls">
    <button onclick={prev} disabled={pageNum <= 1}>← Prev</button>
    <span>Page {pageNum} / {pageCount}</span>
    <button onclick={next} disabled={pageNum >= pageCount}>Next →</button>
  </div>
  <canvas id="pdf-canvas"></canvas>
```

- [ ] **Step 2: Extend the existing test**

In `AttachmentPreviewModal.test.ts`, add:

```ts
  it("disables prev on page 1 and next on last page", async () => {
    // ... existing setup that mocks getAttachmentBytes for a 3-page PDF ...
    // Verify initial state: prev disabled, next enabled.
    // Click next twice. Verify: prev enabled, next disabled.
  });
```

(The full body depends on the existing test's structure — extend in place rather than copying.)

- [ ] **Step 3: Tests pass + commit**

```bash
npm test -- --run src/components/AttachmentPreviewModal.test.ts
git add ...
git commit -m "feat(gui-client): multi-page PDF preview with prev/next controls"
```

### Task B23: Wire `date_from` / `date_to` / `lang` through `SearchFiltersUI` + `FilterPopover`

**Files:**
- Modify: `gui/src/lib/api/search.ts` (SearchFiltersUI type)
- Modify: `gui/src/lib/filter_parse.ts`
- Modify: `gui/src/lib/filter_parse.test.ts`
- Modify: `gui/src/components/FilterPopover.svelte`
- Modify: `gui/src/components/FilterPopover.test.ts`
- Modify: `gui/src-tauri/src/commands/search.rs` (SearchFiltersWire struct)

**Note:** depends on Phase A being on the server. Verify `/v1/search` accepts `date_from`, `date_to`, `lang` before starting this task.

- [ ] **Step 1: Extend `SearchFiltersUI`**

```ts
export interface SearchFiltersUI {
  // … existing fields …
  dateFrom?: string;   // YYYY-MM-DD
  dateTo?: string;     // YYYY-MM-DD
  language?: string;   // ISO 639-1 lowercase
}
```

- [ ] **Step 2: Extend `SearchFiltersWire` in Rust**

```rust
#[derive(Debug, Default, Deserialize, Serialize)]
pub struct SearchFiltersWire {
    // … existing fields …
    #[serde(skip_serializing_if = "Option::is_none")]
    pub date_from: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub date_to: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub lang: Option<String>,
}
```

And in the converter from `SearchFiltersUI` JS → `SearchFiltersWire`, forward the three fields.

- [ ] **Step 3: Failing filter_parse round-trip tests**

```ts
// in filter_parse.test.ts:
it("includes date_from as after: token in DSL", () => {
  expect(formatDslTokens({ dateFrom: "2024-01-15" })).toContain("after:2024-01-15");
});
it("includes date_to as before: token", () => {
  expect(formatDslTokens({ dateTo: "2024-12-31" })).toContain("before:2024-12-31");
});
it("includes language as lang: token", () => {
  expect(formatDslTokens({ language: "en" })).toContain("lang:en");
});
it("extractDslFilters round-trips after/before/lang", () => {
  const dsl = "from:alice after:2024-01-15 before:2024-12-31 lang:en";
  const out = extractDslFilters(dsl);
  expect(out.dateFrom).toBe("2024-01-15");
  expect(out.dateTo).toBe("2024-12-31");
  expect(out.language).toBe("en");
});
```

- [ ] **Step 4: Implement extraction + emission**

In `filter_parse.ts`, extend `extractDslFilters` to populate `dateFrom`/`dateTo`/`language` from `after:`/`before:`/`lang:` tokens, and extend `formatDslTokens` to emit them.

- [ ] **Step 5: Extend `FilterPopover.svelte`**

Add three new inputs:

```svelte
  <label>From date<input type="date" bind:value={dateFrom} /></label>
  <label>To date<input type="date" bind:value={dateTo} /></label>
  <label>Language<input type="text" maxlength="5" bind:value={language} placeholder="e.g. en" /></label>
```

And in the popover's apply handler, include the new keys in the emitted filter object.

- [ ] **Step 6: Tests pass + commit**

```bash
npm test -- --run && (cd src-tauri && cargo test)
git add ...
git commit -m "feat(gui-client): date_from/date_to/lang through SearchFiltersUI + popover + DSL round-trip"
```

### Task B24: Branded icons + Tauri bundle config

**Files:**
- Create: `gui/src-tauri/icons/icon.png` (1024×1024 master)
- Generated: `gui/src-tauri/icons/*.{png,icns,ico}` (via `npx @tauri-apps/cli icon`)
- Modify: `gui/src-tauri/tauri.conf.json`
- Create: `docs/superpowers/notes/2026-05-18-bundle-smoke.md` (build notes)

- [ ] **Step 1: Create the master icon**

The brand icon is a 1024×1024 PNG with a transparent background; localmail's identity for v1 is a stylized envelope + database stack. Without a designer asset, use a quick SVG → PNG conversion: generate a flat-colour placeholder using ImageMagick or `rsvg-convert`, or commit a hand-drawn SVG and rasterize. **The icon is replaceable post-v1 without code changes.**

For the initial commit:

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-5/gui/src-tauri
mkdir -p icons
# Use a minimal SVG placeholder:
cat > /tmp/localmail-icon.svg <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <rect width="1024" height="1024" rx="180" fill="#1a73e8"/>
  <path d="M192 320 L832 320 L832 768 L192 768 Z" fill="#ffffff"/>
  <path d="M192 320 L512 560 L832 320" stroke="#1a73e8" stroke-width="32" fill="none"/>
  <circle cx="512" cy="640" r="80" fill="#1a73e8"/>
</svg>
SVG
# Render to PNG with rsvg-convert if available; otherwise use ImageMagick:
rsvg-convert -w 1024 -h 1024 /tmp/localmail-icon.svg -o icons/icon.png || \
  convert -background none -resize 1024x1024 /tmp/localmail-icon.svg icons/icon.png
```

(Document in the bundle-smoke note that the placeholder ships in v1 — a brand-designed icon is a follow-up.)

- [ ] **Step 2: Generate platform icons**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-5/gui
npx @tauri-apps/cli icon src-tauri/icons/icon.png
```

This generates `.icns`, `.ico`, and the PNG variants Tauri expects.

- [ ] **Step 3: Update `tauri.conf.json`**

Set the bundle metadata. The exact field names depend on Tauri 2's schema — use:

```json
{
  "productName": "localmail",
  "version": "0.5.0",
  "identifier": "dev.localmail.gui",
  "bundle": {
    "active": true,
    "targets": ["dmg", "msi", "appimage"],
    "icon": [
      "icons/32x32.png",
      "icons/128x128.png",
      "icons/128x128@2x.png",
      "icons/icon.icns",
      "icons/icon.ico"
    ],
    "category": "Productivity",
    "shortDescription": "Local-first IMAP mail mirror viewer",
    "longDescription": "A local-first GUI client for the localmail server. Mirrors IMAP accounts into Postgres and provides hybrid lexical+vector search across messages and attachments.",
    "copyright": "© 2026 localmail contributors",
    "macOS": { "minimumSystemVersion": "11.0" },
    "windows": { "wix": { "language": "en-US" } },
    "linux": { "appimage": { "bundleMediaFramework": false } }
  }
}
```

Note: `gui/src-tauri/Cargo.toml`'s `version` must match `0.5.0`. Update both files. `package.json` version field also needs to be `0.5.0` for consistency.

- [ ] **Step 4: Smoke the macOS build locally**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-5/gui
npm run tauri build -- --bundles dmg
```

This produces `gui/src-tauri/target/release/bundle/dmg/localmail_0.5.0_aarch64.dmg` (or x86_64 depending on arch). Verify the `.dmg` opens and the app launches.

If the build fails, the most common culprits in Tauri 2 are: (1) icon path missing → re-run step 2; (2) missing `productName` → re-check step 3; (3) `identifier` collision with another app on the keychain → bump to `dev.localmail.gui-test` and retry.

- [ ] **Step 5: Document the Windows + Linux build paths**

Create `docs/superpowers/notes/2026-05-18-bundle-smoke.md` with the exact build commands per platform, the smoke-test checklist, and any known issues found during step 4. This is the v1 release-notes scaffold.

- [ ] **Step 6: Commit**

```bash
git add gui/src-tauri/icons/ gui/src-tauri/tauri.conf.json gui/src-tauri/Cargo.toml \
        gui/package.json docs/superpowers/notes/2026-05-18-bundle-smoke.md
git commit -m "feat(gui-client): branded icons + tauri bundle config (.dmg/.msi/.AppImage)"
```

### Task B25: End-to-end smoke + open PR

**Files:**
- Modify: (whichever changed during smoke that needs touching)

- [ ] **Step 1: Run the full client test suite**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-5/gui
npm run check
npm test -- --run
(cd src-tauri && cargo test)
```

All green.

- [ ] **Step 2: Manual smoke**

```bash
npm run tauri dev
```

Walk through:
1. Settings → Server: read URL + pin, change-password form (cancel if no test account).
2. Settings → Display: switch density and watch MessageList re-render. Switch image policy and verify HtmlBody behaviour on a known HTML message.
3. Settings → Search: tick debug → DebugBadges appear on result rows; ReadingPane shows DebugChunks (or just `matched_arms` chips per Task B21 step 1).
4. Settings → About: open log directory.
5. Run a search with `after:2024-01-01 before:2024-12-31 lang:en` and confirm results scope correctly (Phase A must be live on the server).
6. Open a PDF attachment with 3+ pages → prev/next works.
7. Open the Raw body of a message → bytes render.
8. Click "Show full headers" → headers list renders.
9. Drag the splitters → column widths persist across full app restart.
10. Force the server to return `api_major=2` (or run against an older server) → VersionGate modal appears with `[Quit]`.
11. Sit on the message list for >30s with the daemon picking up new mail → new messages appear automatically without manual refresh.

- [ ] **Step 3: Push + open PR**

```bash
git push -u origin gui-client-5
gh pr create --title "feat(gui-client): Sub-plan 5 — polish + packaging" --body "$(cat <<'EOF'
## Summary
- Wires `date_from` / `date_to` / `lang` filters end-to-end (server Phase A).
- Adds Settings screen (Server, Display, Search, About), VersionGate, change-poller, splitter, header-unfold, raw RFC822 view, search debug pane, multi-page PDF preview.
- Produces `.dmg` / `.msi` / `.AppImage` bundles via `npm run tauri build` with branded (placeholder) icons.

## Test plan
- [ ] `npm run check`
- [ ] `npm test -- --run`
- [ ] `(cd gui/src-tauri && cargo test)`
- [ ] Manual smoke per task B25 step 2
- [ ] `npm run tauri build -- --bundles dmg` produces a runnable bundle

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Wait for review + iterate**

After PR review, address any feedback as new commits on `gui-client-5`. Don't squash-merge until the user signs off on the smoke.

---

## Cross-cutting concerns

### Test runtime gotchas (carried forward from Sub-plan 4)

- `@testing-library/jest-dom` is NOT installed. Use `toBeTruthy()` / `toBeFalsy()` / `.textContent` checks. Never `toBeInTheDocument()`.
- Rust structs used as Tauri command arguments need `#[derive(Deserialize)]`.
- The existing `AuthError::Io(String)` variant (added in Sub-plan 4) is fine for new error-mapping paths.
- `pdfjs-dist/build/pdf.worker.mjs?url` already works under Vite for `npm test` and `tauri dev`.

### Persistence keys

All localStorage keys follow `localmail.gui.<scope>`:
- `localmail.gui.paneWidths` (B1–B3)
- `localmail.gui.settings` (B15)

If a future setting moves between scopes, write a one-shot migration in the loader; don't rename keys silently.

### Settings → component plumbing summary

| Setting | Read in | Effect |
|---|---|---|
| `density` | MessageListRow | `class:compact` toggle |
| `dateFormat` | format.ts (callers: ReadingPane, MessageListRow) | switches relative/absolute formatter |
| `imagePolicy` | HtmlBody | initial value of externalImagesAllowed |
| `pageSize` | search store | limit param on /v1/search |
| `defaultLanguage` | search store | injects lang filter when none set |
| `debug` | MessageListRow, ReadingPane | gates DebugBadges + DebugChunks |

---

## Self-review checklist results

**Spec coverage:**
- [x] Polling — B4–B6
- [x] Splitter — B1–B3
- [x] Version-mismatch modal — B7–B8
- [x] Settings (Server, Display, Search, About) — B15–B19
- [x] Header-unfold widget — B12–B14
- [x] Raw RFC822 — B9–B11
- [x] Search debug pane — B20–B21
- [x] Multi-page PDF — B22
- [x] date_from / date_to / lang — A1 + A2 + B23
- [x] Branded icons + bundles — B24
- [x] Smoke + PR — B25

**Placeholder scan:** No "TBD", "TODO", "later", or "similar to". All steps include code or commands. The one tentative branch is B21 step 1 (Option A vs Option B) — this is a real schema-check, not a placeholder.

**Type consistency:**
- `MessageSummary` imported consistently from `lib/api/changes.ts`.
- `VersionInfo` defined in `lib/version_check.ts`; `ServerVersionInfo` extends it in `lib/api/version.ts`.
- `SearchFiltersUI` field names: camelCase (`dateFrom`, `dateTo`, `language`) — Rust wire struct uses snake_case (`date_from`, `date_to`, `lang`) via `#[derive(Serialize)]` default; the converter `searchFiltersUIToWire` (in `lib/api/search.ts`) maps between them.
- `SettingsSnapshot` field names match the setter method names: `density` ↔ `setDensity`, etc.
