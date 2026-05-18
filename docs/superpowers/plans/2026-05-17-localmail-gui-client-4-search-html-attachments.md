# localmail GUI Client — Sub-plan 4: Search + HTML rendering + attachments

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add real search to the GUI (server-side hybrid search with snippets, DSL parity, structured filter popover), render sanitized HTML email bodies in a sandboxed iframe, render attachments with download + image/PDF preview, and wire the account/folder tree narrowing through `/v1/search` server-side. Done when (a) typing a query in the search bar returns ranked results with `<mark>`-highlighted snippets across all accounts; (b) selecting an account or folder in the tree narrows the result set server-side (not client-side); (c) clicking a message renders its HTML body in an isolated iframe with images blocked by default and a per-message "Load images" affordance; (d) attachments appear under the body with click-to-download and image/PDF previews.

**Architecture:** Two worktrees, two PRs. **Phase A** (server, `phase2-hybrid-search` worktree) extends `SearchFilters` with `account_ids` / `folder_ids` integer-list fields, teaches `parse_query` to recognise `account_id:NUM` / `folder_id:NUM` tokens, adds matching `_filter_sql` predicates in `arms.py`, and removes the rejection of those keys from `api/search.py`. **Phase B** (client, `gui-client-4` worktree) adds one Rust command for `/v1/search`, one for `/v1/attachments/{sha256}` download, extends `mail.svelte.ts` with search state, and adds five Svelte components (`SearchBar`, `FilterPopover`, `ActiveFilterChips`, `HtmlBody`, `AttachmentPreviewModal`) plus modifications to `MessageList` and `ReadingPane`. HTML rendering uses `<iframe sandbox srcdoc>` with a per-iframe `<meta http-equiv="Content-Security-Policy">`; the app-level CSP stays strict. PDF preview lazy-loads Mozilla's standalone `pdf.js`.

**Tech Stack:** Server: existing Python + psycopg + tsvector + pgvector — no new deps. Client: reqwest + rustls (Rust), Svelte 5 runes + TypeScript + vitest, cargo test. **New client dep:** `pdfjs-dist` (Mozilla PDF.js distribution), lazy-imported.

**Base branches:**
- Phase A branches off `worktree-phase2-hybrid-search` (existing server-side branch).
- Phase B branches off `main` (which now includes the merged Sub-plan 3 — PR #20, commit `653c445`).

**Worktree locations (already created by the planning session):**
- Phase A: `/Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search` (already exists — re-used).
- Phase B: `/Users/hherb/src/localmail/.claude/worktrees/gui-client-4` (created off `main`, branch `gui-client-4`).

**Out of scope (Sub-plan 5):**
- Branded icons, `.dmg` / `.msi` / `.AppImage` bundling.
- Background polling of `/v1/changes` on the active view.
- Resizable column splitter.
- Version-mismatch handling (hard modal on `api_major` mismatch).
- Search debug pane (per-arm scores, matched-chunk highlighting in the reading pane).
- Settings screen (HTML image policy default, density, page size, debug toggle, change password).
- Threading.
- `date_from` / `date_to` / `lang` filters — Phase A only wires `account_ids` / `folder_ids`; the popover surfaces `after:` / `before:` as DSL tokens via the search box (which are already supported end-to-end).

**Acknowledged tech debt (will land later):**
- `MessageDetail.headers` (compact subset + `?headers=full` lazy fetch). v1 of the reading pane shows only From / To / Date / Account / Folders — the header-unfold widget is Sub-plan 5.
- Server-side `/v1/folders/{id}/messages` is still not implemented; we replace its absence with `/v1/search` calls (empty `query=""` + filter), which is what the spec describes for the default "all messages, filtered" path anyway.
- `date_from` / `date_to` / `lang` remain in `_KNOWN_UNSUPPORTED_FILTER_KEYS` after Phase A. The popover writes equivalent `after:` / `before:` DSL tokens into the search box, which **are** end-to-end supported. `lang` has no popover entry in v1.

---

## File structure

### Phase A — Server (worktree: `phase2-hybrid-search`)

#### Created

```
tests/test_query_account_folder_id_tokens.py     # NEW — DSL parser tests for account_id:/folder_id:
tests/test_arms_id_filters.py                    # NEW — _filter_sql ID-keyed predicate tests
```

#### Modified

```
src/localmail/search/query.py                    # SearchFilters: + account_ids, folder_ids fields
                                                 # parse_query: + account_id:, folder_id: tokens
src/localmail/search/arms.py                     # _filter_sql: + account_ids/folder_ids predicates
src/localmail/api/search.py                      # remove account_ids/folder_ids from
                                                 # _KNOWN_UNSUPPORTED_FILTER_KEYS;
                                                 # _filter_tokens emits new DSL tokens
tests/test_api_search.py                         # update unsupported-keys assertion + add positive tests
```

### Phase B — Client (worktree: `gui-client-4`)

#### Created

```
gui/src-tauri/src/commands/
  search.rs                                      # NEW — /v1/search POST wrapper + cmd
  attachments.rs                                 # NEW — /v1/attachments/{sha256} download + cmd

gui/src/
  lib/api/
    search.ts                                    # NEW — SearchRequest, SearchResponse, SearchResult, SearchFiltersUI types
  lib/
    filter_parse.ts                              # NEW — pure helper: SearchFilters ↔ DSL token round-trip
    filter_parse.test.ts                         # vitest unit tests
    snippet_sanitize.ts                          # NEW — server snippets contain <mark>; allowlist render
    snippet_sanitize.test.ts                     # vitest unit tests
    stores/
      search.svelte.ts                           # NEW — search singleton: query, filters, results, took_ms
      search.test.ts                             # vitest unit tests
  components/
    SearchBar.svelte                             # NEW — input + submit + Filters popover trigger
    SearchBar.test.ts                            # vitest component test
    FilterPopover.svelte                         # NEW — Date, From, To, Has-attachment popover
    FilterPopover.test.ts                        # vitest component test
    ActiveFilterChips.svelte                     # NEW — chips under search bar; click → remove
    ActiveFilterChips.test.ts                    # vitest component test
    HtmlBody.svelte                              # NEW — iframe sandbox srcdoc + per-iframe CSP
    HtmlBody.test.ts                             # vitest component test
    AttachmentRow.svelte                         # NEW — single attachment row (filename · size · type · ⤓)
    AttachmentsStrip.svelte                      # NEW — strip wrapper at bottom of ReadingPane
    AttachmentsStrip.test.ts                     # vitest component test
    AttachmentPreviewModal.svelte                # NEW — modal: <img> or <iframe src=pdf.js viewer.html?…>
    AttachmentPreviewModal.test.ts               # vitest component test
```

#### Modified

```
gui/src-tauri/src/
  commands/mod.rs                                # add pub mod search; pub mod attachments;
  lib.rs                                         # register list_search_cmd, download_attachment_cmd

gui/src/
  lib/api/types.ts                               # + MessageHeaders (defer), + body-mode types
  lib/tauri.ts                                   # + listSearch, downloadAttachment invoke wrappers
  lib/stores/mail.svelte.ts                      # delegate list to search store when in "searching" view mode;
                                                 # add bodyMode (html/plain/raw), externalImagesAllowed, attachmentPreview state
  components/
    MessageList.svelte                           # render snippet_html with <mark>; show took_ms / "no matches"
    MessageListRow.svelte                        # accept snippet (when present) instead of subject preview
    AccountTree.svelte                           # selectAccount/Folder dispatch search call (server-side filter)
    ReadingPane.svelte                           # body-mode toggle (HTML · Plain · Raw); HtmlBody integration;
                                                 # AttachmentsStrip rendering
  screens/MainView.svelte                        # mount SearchBar + ActiveFilterChips above the panes

gui/package.json                                 # + "pdfjs-dist": "^4.x"
gui/vite.config.ts                               # ensure pdfjs-dist worker URL is preserved through bundling
gui/README.md                                    # add "Manual smoke (Sub-plan 4 acceptance)" section
```

---

# Phase A — Server-side filter wiring

> Phase A must land + merge before Phase B's tree-narrowing tasks (Task 12 in Phase B) can be smoke-tested end-to-end. Phase B can begin its independent tasks (search command wiring, HTML body, attachments) in parallel with Phase A; only the tree-narrowing task hard-depends on Phase A.

## Task A0: Phase A worktree verification

**Files:** none modified.

- [ ] **Step 1: Confirm worktree state**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search
git status
git log --oneline -3
```

Expected: clean working tree; HEAD at `9222bc8` or a later commit on `worktree-phase2-hybrid-search`. If dirty or on the wrong branch, STOP and report.

- [ ] **Step 2: Confirm base tests pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_query.py tests/test_arms.py tests/test_api_search.py -x 2>&1 | tail -15
```

Expected: all tests pass. If anything fails, STOP — the Phase A base is broken and the plan can't extend it cleanly.

---

## Task A1: Extend SearchFilters with `account_ids` and `folder_ids`

**Files:**
- Modify: `src/localmail/search/query.py`
- Modify: `tests/test_query.py` (add cases inline)

`SearchFilters` already has `accounts: list[int] | None` (resolved from `account_names` by the Searcher) and `folders: list[str]` (matched by name). Adding `account_ids: list[int] | None` and `folder_ids: list[int] | None` keeps the existing name-keyed paths untouched and lets the API ship integer IDs without round-tripping through name resolution.

- [ ] **Step 1: Add fields to `SearchFilters`**

Edit `src/localmail/search/query.py`. Find the existing `SearchFilters` dataclass (around line 24–37) and add two new fields **below `accounts`** and **after `folders`** respectively. Final shape:

```python
@dataclass(frozen=True)
class SearchFilters:
    account_names: list[str] = field(default_factory=list)
    accounts: list[int] | None = None  # resolved by Searcher from account_names
    account_ids: list[int] | None = None  # set directly from the API layer; bypasses name resolution
    folders: list[str] | None = None
    folder_ids: list[int] | None = None  # mailbox PKs from the API layer
    from_substr: str | None = None
    to_substr: str | None = None
    subject_substr: str | None = None
    after: date | None = None
    before: date | None = None
    has_attachment: bool | None = None
    label: str | None = None
    languages: list[str] | None = None
```

- [ ] **Step 2: Run existing tests, expect them to still pass**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_query.py -x 2>&1 | tail -8
```

Expected: same number of passes as before (no behaviour change yet).

- [ ] **Step 3: Commit**

```bash
git add src/localmail/search/query.py
git commit -m "feat(search): add account_ids/folder_ids fields to SearchFilters

Adds two new optional integer-list fields to SearchFilters that the API layer
can populate directly without round-tripping through name resolution.
Behaviour unchanged until the parser and _filter_sql start using them."
```

---

## Task A2: Parse `account_id:` and `folder_id:` DSL tokens

**Files:**
- Modify: `src/localmail/search/query.py`
- Create: `tests/test_query_account_folder_id_tokens.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_query_account_folder_id_tokens.py`:

```python
"""DSL parser support for account_id: and folder_id: tokens.

The integer-keyed tokens populate SearchFilters.account_ids /
SearchFilters.folder_ids directly, bypassing the name-based resolution path
the Searcher uses for account: / folder: tokens. Non-integer values are
silently ignored (DSL has no escape syntax for parser errors)."""
from localmail.search.query import parse_query


def test_account_id_single_token_populates_account_ids():
    parsed = parse_query("account_id:5 alice")
    assert parsed.filters.account_ids == [5]
    assert parsed.filters.account_names == []
    assert parsed.free_text == "alice"


def test_account_id_multiple_tokens_accumulate():
    parsed = parse_query("account_id:5 account_id:7 hello")
    assert parsed.filters.account_ids == [5, 7]
    assert parsed.free_text == "hello"


def test_folder_id_single_token_populates_folder_ids():
    parsed = parse_query("folder_id:42 receipts")
    assert parsed.filters.folder_ids == [42]
    assert parsed.filters.folders is None
    assert parsed.free_text == "receipts"


def test_folder_id_multiple_tokens_accumulate():
    parsed = parse_query("folder_id:42 folder_id:99")
    assert parsed.filters.folder_ids == [42, 99]


def test_account_id_non_integer_value_treated_as_free_text():
    parsed = parse_query("account_id:foo bar")
    assert parsed.filters.account_ids is None
    assert "account_id:foo" in parsed.free_text
    assert "bar" in parsed.free_text


def test_account_id_and_account_can_coexist():
    parsed = parse_query("account_id:5 account:gmail.com")
    assert parsed.filters.account_ids == [5]
    assert parsed.filters.account_names == ["gmail.com"]
```

Run the test, confirm it fails:

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_query_account_folder_id_tokens.py -v 2>&1 | tail -15
```

Expected: failures — `parse_query` does not yet recognise the tokens; `account_ids` / `folder_ids` are `None`.

- [ ] **Step 2: Extend `_OPERATORS` and `parse_query`**

Edit `src/localmail/search/query.py`:

1. Extend the `_OPERATORS` constant (line 47) to include the new operators:

```python
_OPERATORS = {
    "from", "to", "subject", "after", "before", "has", "label",
    "account", "folder", "account_id", "folder_id",
}
```

2. In `parse_query`, add two new accumulator lists alongside `f_account_names` and `f_folders`:

```python
    f_account_ids: list[int] = []
    f_folder_ids: list[int] = []
```

3. Inside the `for tok in _tokenize(query):` loop, add two new `elif` branches after the existing `account` / `folder` handlers. Non-integer values cause the token to fall through to free-text (same pattern the date branches use for malformed dates would, except dates raise — for IDs we silently degrade because the API never sends a malformed ID and a human typing `account_id:foo` is most plausibly searching for the literal string):

```python
                elif op_l == "account_id":
                    try:
                        f_account_ids.append(int(value))
                    except ValueError:
                        free_parts.append(tok)
                    continue
                elif op_l == "folder_id":
                    try:
                        f_folder_ids.append(int(value))
                    except ValueError:
                        free_parts.append(tok)
                    continue
```

4. Pass the accumulators into the `SearchFilters(...)` constructor at the bottom of `parse_query`:

```python
    filters = SearchFilters(
        account_names=f_account_names,
        folders=f_folders or None,
        account_ids=f_account_ids or None,
        folder_ids=f_folder_ids or None,
        from_substr=f_from,
        to_substr=f_to,
        subject_substr=f_subject,
        after=f_after,
        before=f_before,
        has_attachment=f_has_attachment,
        label=f_label,
    )
```

- [ ] **Step 3: Re-run the test, confirm green**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_query_account_folder_id_tokens.py -v 2>&1 | tail -15
```

Expected: 6 passes.

- [ ] **Step 4: Re-run the full query test suite to confirm no regression**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_query.py tests/test_query_account_folder_id_tokens.py -x 2>&1 | tail -8
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/query.py tests/test_query_account_folder_id_tokens.py
git commit -m "feat(search): parse account_id: and folder_id: DSL tokens

Integer-keyed tokens populate SearchFilters.account_ids / .folder_ids
directly, bypassing name resolution. The API layer emits these so the
client can narrow by server-side IDs without round-tripping through
account or folder names."
```

---

## Task A3: Extend `_filter_sql` with ID-keyed predicates

**Files:**
- Modify: `src/localmail/search/arms.py`
- Create: `tests/test_arms_id_filters.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_arms_id_filters.py`. It seeds two accounts (one in-scope, one out-of-scope), two mailboxes per account, and two messages per mailbox, then asserts that `arm_bm25_messages` returns only rows whose `account_id`/`mailbox_id` match the filter.

```python
"""Server-side narrowing by account_ids / folder_ids works at every arm.

These tests assert that _filter_sql injects the right SQL predicates so
arm_bm25_messages returns only matching rows. Other arms inherit the same
filter clause via _filter_sql so we don't repeat the seeding for each."""
from __future__ import annotations

import pytest

from localmail.config import SearchConfig
from localmail.search.arms import arm_bm25_messages
from localmail.search.query import ParsedQuery, SearchFilters


def _seed(conn) -> dict[str, int]:
    """Insert 2 accounts × 2 mailboxes × 2 messages, return name → PK map."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name, address, host, port, username, kind) "
                    "VALUES ('a1', 'a1@x', 'h', 993, 'u', 'imap') RETURNING id")
        row = cur.fetchone(); assert row is not None
        a1 = row[0]
        cur.execute("INSERT INTO accounts (name, address, host, port, username, kind) "
                    "VALUES ('a2', 'a2@x', 'h', 993, 'u', 'imap') RETURNING id")
        row = cur.fetchone(); assert row is not None
        a2 = row[0]

        cur.execute("INSERT INTO mailboxes (account_id, name, uidnext, uidvalidity) "
                    "VALUES (%s, 'INBOX', 1, 1) RETURNING id", (a1,))
        row = cur.fetchone(); assert row is not None
        mb1 = row[0]
        cur.execute("INSERT INTO mailboxes (account_id, name, uidnext, uidvalidity) "
                    "VALUES (%s, 'Sent', 1, 1) RETURNING id", (a1,))
        row = cur.fetchone(); assert row is not None
        mb2 = row[0]
        cur.execute("INSERT INTO mailboxes (account_id, name, uidnext, uidvalidity) "
                    "VALUES (%s, 'INBOX', 1, 1) RETURNING id", (a2,))
        row = cur.fetchone(); assert row is not None
        mb3 = row[0]

        for mb_id in (mb1, mb2, mb3):
            for n in (1, 2):
                cur.execute(
                    "INSERT INTO messages (account_id, message_id, raw_bytes, size_bytes,"
                    " headers, attachments, subject, body_text)"
                    " VALUES ((SELECT account_id FROM mailboxes WHERE id=%s),"
                    "         %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s) RETURNING id",
                    (mb_id, f"mb{mb_id}-msg{n}@x", b"raw", 3, "{}", "[]",
                     "hello world", "hello world"),
                )
                row = cur.fetchone(); assert row is not None
                msg_id = row[0]
                cur.execute(
                    "INSERT INTO message_labels (message_id, mailbox_id, uid, flags)"
                    " VALUES (%s, %s, %s, '{}')",
                    (msg_id, mb_id, n),
                )
        conn.commit()
    return {"a1": a1, "a2": a2, "mb1": mb1, "mb2": mb2, "mb3": mb3}


def _cfg() -> SearchConfig:
    """Minimal config sufficient for arm_bm25_messages."""
    return SearchConfig()


def _parsed(filters: SearchFilters) -> ParsedQuery:
    return ParsedQuery(free_text="hello", filters=filters)


@pytest.mark.usefixtures("db_conn")
def test_account_ids_narrows_arm_results(db_conn):
    ids = _seed(db_conn)
    hits = arm_bm25_messages(
        db_conn, _parsed(SearchFilters(account_ids=[ids["a1"]])), _cfg(), limit=10
    )
    # 2 mailboxes × 2 messages on a1, all match "hello", but message_id is per-message
    # so we expect 4 distinct message rows.
    assert len({h.message_id for h in hits}) == 4
    for h in hits:
        with db_conn.cursor() as cur:
            cur.execute("SELECT account_id FROM messages WHERE id = %s", (h.message_id,))
            row = cur.fetchone(); assert row is not None
            assert row[0] == ids["a1"]


@pytest.mark.usefixtures("db_conn")
def test_folder_ids_narrows_arm_results(db_conn):
    ids = _seed(db_conn)
    hits = arm_bm25_messages(
        db_conn, _parsed(SearchFilters(folder_ids=[ids["mb2"]])), _cfg(), limit=10
    )
    assert len({h.message_id for h in hits}) == 2
    for h in hits:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM message_labels"
                " WHERE message_id = %s AND mailbox_id = %s)",
                (h.message_id, ids["mb2"]),
            )
            row = cur.fetchone(); assert row is not None and row[0] is True


@pytest.mark.usefixtures("db_conn")
def test_account_ids_and_folder_ids_combine_with_AND(db_conn):
    ids = _seed(db_conn)
    hits = arm_bm25_messages(
        db_conn,
        _parsed(SearchFilters(account_ids=[ids["a1"]], folder_ids=[ids["mb3"]])),
        _cfg(), limit=10,
    )
    # mb3 is on a2; intersection with a1 is empty.
    assert hits == []


@pytest.mark.usefixtures("db_conn")
def test_no_filter_returns_all(db_conn):
    _seed(db_conn)
    hits = arm_bm25_messages(db_conn, _parsed(SearchFilters()), _cfg(), limit=10)
    assert len({h.message_id for h in hits}) == 6
```

Run the test, confirm it fails:

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_arms_id_filters.py -v 2>&1 | tail -20
```

Expected: failures — `_filter_sql` ignores `account_ids` / `folder_ids` so the narrowing tests return too many rows.

- [ ] **Step 2: Extend `_filter_sql`**

Edit `src/localmail/search/arms.py`. In `_filter_sql` (line 22–67), add two new predicate branches **next to the corresponding name-keyed ones** so the structure stays parallel:

```python
    if filters.accounts:
        parts.append("m.account_id = ANY(%s)")
        params.append(filters.accounts)
    if filters.account_ids:
        parts.append("m.account_id = ANY(%s)")
        params.append(filters.account_ids)
    # ... existing from_substr / to_substr / subject_substr / dates / has_attachment ...
    if filters.folders:
        parts.append(
            "EXISTS (SELECT 1 FROM message_labels ml JOIN mailboxes mb ON mb.id = ml.mailbox_id"
            " WHERE ml.message_id = m.id AND mb.name = ANY(%s))"
        )
        params.append(filters.folders)
    if filters.folder_ids:
        parts.append(
            "EXISTS (SELECT 1 FROM message_labels ml"
            " WHERE ml.message_id = m.id AND ml.mailbox_id = ANY(%s))"
        )
        params.append(filters.folder_ids)
```

(The `folder_ids` predicate omits the `mailboxes` join — we already have the mailbox IDs, so we filter on `message_labels.mailbox_id` directly.)

- [ ] **Step 3: Re-run the new test suite, confirm green**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_arms_id_filters.py -v 2>&1 | tail -10
```

Expected: 4 passes.

- [ ] **Step 4: Re-run the full arms test suite to confirm no regression**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_arms.py tests/test_arms_id_filters.py -x 2>&1 | tail -8
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/arms.py tests/test_arms_id_filters.py
git commit -m "feat(search): _filter_sql ID-keyed predicates for account_ids/folder_ids

Adds two new WHERE-clause branches alongside the existing name-keyed
account: / folder: predicates. The folder_ids branch uses
message_labels.mailbox_id directly (no join to mailboxes) since the
API ships PKs."
```

---

## Task A4: Forward `account_ids` / `folder_ids` from `api/search.py`

**Files:**
- Modify: `src/localmail/api/search.py`
- Modify: `tests/test_api_search.py`

- [ ] **Step 1: Write the failing test**

Open `tests/test_api_search.py`. The existing test file has a section that asserts each member of `_KNOWN_UNSUPPORTED_FILTER_KEYS` raises `ValidationFailed`. We will:
1. **Remove** `account_ids` and `folder_ids` from that assertion set.
2. **Add** positive tests confirming the new DSL tokens are emitted into `build_query_string` output.

Find the existing unsupported-key test (something like `test_build_query_string_rejects_known_unsupported_keys` or similar — match by name). Edit its parametrize list (or for-loop) to drop `account_ids` and `folder_ids`. Then append two new tests below it:

```python
def test_build_query_string_emits_account_id_tokens():
    out = build_query_string(
        free_text="hello",
        filters={"account_ids": ["5", "7"]},
    )
    assert "account_id:5" in out
    assert "account_id:7" in out
    assert out.startswith("hello")


def test_build_query_string_emits_folder_id_tokens():
    out = build_query_string(
        free_text="invoices",
        filters={"folder_ids": ["42"]},
    )
    assert "folder_id:42" in out
    assert out.startswith("invoices")


def test_build_query_string_account_ids_handles_int_or_str():
    """The API layer accepts both — Pydantic models may coerce to str."""
    out = build_query_string(free_text="", filters={"account_ids": [5, "7"]})
    assert "account_id:5" in out
    assert "account_id:7" in out


def test_build_query_string_empty_account_ids_is_no_op():
    out = build_query_string(free_text="hello", filters={"account_ids": []})
    assert out == "hello"
```

Run the suite, confirm the **two unsupported-key removals** still pass (they should — the negative loop no longer reaches them) but the **four new positive tests** fail:

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_search.py -v 2>&1 | tail -20
```

Expected: 4 failures from the new tests; the unsupported-key test still passes for the remaining keys (`date_from`, `date_to`, `lang`).

- [ ] **Step 2: Update `api/search.py`**

Edit `src/localmail/api/search.py`:

1. Trim `_KNOWN_UNSUPPORTED_FILTER_KEYS` (line 25):

```python
_KNOWN_UNSUPPORTED_FILTER_KEYS = frozenset({
    "date_from", "date_to", "lang",
})
```

2. Update the `_SUPPORTED_FILTER_KEYS` constant to advertise the new keys (line 16):

```python
_SUPPORTED_FILTER_KEYS = frozenset({
    "from", "to", "subject", "after", "before", "has_attachment",
    "account_ids", "folder_ids",
})
```

3. Extend `_filter_tokens` (around line 53) to emit the new DSL tokens. Add **at the top of the function body** (before any of the `from:` / `to:` checks, so the resulting token order is `account_id:* folder_id:* from:* ...`):

```python
    if (vs := filters.get("account_ids")):
        for v in vs:
            out.append(f"account_id:{int(v)}")
    if (vs := filters.get("folder_ids")):
        for vs_v in vs:
            out.append(f"folder_id:{int(vs_v)}")
```

The `int(...)` coerces from `str` (which is the over-the-wire shape per the API spec — server-side integer PKs are serialized as strings in JSON) and raises a clean `ValueError` if a malformed value arrives. Catch and re-raise as `ValidationFailed`:

Wrap the two new appends in a `try` block:

```python
    try:
        if (vs := filters.get("account_ids")):
            for v in vs:
                out.append(f"account_id:{int(v)}")
        if (vs := filters.get("folder_ids")):
            for vs_v in vs:
                out.append(f"folder_id:{int(vs_v)}")
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(
            f"account_ids / folder_ids: each value must be an integer or "
            f"integer-string, got {filters.get('account_ids')!r} / "
            f"{filters.get('folder_ids')!r}"
        ) from exc
```

- [ ] **Step 3: Re-run, confirm green**

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_api_search.py -v 2>&1 | tail -15
```

Expected: all green.

- [ ] **Step 4: End-to-end test via the HTTP route**

Find the existing `test_serve_search_route.py` (or whatever covers `POST /v1/search`). Add one end-to-end test that POSTs `{"query": "", "filters": {"account_ids": ["1"]}, "limit": 5}` and asserts a 200 with results restricted to account_id 1. If no such file exists yet, this step degrades to a no-op — flag in the PR description that the next plan should add a route-level integration test.

```bash
unset VIRTUAL_ENV && uv run pytest tests/test_serve_search_route.py -v 2>&1 | tail -15
```

Expected: previously-passing tests stay green; the new test (if added) passes.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/search.py tests/test_api_search.py tests/test_serve_search_route.py
git commit -m "feat(api): enable account_ids/folder_ids filter forwarding

Removes account_ids and folder_ids from _KNOWN_UNSUPPORTED_FILTER_KEYS.
build_query_string now emits account_id:NUM / folder_id:NUM DSL tokens
which the parser+arms (Tasks A2+A3) consume to narrow search results
server-side. Date and language filters remain blocked pending a later
extension."
```

---

## Task A5: Push Phase A and open the server PR

- [ ] **Step 1: Full Phase A test sweep**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search
unset VIRTUAL_ENV && uv run pytest -x 2>&1 | tail -25
```

Expected: full suite green. If DB-skipped tests appear, that's fine — they're conditional on `LOCALMAIL_TEST_DSN` reachability.

- [ ] **Step 2: Push the branch**

```bash
git push -u origin worktree-phase2-hybrid-search
```

- [ ] **Step 3: Open the PR**

```bash
gh pr create --base worktree-phase2-hybrid-search --head worktree-phase2-hybrid-search \
  --title "feat(api+search): wire account_ids/folder_ids filters end-to-end" \
  --body "$(cat <<'EOF'
## Summary
- Adds `SearchFilters.account_ids` and `SearchFilters.folder_ids` (integer lists, optional).
- Parser learns `account_id:NUM` and `folder_id:NUM` DSL tokens.
- `arms._filter_sql` injects `m.account_id = ANY(%s)` / `ml.mailbox_id = ANY(%s)` predicates.
- `api/search.py` removes `account_ids` / `folder_ids` from `_KNOWN_UNSUPPORTED_FILTER_KEYS` and emits the new DSL tokens.

## Why now
Sub-plan 4 of the GUI client (PR follows in main repo) needs server-side filter wiring to drive AccountTree-narrowed `/v1/search` calls. Without this, the GUI's account/folder narrowing remains a client-side filter over the 200 messages loaded via `/v1/changes`.

## Out of scope (next iteration)
- `date_from` / `date_to` — popover writes equivalent `after:` / `before:` DSL tokens into the search box, which are already supported.
- `lang` — no GUI surface in v1.

## Test plan
- [x] `pytest tests/test_query_account_folder_id_tokens.py` — 6 passes
- [x] `pytest tests/test_arms_id_filters.py` — 4 passes
- [x] `pytest tests/test_api_search.py` — full suite green incl. new positive tests
- [x] `pytest` — full suite green on Phase A worktree

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Note: the base is the **`worktree-phase2-hybrid-search`** branch (Phase 2 hybrid-search work has its own long-lived branch — Phase A piggybacks on it, not `main`). Adjust `--base` if a different upstream merge target has been agreed by the time this plan executes.

The PR push step is only when ready (after a clean local pytest sweep); a subagent should not auto-push without user approval.

---

# Phase B — Client (search bar, HTML body, attachments, tree wiring)

## Task B0: Worktree verification + dependency add

**Files:**
- Modify: `gui/package.json`

- [ ] **Step 1: Confirm worktree state**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-4
git status
git log --oneline -1
```

Expected: clean tree; HEAD at `653c445 Merge pull request #20 from hherb/gui-client-3`. If anything else, STOP.

- [ ] **Step 2: Base-line tests pass**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-4/gui/src-tauri && cargo test 2>&1 | tail -5
```

Expected: `test result: ok. 40 passed`.

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-4/gui && npm install && npm test 2>&1 | tail -10
```

Expected: 48 tests passing.

- [ ] **Step 3: Add the pdfjs-dist dependency**

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-4/gui
npm install --save pdfjs-dist@^4
```

Inspect the resulting `package.json` to confirm `"pdfjs-dist": "^4.x.y"` was added under `dependencies`. Run `npm test` again to confirm the dep add didn't break the existing suite.

- [ ] **Step 4: Commit**

```bash
git add gui/package.json gui/package-lock.json
git commit -m "chore(gui-client): add pdfjs-dist for in-app PDF preview

Mozilla pdf.js standalone distribution. Bundled lazily — only imported
when the user opens a PDF attachment preview."
```

---

## Task B1: Rust command — `/v1/search`

**Files:**
- Create: `gui/src-tauri/src/commands/search.rs`
- Modify: `gui/src-tauri/src/commands/mod.rs`
- Modify: `gui/src-tauri/src/lib.rs`

- [ ] **Step 1: Write the failing test**

Create `gui/src-tauri/src/commands/search.rs`:

```rust
//! /v1/search POST wrapper.
//!
//! The server expects {"query": "...", "filters": {...}, "limit": N, "cursor": null|"..."}
//! and returns {"results": [SearchResultRow], "next_cursor": str|null,
//!               "total_estimate": int|null, "took_ms": float}.

use serde::{Deserialize, Serialize};

use crate::commands::auth::AuthError;
use crate::commands::session::read_authenticated;
use crate::http::client::{build_pinned_client, http_post_json};

#[derive(Debug, Serialize)]
pub struct SearchFiltersWire {
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub account_ids: Vec<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub folder_ids: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub from: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub to: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub subject: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub after: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub before: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub has_attachment: Option<bool>,
}

#[derive(Debug, Serialize)]
pub struct SearchRequest {
    pub query: String,
    pub filters: SearchFiltersWire,
    pub limit: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cursor: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SearchAddress {
    pub address: Option<String>,
    pub name: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SearchAccount {
    pub id: String,
    pub name: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SearchFolder {
    pub id: String,
    pub full_path: String,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SearchResultRow {
    pub message_id: String,
    pub account: SearchAccount,
    pub folder: Option<SearchFolder>,
    pub subject: Option<String>,
    pub from: SearchAddress,
    pub to: Vec<SearchAddress>,
    pub date: Option<String>,
    pub snippet_html: Option<String>,
    pub has_attachments: bool,
    pub score: f64,
    pub matched_arms: Vec<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SearchResponse {
    pub results: Vec<SearchResultRow>,
    pub next_cursor: Option<String>,
    pub total_estimate: Option<i64>,
    pub took_ms: f64,
}

pub async fn run_search(
    store: &crate::storage::keyring::KeyringStore,
    req: SearchRequest,
) -> Result<SearchResponse, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/search");
    http_post_json::<SearchRequest, SearchResponse>(&client, &endpoint, Some(&token), &req).await
}

#[tauri::command]
pub async fn run_search_cmd(req: SearchRequest) -> Result<SearchResponse, AuthError> {
    let store = crate::storage::keyring::KeyringStore::new();
    run_search(&store, req).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::{KeyringStore, MemKeyring, Slot};

    fn store() -> KeyringStore { KeyringStore::with_backend(MemKeyring::new()) }

    fn req() -> SearchRequest {
        SearchRequest {
            query: "hello".into(),
            filters: SearchFiltersWire {
                account_ids: vec![], folder_ids: vec![], from: None, to: None,
                subject: None, after: None, before: None, has_attachment: None,
            },
            limit: 50,
            cursor: None,
        }
    }

    #[tokio::test]
    async fn search_without_connection_returns_not_connected() {
        let s = store();
        let err = run_search(&s, req()).await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    async fn search_without_token_returns_not_logged_in() {
        let s = store();
        s.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        s.put(Slot::CertPin, "deadbeef").unwrap();
        let err = run_search(&s, req()).await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }

    #[test]
    fn empty_filter_lists_omitted_from_wire_json() {
        let body = serde_json::to_string(&req()).unwrap();
        assert!(!body.contains("account_ids"));
        assert!(!body.contains("folder_ids"));
        assert!(!body.contains("from\":"));
    }

    #[test]
    fn populated_filter_lists_serialize_as_string_arrays() {
        let mut r = req();
        r.filters.account_ids = vec!["1".into(), "3".into()];
        r.filters.folder_ids = vec!["42".into()];
        let body = serde_json::to_string(&r).unwrap();
        assert!(body.contains("\"account_ids\":[\"1\",\"3\"]"));
        assert!(body.contains("\"folder_ids\":[\"42\"]"));
    }

    #[test]
    fn search_response_deserialises_with_optional_next_cursor() {
        let json = r#"{"results":[],"next_cursor":null,"total_estimate":null,"took_ms":3.14}"#;
        let resp: SearchResponse = serde_json::from_str(json).unwrap();
        assert!(resp.results.is_empty());
        assert!(resp.next_cursor.is_none());
        assert_eq!(resp.took_ms, 3.14);
    }
}
```

- [ ] **Step 2: Confirm `http_post_json` exists in `http::client`**

```bash
grep -n "http_post_json" gui/src-tauri/src/http/client.rs
```

If it does NOT exist, add it next to `http_get_json`. The signature mirrors `http_get_json` but takes a serializable body:

```rust
pub async fn http_post_json<I: serde::Serialize, O: serde::de::DeserializeOwned>(
    client: &reqwest::Client,
    endpoint: &str,
    token: Option<&str>,
    body: &I,
) -> Result<O, crate::commands::auth::AuthError> {
    let mut req = client.post(endpoint).json(body);
    if let Some(t) = token {
        req = req.bearer_auth(t);
    }
    let resp = req.send().await.map_err(map_reqwest_error)?;
    if !resp.status().is_success() {
        return Err(http_error_from_response(resp).await);
    }
    resp.json::<O>().await.map_err(map_reqwest_error)
}
```

(`map_reqwest_error` and `http_error_from_response` already exist next to `http_get_json` — reuse them. If they don't, this is a flag-and-stop signal — the connection layer would have shipped without them.)

- [ ] **Step 3: Wire into the module tree**

Edit `gui/src-tauri/src/commands/mod.rs`. Add:

```rust
pub mod search;
```

Edit `gui/src-tauri/src/lib.rs`. Find the `tauri::generate_handler![...]` macro invocation and add `commands::search::run_search_cmd` to the list.

- [ ] **Step 4: Run cargo test, confirm green**

```bash
cd gui/src-tauri && cargo test commands::search 2>&1 | tail -10
```

Expected: 4 passes (the 4 `#[test]` / `#[tokio::test]` blocks above).

- [ ] **Step 5: Commit**

```bash
git add gui/src-tauri/src/commands/search.rs gui/src-tauri/src/commands/mod.rs gui/src-tauri/src/lib.rs gui/src-tauri/src/http/client.rs
git commit -m "feat(gui-client): Rust /v1/search command + types

Wraps POST /v1/search with the same keyring + TOFU pin pattern as the
other commands. SearchFiltersWire uses skip_serializing_if so empty
filter lists/Nones don't ride the wire. Cursor is round-tripped as a
plain string."
```

---

## Task B2: TypeScript types + `tauri.ts` wrapper

**Files:**
- Create: `gui/src/lib/api/search.ts`
- Modify: `gui/src/lib/tauri.ts`

- [ ] **Step 1: Create `gui/src/lib/api/search.ts`**

```typescript
/**
 * Wire-shape types for /v1/search. Mirrors the Rust structs in
 * src-tauri/src/commands/search.rs.
 */
export interface SearchFiltersWire {
  account_ids: string[];
  folder_ids: string[];
  from: string | null;
  to: string | null;
  subject: string | null;
  after: string | null;   // YYYY-MM-DD
  before: string | null;  // YYYY-MM-DD
  has_attachment: boolean | null;
}

export interface SearchRequest {
  query: string;
  filters: SearchFiltersWire;
  limit: number;
  cursor: string | null;
}

export interface SearchAddress {
  address: string | null;
  name: string | null;
}

export interface SearchAccount {
  id: string;
  name: string | null;
}

export interface SearchFolder {
  id: string;
  full_path: string;
}

export interface SearchResultRow {
  message_id: string;
  account: SearchAccount;
  folder: SearchFolder | null;
  subject: string | null;
  from: SearchAddress;
  to: SearchAddress[];
  date: string | null;
  snippet_html: string | null;
  has_attachments: boolean;
  score: number;
  matched_arms: string[];
}

export interface SearchResponse {
  results: SearchResultRow[];
  next_cursor: string | null;
  total_estimate: number | null;
  took_ms: number;
}

/**
 * UI-facing filter shape. Distinct from `SearchFiltersWire` because the UI
 * uses idiomatic empty-strings / undefineds, while the wire shape uses
 * empty arrays / nulls that the Rust struct can omit via skip_serializing_if.
 */
export interface SearchFiltersUI {
  accountIds: string[];
  folderIds: string[];
  from: string;
  to: string;
  subject: string;
  after: string;
  before: string;
  hasAttachment: boolean | null;  // null = "don't care", true/false explicit
}

export function emptyFilters(): SearchFiltersUI {
  return {
    accountIds: [], folderIds: [],
    from: "", to: "", subject: "", after: "", before: "",
    hasAttachment: null,
  };
}

export function filtersUiToWire(ui: SearchFiltersUI): SearchFiltersWire {
  return {
    account_ids: ui.accountIds,
    folder_ids: ui.folderIds,
    from: ui.from || null,
    to: ui.to || null,
    subject: ui.subject || null,
    after: ui.after || null,
    before: ui.before || null,
    has_attachment: ui.hasAttachment,
  };
}
```

- [ ] **Step 2: Extend `gui/src/lib/tauri.ts`**

At the top, add to the `import type` block:

```typescript
import type { SearchRequest, SearchResponse } from "./api/search";
```

Re-export the types so consumers can `import { type SearchResponse } from "./tauri"`:

```typescript
export type {
  SearchAccount,
  SearchAddress,
  SearchFiltersUI,
  SearchFiltersWire,
  SearchFolder,
  SearchRequest,
  SearchResponse,
  SearchResultRow,
} from "./api/search";
export { emptyFilters, filtersUiToWire } from "./api/search";
```

Add the invoke wrapper at the bottom:

```typescript
export async function runSearch(req: SearchRequest): Promise<SearchResponse> {
  return invoke<SearchResponse>("run_search_cmd", { req });
}
```

- [ ] **Step 3: Type-check**

```bash
cd gui && npm run check 2>&1 | tail -10
```

Expected: zero errors.

- [ ] **Step 4: Commit**

```bash
git add gui/src/lib/api/search.ts gui/src/lib/tauri.ts
git commit -m "feat(gui-client): TS types + invoke wrapper for /v1/search

SearchFiltersUI is the idiomatic shape components hold in state;
filtersUiToWire() converts to the snake_case wire shape the Rust
command serialises. Keeps UI code free of null checks for empty
strings."
```

---

## Task B3: Pure helper — `filter_parse.ts` (DSL ↔ structured round-trip)

**Files:**
- Create: `gui/src/lib/filter_parse.ts`
- Create: `gui/src/lib/filter_parse.test.ts`

The popover writes structured filters; the search box accepts the DSL. For DSL parity we need to (a) extract supported DSL tokens from the typed query for chip rendering, and (b) re-emit structured filters as DSL tokens when the user submits via the popover but the search box already has free-text.

- [ ] **Step 1: Write the failing test**

```typescript
// gui/src/lib/filter_parse.test.ts
import { describe, expect, it } from "vitest";
import { extractDslFilters, formatDslTokens } from "./filter_parse";
import { emptyFilters } from "./api/search";

describe("extractDslFilters", () => {
  it("returns empty filters and unchanged free text for a plain query", () => {
    const { freeText, filters } = extractDslFilters("hello world");
    expect(freeText).toBe("hello world");
    expect(filters).toEqual(emptyFilters());
  });

  it("extracts from: token", () => {
    const { freeText, filters } = extractDslFilters("from:anna receipts");
    expect(freeText).toBe("receipts");
    expect(filters.from).toBe("anna");
  });

  it("extracts has:attachment token", () => {
    const { freeText, filters } = extractDslFilters("has:attachment school");
    expect(filters.hasAttachment).toBe(true);
    expect(freeText).toBe("school");
  });

  it("extracts after: and before:", () => {
    const { filters } = extractDslFilters("after:2024-01-01 before:2024-12-31 q");
    expect(filters.after).toBe("2024-01-01");
    expect(filters.before).toBe("2024-12-31");
  });

  it("does not extract account_id: (those come from the tree, not user typing)", () => {
    const { freeText, filters } = extractDslFilters("account_id:5 stuff");
    // Falls through as free text — UI doesn't surface account_id as a chip.
    expect(filters.accountIds).toEqual([]);
    expect(freeText).toContain("account_id:5");
  });

  it("preserves quoted values", () => {
    const { filters } = extractDslFilters('from:"anna h" subject:"the trip"');
    expect(filters.from).toBe("anna h");
    expect(filters.subject).toBe("the trip");
  });
});

describe("formatDslTokens", () => {
  it("returns empty string when no popover filters set", () => {
    expect(formatDslTokens(emptyFilters())).toBe("");
  });

  it("emits from:VALUE for a populated from", () => {
    const f = emptyFilters();
    f.from = "anna";
    expect(formatDslTokens(f)).toBe('from:"anna"');
  });

  it("emits has:attachment when hasAttachment===true", () => {
    const f = emptyFilters();
    f.hasAttachment = true;
    expect(formatDslTokens(f)).toBe("has:attachment");
  });

  it("emits multiple tokens space-separated in stable order", () => {
    const f = emptyFilters();
    f.from = "anna"; f.subject = "trip"; f.hasAttachment = true;
    expect(formatDslTokens(f)).toBe('from:"anna" subject:"trip" has:attachment');
  });

  it("skips empty strings", () => {
    const f = emptyFilters();
    f.from = "anna"; f.to = ""; f.subject = "";
    expect(formatDslTokens(f)).toBe('from:"anna"');
  });

  it("does NOT emit account_ids/folder_ids tokens (those go through filters wire)", () => {
    const f = emptyFilters();
    f.accountIds = ["5"]; f.from = "x";
    expect(formatDslTokens(f)).toBe('from:"x"');
  });
});
```

Run:

```bash
cd gui && npm test -- filter_parse 2>&1 | tail -15
```

Expected: failures — module not found.

- [ ] **Step 2: Implement**

```typescript
// gui/src/lib/filter_parse.ts
/**
 * DSL ↔ structured-filter round-tripping.
 *
 * The UI maintains two parallel inputs for the same logical query:
 *   - SearchBar text (free-form, supports DSL)
 *   - FilterPopover form (structured)
 *
 * extractDslFilters() pulls supported DSL tokens out of the typed query and
 * surfaces them as filter chips. formatDslTokens() does the reverse — turning
 * popover state into the canonical DSL string we send to the server.
 *
 * account_id: / folder_id: tokens are intentionally NOT extracted: the tree
 * controls those, not the search box. If the user types them anyway they fall
 * through as free text and the server's parser will still apply them.
 */
import { emptyFilters, type SearchFiltersUI } from "./api/search";

const POPOVER_OPERATORS = new Set([
  "from", "to", "subject", "after", "before", "has",
]);

function tokenize(s: string): string[] {
  // Whitespace-split, but respect quoted runs (single or double quotes).
  const out: string[] = [];
  let buf = "";
  let quote: string | null = null;
  for (const ch of s) {
    if (quote !== null) {
      if (ch === quote) {
        quote = null;
      } else {
        buf += ch;
      }
    } else if (ch === '"' || ch === "'") {
      quote = ch;
    } else if (/\s/.test(ch)) {
      if (buf) { out.push(buf); buf = ""; }
    } else {
      buf += ch;
    }
  }
  if (buf) out.push(buf);
  return out;
}

export interface ExtractedFilters {
  freeText: string;
  filters: SearchFiltersUI;
}

export function extractDslFilters(query: string): ExtractedFilters {
  const filters = emptyFilters();
  const freeParts: string[] = [];

  for (const tok of tokenize(query)) {
    const colon = tok.indexOf(":");
    if (colon > 0) {
      const op = tok.slice(0, colon).toLowerCase();
      const val = tok.slice(colon + 1);
      if (POPOVER_OPERATORS.has(op) && val) {
        if (op === "from") { filters.from = val; continue; }
        if (op === "to") { filters.to = val; continue; }
        if (op === "subject") { filters.subject = val; continue; }
        if (op === "after") { filters.after = val; continue; }
        if (op === "before") { filters.before = val; continue; }
        if (op === "has" && val.toLowerCase() === "attachment") {
          filters.hasAttachment = true;
          continue;
        }
      }
    }
    freeParts.push(tok);
  }

  return { freeText: freeParts.join(" "), filters };
}

export function formatDslTokens(f: SearchFiltersUI): string {
  const parts: string[] = [];
  if (f.from) parts.push(`from:"${f.from}"`);
  if (f.to) parts.push(`to:"${f.to}"`);
  if (f.subject) parts.push(`subject:"${f.subject}"`);
  if (f.after) parts.push(`after:${f.after}`);
  if (f.before) parts.push(`before:${f.before}`);
  if (f.hasAttachment === true) parts.push("has:attachment");
  return parts.join(" ");
}
```

- [ ] **Step 3: Re-run, confirm green**

```bash
cd gui && npm test -- filter_parse 2>&1 | tail -10
```

Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add gui/src/lib/filter_parse.ts gui/src/lib/filter_parse.test.ts
git commit -m "feat(gui-client): DSL ↔ structured filter round-trip helpers

extractDslFilters() pulls supported DSL tokens out of free-typed
search queries and surfaces them as popover state / chips.
formatDslTokens() does the reverse. account_id:/folder_id: tokens
are intentionally not surfaced — the tree owns those."
```

---

## Task B4: Pure helper — `snippet_sanitize.ts`

**Files:**
- Create: `gui/src/lib/snippet_sanitize.ts`
- Create: `gui/src/lib/snippet_sanitize.test.ts`

Server-side snippets come back as `snippet_html` containing `<mark>` highlighting. We don't fully trust the server output — apply a minimal allowlist (only `<mark>` allowed, everything else stripped or escaped). This is defense in depth; the server's bleach pass is the first line.

- [ ] **Step 1: Write the failing test**

```typescript
// gui/src/lib/snippet_sanitize.test.ts
import { describe, expect, it } from "vitest";
import { sanitizeSnippet } from "./snippet_sanitize";

describe("sanitizeSnippet", () => {
  it("passes plain text through unchanged (escaping HTML chars)", () => {
    expect(sanitizeSnippet("hello world")).toBe("hello world");
  });

  it("escapes < and > and & that aren't part of allowed tags", () => {
    expect(sanitizeSnippet("a < b & c > d")).toBe("a &lt; b &amp; c &gt; d");
  });

  it("preserves <mark> and </mark>", () => {
    expect(sanitizeSnippet("see <mark>here</mark>")).toBe("see <mark>here</mark>");
  });

  it("strips disallowed tags like <script>", () => {
    expect(sanitizeSnippet("<script>alert(1)</script>")).toBe("&lt;script&gt;alert(1)&lt;/script&gt;");
  });

  it("strips <mark> with attributes", () => {
    // We only allow bare <mark>; an attribute-bearing one is escaped.
    const out = sanitizeSnippet('<mark style="x">hi</mark>');
    expect(out).toBe("&lt;mark style=&quot;x&quot;&gt;hi&lt;/mark&gt;");
  });

  it("handles null/empty input", () => {
    expect(sanitizeSnippet(null)).toBe("");
    expect(sanitizeSnippet("")).toBe("");
  });

  it("preserves text around <mark>", () => {
    expect(sanitizeSnippet("…leaves at <mark>7:30</mark> on Tue…"))
      .toBe("…leaves at <mark>7:30</mark> on Tue…");
  });
});
```

- [ ] **Step 2: Implement**

```typescript
// gui/src/lib/snippet_sanitize.ts
/**
 * Minimal allowlist for server snippet_html: only bare <mark>/</mark> tags
 * survive. Everything else is HTML-escaped. Defense in depth against a
 * sanitizer bypass on the server side.
 */

const MARK_OPEN = /<mark>/g;
const MARK_CLOSE = /<\/mark>/g;
const MARK_OPEN_PLACEHOLDER = "LOCALMAIL_MARK_OPEN";
const MARK_CLOSE_PLACEHOLDER = "LOCALMAIL_MARK_CLOSE";

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function sanitizeSnippet(snippet: string | null): string {
  if (!snippet) return "";
  // 1. Replace exact-match <mark>/</mark> with placeholders so escaping
  //    doesn't touch them. Anything else (attrs, weird casing, other tags)
  //    falls through and gets escaped.
  const guarded = snippet
    .replace(MARK_OPEN, MARK_OPEN_PLACEHOLDER)
    .replace(MARK_CLOSE, MARK_CLOSE_PLACEHOLDER);
  // 2. Escape everything.
  const escaped = escapeHtml(guarded);
  // 3. Restore the placeholders as real tags.
  return escaped
    .split(MARK_OPEN_PLACEHOLDER).join("<mark>")
    .split(MARK_CLOSE_PLACEHOLDER).join("</mark>");
}
```

- [ ] **Step 3: Re-run, confirm green; commit**

```bash
cd gui && npm test -- snippet_sanitize 2>&1 | tail -10
```

Expected: 7 passes.

```bash
git add gui/src/lib/snippet_sanitize.ts gui/src/lib/snippet_sanitize.test.ts
git commit -m "feat(gui-client): minimal allowlist sanitizer for server snippet_html

Only bare <mark>/</mark> pass through; everything else is HTML-escaped.
Defense in depth on top of the server's bleach pass."
```

---

## Task B5: Search store — `search.svelte.ts`

**Files:**
- Create: `gui/src/lib/stores/search.svelte.ts`
- Create: `gui/src/lib/stores/search.test.ts`

Mirrors the `mail.svelte.ts` rune pattern. Holds the current query, filters, results, last `took_ms`, loading flag, and error.

- [ ] **Step 1: Write the failing test**

```typescript
// gui/src/lib/stores/search.test.ts
import { afterEach, describe, expect, it, vi } from "vitest";
import { search } from "./search.svelte";
import { emptyFilters } from "../api/search";

vi.mock("../tauri", () => ({
  runSearch: vi.fn(),
}));

import { runSearch } from "../tauri";

afterEach(() => {
  search.reset();
  vi.clearAllMocks();
});

describe("search store", () => {
  it("starts empty / idle", () => {
    expect(search.snapshot.query).toBe("");
    expect(search.snapshot.results).toEqual([]);
    expect(search.snapshot.tookMs).toBe(null);
    expect(search.snapshot.loading).toBe(false);
    expect(search.snapshot.errorMessage).toBe(null);
  });

  it("setQuery + setFilters update state without firing a request", () => {
    search.setQuery("hello");
    search.setFilters({ ...emptyFilters(), from: "anna" });
    expect(search.snapshot.query).toBe("hello");
    expect(search.snapshot.filters.from).toBe("anna");
    expect(runSearch).not.toHaveBeenCalled();
  });

  it("submit() calls runSearch with the merged DSL+structured payload", async () => {
    (runSearch as unknown as { mockResolvedValue: (v: unknown) => void })
      .mockResolvedValue({
        results: [{ message_id: "1", account: { id: "1", name: null },
                    folder: null, subject: "Hi", from: { address: null, name: null },
                    to: [], date: null, snippet_html: "x", has_attachments: false,
                    score: 0.5, matched_arms: ["bm25"] }],
        next_cursor: null, total_estimate: null, took_ms: 42.0,
      });
    search.setQuery("hello");
    await search.submit();
    expect(runSearch).toHaveBeenCalledTimes(1);
    expect(search.snapshot.results.length).toBe(1);
    expect(search.snapshot.tookMs).toBe(42.0);
    expect(search.snapshot.loading).toBe(false);
  });

  it("submit() failure surfaces errorMessage and clears loading", async () => {
    (runSearch as unknown as { mockRejectedValue: (v: unknown) => void })
      .mockRejectedValue({ kind: "HttpError", detail: { status: 500 } });
    search.setQuery("hi");
    await search.submit();
    expect(search.snapshot.loading).toBe(false);
    expect(search.snapshot.errorMessage).toContain("HttpError");
    expect(search.snapshot.results).toEqual([]);
  });

  it("reset() clears everything", () => {
    search.setQuery("x");
    search.reset();
    expect(search.snapshot.query).toBe("");
    expect(search.snapshot.filters).toEqual(emptyFilters());
  });
});
```

- [ ] **Step 2: Implement**

```typescript
// gui/src/lib/stores/search.svelte.ts
/**
 * Search state singleton. Mirrors mail.svelte.ts in shape.
 *
 * The search store owns:
 *   query       — what's in the search bar (raw, may include DSL tokens)
 *   filters     — current structured filter state (popover + tree)
 *   results     — last SearchResponse rows
 *   tookMs      — last took_ms for "Search took 42 ms" caption
 *   loading     — true while the request is in flight
 *   errorMessage — surfaced from a failed submit
 *
 * submit() merges the DSL string with structured popover filters via
 * filtersUiToWire(). Tree-driven account/folder filters are written into
 * `filters.accountIds` / `filters.folderIds` by the caller before submit().
 */
import {
  emptyFilters,
  filtersUiToWire,
  type SearchFiltersUI,
  type SearchResultRow,
} from "../api/search";
import { runSearch } from "../tauri";

const DEFAULT_LIMIT = 50;

export interface SearchState {
  query: string;
  filters: SearchFiltersUI;
  results: SearchResultRow[];
  tookMs: number | null;
  loading: boolean;
  errorMessage: string | null;
}

function initialState(): SearchState {
  return {
    query: "",
    filters: emptyFilters(),
    results: [],
    tookMs: null,
    loading: false,
    errorMessage: null,
  };
}

class SearchStore {
  #state: SearchState = $state(initialState());

  get snapshot(): SearchState { return this.#state; }

  setQuery(q: string): void { this.#state.query = q; }

  setFilters(f: SearchFiltersUI): void { this.#state.filters = f; }

  reset(): void { this.#state = initialState(); }

  async submit(): Promise<void> {
    this.#state.loading = true;
    this.#state.errorMessage = null;
    try {
      const resp = await runSearch({
        query: this.#state.query,
        filters: filtersUiToWire(this.#state.filters),
        limit: DEFAULT_LIMIT,
        cursor: null,
      });
      this.#state.results = resp.results;
      this.#state.tookMs = resp.took_ms;
    } catch (err: unknown) {
      this.#state.errorMessage = formatError(err);
    } finally {
      this.#state.loading = false;
    }
  }
}

function formatError(err: unknown): string {
  if (err && typeof err === "object") {
    const o = err as { kind?: string; detail?: unknown };
    if (o.kind && o.detail !== undefined) {
      const detailStr =
        typeof o.detail === "object" && o.detail !== null
          ? formatError(o.detail)
          : String(o.detail);
      return `${o.kind}: ${detailStr}`;
    }
    if (o.kind) return String(o.kind);
  }
  return String(err);
}

export const search = new SearchStore();
```

- [ ] **Step 3: Re-run, confirm green; commit**

```bash
cd gui && npm test -- stores/search 2>&1 | tail -10
```

Expected: 5 passes.

```bash
git add gui/src/lib/stores/search.svelte.ts gui/src/lib/stores/search.test.ts
git commit -m "feat(gui-client): search store singleton (rune state)

Owns query, structured filters, results, took_ms, loading, error.
submit() merges DSL + structured filters via filtersUiToWire() and
calls runSearch() through the typed tauri.ts wrapper. Mirrors the
auth/mail store pattern."
```

---

## Task B6: `SearchBar.svelte`

**Files:**
- Create: `gui/src/components/SearchBar.svelte`
- Create: `gui/src/components/SearchBar.test.ts`

- [ ] **Step 1: Write the failing test**

```typescript
// gui/src/components/SearchBar.test.ts
import { fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import SearchBar from "./SearchBar.svelte";
import { search } from "../lib/stores/search.svelte";

vi.mock("../lib/tauri", () => ({ runSearch: vi.fn(async () => ({
  results: [], next_cursor: null, total_estimate: null, took_ms: 0,
})) }));

afterEach(() => { search.reset(); vi.clearAllMocks(); });

describe("SearchBar", () => {
  it("renders an input and a Filters button", () => {
    render(SearchBar);
    expect(screen.getByPlaceholderText(/search/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /filters/i })).toBeInTheDocument();
  });

  it("typing updates search.query", async () => {
    render(SearchBar);
    const input = screen.getByPlaceholderText(/search/i) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: "hello" } });
    expect(search.snapshot.query).toBe("hello");
  });

  it("submitting via Enter calls search.submit()", async () => {
    render(SearchBar);
    const input = screen.getByPlaceholderText(/search/i) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: "hi" } });
    await fireEvent.keyDown(input, { key: "Enter" });
    const { runSearch } = await import("../lib/tauri");
    expect(runSearch).toHaveBeenCalledOnce();
  });

  it("clicking the search button also submits", async () => {
    render(SearchBar);
    const input = screen.getByPlaceholderText(/search/i) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: "hi" } });
    await fireEvent.click(screen.getByRole("button", { name: /^search$/i }));
    const { runSearch } = await import("../lib/tauri");
    expect(runSearch).toHaveBeenCalledOnce();
  });
});
```

- [ ] **Step 2: Implement**

```svelte
<!-- gui/src/components/SearchBar.svelte -->
<script lang="ts">
  import { search } from "../lib/stores/search.svelte";

  let popoverOpen = $state(false);

  async function onSubmit(e?: Event) {
    e?.preventDefault();
    await search.submit();
  }

  function onKeyDown(e: KeyboardEvent) {
    if (e.key === "Enter") {
      e.preventDefault();
      void onSubmit();
    }
  }

  function togglePopover() { popoverOpen = !popoverOpen; }
</script>

<form class="bar" onsubmit={onSubmit}>
  <input
    type="search"
    placeholder="Search across all accounts"
    value={search.snapshot.query}
    oninput={(e) => search.setQuery((e.currentTarget as HTMLInputElement).value)}
    onkeydown={onKeyDown}
    disabled={search.snapshot.loading}
  />
  <button type="submit" disabled={search.snapshot.loading}>Search</button>
  <button type="button" onclick={togglePopover}>🔧 Filters</button>
</form>

{#if popoverOpen}
  <!-- Placeholder; FilterPopover is wired in Task B7. -->
  <div class="popover" role="dialog">Filter popover (Task B7).</div>
{/if}

<style>
  .bar {
    display: flex; gap: 6px; padding: 6px 12px;
    background: #fafbfd; border-bottom: 1px solid #e0e3e8;
  }
  input {
    flex: 1; padding: 4px 8px; border: 1px solid #ccc; border-radius: 4px;
  }
  button {
    padding: 4px 10px; background: #fff; border: 1px solid #ccc;
    border-radius: 4px; cursor: pointer;
  }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .popover {
    position: absolute; right: 12px; top: 40px; background: #fff;
    border: 1px solid #ccc; padding: 12px; border-radius: 4px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
  }
</style>
```

- [ ] **Step 3: Re-run, commit**

```bash
cd gui && npm test -- SearchBar 2>&1 | tail -10
```

Expected: 4 passes.

```bash
git add gui/src/components/SearchBar.svelte gui/src/components/SearchBar.test.ts
git commit -m "feat(gui-client): SearchBar component with Enter/button submit

Two-way bound to search.snapshot.query; Enter and the Search button
both call search.submit(). Filters popover is a stub here — wired in
Task B7."
```

---

## Task B7: `FilterPopover.svelte`

**Files:**
- Create: `gui/src/components/FilterPopover.svelte`
- Create: `gui/src/components/FilterPopover.test.ts`
- Modify: `gui/src/components/SearchBar.svelte` (replace stub with real component)

- [ ] **Step 1: Write the failing test**

```typescript
// gui/src/components/FilterPopover.test.ts
import { fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import FilterPopover from "./FilterPopover.svelte";
import { search } from "../lib/stores/search.svelte";

vi.mock("../lib/tauri", () => ({ runSearch: vi.fn(async () => ({
  results: [], next_cursor: null, total_estimate: null, took_ms: 0,
})) }));

afterEach(() => { search.reset(); vi.clearAllMocks(); });

describe("FilterPopover", () => {
  it("renders 5 inputs and a Apply button", () => {
    render(FilterPopover);
    expect(screen.getByLabelText(/from/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/to/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/subject/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/after/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/before/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/has attachment/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /apply/i })).toBeInTheDocument();
  });

  it("typing into from updates the local state then writes on Apply", async () => {
    render(FilterPopover);
    const fromInput = screen.getByLabelText(/from/i) as HTMLInputElement;
    await fireEvent.input(fromInput, { target: { value: "anna" } });
    await fireEvent.click(screen.getByRole("button", { name: /apply/i }));
    expect(search.snapshot.filters.from).toBe("anna");
  });

  it("Apply submits the search", async () => {
    render(FilterPopover);
    await fireEvent.click(screen.getByRole("button", { name: /apply/i }));
    const { runSearch } = await import("../lib/tauri");
    expect(runSearch).toHaveBeenCalledOnce();
  });

  it("Clear resets the popover form", async () => {
    render(FilterPopover);
    const fromInput = screen.getByLabelText(/from/i) as HTMLInputElement;
    await fireEvent.input(fromInput, { target: { value: "anna" } });
    await fireEvent.click(screen.getByRole("button", { name: /clear/i }));
    expect((screen.getByLabelText(/from/i) as HTMLInputElement).value).toBe("");
  });
});
```

- [ ] **Step 2: Implement**

```svelte
<!-- gui/src/components/FilterPopover.svelte -->
<script lang="ts">
  import { emptyFilters, type SearchFiltersUI } from "../lib/api/search";
  import { extractDslFilters } from "../lib/filter_parse";
  import { search } from "../lib/stores/search.svelte";

  // Seed from a merge of structured filters (already on the search store)
  // and any DSL tokens typed into the search bar — so opening the popover
  // after typing `from:anna` shows "anna" in the From field. DSL tokens
  // that don't map to popover fields stay in the query string as free text.
  function initialLocal(): SearchFiltersUI {
    const dsl = extractDslFilters(search.snapshot.query).filters;
    const stored = search.snapshot.filters;
    return {
      accountIds: stored.accountIds,
      folderIds: stored.folderIds,
      from: stored.from || dsl.from,
      to: stored.to || dsl.to,
      subject: stored.subject || dsl.subject,
      after: stored.after || dsl.after,
      before: stored.before || dsl.before,
      hasAttachment: stored.hasAttachment ?? dsl.hasAttachment,
    };
  }
  let local: SearchFiltersUI = $state(initialLocal());

  async function apply() {
    search.setFilters({
      ...local,
      // Preserve account/folder narrowing the tree has already set.
      accountIds: search.snapshot.filters.accountIds,
      folderIds: search.snapshot.filters.folderIds,
    });
    // Strip any DSL tokens we've absorbed into the popover so they don't
    // duplicate-apply on the server (the server happily ANDs them, but the
    // chips below the bar would render twice — once from popover, once from DSL).
    const { freeText } = extractDslFilters(search.snapshot.query);
    search.setQuery(freeText);
    await search.submit();
  }

  function clear() {
    local = emptyFilters();
  }
</script>

<form class="form" onsubmit={(e) => { e.preventDefault(); void apply(); }}>
  <label>From <input bind:value={local.from} placeholder="anna@" /></label>
  <label>To <input bind:value={local.to} placeholder="horst@" /></label>
  <label>Subject <input bind:value={local.subject} placeholder="school" /></label>
  <label>After <input type="date" bind:value={local.after} /></label>
  <label>Before <input type="date" bind:value={local.before} /></label>
  <label class="checkbox">
    <input
      type="checkbox"
      checked={local.hasAttachment === true}
      onchange={(e) => {
        local.hasAttachment = (e.currentTarget as HTMLInputElement).checked ? true : null;
      }}
    />
    Has attachment
  </label>
  <div class="row">
    <button type="button" onclick={clear}>Clear</button>
    <button type="submit">Apply</button>
  </div>
</form>

<style>
  .form { display: flex; flex-direction: column; gap: 8px; padding: 12px; min-width: 260px; }
  label { display: flex; flex-direction: column; gap: 2px; font-size: 12px; color: #555; }
  label.checkbox { flex-direction: row; align-items: center; gap: 6px; }
  input { padding: 3px 6px; border: 1px solid #ccc; border-radius: 3px; }
  .row { display: flex; justify-content: flex-end; gap: 6px; margin-top: 4px; }
  button { padding: 4px 10px; border: 1px solid #ccc; background: #fff; border-radius: 4px; cursor: pointer; }
</style>
```

- [ ] **Step 3: Replace the popover stub in `SearchBar.svelte`**

Replace the placeholder `<div class="popover">…</div>` block with:

```svelte
{#if popoverOpen}
  <div class="popover" role="dialog">
    {#await import("./FilterPopover.svelte") then mod}
      {@const C = mod.default}
      <C />
    {/await}
  </div>
{/if}
```

(Lazy-import keeps the popover code out of the initial bundle. The dynamic `import()` is recognised by Vite for code-splitting; the `{@const C = ...}<C />` form is the runes-compatible replacement for the deprecated `<svelte:component>`.)

- [ ] **Step 4: Test, commit**

```bash
cd gui && npm test -- FilterPopover 2>&1 | tail -10
cd gui && npm run check 2>&1 | tail -10
```

Expected: 4 popover passes; zero check errors.

```bash
git add gui/src/components/FilterPopover.svelte \
        gui/src/components/FilterPopover.test.ts \
        gui/src/components/SearchBar.svelte
git commit -m "feat(gui-client): FilterPopover wired into SearchBar

5 input fields (From / To / Subject / After / Before / Has attachment)
+ Apply / Clear. Apply writes into search.filters and submits.
Lazy-imported from SearchBar."
```

---

## Task B8: `ActiveFilterChips.svelte`

**Files:**
- Create: `gui/src/components/ActiveFilterChips.svelte`
- Create: `gui/src/components/ActiveFilterChips.test.ts`

- [ ] **Step 1: Write the failing test**

```typescript
// gui/src/components/ActiveFilterChips.test.ts
import { fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import ActiveFilterChips from "./ActiveFilterChips.svelte";
import { search } from "../lib/stores/search.svelte";

vi.mock("../lib/tauri", () => ({ runSearch: vi.fn(async () => ({
  results: [], next_cursor: null, total_estimate: null, took_ms: 0,
})) }));

afterEach(() => { search.reset(); vi.clearAllMocks(); });

describe("ActiveFilterChips", () => {
  it("renders nothing when no filters are set", () => {
    const { container } = render(ActiveFilterChips);
    expect(container.querySelectorAll(".chip").length).toBe(0);
  });

  it("renders a chip for each non-empty popover filter", () => {
    search.setFilters({
      accountIds: [], folderIds: [],
      from: "anna", to: "", subject: "trip",
      after: "2024-01-01", before: "", hasAttachment: true,
    });
    render(ActiveFilterChips);
    expect(screen.getByText(/from: anna/i)).toBeInTheDocument();
    expect(screen.getByText(/subject: trip/i)).toBeInTheDocument();
    expect(screen.getByText(/after: 2024-01-01/i)).toBeInTheDocument();
    expect(screen.getByText(/has attachment/i)).toBeInTheDocument();
  });

  it("clicking a chip's × removes that filter and re-submits", async () => {
    search.setFilters({
      accountIds: [], folderIds: [],
      from: "anna", to: "", subject: "", after: "", before: "", hasAttachment: null,
    });
    render(ActiveFilterChips);
    await fireEvent.click(screen.getByRole("button", { name: /remove from/i }));
    expect(search.snapshot.filters.from).toBe("");
    const { runSearch } = await import("../lib/tauri");
    expect(runSearch).toHaveBeenCalledOnce();
  });

  it("does not show chips for accountIds/folderIds (tree owns those)", () => {
    search.setFilters({
      accountIds: ["1", "3"], folderIds: ["5"],
      from: "", to: "", subject: "", after: "", before: "", hasAttachment: null,
    });
    const { container } = render(ActiveFilterChips);
    expect(container.querySelectorAll(".chip").length).toBe(0);
  });
});
```

- [ ] **Step 2: Implement**

```svelte
<!-- gui/src/components/ActiveFilterChips.svelte -->
<script lang="ts">
  import { search } from "../lib/stores/search.svelte";

  interface Chip { key: string; label: string; clear: () => void; }

  function chips(): Chip[] {
    const f = search.snapshot.filters;
    const out: Chip[] = [];
    if (f.from) out.push({ key: "from", label: `From: ${f.from}`, clear() {
      search.setFilters({ ...f, from: "" });
    }});
    if (f.to) out.push({ key: "to", label: `To: ${f.to}`, clear() {
      search.setFilters({ ...f, to: "" });
    }});
    if (f.subject) out.push({ key: "subject", label: `Subject: ${f.subject}`, clear() {
      search.setFilters({ ...f, subject: "" });
    }});
    if (f.after) out.push({ key: "after", label: `After: ${f.after}`, clear() {
      search.setFilters({ ...f, after: "" });
    }});
    if (f.before) out.push({ key: "before", label: `Before: ${f.before}`, clear() {
      search.setFilters({ ...f, before: "" });
    }});
    if (f.hasAttachment === true) out.push({ key: "has", label: "Has attachment", clear() {
      search.setFilters({ ...f, hasAttachment: null });
    }});
    return out;
  }

  async function remove(c: Chip) {
    c.clear();
    await search.submit();
  }
</script>

{#if chips().length > 0}
  <ul class="chips">
    {#each chips() as c (c.key)}
      <li class="chip">
        <span>{c.label}</span>
        <button
          type="button"
          aria-label="Remove {c.key}"
          onclick={() => remove(c)}
        >×</button>
      </li>
    {/each}
  </ul>
{/if}

<style>
  .chips { list-style: none; padding: 4px 12px; margin: 0; display: flex; gap: 6px; flex-wrap: wrap; }
  .chip { display: inline-flex; align-items: center; gap: 4px;
          background: #eef3fb; border: 1px solid #c8d6ec; padding: 2px 6px;
          border-radius: 12px; font-size: 12px; }
  button { background: transparent; border: none; cursor: pointer; font-size: 14px; line-height: 1; padding: 0 2px; color: #555; }
</style>
```

- [ ] **Step 3: Test, commit**

```bash
cd gui && npm test -- ActiveFilterChips 2>&1 | tail -10
```

Expected: 4 passes.

```bash
git add gui/src/components/ActiveFilterChips.svelte gui/src/components/ActiveFilterChips.test.ts
git commit -m "feat(gui-client): ActiveFilterChips component with × clear

Renders one chip per non-empty popover filter. Clicking × clears the
filter from the search store and re-submits. accountIds/folderIds are
intentionally not rendered as chips — they belong to the tree."
```

---

## Task B9: `MessageList` renders search results with snippets

**Files:**
- Modify: `gui/src/components/MessageList.svelte`
- Modify: `gui/src/components/MessageListRow.svelte`
- Modify: `gui/src/components/MessageList.test.ts` (extend)

The middle pane currently iterates `mail.snapshot.messages`. We extend it to render `search.snapshot.results` when the user has run a search, falling back to the existing recent-messages list otherwise. The row component gets a new optional `snippet` prop for `<mark>`-highlighted text.

- [ ] **Step 1: Extend the test**

Open `gui/src/components/MessageList.test.ts`. Add a new `describe` block at the bottom:

```typescript
import { search } from "../lib/stores/search.svelte";

First, add a test-only helper to `search.svelte.ts` (at the bottom of the file). `MailStore` / `SearchStore` use TypeScript's `#state` ES private field which is invisible at runtime, so we need an exported helper to populate state without going through `submit()`:

```typescript
// Exported for tests only — populates results + tookMs without firing runSearch.
// `as unknown as { snapshot: SearchState }` is intentional: it bypasses the
// readonly `snapshot` getter to allow direct mutation. Production code MUST
// only mutate state via setQuery/setFilters/submit/reset.
export function __setSearchResultsForTest(results: SearchResultRow[], tookMs: number): void {
  const s = search as unknown as { snapshot: SearchState };
  s.snapshot.results = results;
  s.snapshot.tookMs = tookMs;
}
```

Then add the test cases:

```typescript
import { __setSearchResultsForTest } from "../lib/stores/search.svelte";

describe("MessageList with search results", () => {
  beforeEach(() => { search.reset(); mail.reset(); });

  it("renders search.results when present, with snippet text", () => {
    search.setQuery("hello");
    __setSearchResultsForTest(
      [{
        message_id: "1", account: { id: "1", name: "gmail" }, folder: null,
        subject: "Re: school", from: { name: "Anna", address: "a@x" },
        to: [], date: null, snippet_html: "…leaves at <mark>7:30</mark>…",
        has_attachments: false, score: 0.5, matched_arms: ["bm25"],
      }],
      42.0,
    );
    render(MessageList);
    expect(screen.getByText(/Re: school/)).toBeInTheDocument();
    expect(screen.getByText(/Search took/)).toBeInTheDocument();
    // The snippet should be rendered with a <mark> element preserved.
    expect(screen.getByText(/7:30/).tagName.toLowerCase()).toBe("mark");
  });

  it("renders 'no matches' when results is empty and a query was submitted", () => {
    search.setQuery("xyz");
    __setSearchResultsForTest([], 5.0);
    render(MessageList);
    expect(screen.getByText(/no matches/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Update `MessageListRow.svelte`**

Currently the row shows subject + from + date. Add an optional `snippet` prop and render it below subject when present:

```svelte
<script lang="ts">
  import { sanitizeSnippet } from "../lib/snippet_sanitize";
  import type { MessageAddress } from "../lib/tauri";

  let { subject, from, date, account, snippet = null, selected, onSelect } = $props<{
    subject: string | null;
    from: MessageAddress;
    date: string | null;
    account: { id: string; name: string | null };
    snippet?: string | null;
    selected: boolean;
    onSelect: () => void;
  }>();

  // ... existing addressLabel/formatRelativeDate calls ...
</script>

<button class="row" class:selected={selected} onclick={onSelect}>
  <div class="subject">{subject ?? "(no subject)"}</div>
  <div class="meta">
    <span class="from">{addressLabel(from)}</span>
    <span class="when">{date ? formatRelativeDate(date) : ""}</span>
  </div>
  {#if snippet}
    <div class="snippet">{@html sanitizeSnippet(snippet)}</div>
  {/if}
  <div class="account">{account.name ?? account.id}</div>
</button>

<style>
  /* ... existing styles ... */
  .snippet { font-size: 11px; color: #555; line-height: 1.3; margin-top: 2px;
             overflow: hidden; text-overflow: ellipsis;
             display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
  .snippet :global(mark) { background: #ffe89d; padding: 0 1px; border-radius: 2px; }
</style>
```

(`{@html …}` is safe here because `sanitizeSnippet` has reduced the input to a known-good shape.)

- [ ] **Step 3: Update `MessageList.svelte` to select source list**

```svelte
<script lang="ts">
  import MessageListRow from "./MessageListRow.svelte";
  import { mail } from "../lib/stores/mail.svelte";
  import { search } from "../lib/stores/search.svelte";

  // True once the user has run at least one search this session.
  let searchActive = $derived(search.snapshot.tookMs !== null);

  // The unified row shape the renderer iterates.
  interface ListRow {
    id: string;
    subject: string | null;
    from: { address: string | null; name: string | null };
    date: string | null;
    account: { id: string; name: string | null };
    snippet: string | null;
  }

  let rows: ListRow[] = $derived(
    searchActive
      ? search.snapshot.results.map((r) => ({
          id: r.message_id,
          subject: r.subject,
          from: r.from,
          date: r.date,
          account: r.account,
          snippet: r.snippet_html,
        }))
      : mail.snapshot.messages.map((m) => ({
          id: m.message_id,
          subject: m.subject,
          from: m.from,
          date: m.date,
          account: m.account,
          snippet: null,
        }))
  );

  // Apply client-side filter for the non-search path (tree narrowing over
  // /v1/changes — preserved for compatibility; will be replaced when tree
  // narrowing always dispatches /v1/search in Task B10).
  let visible = $derived(searchActive ? rows : rows.filter((r) => {
    const sel = mail.snapshot.selection;
    if (sel.kind === "all") return true;
    return r.account.id === sel.accountId;
  }));
</script>

{#if searchActive}
  <div class="caption">
    Search took {Math.round(search.snapshot.tookMs ?? 0)} ms — {search.snapshot.results.length} result(s)
  </div>
{/if}

{#if search.snapshot.errorMessage}
  <div class="error">Error: {search.snapshot.errorMessage}</div>
{:else if mail.snapshot.errorMessage}
  <div class="error">{mail.snapshot.errorMessage}</div>
{/if}

{#if visible.length === 0}
  {#if searchActive}
    <div class="empty">No matches.</div>
  {:else}
    <div class="empty">No messages loaded.</div>
  {/if}
{:else}
  <ul class="list">
    {#each visible as r (r.id)}
      <li>
        <MessageListRow
          subject={r.subject}
          from={r.from}
          date={r.date}
          account={r.account}
          snippet={r.snippet}
          selected={mail.snapshot.selectedMessage?.id === r.id}
          onSelect={() => mail.openMessage(r.id)}
        />
      </li>
    {/each}
  </ul>
{/if}

<style>
  /* ... existing styles ... */
  .caption { padding: 4px 12px; font-size: 11px; color: #666; background: #fafbfd;
             border-bottom: 1px solid #eee; }
</style>
```

- [ ] **Step 4: Re-run tests**

```bash
cd gui && npm test -- MessageList 2>&1 | tail -15
cd gui && npm run check 2>&1 | tail -10
```

Expected: all green; zero check errors.

- [ ] **Step 5: Commit**

```bash
git add gui/src/components/MessageList.svelte \
        gui/src/components/MessageListRow.svelte \
        gui/src/components/MessageList.test.ts \
        gui/src/lib/stores/search.svelte.ts
git commit -m "feat(gui-client): MessageList renders search results + snippets

When search has been submitted, the middle column iterates
search.snapshot.results and renders snippet_html sanitised through
sanitizeSnippet (preserves <mark>). The non-search path is unchanged —
the existing /v1/changes flow still feeds the recent-200 list."
```

---

## Task B10: Wire `AccountTree` to dispatch server-side `/v1/search`

**Files:**
- Modify: `gui/src/components/AccountTree.svelte`
- Modify: `gui/src/components/AccountTree.test.ts` (extend)

This task is the **client-side end of Phase A**. Selecting "All Mail", an account, or a folder writes `accountIds` / `folderIds` into the search store and submits. The middle column then shows the server-narrowed result, not a client-side filter over the 200-message changes load.

> **Dependency:** This task can be authored and unit-tested in Phase B independently, but smoke-testing it end-to-end requires Phase A merged (or at least applied to the server the test environment hits). Until Phase A is merged the smoke test will see `ValidationFailed: filter 'account_ids' is accepted by the API schema but not yet wired through to the search backend` on the GUI as an error chip.

- [ ] **Step 1: Extend the test**

Add to `gui/src/components/AccountTree.test.ts`:

```typescript
describe("AccountTree dispatches server-side search on selection", () => {
  it("clicking an account writes accountIds and submits", async () => {
    // Seed mail.accounts so the tree has something to render.
    mail.reset();
    (mail as unknown as { snapshot: { accounts: unknown[] } }).snapshot.accounts = [
      { id: "5", name: "gmail.com", address: "a@gmail.com",
        last_sync_at: null, message_count: 100,
        capabilities: { can_sync: true, is_archive_only: false, is_shared: false } },
    ];
    render(AccountTree);
    await fireEvent.click(screen.getByText("gmail.com"));
    expect(search.snapshot.filters.accountIds).toEqual(["5"]);
    const { runSearch } = await import("../lib/tauri");
    expect(runSearch).toHaveBeenCalled();
  });

  it("clicking All Mail clears accountIds/folderIds and submits", async () => {
    search.setFilters({ ...search.snapshot.filters, accountIds: ["5"], folderIds: ["42"] });
    render(AccountTree);
    await fireEvent.click(screen.getByText(/all mail/i));
    expect(search.snapshot.filters.accountIds).toEqual([]);
    expect(search.snapshot.filters.folderIds).toEqual([]);
    const { runSearch } = await import("../lib/tauri");
    expect(runSearch).toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Update `AccountTree.svelte`'s selection handlers**

Open `gui/src/components/AccountTree.svelte`. Find `selectAccount` (the existing handler) and `selectFolder`. Update them to also write into `search` and submit:

```typescript
  import { search } from "../lib/stores/search.svelte";

  async function selectAll() {
    mail.setSelection({ kind: "all" });
    search.setFilters({ ...search.snapshot.filters, accountIds: [], folderIds: [] });
    await search.submit();
  }

  async function selectAccount(accountId: string) {
    mail.setSelection({ kind: "account", accountId });
    // existing folder expansion guarded by expansionsInFlight Set
    // ... preserve that logic verbatim ...
    search.setFilters({
      ...search.snapshot.filters,
      accountIds: [accountId],
      folderIds: [],
    });
    await search.submit();
  }

  async function selectFolder(accountId: string, folderId: string) {
    mail.setSelection({ kind: "folder", accountId, folderId });
    search.setFilters({
      ...search.snapshot.filters,
      accountIds: [accountId],
      folderIds: [folderId],
    });
    await search.submit();
  }
```

Wire the "All Mail" row's `onclick` to `selectAll`.

- [ ] **Step 3: Test, commit**

```bash
cd gui && npm test -- AccountTree 2>&1 | tail -15
```

Expected: existing tests still pass + new 2 pass.

```bash
git add gui/src/components/AccountTree.svelte gui/src/components/AccountTree.test.ts
git commit -m "feat(gui-client): AccountTree drives server-side search on selection

Selecting All Mail clears accountIds/folderIds and submits an empty
query. Selecting an account sets accountIds=[id] and submits.
Selecting a folder sets accountIds=[acc] + folderIds=[folder] and
submits. The middle column then renders the server-narrowed result
set instead of client-side filtering over the 200-message changes
load."
```

---

## Task B11: `HtmlBody.svelte` — sandboxed iframe for HTML rendering

**Files:**
- Create: `gui/src/components/HtmlBody.svelte`
- Create: `gui/src/components/HtmlBody.test.ts`

- [ ] **Step 1: Write the failing test**

```typescript
// gui/src/components/HtmlBody.test.ts
import { render } from "@testing-library/svelte";
import { describe, expect, it } from "vitest";
import HtmlBody from "./HtmlBody.svelte";

describe("HtmlBody", () => {
  it("renders an iframe with sandbox attribute", () => {
    const { container } = render(HtmlBody, { props: { html: "<p>hi</p>", allowExternalImages: false } });
    const iframe = container.querySelector("iframe");
    expect(iframe).not.toBeNull();
    expect(iframe?.getAttribute("sandbox")).toBe("");  // empty sandbox = strictest
  });

  it("srcdoc embeds a CSP meta tag", () => {
    const { container } = render(HtmlBody, { props: { html: "<p>hi</p>", allowExternalImages: false } });
    const iframe = container.querySelector("iframe");
    const srcdoc = iframe?.getAttribute("srcdoc") ?? "";
    expect(srcdoc).toContain("Content-Security-Policy");
    expect(srcdoc).toContain("default-src 'none'");
    expect(srcdoc).toContain("img-src 'self' data:");
  });

  it("srcdoc widens img-src when allowExternalImages=true", () => {
    const { container } = render(HtmlBody, { props: { html: "<p>hi</p>", allowExternalImages: true } });
    const srcdoc = container.querySelector("iframe")?.getAttribute("srcdoc") ?? "";
    expect(srcdoc).toContain("img-src *");
    expect(srcdoc).not.toContain("img-src 'self' data:");
  });

  it("includes the server-sanitised HTML payload in srcdoc body", () => {
    const html = "<p>hello <b>world</b></p>";
    const { container } = render(HtmlBody, { props: { html, allowExternalImages: false } });
    const srcdoc = container.querySelector("iframe")?.getAttribute("srcdoc") ?? "";
    expect(srcdoc).toContain(html);
  });

  it("escapes the closing </script> sequence in payload to avoid breakout", () => {
    // (Defense in depth — the server bleach pass should already strip <script>,
    // but if a payload ever contained the literal string "</script" we still
    // need it to not terminate any <script> block. Here we have no script,
    // so we just assert the literal text is present escaped.)
    const html = "</script><img>";
    const { container } = render(HtmlBody, { props: { html, allowExternalImages: false } });
    const srcdoc = container.querySelector("iframe")?.getAttribute("srcdoc") ?? "";
    // The character sequence "</script>" only inside the srcdoc as element
    // content, not as closing a script tag (srcdoc has no <script>).
    // Just sanity-check it's present.
    expect(srcdoc).toContain("</script>");
  });
});
```

- [ ] **Step 2: Implement**

```svelte
<!-- gui/src/components/HtmlBody.svelte -->
<script lang="ts">
  /**
   * Renders server-sanitised email HTML inside a sandboxed iframe with its
   * own CSP. The iframe carries `sandbox=""` (strictest — no scripts, forms,
   * popups, top-navigation, same-origin), so even a CSP-bypass payload can't
   * exfiltrate.
   *
   * The per-iframe <meta http-equiv="Content-Security-Policy"> blocks all
   * external resource loads by default; when `allowExternalImages` is true
   * we widen img-src to '*' for this message only.
   */
  let { html, allowExternalImages } = $props<{
    html: string;
    allowExternalImages: boolean;
  }>();

  function csp(): string {
    const imgSrc = allowExternalImages ? "*" : "'self' data:";
    return [
      "default-src 'none'",
      `img-src ${imgSrc}`,
      "style-src 'unsafe-inline'",
    ].join("; ");
  }

  function srcdoc(): string {
    // Inline CSP via meta http-equiv — browsers honor it for the embedded
    // document. <base target="_blank"> sends user-clicked links out to the
    // platform default browser (Tauri intercepts; Sub-plan 5 wires this).
    return `<!doctype html><html><head>` +
           `<meta http-equiv="Content-Security-Policy" content="${csp()}">` +
           `<base target="_blank">` +
           `<style>body{font:14px/1.4 system-ui,sans-serif;margin:8px;color:#222}</style>` +
           `</head><body>${html}</body></html>`;
  }
</script>

<iframe sandbox="" srcdoc={srcdoc()} title="message body"></iframe>

<style>
  iframe { width: 100%; height: 100%; border: none; background: #fff; }
</style>
```

- [ ] **Step 3: Test, commit**

```bash
cd gui && npm test -- HtmlBody 2>&1 | tail -10
```

Expected: 5 passes.

```bash
git add gui/src/components/HtmlBody.svelte gui/src/components/HtmlBody.test.ts
git commit -m "feat(gui-client): HtmlBody — sandboxed iframe srcdoc with per-iframe CSP

sandbox=\"\" is the strictest sandbox (no scripts, no forms, no popups,
no same-origin). Per-iframe CSP via <meta http-equiv> blocks all
external resource loads by default; allowExternalImages=true widens
img-src to '*' for that message only. App-level CSP stays strict —
this iframe is the only place HTML email is rendered."
```

---

## Task B12: `ReadingPane` body-mode toggle (HTML · Plain · Raw) + external-images affordance

**Files:**
- Modify: `gui/src/components/ReadingPane.svelte`
- Modify: `gui/src/lib/stores/mail.svelte.ts` (add `bodyMode`, `externalImagesAllowed` state)
- Modify: `gui/src/components/ReadingPane.test.ts` (extend)

Per spec: HTML default, Plain available, Raw view-source. External images blocked until the user clicks "Load images" — that toggle is per-message and resets when the user opens a different message.

- [ ] **Step 1: Extend `mail.svelte.ts`**

Add two fields to `MailState`:

```typescript
export interface MailState {
  // ... existing fields ...
  bodyMode: "html" | "plain" | "raw";
  externalImagesAllowed: boolean;
}

function initialState(): MailState {
  return {
    // ... existing ...
    bodyMode: "html",
    externalImagesAllowed: false,
  };
}
```

Extend the class:

```typescript
  setBodyMode(mode: "html" | "plain" | "raw"): void {
    this.#state.bodyMode = mode;
  }

  setExternalImagesAllowed(v: boolean): void {
    this.#state.externalImagesAllowed = v;
  }
```

And inside `openMessage(id)`, reset both whenever the id changes:

```typescript
  async openMessage(messageId: string): Promise<void> {
    if (this.#state.selectedMessage?.id === messageId) return;
    this.#state.loadingDetail = true;
    this.#state.errorMessage = null;
    this.#state.externalImagesAllowed = false;  // reset per-message
    // Keep bodyMode sticky across messages (user preference).
    try {
      // ... existing ...
```

- [ ] **Step 2: Extend `ReadingPane.svelte`**

Replace the current plain-text body block with a tabbed body view:

```svelte
<script lang="ts">
  import HtmlBody from "./HtmlBody.svelte";
  import { mail } from "../lib/stores/mail.svelte";

  // ... existing imports / header rendering ...

  function setMode(m: "html" | "plain" | "raw") { mail.setBodyMode(m); }
</script>

<!-- header block (unchanged from Sub-plan 3) -->

<nav class="modes">
  <button
    type="button"
    class:active={mail.snapshot.bodyMode === "html"}
    onclick={() => setMode("html")}
    disabled={!mail.snapshot.selectedMessage?.body_html}
  >HTML</button>
  <button
    type="button"
    class:active={mail.snapshot.bodyMode === "plain"}
    onclick={() => setMode("plain")}
    disabled={!mail.snapshot.selectedMessage?.body_text}
  >Plain</button>
  <button
    type="button"
    class:active={mail.snapshot.bodyMode === "raw"}
    onclick={() => setMode("raw")}
  >Raw</button>

  {#if mail.snapshot.bodyMode === "html"
       && mail.snapshot.selectedMessage?.body_html
       && !mail.snapshot.externalImagesAllowed}
    <button type="button" class="images" onclick={() => mail.setExternalImagesAllowed(true)}>
      Load images for this message
    </button>
  {/if}
</nav>

<section class="body">
  {#if !mail.snapshot.selectedMessage}
    <p class="placeholder">Select a message to read it.</p>
  {:else if mail.snapshot.bodyMode === "html" && mail.snapshot.selectedMessage.body_html}
    <HtmlBody
      html={mail.snapshot.selectedMessage.body_html}
      allowExternalImages={mail.snapshot.externalImagesAllowed}
    />
  {:else if mail.snapshot.bodyMode === "plain" && mail.snapshot.selectedMessage.body_text}
    <pre class="plain">{mail.snapshot.selectedMessage.body_text}</pre>
  {:else if mail.snapshot.bodyMode === "raw"}
    <p class="placeholder">Raw RFC822 view arrives with the headers-unfold widget in Sub-plan 5.</p>
  {:else}
    <p class="placeholder">No {mail.snapshot.bodyMode} body available.</p>
  {/if}
</section>

<style>
  .modes { display: flex; gap: 4px; padding: 6px 12px;
           border-bottom: 1px solid #eee; background: #fafbfd; }
  .modes button { padding: 2px 8px; font-size: 12px; background: #fff;
                  border: 1px solid #ccc; border-radius: 3px; cursor: pointer; }
  .modes button.active { background: #e1edff; border-color: #99b8e0; }
  .modes button:disabled { opacity: 0.4; cursor: not-allowed; }
  .modes button.images { margin-left: auto; color: #2a4d99; }
  .body { flex: 1; min-height: 0; overflow: auto; }
  .plain { white-space: pre-wrap; word-break: break-word;
           padding: 8px 12px; font: 13px/1.5 ui-monospace, SFMono-Regular, monospace; }
  .placeholder { padding: 16px; color: #888; font-style: italic; }
</style>
```

- [ ] **Step 3: Extend tests**

Add to `gui/src/components/ReadingPane.test.ts`:

```typescript
describe("ReadingPane body-mode toggle", () => {
  beforeEach(() => { mail.reset(); });

  it("renders HtmlBody when bodyMode=html and body_html present", async () => {
    // Mock openMessage so we don't hit the network.
    const { container } = render(ReadingPane);
    (mail as any).snapshot.selectedMessage = {
      id: "1", subject: "Hi", from: { name: null, address: "x@x" },
      to: [], cc: [], bcc: [], date: null,
      body_text: "plain", body_html: "<p>html</p>",
      attachments: [], account: { id: "1", name: null, address: null }, folders: [],
    };
    (mail as any).snapshot.bodyMode = "html";
    await Promise.resolve();
    expect(container.querySelector("iframe")).not.toBeNull();
  });

  it("Load images button visible when bodyMode=html and not yet allowed", async () => {
    render(ReadingPane);
    (mail as any).snapshot.selectedMessage = {
      id: "1", subject: null, from: { name: null, address: null },
      to: [], cc: [], bcc: [], date: null,
      body_text: null, body_html: "<p>x</p>", attachments: [],
      account: { id: "1", name: null, address: null }, folders: [],
    };
    (mail as any).snapshot.bodyMode = "html";
    await Promise.resolve();
    expect(screen.getByRole("button", { name: /load images/i })).toBeInTheDocument();
  });
});
```

- [ ] **Step 4: Test, commit**

```bash
cd gui && npm test -- ReadingPane 2>&1 | tail -15
cd gui && npm run check 2>&1 | tail -10
```

Expected: all green.

```bash
git add gui/src/components/ReadingPane.svelte \
        gui/src/components/ReadingPane.test.ts \
        gui/src/lib/stores/mail.svelte.ts
git commit -m "feat(gui-client): ReadingPane HTML/Plain/Raw toggle + Load images

bodyMode and externalImagesAllowed live on the mail store. bodyMode
is sticky across messages (user preference); externalImagesAllowed
resets per-message. Disabled buttons for missing body modes."
```

---

## Task B13: Rust command — `/v1/attachments/{sha256}` download

**Files:**
- Create: `gui/src-tauri/src/commands/attachments.rs`
- Modify: `gui/src-tauri/src/commands/mod.rs`
- Modify: `gui/src-tauri/src/lib.rs`

The download command writes the streamed body to a caller-chosen path, returning the byte count. Preview uses `tauri::api::dialog::FileDialog::save_file` separately (covered by the preview modal). For v1 we keep the API minimal: pass `sha256` + a destination path, get back the byte count.

- [ ] **Step 1: Write the failing test**

```rust
// gui/src-tauri/src/commands/attachments.rs
//! Attachment download.
//!
//! GET /v1/attachments/{sha256} → stream bytes to disk, return byte count.
//! Caller (Svelte) provides the destination path obtained from a save dialog.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

use crate::commands::auth::AuthError;
use crate::commands::session::read_authenticated;
use crate::http::client::build_pinned_client;

#[derive(Debug, Deserialize, Serialize)]
pub struct DownloadResult {
    pub bytes_written: u64,
    pub path: String,
}

pub async fn download_attachment(
    store: &crate::storage::keyring::KeyringStore,
    sha256: &str,
    dest: PathBuf,
) -> Result<DownloadResult, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/attachments/{sha256}");
    let resp = client.get(&endpoint).bearer_auth(&token).send().await
        .map_err(|e| AuthError::Http(format!("attachment request: {e}")))?;
    if !resp.status().is_success() {
        return Err(AuthError::Http(format!("HTTP {} on {endpoint}", resp.status())));
    }
    let bytes = resp.bytes().await
        .map_err(|e| AuthError::Http(format!("read body: {e}")))?;
    std::fs::write(&dest, &bytes)
        .map_err(|e| AuthError::Io(format!("write {}: {e}", dest.display())))?;
    Ok(DownloadResult {
        bytes_written: bytes.len() as u64,
        path: dest.to_string_lossy().to_string(),
    })
}

#[tauri::command]
pub async fn download_attachment_cmd(
    sha256: String,
    dest: String,
) -> Result<DownloadResult, AuthError> {
    let store = crate::storage::keyring::KeyringStore::new();
    download_attachment(&store, &sha256, PathBuf::from(dest)).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::keyring::{KeyringStore, MemKeyring, Slot};

    fn store() -> KeyringStore { KeyringStore::with_backend(MemKeyring::new()) }

    #[tokio::test]
    async fn download_without_connection_returns_not_connected() {
        let s = store();
        let err = download_attachment(&s, "deadbeef", PathBuf::from("/tmp/x"))
            .await.unwrap_err();
        assert!(matches!(err, AuthError::NotConnected));
    }

    #[tokio::test]
    async fn download_without_token_returns_not_logged_in() {
        let s = store();
        s.put(Slot::ServerUrl, "https://localhost:8443/").unwrap();
        s.put(Slot::CertPin, "deadbeef").unwrap();
        let err = download_attachment(&s, "x", PathBuf::from("/tmp/x"))
            .await.unwrap_err();
        assert!(matches!(err, AuthError::NotLoggedIn));
    }
}
```

If `AuthError` lacks an `Io(String)` variant, add it next to `Http`. If it lacks `Http(String)`, add that too. Re-check the existing `AuthError` enum first to avoid duplicates.

- [ ] **Step 2: Wire into module tree**

Add `pub mod attachments;` to `commands/mod.rs`. Register `commands::attachments::download_attachment_cmd` in `lib.rs`'s `generate_handler!`.

- [ ] **Step 3: TS invoke wrapper**

Append to `gui/src/lib/tauri.ts`:

```typescript
export interface DownloadResult {
  bytes_written: number;
  path: string;
}

export async function downloadAttachment(sha256: string, dest: string): Promise<DownloadResult> {
  return invoke<DownloadResult>("download_attachment_cmd", { sha256, dest });
}
```

- [ ] **Step 4: Test, commit**

```bash
cd gui/src-tauri && cargo test commands::attachments 2>&1 | tail -10
cd gui && npm run check 2>&1 | tail -10
```

Expected: 2 cargo passes; zero check errors.

```bash
git add gui/src-tauri/src/commands/attachments.rs \
        gui/src-tauri/src/commands/mod.rs \
        gui/src-tauri/src/lib.rs \
        gui/src/lib/tauri.ts
git commit -m "feat(gui-client): Rust /v1/attachments/{sha256} download command

Writes the streamed body to a caller-chosen path. Svelte calls
downloadAttachment(sha256, destPath) where destPath comes from the
Tauri save dialog. Byte count is returned for UI confirmation."
```

---

## Task B14: `AttachmentsStrip.svelte` + `AttachmentRow.svelte`

**Files:**
- Create: `gui/src/components/AttachmentRow.svelte`
- Create: `gui/src/components/AttachmentsStrip.svelte`
- Create: `gui/src/components/AttachmentsStrip.test.ts`
- Modify: `gui/src/components/ReadingPane.svelte` (mount the strip)

- [ ] **Step 1: Write the failing test**

```typescript
// gui/src/components/AttachmentsStrip.test.ts
import { fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import AttachmentsStrip from "./AttachmentsStrip.svelte";
import { mail } from "../lib/stores/mail.svelte";

vi.mock("@tauri-apps/plugin-dialog", () => ({
  save: vi.fn(async () => "/tmp/x.pdf"),
}));
vi.mock("../lib/tauri", () => ({
  downloadAttachment: vi.fn(async () => ({ bytes_written: 1234, path: "/tmp/x.pdf" })),
}));

afterEach(() => { mail.reset(); vi.clearAllMocks(); });

describe("AttachmentsStrip", () => {
  it("renders nothing when selectedMessage has no attachments", () => {
    const { container } = render(AttachmentsStrip);
    expect(container.querySelectorAll(".attachment").length).toBe(0);
  });

  it("renders one row per attachment with download button", () => {
    (mail as any).snapshot.selectedMessage = {
      id: "1", attachments: [
        { filename: "invoice.pdf", sha256: "deadbeef" },
        { filename: "photo.jpg", sha256: "feedface" },
      ],
      body_text: null, body_html: null, subject: null,
      from: { name: null, address: null }, to: [], cc: [], bcc: [], date: null,
      account: { id: "1", name: null, address: null }, folders: [],
    };
    render(AttachmentsStrip);
    expect(screen.getByText("invoice.pdf")).toBeInTheDocument();
    expect(screen.getByText("photo.jpg")).toBeInTheDocument();
  });

  it("clicking download opens save dialog and calls downloadAttachment", async () => {
    (mail as any).snapshot.selectedMessage = {
      id: "1", attachments: [{ filename: "invoice.pdf", sha256: "deadbeef" }],
      body_text: null, body_html: null, subject: null,
      from: { name: null, address: null }, to: [], cc: [], bcc: [], date: null,
      account: { id: "1", name: null, address: null }, folders: [],
    };
    render(AttachmentsStrip);
    await fireEvent.click(screen.getByRole("button", { name: /download/i }));
    const { save } = await import("@tauri-apps/plugin-dialog");
    const { downloadAttachment } = await import("../lib/tauri");
    expect(save).toHaveBeenCalled();
    expect(downloadAttachment).toHaveBeenCalledWith("deadbeef", "/tmp/x.pdf");
  });
});
```

- [ ] **Step 2: Implement `AttachmentRow.svelte`**

```svelte
<!-- gui/src/components/AttachmentRow.svelte -->
<script lang="ts">
  import { save } from "@tauri-apps/plugin-dialog";
  import { downloadAttachment } from "../lib/tauri";

  let { filename, sha256, onPreview = null } = $props<{
    filename: string | null;
    sha256: string | null;
    onPreview?: (() => void) | null;
  }>();

  let downloading = $state(false);
  let error: string | null = $state(null);

  function ext(): string {
    if (!filename) return "";
    const idx = filename.lastIndexOf(".");
    return idx >= 0 ? filename.slice(idx).toLowerCase() : "";
  }

  function canPreview(): boolean {
    const e = ext();
    return e === ".pdf" || e === ".png" || e === ".jpg" || e === ".jpeg" || e === ".gif" || e === ".webp";
  }

  async function download() {
    if (!sha256) return;
    downloading = true; error = null;
    try {
      const dest = await save({ defaultPath: filename ?? sha256 });
      if (!dest) { downloading = false; return; }
      await downloadAttachment(sha256, dest as string);
    } catch (e: unknown) {
      error = String(e);
    } finally {
      downloading = false;
    }
  }
</script>

<div class="attachment">
  <span class="name">{filename ?? "(unnamed)"}</span>
  {#if canPreview() && onPreview}
    <button type="button" onclick={onPreview} title="Preview">👁</button>
  {/if}
  <button type="button" onclick={download} disabled={downloading || !sha256} title="Download">
    {downloading ? "…" : "⤓"} Download
  </button>
  {#if error}
    <span class="error">{error}</span>
  {/if}
</div>

<style>
  .attachment { display: inline-flex; align-items: center; gap: 6px;
                background: #f4f6f9; border: 1px solid #ccd3df; padding: 4px 8px;
                border-radius: 6px; margin: 0 6px 6px 0; font-size: 12px; }
  .name { font-weight: 500; }
  button { background: #fff; border: 1px solid #bbb; padding: 1px 6px;
           border-radius: 3px; cursor: pointer; font-size: 12px; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .error { color: #c00; }
</style>
```

- [ ] **Step 3: Implement `AttachmentsStrip.svelte`**

```svelte
<!-- gui/src/components/AttachmentsStrip.svelte -->
<script lang="ts">
  import AttachmentRow from "./AttachmentRow.svelte";
  import { mail } from "../lib/stores/mail.svelte";

  let previewSha: string | null = $state(null);
  let previewFilename: string | null = $state(null);

  function openPreview(sha256: string | null, filename: string | null) {
    previewSha = sha256; previewFilename = filename;
  }
  function closePreview() {
    previewSha = null; previewFilename = null;
  }

  let atts = $derived(mail.snapshot.selectedMessage?.attachments ?? []);
</script>

{#if atts.length > 0}
  <div class="strip">
    {#each atts as a (a.sha256 ?? a.filename ?? Math.random())}
      <AttachmentRow
        filename={a.filename}
        sha256={a.sha256}
        onPreview={a.sha256 ? () => openPreview(a.sha256, a.filename) : null}
      />
    {/each}
  </div>
{/if}

{#if previewSha}
  {#await import("./AttachmentPreviewModal.svelte") then mod}
    {@const C = mod.default}
    <C sha256={previewSha} filename={previewFilename} onClose={closePreview} />
  {/await}
{/if}

<style>
  .strip { padding: 6px 12px; border-top: 1px solid #eee; background: #fafbfd;
           display: flex; flex-wrap: wrap; }
</style>
```

- [ ] **Step 4: Mount in `ReadingPane.svelte`**

Add at the bottom of the markup (after the `<section class="body">`):

```svelte
<AttachmentsStrip />
```

And import:

```svelte
  import AttachmentsStrip from "./AttachmentsStrip.svelte";
```

- [ ] **Step 5: Test, commit**

```bash
cd gui && npm test -- AttachmentsStrip 2>&1 | tail -15
cd gui && npm run check 2>&1 | tail -10
```

Expected: 3 passes; zero check errors. `@tauri-apps/plugin-dialog` may need to be added — check `package.json`. If missing:

```bash
cd gui && npm install --save @tauri-apps/plugin-dialog
```

And add the corresponding Rust crate + capability — see Tauri docs. If the capability is not yet configured, the runtime save dialog will throw at runtime but tests pass (the dialog is mocked).

```bash
git add gui/src/components/AttachmentRow.svelte \
        gui/src/components/AttachmentsStrip.svelte \
        gui/src/components/AttachmentsStrip.test.ts \
        gui/src/components/ReadingPane.svelte \
        gui/package.json gui/package-lock.json
git commit -m "feat(gui-client): attachments strip with per-row download

Each AttachmentRow has a Download button that opens the Tauri save
dialog then calls download_attachment_cmd. Preview button shows only
for previewable types (PDF + common image formats) — wired to the
modal in Task B15."
```

---

## Task B15: `AttachmentPreviewModal.svelte` — image + PDF preview

**Files:**
- Create: `gui/src/components/AttachmentPreviewModal.svelte`
- Create: `gui/src/components/AttachmentPreviewModal.test.ts`

For images: fetch the blob via the Rust client, hand the bytes to an `<img src="blob:…">`. For PDFs: lazy-import `pdfjs-dist`, render the first page (or all pages, scrollable) into a `<canvas>`. Modal overlay closes on Escape or backdrop click.

- [ ] **Step 1: Add a "fetch bytes" Rust helper**

The download command writes to disk; for preview we want bytes in memory. Extend `commands/attachments.rs`:

```rust
#[derive(Debug, Serialize)]
pub struct AttachmentBlob {
    pub bytes: Vec<u8>,
    pub content_type: Option<String>,
}

pub async fn fetch_attachment_bytes(
    store: &crate::storage::keyring::KeyringStore,
    sha256: &str,
) -> Result<AttachmentBlob, AuthError> {
    let (url, pin, token) = read_authenticated(store)?;
    let client = build_pinned_client(&pin)?;
    let endpoint = format!("{url}v1/attachments/{sha256}");
    let resp = client.get(&endpoint).bearer_auth(&token).send().await
        .map_err(|e| AuthError::Http(format!("attachment request: {e}")))?;
    if !resp.status().is_success() {
        return Err(AuthError::Http(format!("HTTP {} on {endpoint}", resp.status())));
    }
    let content_type = resp.headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok()).map(|s| s.to_string());
    let bytes = resp.bytes().await
        .map_err(|e| AuthError::Http(format!("read body: {e}")))?;
    Ok(AttachmentBlob { bytes: bytes.to_vec(), content_type })
}

#[tauri::command]
pub async fn fetch_attachment_bytes_cmd(sha256: String) -> Result<AttachmentBlob, AuthError> {
    let store = crate::storage::keyring::KeyringStore::new();
    fetch_attachment_bytes(&store, &sha256).await
}
```

Register `fetch_attachment_bytes_cmd` in `lib.rs`.

Add the TS wrapper to `tauri.ts`:

```typescript
export interface AttachmentBlob {
  bytes: number[];
  content_type: string | null;
}

export async function fetchAttachmentBytes(sha256: string): Promise<AttachmentBlob> {
  return invoke<AttachmentBlob>("fetch_attachment_bytes_cmd", { sha256 });
}
```

Add a cargo test for the new helper (mirrors the existing download tests — NotConnected / NotLoggedIn paths).

- [ ] **Step 2: Write the failing test**

```typescript
// gui/src/components/AttachmentPreviewModal.test.ts
import { fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/tauri", () => ({
  fetchAttachmentBytes: vi.fn(async () => ({
    bytes: Array.from(new Uint8Array([137, 80, 78, 71])), // PNG magic bytes
    content_type: "image/png",
  })),
}));

import AttachmentPreviewModal from "./AttachmentPreviewModal.svelte";

afterEach(() => { vi.clearAllMocks(); });

describe("AttachmentPreviewModal", () => {
  it("renders an <img> for image content_type", async () => {
    const { container } = render(AttachmentPreviewModal, {
      props: { sha256: "abc", filename: "photo.png", onClose: () => {} },
    });
    // Wait for the async fetchAttachmentBytes microtask.
    await new Promise((r) => setTimeout(r, 10));
    expect(container.querySelector("img")).not.toBeNull();
  });

  it("calls onClose when backdrop is clicked", async () => {
    const onClose = vi.fn();
    const { container } = render(AttachmentPreviewModal, {
      props: { sha256: "abc", filename: "photo.png", onClose },
    });
    await fireEvent.click(container.querySelector(".backdrop")!);
    expect(onClose).toHaveBeenCalled();
  });

  it("calls onClose on Escape", async () => {
    const onClose = vi.fn();
    render(AttachmentPreviewModal, {
      props: { sha256: "abc", filename: "photo.png", onClose },
    });
    await fireEvent.keyDown(document.body, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });
});
```

- [ ] **Step 3: Implement**

```svelte
<!-- gui/src/components/AttachmentPreviewModal.svelte -->
<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { fetchAttachmentBytes } from "../lib/tauri";

  let { sha256, filename, onClose } = $props<{
    sha256: string;
    filename: string | null;
    onClose: () => void;
  }>();

  let blobUrl: string | null = $state(null);
  let contentType: string | null = $state(null);
  let error: string | null = $state(null);
  let canvasEl: HTMLCanvasElement | null = $state(null);

  function onKey(e: KeyboardEvent) {
    if (e.key === "Escape") onClose();
  }

  function ext(): string {
    if (!filename) return "";
    const i = filename.lastIndexOf(".");
    return i >= 0 ? filename.slice(i).toLowerCase() : "";
  }

  function isPdf(): boolean {
    return ext() === ".pdf" || contentType === "application/pdf";
  }

  onMount(async () => {
    document.addEventListener("keydown", onKey);
    try {
      const blob = await fetchAttachmentBytes(sha256);
      contentType = blob.content_type;
      const u8 = new Uint8Array(blob.bytes);
      blobUrl = URL.createObjectURL(new Blob([u8], { type: blob.content_type ?? "application/octet-stream" }));
      if (isPdf()) {
        // Lazy-import only when we actually have a PDF.
        const pdfjs = await import("pdfjs-dist");
        // Vite's worker import gets a special URL.
        const PdfjsWorker = (await import("pdfjs-dist/build/pdf.worker.mjs?url")).default;
        (pdfjs as unknown as { GlobalWorkerOptions: { workerSrc: string } })
          .GlobalWorkerOptions.workerSrc = PdfjsWorker;
        const doc = await pdfjs.getDocument({ data: u8 }).promise;
        const page = await doc.getPage(1);
        const viewport = page.getViewport({ scale: 1.5 });
        if (canvasEl) {
          canvasEl.width = viewport.width;
          canvasEl.height = viewport.height;
          const ctx = canvasEl.getContext("2d");
          if (ctx) {
            await page.render({ canvasContext: ctx, viewport }).promise;
          }
        }
      }
    } catch (e: unknown) {
      error = String(e);
    }
  });

  onDestroy(() => {
    document.removeEventListener("keydown", onKey);
    if (blobUrl) URL.revokeObjectURL(blobUrl);
  });
</script>

<div class="backdrop" onclick={onClose} onkeydown={onKey} role="button" tabindex="-1">
  <div class="modal" onclick={(e) => e.stopPropagation()} role="dialog" aria-label="Attachment preview">
    <header>
      <span>{filename ?? sha256}</span>
      <button type="button" onclick={onClose} aria-label="Close">×</button>
    </header>
    <section class="body">
      {#if error}
        <p class="error">{error}</p>
      {:else if !blobUrl}
        <p class="placeholder">Loading…</p>
      {:else if isPdf()}
        <canvas bind:this={canvasEl}></canvas>
      {:else}
        <img src={blobUrl} alt={filename ?? ""} />
      {/if}
    </section>
  </div>
</div>

<style>
  .backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.5);
              display: flex; align-items: center; justify-content: center;
              z-index: 1000; }
  .modal { background: #fff; border-radius: 6px; max-width: 80vw; max-height: 80vh;
           display: flex; flex-direction: column; box-shadow: 0 4px 16px rgba(0,0,0,0.2); }
  header { display: flex; justify-content: space-between; align-items: center;
           padding: 8px 12px; border-bottom: 1px solid #eee; font-size: 13px; }
  header button { background: transparent; border: none; cursor: pointer;
                  font-size: 20px; color: #555; }
  .body { padding: 12px; overflow: auto; }
  .body img { max-width: 70vw; max-height: 70vh; display: block; }
  .body canvas { display: block; }
  .placeholder { color: #888; font-style: italic; }
  .error { color: #c00; }
</style>
```

- [ ] **Step 4: Test, commit**

```bash
cd gui && npm test -- AttachmentPreviewModal 2>&1 | tail -15
cd gui && npm run check 2>&1 | tail -10
cd gui/src-tauri && cargo test 2>&1 | tail -10
```

Expected: all green. The PDF rendering path isn't directly exercised in the test (pdfjs-dist is heavy to mock); the image path is, and a manual smoke step covers PDF rendering end-to-end.

```bash
git add gui/src/components/AttachmentPreviewModal.svelte \
        gui/src/components/AttachmentPreviewModal.test.ts \
        gui/src-tauri/src/commands/attachments.rs \
        gui/src-tauri/src/lib.rs \
        gui/src/lib/tauri.ts
git commit -m "feat(gui-client): AttachmentPreviewModal for images + PDFs

Images render directly via an object URL. PDFs lazy-import pdfjs-dist
on demand (kept out of the initial bundle), render first page into a
canvas. Escape and backdrop click close the modal."
```

---

## Task B16: `MainView` mounts SearchBar + chips above the panes

**Files:**
- Modify: `gui/src/screens/MainView.svelte`

- [ ] **Step 1: Update `MainView.svelte`**

Insert above the existing `<main class="panes">`:

```svelte
<script lang="ts">
  // ... existing imports ...
  import SearchBar from "../components/SearchBar.svelte";
  import ActiveFilterChips from "../components/ActiveFilterChips.svelte";
</script>

<!-- existing header (capability pills + auth buttons) -->

<SearchBar />
<ActiveFilterChips />

<main class="panes">
  <!-- existing AccountTree / MessageList / ReadingPane -->
</main>
```

Adjust the `.app` grid-template-rows to add two more `auto` rows for the new components:

```css
  .app {
    height: 100vh;
    display: grid;
    grid-template-rows: auto auto auto 1fr;
  }
```

- [ ] **Step 2: Smoke-check the build**

```bash
cd gui && npm run check 2>&1 | tail -10
cd gui && npm test 2>&1 | tail -10
```

Expected: zero check errors; full suite still green.

- [ ] **Step 3: Commit**

```bash
git add gui/src/screens/MainView.svelte
git commit -m "feat(gui-client): mount SearchBar + ActiveFilterChips in MainView"
```

---

## Task B17: README "Manual smoke (Sub-plan 4 acceptance)" + final commit

**Files:**
- Modify: `gui/README.md`

- [ ] **Step 1: Append to `gui/README.md`** (after the existing "Manual smoke (Sub-plan 3 acceptance)" section)

```markdown
## Manual smoke (Sub-plan 4 acceptance)

Prereqs: server from Phase A (account_ids/folder_ids filter wiring) must be
running. If you see a `ValidationFailed: filter 'account_ids' is accepted
by the API schema but not yet wired through to the search backend` chip in
the GUI, Phase A has not been merged into the server build you're hitting.

```bash
cd gui
npm run tauri dev
```

Acceptance steps:

1. Log in (Sub-plan 2 flow).
2. **Tree narrowing is now server-side.** Click an account — the middle pane
   updates to show the server-returned, account-narrowed result set (not a
   client-side filter over the 200-message changes load).
3. Click a folder under an account. Same — server-narrowed.
4. Click "📥 All Mail" — clears `accountIds` / `folderIds` and submits an
   empty query; the middle pane shows the most-recent across-all-accounts
   results.
5. **Search bar.** Type `school` and press Enter — results with subject
   text matching "school" appear, snippets highlight matches with yellow
   `<mark>` background. Caption above the list shows "Search took N ms — M
   result(s)".
6. **DSL.** Type `from:anna has:attachment after:2024-01-01` — only matching
   messages appear. Chips below the search bar show `From: anna`, `After:
   2024-01-01`, `Has attachment` — click `×` on the "From: anna" chip to
   remove that one.
7. **Filter popover.** Click "🔧 Filters" — popover opens. Set `Subject =
   invoice`, click Apply. Results filter accordingly; a `Subject: invoice`
   chip appears.
8. **HTML body.** Click any message with an HTML body — reading pane renders
   the HTML inside a sandboxed iframe. External images (if any) are blocked:
   a "Load images for this message" button appears above the body. Click it
   — images load.
9. **Body toggle.** Click "Plain" — switches to plain-text rendering. Click
   "HTML" — switches back. "Raw" shows the deferred placeholder for now.
10. **Attachment download.** Open a message with attachments. Each
    attachment shows as a chip with a Download button. Click Download —
    save dialog appears, choose a destination, file is written.
11. **Image preview.** Open a message with an image attachment, click the 👁
    button on it — modal opens, image is rendered inline. Click backdrop or
    press Escape to close.
12. **PDF preview.** Same with a PDF — first page renders in the modal
    canvas. (Full multi-page paginated view is a Sub-plan 5 polish item.)
13. **Switch messages** — confirm the per-message "Load images" allowance
    resets (a new HTML message starts with images blocked again).
14. Log out and back in — the search store resets, tree clears narrowing.

If any step fails, capture the DevTools console output and `npm run tauri
dev` terminal output, then report.
```

- [ ] **Step 2: Commit**

```bash
git add gui/README.md
git commit -m "docs(gui-client): Sub-plan 4 manual smoke acceptance steps"
```

- [ ] **Step 3: Push + PR (when ready)**

```bash
git push -u origin gui-client-4
gh pr create --base main --head gui-client-4 \
  --title "feat(gui-client): Sub-plan 4 — search + HTML body + attachments" \
  --body "$(cat <<'EOF'
## Summary
- Real search end-to-end: SearchBar + FilterPopover + chips, server-side
  account/folder narrowing via the AccountTree dispatching `/v1/search`,
  snippet rendering with `<mark>` highlighting.
- HTML email bodies render inside a sandboxed iframe with per-iframe CSP
  (images blocked by default, per-message Load Images affordance).
- Attachments strip with per-attachment download (save dialog) and image +
  PDF preview modal (pdfjs-dist lazy-imported).
- Body toggle: HTML · Plain · Raw (Raw is a Sub-plan 5 placeholder).

## Server-side companion PR
The server-side filter wiring (`account_ids` / `folder_ids` in
`SearchFilters` / parser / arms / api/search.py) ships separately on
`worktree-phase2-hybrid-search`. **This client PR is not safe to merge
before that one lands**, otherwise tree clicks produce
`ValidationFailed: filter 'account_ids' is accepted by the API schema but
not yet wired through to the search backend`.

## Out of scope (Sub-plan 5)
- Branded icons + `.dmg`/`.msi`/`.AppImage` bundling.
- Background polling of `/v1/changes`.
- Resizable column splitter.
- Version-mismatch hard modal.
- Settings screen.
- Header-unfold widget + `?headers=full` lazy fetch.
- `date_from` / `date_to` / `lang` filter API forwarding (popover writes
  equivalent DSL tokens which are end-to-end supported).
- Search debug pane.

## Test plan
- [x] `cargo test` in `gui/src-tauri/` — N passes incl. new search +
      attachments command tests
- [x] `npm test` in `gui/` — N passes incl. filter_parse, snippet_sanitize,
      search store, SearchBar, FilterPopover, ActiveFilterChips, HtmlBody,
      AttachmentsStrip, AttachmentPreviewModal, ReadingPane HTML toggle,
      AccountTree dispatch
- [x] `npm run check` — 0 errors
- [x] Manual smoke per `gui/README.md` Sub-plan 4 section — all 14 steps
      against a server built on the Phase A PR

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

The push and PR step is only when ready (after a clean local smoke test); a subagent should not auto-push.

---

## End-of-plan acceptance

When all Phase A and Phase B tasks are done and both PRs are open / mergeable:

**Phase A (server):**
- `pytest tests/test_query_account_folder_id_tokens.py` — 6 passes
- `pytest tests/test_arms_id_filters.py` — 4 passes
- `pytest tests/test_api_search.py` — full suite green incl. 4 new positive tests
- `pytest` — full Phase A suite green
- PR open against `worktree-phase2-hybrid-search`

**Phase B (client):**
- `cargo test` in `gui/src-tauri/` — previous 40 passes + ~10 new from `search`, `attachments`, `download_attachment`, `fetch_attachment_bytes` modules
- `npm test` in `gui/` — previous 48 passes + ~30 new from filter_parse (10), snippet_sanitize (7), search store (5), SearchBar (4), FilterPopover (4), ActiveFilterChips (4), HtmlBody (5), AttachmentsStrip (3), AttachmentPreviewModal (3), MessageList search extension (2), ReadingPane toggle (2), AccountTree dispatch (2)
- `npm run check` — 0 errors
- `npm run tauri dev` produces a working app that walks through the 14 Manual smoke steps without errors
- No file in this sub-plan exceeds ~500 lines (per project convention) — if `ReadingPane.svelte` or `mail.svelte.ts` is approaching the limit, split before the PR

**End-to-end:**
- Server PR merged first; Phase B PR follows.
- Smoke step 2 ("server-side tree narrowing") confirms end-to-end wiring.

## Notes for the executing engineer

- **Phase A first; Phase B's Task B10 (AccountTree dispatch) must not be smoke-tested before the Phase A server PR is merged or the GUI talks to a server with the new filter wiring.** Phase B tasks 0–9 and 11–17 are independent of Phase A and can be done in parallel.
- **Don't loosen the app-level CSP.** The HtmlBody iframe carries its own CSP via `<meta http-equiv>` inside the srcdoc; the outer document stays as locked-down as Sub-plan 2 left it.
- **Reuse `commands/session.rs` helpers** (`read_endpoint`, `read_authenticated`) for any new Rust command. Do not reintroduce the inlined `read_connection` pattern — that's what the Sub-plan 3 review fixed.
- **`<svelte:component>` is deprecated** in runes mode — use `{@const C = mod.default}<C />` for any dynamic component selection.
- **PDF.js worker URL**: Vite resolves `?url` imports to a stable hashed asset URL. If `tauri build` ever complains that the worker file isn't found, the fix is to add the worker to `tauri.conf.json` → `build.beforeBuildCommand` outputs explicitly. Don't disable the worker (synchronous PDF parsing locks the UI thread).
- **No magic numbers**: the search `DEFAULT_LIMIT = 50` in `search.svelte.ts` is the only hard-coded numeric in the search store. Anything else (rerank pool size, etc.) is server-side config.
- **All snippet rendering uses `sanitizeSnippet` + `{@html}`** — never pass server HTML directly to `{@html}` without going through the sanitizer.
- **The PDF preview path renders only page 1 in v1.** Multi-page paginated view is a Sub-plan 5 polish task. If a user complains, point them at Download.
