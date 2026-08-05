# Language-detection mislabel fix (#255) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `body_lang` assigning confident wrong languages (Yoruba, Finnish,
Welsh, Latin…) to English marketing mail — 17% of all labels on the live
archive — by stripping URLs from the detector's input and switching lingua to
full-accuracy mode, then re-labelling both deployments.

**Architecture:** One new pure module (`search/lang_text.py`) holding the single
rule for what the detector sees, applied only inside `LinguaDetector.detect`. A
config default flip. One new DB helper (`lang_detect.reopen_all`) plus a
`lang-backfill --relabel` flag to re-run the archive. No schema change.

**Tech Stack:** Python 3.12, `uv`, `psycopg` v3 (raw SQL), `pydantic` v2 config,
`click` CLI, `pytest`, `lingua-language-detector` 2.2.0.

## Global Constraints

- **Spec:** [docs/superpowers/specs/2026-08-05-lang-detect-mislabel-design.md](../specs/2026-08-05-lang-detect-mislabel-design.md)
- **No migration.** Latest stays `0035_messages_body_lang_attempted_at.sql`; do
  not add `0036_*.sql`.
- **Every new `src/localmail/` file starts with the two-line SPDX header:**
  `# SPDX-License-Identifier: AGPL-3.0-or-later` then
  `# Copyright (C) 2026 Horst Herb`.
- **No magic numbers.** Search tunables live in `LocalmailConfig.search`
  (`SearchConfig` in `src/localmail/config.py`).
- **No comments restating the code.** Comment only a non-obvious WHY.
- **Branch is `fix/lang-detect-url-normalisation`.** Already created; land via
  PR, never push to `main`.
- **Run tests as** `unset VIRTUAL_ENV && uv run pytest …` — a stray
  `VIRTUAL_ENV` picks the wrong interpreter.
- **mypy is enabled.** Annotate every new DB helper's `conn` parameter as
  `psycopg.Connection`, and never index a `fetchone()` without
  `assert row is not None` first.
- **Do not run the full test suite while a backfill is draining** — cluster
  contention produces dozens of false failures.

---

### Task 1: `search/lang_text.py` — the pure normalisation rule

**Files:**
- Create: `src/localmail/search/lang_text.py`
- Test: `tests/test_lang_text.py`

**Interfaces:**
- Consumes: nothing (pure; stdlib `re` only).
- Produces: `normalize_for_detection(text: str) -> str` — used by Task 2.

- [ ] **Step 1: Write the failing test**

Create `tests/test_lang_text.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Tests for the language-detector input rule (#255)."""

from __future__ import annotations

import pytest

from localmail.search.lang_text import normalize_for_detection


@pytest.mark.parametrize(
    "raw",
    [
        "Read more at https://ct.klclick.com/f/a/IgDYzk3AXlDhs1ogMp1raw~~/AASl5QA~/RgRlWoXg now",
        "Read more at http://info.cirrusmedia.com/x?id=t8689239,64ac8b1 now",
        "Read more at www.askapatient.com/viewrating.asp?drug=20233&name=RHINOCORT now",
        "Read more at <http://info.cirrusmedia.com/x?id=t868> now",
    ],
)
def test_urls_are_removed(raw: str) -> None:
    """Every URL form seen in the live archive leaves no residue."""
    out = normalize_for_detection(raw)
    assert "http" not in out
    assert "www." not in out
    assert out.startswith("Read more at")
    assert out.endswith("now")


def test_markdown_link_keeps_its_anchor_text() -> None:
    """The human-readable anchor is the linguistic signal worth keeping."""
    out = normalize_for_detection("[View in Your Browser](https://ct.klclick.com/f/a/x~~/A)")
    assert "View in Your Browser" in out
    assert "klclick" not in out


def test_text_without_urls_is_unchanged_apart_from_whitespace() -> None:
    """The common path must not mangle ordinary prose."""
    assert normalize_for_detection("Hallo Horst, wie geht es dir?") == (
        "Hallo Horst, wie geht es dir?"
    )


def test_whitespace_is_collapsed() -> None:
    assert normalize_for_detection("a\n\n\tb   c") == "a b c"


def test_url_only_body_normalises_to_empty() -> None:
    """The load-bearing case: no linguistic content means the caller declines.

    A body of pure tracking URLs clears the 20-char floor when measured raw and
    receives a confident garbage label. Measured after normalisation it is
    empty, so `LinguaDetector.detect` declines it.
    """
    assert normalize_for_detection("https://a.example/x  http://b.example/y") == ""


def test_is_idempotent() -> None:
    once = normalize_for_detection("see https://x.example/a b")
    assert normalize_for_detection(once) == once


def test_empty_input() -> None:
    assert normalize_for_detection("") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_lang_text.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'localmail.search.lang_text'`

- [ ] **Step 3: Write minimal implementation**

Create `src/localmail/search/lang_text.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""What the language detector is allowed to see.

Marketing and newsletter mail is dominated by tracking URLs whose path
segments are long runs of high-entropy alphanumerics. Lingua scores that soup
confidently and lands on a low-resource language: on the live Mac archive 17%
of all labels named a language with no plausible presence in the archive,
Yoruba alone accounting for 7593 rows (#255).

Stripping URLs before detection resolves 99% of those rows when paired with
full-accuracy mode. Measured on the same archive, stripping invisible
characters (the U+034F preheader padding), email addresses, HTML tags and
separator rules each add **zero** further benefit — so this module does one
thing, and additions belong here only with a measurement behind them.

This is the detector's input rule and nothing else: `messages.body_text`, the
FTS tsvector, chunking and embeddings all continue to see the original body.

Pure: no IO, stdlib only.
"""

from __future__ import annotations

import re

#: Matches the URL forms that occur in mail bodies: an explicit scheme, or the
#: bare `www.` host form that mail clients linkify. `\S+` runs to the next
#: whitespace, which deliberately swallows trailing `>` and `)` from
#: angle-bracketed and markdown-parenthesised links — we are deleting, so
#: over-consuming punctuation costs nothing and under-consuming leaves
#: high-entropy residue behind.
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_for_detection(text: str) -> str:
    """Return `text` with URLs removed and whitespace collapsed.

    The result is what the language detector sees. An empty return means the
    body carried no linguistic content — callers must treat that as "unknown"
    rather than detecting against the original.
    """
    return _WHITESPACE_RE.sub(" ", _URL_RE.sub(" ", text)).strip()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_lang_text.py -q`
Expected: PASS — 11 passed (7 test functions, 4 parametrised cases)

- [ ] **Step 5: Typecheck and lint**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail && uv run ruff check src/localmail/search/lang_text.py`
Expected: mypy `Success`; ruff clean on the new file.

- [ ] **Step 6: Commit**

```bash
git add src/localmail/search/lang_text.py tests/test_lang_text.py
git commit -m "feat(search): pure URL-stripping rule for language-detector input (#255)"
```

---

### Task 2: Apply normalisation in `LinguaDetector.detect`

**Files:**
- Modify: `src/localmail/search/lang_detect.py:149-164` (the `detect` method)
- Modify: `src/localmail/search/lang_detect.py:4-35` (module docstring)
- Test: `tests/test_lang_detect.py`

**Interfaces:**
- Consumes: `normalize_for_detection(text: str) -> str` from Task 1.
- Produces: no signature change. `LinguaDetector.detect(text) -> str | None`
  now declines URL-only bodies.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lang_detect.py`. Note the fake detector: these tests must
not download the lingua model, so they exercise `LinguaDetector`'s policy with
an injected `_detector`.

```python
class _StubLingua:
    """Stands in for a built lingua detector; records what it was asked."""

    def __init__(self, code: str = "YO", confidence: float = 0.99) -> None:
        self.seen: list[str] = []
        self._code = code
        self._confidence = confidence

    def compute_language_confidence_values(self, text: str):  # noqa: ANN201
        self.seen.append(text)
        iso = type("Iso", (), {"name": self._code})
        lang = type("Lang", (), {"iso_code_639_1": iso})
        return [type("Val", (), {"value": self._confidence, "language": lang})()]


def _detector_with(stub: _StubLingua, **kw) -> LinguaDetector:
    det = LinguaDetector(min_confidence=0.65, min_text_chars=20, **kw)
    det._detector = stub
    return det


def test_detector_sees_the_body_with_urls_stripped() -> None:
    """The tracking URL never reaches lingua (#255)."""
    stub = _StubLingua()
    det = _detector_with(stub)
    det.detect("Last chance to save https://ct.klclick.com/f/a/IgDYzk3AXlDh~~/AASl5QA today")
    assert stub.seen == ["Last chance to save today"]


def test_url_only_body_is_declined_without_consulting_the_detector() -> None:
    """A body of pure tracking URLs has no linguistic content.

    Measured raw it clears the 20-char floor and earns a confident wrong label;
    measured after normalisation it is empty. The floor must therefore apply to
    the normalised text, and the detector must not be consulted at all.
    """
    stub = _StubLingua()
    det = _detector_with(stub)
    assert det.detect("https://a.example/aaaaaaaaaaaaaaaaaaaa http://b.example/bbbb") is None
    assert stub.seen == []


def test_length_floor_applies_to_the_normalised_text() -> None:
    """Long enough raw, too short once the URL is gone."""
    stub = _StubLingua()
    det = _detector_with(stub)
    assert det.detect("Hi https://example.com/a-very-long-tracking-path-here") is None
    assert stub.seen == []
```

Add `LinguaDetector` to the existing import block at the top of the file
(it currently imports `CLAIMABLE_WHERE_SQL`, `DECLINED_WHERE_SQL`,
`FixedDetector`, `run_lang_detect_pass` from `localmail.search.lang_detect`).

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_lang_detect.py -q -k "url or normalised"`
Expected: FAIL — `test_detector_sees_the_body_with_urls_stripped` asserts the
stub saw the stripped text but it saw the raw text; the two decline tests fail
because the detector was consulted.

- [ ] **Step 3: Write minimal implementation**

Replace the body of `LinguaDetector.detect` (currently at
`src/localmail/search/lang_detect.py:149-164`):

```python
    def detect(self, text: str) -> str | None:
        normalized = normalize_for_detection(text) if text else ""
        # The floor measures the *normalised* text. A body of pure tracking
        # URLs is long enough raw to clear it and earns a confident wrong
        # label; normalised it is empty and correctly declines (#255).
        if len(normalized) < self._min_text_chars:
            return None
        self._ensure_built()
        assert self._detector is not None
        confidences = self._detector.compute_language_confidence_values(normalized)
        if not confidences:
            return None
        top = confidences[0]
        if top.value < self._min_confidence:
            return None
        return top.language.iso_code_639_1.name.lower()
```

Add the import at the top of the module, beside the existing
`from localmail.config import SearchConfig`:

```python
from localmail.search.lang_text import normalize_for_detection
```

Then extend the module docstring's `LinguaDetector` bullet (line ~15) to read:

```
  - `LinguaDetector`: wraps lingua-py. Normalises the body through
    `lang_text.normalize_for_detection` (URLs out), then applies a confidence
    + length floor to the *normalised* text. Returns None for empty / short /
    low-confidence text so the caller can leave the column NULL ("unknown").
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_lang_detect.py tests/test_lang_text.py -q`
Expected: PASS — all pre-existing `test_lang_detect.py` tests still green
(they use `FixedDetector`, which is untouched).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/lang_detect.py tests/test_lang_detect.py
git commit -m "fix(search): strip URLs before language detection, floor on normalised text (#255)"
```

---

### Task 3: Flip the `body_lang_low_accuracy` default

**Files:**
- Modify: `src/localmail/config.py:503-505`
- Test: `tests/test_lang_detect.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SearchConfig.body_lang_low_accuracy` now defaults `False`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lang_detect.py`:

```python
def test_full_accuracy_is_the_default() -> None:
    """Low-accuracy mode measured worse on every axis (#255).

    On the live Mac archive it left 300/300 implausibly-labelled rows wrong
    where full accuracy left 3, while costing *more* resident memory (239 MB
    vs 227 MB) and running 2.3x slower. The knob survives for a
    memory-constrained host; the default must not.
    """
    assert SearchConfig().body_lang_low_accuracy is False
    assert make_detector(SearchConfig())._low_accuracy is False
```

Add `make_detector` to the existing `localmail.search.lang_detect` import block.

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_lang_detect.py -q -k full_accuracy`
Expected: FAIL — `assert True is False`

- [ ] **Step 3: Write minimal implementation**

Replace `src/localmail/config.py:503-505` (the comment is factually wrong and
goes with it):

```python
    # Lingua's low-accuracy mode uses trigrams only. Measured on the live
    # 100k-message archive it left 300/300 implausibly-labelled rows wrong
    # where full accuracy left 3 — and it is not the cheap option the name
    # suggests: peak RSS 239 MB vs 227 MB for full, at 2.3x the wall time,
    # because lingua loads per-language models lazily either way (#255).
    # Retained only as an escape hatch for a memory-constrained host.
    body_lang_low_accuracy: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_lang_detect.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/localmail/config.py tests/test_lang_detect.py
git commit -m "fix(config): default body_lang detection to full accuracy (#255)"
```

---

### Task 4: `reopen_all` + `RELABELABLE_WHERE_SQL`

**Files:**
- Modify: `src/localmail/search/lang_detect.py` (add constant near
  `CLAIMABLE_WHERE_SQL`/`DECLINED_WHERE_SQL` at lines 51-71; add function
  after `retry_declined`)
- Test: `tests/test_lang_detect.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `RELABELABLE_WHERE_SQL: str` and
  `reopen_all(conn: psycopg.Connection) -> int` — both used by Task 5.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lang_detect.py`:

```python
def test_reopen_all_clears_labels_and_attempt_stamps(db_conn) -> None:
    """Re-labelling must reach rows that already carry a (wrong) label.

    `retry_declined` cannot: by construction it only re-opens rows with no
    label, and the #255 defect is rows labelled confidently and wrongly.
    """
    acct = _seed_account(db_conn)
    _seed_message(db_conn, acct, 1, "anything")      # will be labelled
    _seed_message(db_conn, acct, 2, "undetectable")  # will be declined
    _seed_message(db_conn, acct, 3, None)            # no body
    db_conn.commit()
    run_lang_detect_pass(
        db_conn, SearchConfig(), FixedDetector({"anything": "en"}),
    )

    assert reopen_all(db_conn) == 2  # the bodied rows only

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM messages"
            " WHERE body_lang IS NOT NULL OR body_lang_attempted_at IS NOT NULL"
        )
        row = cur.fetchone()
    assert row is not None and row[0] == 0


def test_relabelable_contains_claimable_and_declined(db_conn) -> None:
    """One authority per predicate; claimable and declined partition it.

    `search-status` reads the first two and the relabel path reads the third.
    A drift between them is how #251 stayed invisible for weeks.
    """
    acct = _seed_account(db_conn)
    _seed_message(db_conn, acct, 1, "anything")
    _seed_message(db_conn, acct, 2, "undetectable")
    _seed_message(db_conn, acct, 3, None)
    db_conn.commit()
    run_lang_detect_pass(
        db_conn, SearchConfig(), FixedDetector({"anything": "en"}),
    )

    with db_conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM messages"
            f" WHERE ({CLAIMABLE_WHERE_SQL}) AND NOT ({RELABELABLE_WHERE_SQL})"
        )
        claimable_outside = cur.fetchone()
        cur.execute(
            f"SELECT count(*) FROM messages"
            f" WHERE ({DECLINED_WHERE_SQL}) AND NOT ({RELABELABLE_WHERE_SQL})"
        )
        declined_outside = cur.fetchone()
    assert claimable_outside is not None and claimable_outside[0] == 0
    assert declined_outside is not None and declined_outside[0] == 0
```

Add `RELABELABLE_WHERE_SQL` and `reopen_all` to the import block.

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_lang_detect.py -q -k "reopen_all or relabelable"`
Expected: FAIL — `ImportError: cannot import name 'reopen_all'`

- [ ] **Step 3: Write minimal implementation**

Add after `DECLINED_WHERE_SQL` (around line 71) in
`src/localmail/search/lang_detect.py`:

```python
#: Every row a re-label pass may reset — i.e. every row with a body, whatever
#: it currently holds. `CLAIMABLE_WHERE_SQL` and `DECLINED_WHERE_SQL` are
#: disjoint subsets of this; the difference is the rows that carry a label.
#: Those are exactly what a detector-policy change invalidates and what
#: `retry_declined` cannot reach (#255).
RELABELABLE_WHERE_SQL = "body_text IS NOT NULL"
```

Add after `retry_declined` (end of module):

```python
def reopen_all(conn: psycopg.Connection) -> int:
    """Clear every body_lang label and attempt stamp; return the row count.

    The escape hatch for a change in *detector policy* rather than in
    thresholds. `retry_declined` re-opens only rows the detector turned away,
    which by construction excludes the rows a wrong policy labelled — and a
    confidently wrong label is the whole of #255.

    Destructive: it discards every existing label, so the archive is
    unsearchable by `lang:` until a drain completes. The caller owns
    confirming that (see `lang-backfill --relabel`).
    """
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE messages SET body_lang = NULL, body_lang_attempted_at = NULL"
            f" WHERE {RELABELABLE_WHERE_SQL}"  # noqa: S608 — module constant
        )
        reopened = cur.rowcount
    conn.commit()
    return reopened
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_lang_detect.py -q`
Expected: PASS

- [ ] **Step 5: Typecheck**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: `Success`

- [ ] **Step 6: Commit**

```bash
git add src/localmail/search/lang_detect.py tests/test_lang_detect.py
git commit -m "feat(search): reopen_all re-opens labelled rows for a policy change (#255)"
```

---

### Task 5: `lang-backfill --relabel`

**Files:**
- Modify: `src/localmail/cli.py:1064-1121` (the `lang_backfill` command)
- Test: `tests/test_cli_lang_backfill.py`

**Interfaces:**
- Consumes: `lang_detect.reopen_all(conn) -> int` from Task 4.
- Produces: `localmail lang-backfill --relabel [--yes]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli_lang_backfill.py`:

```python
def test_relabel_requires_confirmation(monkeypatch, db_dsn, db_conn, cli_config) -> None:
    """The only destructive verb here must not fire on a typo."""
    _seed_messages(db_conn, ["hello world this is a body"])
    monkeypatch.setattr(
        "localmail.search.lang_detect.make_detector",
        lambda cfg: FixedDetector({"hello world this is a body": "en"}),
    )
    result = CliRunner().invoke(
        main, ["--config", str(cli_config), "lang-backfill", "--relabel"], input="n\n"
    )
    assert result.exit_code != 0
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages WHERE body_lang IS NOT NULL")
        row = cur.fetchone()
    assert row is not None and row[0] == 0


def test_relabel_reopens_labelled_rows_and_redetects(
    monkeypatch, db_dsn, db_conn, cli_config
) -> None:
    """A row labelled by the old policy is re-detected under the new one."""
    ids = _seed_messages(db_conn, ["hello world this is a body"])
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE messages SET body_lang = 'yo', body_lang_attempted_at = now()"
            " WHERE id = %s",
            (ids[0],),
        )
    db_conn.commit()

    monkeypatch.setattr(
        "localmail.search.lang_detect.make_detector",
        lambda cfg: FixedDetector({"hello world this is a body": "en"}),
    )
    result = CliRunner().invoke(
        main,
        ["--config", str(cli_config), "lang-backfill", "--relabel", "--yes", "--no-progress"],
    )
    assert result.exit_code == 0, result.output
    assert "re-opened 1" in result.output

    with db_conn.cursor() as cur:
        cur.execute("SELECT body_lang FROM messages WHERE id = %s", (ids[0],))
        row = cur.fetchone()
    assert row is not None and row[0] == "en"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_lang_backfill.py -q -k relabel`
Expected: FAIL — `Error: No such option: --relabel`

- [ ] **Step 3: Write minimal implementation**

Add two options to the `lang_backfill` command in `src/localmail/cli.py`,
after the existing `--retry-declined`:

```python
@click.option(
    "--relabel",
    is_flag=True,
    help="Discard EVERY existing body_lang label and re-detect the archive.",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Skip the --relabel confirmation prompt (for scripted use).",
)
```

Change the signature to:

```python
def lang_backfill(
    ctx: click.Context,
    no_progress: bool,
    retry_declined: bool,
    relabel: bool,
    yes: bool,
) -> None:
```

Extend the docstring with a paragraph:

```
    `--relabel` is for a change in detector *policy* rather than in
    thresholds: it clears every label, not just the declines, because the rows
    a wrong policy labelled confidently are precisely the ones
    `--retry-declined` cannot reach (#255). It is destructive — the archive is
    unsearchable by `lang:` until the drain completes — so it prompts unless
    `--yes` is given.
```

Insert this block immediately after the `pool = open_pool(_dsn(ctx))` /
`try:` line and **before** the existing `if retry_declined:` block, so a
combined invocation re-opens everything first:

```python
        if relabel:
            if not yes:
                click.confirm(
                    "Discard every existing body_lang label and re-detect"
                    " the whole archive?",
                    abort=True,
                )
            with pool.connection() as conn:
                reopened = lang_detect.reopen_all(conn)
            click.echo(f"re-opened {reopened} messages for re-detection")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_lang_backfill.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/localmail/cli.py tests/test_cli_lang_backfill.py
git commit -m "feat(cli): lang-backfill --relabel re-detects the whole archive (#255)"
```

---

### Task 6: Documentation

**Files:**
- Modify: `README.md` (the `lang-backfill` line in the command list)
- Modify: `CLAUDE.md` (the language-detection section under Search subsystem;
  and the `lang-backfill` line in Commands)

**Interfaces:**
- Consumes: everything above. Produces: no code.

- [ ] **Step 1: Update README.md**

Find the `lang-backfill` entry and extend it to mention `--relabel`. Add a
sentence to whatever prose describes `lang:` search noting that a detector
policy change requires `lang-backfill --relabel` to take effect on existing
rows.

- [ ] **Step 2: Update CLAUDE.md**

In the Commands block, change the `lang-backfill` line to:

```
uv run localmail lang-backfill [--retry-declined] [--relabel [--yes]]  # one-shot body_lang detection
```

Under "Search subsystem", after the existing #251 block, add a #255 block
recording: URL stripping is the one detector-input rule and lives in the pure
`search/lang_text.py`; the length floor applies to the **normalised** text (a
URL-only body declines instead of earning a confident wrong label); the
`body_lang_low_accuracy` default is now `False` with the measured figures
(239 MB low vs 227 MB full, full 2.3x faster — the "~100 MB vs ~1 GB" claim
that was in the config comment was wrong); the ablation showing invisible-char
/ email / HTML-tag / separator stripping each add zero, so **do not add them
without a measurement**; and `reopen_all` / `--relabel` being the escape hatch
`retry_declined` cannot provide because it only reaches unlabelled rows.

Update the `search/` layout listing to include `lang_text.py`.

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: record the #255 detector-input rule and --relabel"
```

---

### Task 7: Full verification

**Files:** none modified.

- [ ] **Step 1: Full test suite**

Run: `unset VIRTUAL_ENV && uv run pytest -q --deselect tests/test_daemon_control_socket.py`
Expected: **at least 2186 passed, 0 failed** (2186 was the session-15 baseline;
this plan adds tests, so the count rises).

- [ ] **Step 2: Typecheck**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: `Success` — 135 source files (was 134; `lang_text.py` is new).

- [ ] **Step 3: Lint**

Run: `unset VIRTUAL_ENV && uv run ruff check src/localmail`
Expected: the pre-existing 10 findings, none in a file this plan touched.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin fix/lang-detect-url-normalisation
gh pr create --title "fix(search): stop language detection mislabelling English mail (#255)" --body "..."
```

---

### Task 8: Deploy and re-label both hosts

**Files:** none. Operational; run only after the PR merges.

- [ ] **Step 1: Capture the before state on each host**

```bash
psql -h localhost -p 5532 -U localmail -d localmail -c "
  SELECT body_lang, count(*) FROM messages WHERE body_lang IS NOT NULL
  GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
```

- [ ] **Step 2: Mac — pull, restart, re-label**

```bash
cd /Users/hherb/src/localmail && git checkout main && git pull
unset VIRTUAL_ENV && uv sync --all-extras
launchctl kickstart -k gui/$UID/com.localmail.daemon
unset VIRTUAL_ENV && uv run localmail lang-backfill --relabel --yes
```

Expected: ~10 minutes at ~176 rows/s for ~100k rows.

- [ ] **Step 3: DGX — same, over the LAN**

Look the address up first; it is a DHCP lease and has moved three times.
Sync with `uv sync --extra mcp --extra extraction` (a bare `uv sync` silently
downgrades the host).

- [ ] **Step 4: Verify acceptance on both hosts**

```bash
psql -h localhost -p 5532 -U localmail -d localmail -c "
  SELECT count(*) FILTER (WHERE body_lang IS NOT NULL) AS populated,
         count(*) FILTER (WHERE body_lang IS NULL AND body_text IS NOT NULL
                            AND body_lang_attempted_at IS NULL) AS claimable,
         count(*) FILTER (WHERE body_lang IS NOT NULL AND NOT (body_lang = ANY(
           ARRAY['en','de','fr','es','it','nl','sv','da','no','pt']))) AS implausible
    FROM messages"
```

Acceptance: `claimable` 0, `implausible` under ~200 on the Mac (was 17,129),
and the `en` count materially higher than the previous 73,900.

---

## Self-Review

**Spec coverage.** §1 `lang_text.py` → Task 1. §2 normalize→floor→detect
ordering → Task 2. §3 config flip → Task 3. §4 `reopen_all` +
`RELABELABLE_WHERE_SQL` + `--relabel` + confirmation → Tasks 4-5. §5 no
migration → Global Constraints. §Testing's four bullets → Tasks 1-5. §Acceptance
→ Task 8. No gaps.

**Placeholders.** The only `"..."` is the `gh pr create --body` in Task 7,
which is prose written at the time from the commits. No TBDs.

**Type consistency.** `normalize_for_detection(text: str) -> str` defined in
Task 1, imported under that exact name in Task 2. `reopen_all(conn) -> int` and
`RELABELABLE_WHERE_SQL` defined in Task 4, consumed under those names in Tasks
5. `_low_accuracy` in Task 3's assertion is the existing private attribute set
by `LinguaDetector.__init__`. `_seed_messages` / `_seed_account` /
`_seed_message` are pre-existing test helpers in their respective files.
