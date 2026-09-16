# Message headers per occurrence (`?headers=list`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve a message's headers as one `{name, value}` entry per occurrence in wire order, from the same parsed sequence that `headers=full` groups.

**Architecture:** A new pure module `header_block.py` turns the bytes before the first blank line into an ordered list of `HeaderEntry`. `full` is `group_entries(...)` of that list and `list` is the list itself, so the two modes cannot disagree. `parser._headers_dict` is reimplemented on the same function, so the stored column and the served value come from one rule. The read path fetches a bounded `raw_bytes` prefix instead of the stored JSONB.

**Tech Stack:** Python 3.13 (CI matrixes 3.12), psycopg v3 + raw SQL, FastAPI, pytest, the `mcp` SDK under the `[mcp]` extra.

**Spec:** [docs/superpowers/specs/2026-09-16-message-headers-list-design.md](../specs/2026-09-16-message-headers-list-design.md)

## Global Constraints

- Issue **#379**; branch `fix/kastellan-b-headers-list`, already created off `main` (`2ccfb1b`). One PR, with the handoff, at the end.
- **TDD**: every task writes the failing test first and runs it to see it fail.
- **No magic numbers**: `HEADER_BLOCK_READ_BYTES = 64 * 1024` is the only new constant and it lives in `header_block.py`.
- Every new `src/localmail/*.py` file starts with the two SPDX lines:
  `# SPDX-License-Identifier: AGPL-3.0-or-later` / `# Copyright (C) 2026 Horst Herb`. Files under `gui/` do not.
- **No comments unless the WHY is non-obvious** (CLAUDE.md). Do not restate the code.
- Run the suite with `unset VIRTUAL_ENV && uv run pytest …`. **Never two pytest sessions against one test database** (#336).
- `uv run mypy src/localmail` must stay at **Success, 155 files**; `uv run ruff check src/localmail/ | tail -1` must stay at **10** (the #285 baseline).
- Baseline before any change: **3682 passed, 2 warnings** on this Mac.
- Accepted wire changes: `full` loses leading whitespace on ~12% of live rows; `?headers=<unknown>` becomes 400; `api_minor` becomes 1; MCP `full_headers` becomes `headers`.

---

### Task 1: The pure module

**Files:**
- Create: `src/localmail/header_block.py`
- Test: `tests/test_header_block.py`

**Interfaces:**
- Consumes: `localmail.pgtext.strip_nuls` (existing).
- Produces: `HeaderMode`, `HEADER_MODES`, `HEADER_BLOCK_READ_BYTES`, `HeaderEntry(name, value)`, `header_mode_error(value) -> str | None`, `header_block(data, *, truncated) -> bytes | None`, `entries_from_message(msg) -> list[HeaderEntry]`, `parse_header_block(block) -> list[HeaderEntry]`, `group_entries(entries) -> dict[str, list[str]]`, `entries_to_wire(entries) -> list[dict[str, str]]`.

- [ ] **Step 1: Write the failing test**

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The header block: where it ends, what occurrences it holds, how they group."""
import pytest

from localmail.header_block import (
    HEADER_BLOCK_READ_BYTES,
    HEADER_MODES,
    HeaderEntry,
    entries_to_wire,
    group_entries,
    header_block,
    header_mode_error,
    parse_header_block,
)

# Two Received headers and a case-variant third, interleaved with other names —
# the shape `headers=full` cannot express (it groups by name, and `received`
# lands under a different key than `Received`).
_INTERLEAVED = (
    b"Received: by 10.0.0.1 with SMTP id aaa; Tue, 15 Sep 2026 19:18:28 -0700\r\n"
    b"Authentication-Results: mx.example; dkim=pass\r\n"
    b"received: from relay.example (relay.example [10.0.0.2])\r\n"
    b"Subject: hello\r\n"
    b"Received: from origin.example (origin.example [10.0.0.3])\r\n"
)


def test_block_ends_at_the_first_crlf_blank_line():
    assert header_block(_INTERLEAVED + b"\r\n body", truncated=False) == _INTERLEAVED


def test_block_ends_at_a_bare_lf_blank_line():
    raw = b"From: a@x\n\nbody\n"
    assert header_block(raw, truncated=False) == b"From: a@x\n"


def test_a_message_with_no_body_is_all_header_block():
    raw = b"From: a@x\r\nSubject: s\r\n"
    assert header_block(raw, truncated=False) == raw


def test_a_truncated_prefix_with_no_separator_reports_it_rather_than_guessing():
    """None is the caller's signal to re-read in full.

    Returning the prefix would hand back a header list that looks complete and
    is not — the defect this mode exists to end.
    """
    assert header_block(b"From: a@x\r\nSubject: s\r\n", truncated=True) is None


def test_every_occurrence_is_kept_in_wire_order_with_its_own_spelling():
    entries = parse_header_block(_INTERLEAVED)
    assert [(e.name, e.value.split(";")[0].split(" (")[0]) for e in entries] == [
        ("Received", "by 10.0.0.1 with SMTP id aaa"),
        ("Authentication-Results", "mx.example"),
        ("received", "from relay.example"),
        ("Subject", "hello"),
        ("Received", "from origin.example"),
    ]


def test_grouping_the_entries_is_the_full_shape():
    grouped = group_entries(parse_header_block(_INTERLEAVED))
    assert list(grouped) == ["Received", "Authentication-Results", "received", "Subject"]
    assert len(grouped["Received"]) == 2
    assert len(grouped["received"]) == 1
    assert grouped["Subject"] == ["hello"]


def test_the_wire_form_is_one_object_per_occurrence():
    assert entries_to_wire([HeaderEntry(name="To", value="a@x")]) == [
        {"name": "To", "value": "a@x"}
    ]


def test_a_nul_in_a_value_never_reaches_either_projection():
    """Postgres TEXT and JSONB both reject \\x00; the one rule is pgtext."""
    entries = parse_header_block(b"Subject: he\x00llo\r\n")
    assert entries[0].value == "hello"
    assert group_entries(entries)["Subject"] == ["hello"]


def test_a_folded_value_is_unfolded_like_the_parser_does():
    entries = parse_header_block(b"X-Long:\r\n\tCIP:10.0.0.1;CTRY:US\r\n")
    assert entries[0].value == "CIP:10.0.0.1;CTRY:US"


@pytest.mark.parametrize("mode", HEADER_MODES)
def test_every_declared_mode_is_accepted(mode):
    assert header_mode_error(mode) is None


@pytest.mark.parametrize("value", ["", "FULL", "list ", "xyzzy", "true"])
def test_an_unknown_mode_is_refused_with_the_accepted_values_named(value):
    problem = header_mode_error(value)
    assert problem is not None
    for mode in HEADER_MODES:
        assert mode in problem
    assert repr(value) in problem


def test_the_read_ceiling_clears_the_largest_header_block_measured():
    """p95 is 8.7 KB and the largest of 2,746 sampled was 24 KB (#379)."""
    assert HEADER_BLOCK_READ_BYTES >= 32 * 1024
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_header_block.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'localmail.header_block'`

- [ ] **Step 3: Write minimal implementation**

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""One message's header block: its occurrences, in wire order (pure, no IO).

`api/messages.py` serves these and `parser.py` stores their grouping, so the
rule lives beside neither. `full` is `group_entries` of the same sequence
`list` emits, which is what keeps the two modes from disagreeing.
"""
from __future__ import annotations

import email
import email.policy
from collections.abc import Iterable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Literal

from localmail.pgtext import strip_nuls

HeaderMode = Literal["compact", "full", "list"]
HEADER_MODES: tuple[HeaderMode, ...] = ("compact", "full", "list")

# The block is read as a bounded prefix of `raw_bytes`, which carries the
# attachments too: p50 44 KB, p99 2.6 MB, max 35 MB on the live archive against
# a p95 header block of 8.7 KB. Measured for #379: 0 of 129,590 messages have a
# block that does not end within this ceiling.
HEADER_BLOCK_READ_BYTES = 64 * 1024

_SEPARATORS = (b"\r\n\r\n", b"\n\n")


@dataclass(frozen=True)
class HeaderEntry:
    """One occurrence: the name as spelled on the wire, and its value."""

    name: str
    value: str


def header_mode_error(value: str) -> str | None:
    """A message naming the accepted modes, or None when `value` is one."""
    if value in HEADER_MODES:
        return None
    accepted = ", ".join(repr(m) for m in HEADER_MODES)
    return f"headers must be one of {accepted}; got {value!r}"


def header_block(data: bytes, *, truncated: bool) -> bytes | None:
    """The bytes before the first blank line, or None if it has none.

    `truncated` says whether `data` was cut short by a read ceiling. Without a
    separator the answer differs: a complete message simply has no body (RFC
    5322 permits it), while a truncated one may have the rest of its headers
    past the cut — reporting None is what stops a short list passing for a
    complete one.
    """
    ends = [i for i in (data.find(sep) for sep in _SEPARATORS) if i >= 0]
    if ends:
        return data[: min(ends)]
    return None if truncated else data


def entries_from_message(msg: EmailMessage) -> list[HeaderEntry]:
    """Every occurrence, degrading only the ones the parser chokes on.

    `raw_items()` is the wire sequence unparsed, so each occurrence is parsed
    individually and a failing one falls back to its raw text rather than
    costing the other headers (#314).
    """
    out: list[HeaderEntry] = []
    for name, raw_value in msg.raw_items():
        try:
            value = str(msg.policy.header_fetch_parse(name, raw_value))
        except Exception:
            value = raw_value
        out.append(HeaderEntry(name=name, value=strip_nuls(value)))
    return out


def parse_header_block(block: bytes) -> list[HeaderEntry]:
    """Occurrences in `block`, under the policy `parse_message` reads with."""
    msg = email.message_from_bytes(
        block, _class=EmailMessage, policy=email.policy.default
    )
    return entries_from_message(msg)


def group_entries(entries: Iterable[HeaderEntry]) -> dict[str, list[str]]:
    """The `headers=full` shape: one key per wire spelling, values in order."""
    out: dict[str, list[str]] = {}
    for entry in entries:
        out.setdefault(entry.name, []).append(entry.value)
    return out


def entries_to_wire(entries: Iterable[HeaderEntry]) -> list[dict[str, str]]:
    """The `headers=list` payload."""
    return [{"name": e.name, "value": e.value} for e in entries]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_header_block.py`
Expected: PASS (14 tests). Then `uv run mypy src/localmail` → Success, 156 files.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/header_block.py tests/test_header_block.py
git commit -m "feat(headers): one pure rule for a message's header occurrences (#379)"
```

---

### Task 2: The parser is reimplemented on it

**Files:**
- Modify: `src/localmail/parser.py:118-134` (`_headers_dict`), and its import block
- Test: `tests/test_parser_headers_one_rule.py` (create)

**Interfaces:**
- Consumes: `header_block.entries_from_message`, `header_block.group_entries` from Task 1.
- Produces: no new names. `parser._headers_dict` keeps its exact signature `(msg: EmailMessage) -> dict[str, list[str]]` and its exact output.

- [ ] **Step 1: Write the failing test**

The point is that the stored column does not move while its implementation does. Capture today's output as the oracle: the test reimplements the *pre-change* body inline and requires the shipped one to agree on every fixture.

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""`_headers_dict` and the served header list are one rule, not two.

The stored column and `?headers=full` must stay byte-identical for freshly
parsed mail, which they only do if `parse_message` and `api.messages` read the
occurrences through the same function (#379).
"""
import email
import email.policy
from email.message import EmailMessage

import pytest

from localmail import parser
from localmail.header_block import entries_from_message, group_entries
from tests import _eml


def _pre_change_headers_dict(msg: EmailMessage) -> dict[str, list[str]]:
    """The implementation this task replaces, kept as the oracle."""
    out: dict[str, list[str]] = {}
    for name, raw_value in msg.raw_items():
        try:
            value = str(msg.policy.header_fetch_parse(name, raw_value))
        except Exception:
            value = raw_value
        out.setdefault(name, []).append(value)
    return out


_FIXTURES = [
    _eml.plain(),
    _eml.multipart_alt(),
    _eml.with_attachment(),
    _eml.utf8_subject(),
    _eml.no_message_id(),
    _eml.degenerate_message_id("body"),
    _eml.two_attachments_same_name(),
    _eml.html_with_inline_image(),
]


@pytest.mark.parametrize("raw", _FIXTURES, ids=range(len(_FIXTURES)))
def test_the_grouping_is_what_the_parser_used_to_build(raw: bytes) -> None:
    msg = email.message_from_bytes(
        raw, _class=EmailMessage, policy=email.policy.default
    )
    assert parser._headers_dict(msg) == _pre_change_headers_dict(msg)


@pytest.mark.parametrize("raw", _FIXTURES, ids=range(len(_FIXTURES)))
def test_the_served_list_groups_back_to_the_stored_shape(raw: bytes) -> None:
    """The refinement invariant, on the objects both modes are built from."""
    parsed = parser.parse_message(raw)
    msg = email.message_from_bytes(
        raw, _class=EmailMessage, policy=email.policy.default
    )
    assert group_entries(entries_from_message(msg)) == parsed.headers


class _SubjectParseRaises(email.policy.EmailPolicy):
    """A policy whose `Subject` parse raises, as older stdlibs really do.

    The seam `tests/test_parser.py::_MessageIdParseRaises` already uses, for
    the same reason: on CPython 3.13 no real input provokes the failure, so a
    fixture-driven test would silently stop exercising the guard on exactly
    the interpreter that still needs it. It must be a SUBCLASS — `EmailPolicy`
    is immutable, so `monkeypatch.setattr(msg.policy, ...)` raises
    `AttributeError` out of `email/_policybase.py`.
    """

    def header_fetch_parse(self, name, value):  # type: ignore[no-untyped-def]
        if name.lower() == "subject":
            raise IndexError("list index out of range")
        return super().header_fetch_parse(name, value)


def test_a_header_the_stdlib_cannot_parse_degrades_alone() -> None:
    """#314: the guard lives in `entries_from_message` now, and still holds.

    Driven through `entries_from_message`, not `parse_message`:
    `tests/test_parser.py` already pins the guard on the `parse_message` path,
    and the read path Task 3 adds reaches this catch through
    `parse_header_block` without ever calling `parse_message`.
    """
    msg = email.message_from_bytes(
        b"From: a@x\r\nSubject: s\r\n\r\nbody",
        _class=EmailMessage,
        policy=_SubjectParseRaises(),
    )
    out = group_entries(entries_from_message(msg))
    assert out["Subject"] == [" s"]
    assert out["From"] == ["a@x"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_parser_headers_one_rule.py`
Expected: FAIL on `test_the_served_list_groups_back_to_the_stored_shape` —
`ImportError` is wrong here; `header_block` exists from Task 1, so the real
expected failure is `AttributeError`/mismatch only if Task 1 is missing. If all
tests pass at this point, `_headers_dict` has not yet been rewritten and the
first two tests are tautological against the oracle — that is expected; the
value of this file is that it must keep passing through Step 3.

**Note for the implementer:** this is a refactor-under-test task, so the test is
written green against the oracle and must stay green after the rewrite. Do not
"make it fail" by breaking the parser.

- [ ] **Step 3: Write minimal implementation**

Replace the body of `_headers_dict` (`src/localmail/parser.py:118-134`) with:

```python
def _headers_dict(msg: EmailMessage) -> dict[str, list[str]]:
    """Every header, grouped by wire spelling.

    The occurrences come from `header_block`, which `api.messages` also serves
    per occurrence — one rule, so the stored column and the wire cannot drift.
    """
    return group_entries(entries_from_message(msg))
```

and add to the imports at the top of `parser.py`:

```python
from localmail.header_block import entries_from_message, group_entries
```

`parse_message:240` keeps `strip_nuls_all` — it is now idempotent (the
occurrences are stripped one layer down), and removing it would put the NUL rule
for stored headers somewhere a reader of `parse_message` cannot see.

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_parser_headers_one_rule.py tests/test_parser.py`
Expected: PASS, including `test_headers_are_dict_of_lists_and_jsonable` and the `Message-Id: <>` degradation test.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/parser.py tests/test_parser_headers_one_rule.py
git commit -m "refactor(parser): _headers_dict is the grouping of the header occurrences (#379)"
```

---

### Task 3: `get_message` serves the mode

**Files:**
- Modify: `src/localmail/api/messages.py:14-96`
- Test: `tests/test_api_messages.py` (modify `_seed_msg` + the two header tests, add five)

**Interfaces:**
- Consumes: Task 1's `header_block`, `parse_header_block`, `group_entries`, `entries_to_wire`, `header_mode_error`, `HEADER_BLOCK_READ_BYTES`; existing `get_message_raw`, `ValidationFailed`, `NotFound`.
- Produces: `get_message(conn, message_id, *, allowed_account_ids, headers="compact", allow_external_images=False)`. `full_headers` is gone.

- [ ] **Step 1: Write the failing test**

First fix the fixture: `_seed_msg`'s `raw_bytes` and `headers` disagree today
(`b"From: anna\r\nSubject: hello\r\n\r\nhi"` against a JSONB naming
`anna@example.com`), and production never writes the flat-string shape the JSONB
uses. Change the two defaults in `tests/test_api_messages.py::_seed_msg` to:

```python
            raw_bytes=(
                b"Received: by 10.0.0.1 with SMTP id aaa\r\n"
                b"From: Anna <anna@example.com>\r\n"
                b"received: from relay.example\r\n"
                b"Subject: hello\r\n"
                b"Date: Mon, 4 Mar 2026 10:00:00 +0000\r\n"
                b"\r\nhi there"
            ),
            headers={"From": ["Anna <anna@example.com>"], "Subject": ["hello"]},
```

Then replace `test_get_message_returns_compact_headers` /
`test_get_message_full_headers_includes_all` and add the rest:

```python
def test_get_message_returns_compact_headers(db_conn: psycopg.Connection) -> None:
    mid = _seed_msg(db_conn)
    db_conn.commit()
    msg = get_message(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT, headers="compact")
    assert msg["id"] == str(mid)
    assert msg["subject"] == "hello"
    assert msg["from"]["address"] == "anna@example.com"
    assert msg["from"]["name"] == "Anna"
    assert msg["to"][0]["address"] == "horst@example.com"
    assert "<p>hi" in msg["body_html"]
    assert msg["body_text"] == "hi there"
    assert msg["account"]["name"] == "acct"
    assert msg["folders"][0]["name"] == "INBOX"
    assert "headers" not in msg


def test_full_headers_are_read_from_the_message_not_the_stored_column(
    db_conn: psycopg.Connection,
) -> None:
    """The column is a snapshot of the parser that ran at sync (#379).

    The fixture's JSONB deliberately omits `Date` and `Received`; a passthrough
    would return the column and miss them.
    """
    mid = _seed_msg(db_conn)
    db_conn.commit()
    msg = get_message(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT, headers="full")
    assert msg["headers"]["From"] == ["Anna <anna@example.com>"]
    assert msg["headers"]["Date"] == ["Mon, 4 Mar 2026 10:00:00 +0000"]
    assert msg["headers"]["Received"] == ["by 10.0.0.1 with SMTP id aaa"]


def test_list_keeps_every_occurrence_in_wire_order(db_conn: psycopg.Connection) -> None:
    mid = _seed_msg(db_conn)
    db_conn.commit()
    msg = get_message(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT, headers="list")
    assert [e["name"] for e in msg["headers"]] == [
        "Received", "From", "received", "Subject", "Date",
    ]
    assert msg["headers"][0]["value"] == "by 10.0.0.1 with SMTP id aaa"


def test_grouping_the_list_reproduces_full(db_conn: psycopg.Connection) -> None:
    """The refinement invariant, end to end through the accessor."""
    mid = _seed_msg(db_conn)
    db_conn.commit()
    listed = get_message(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT, headers="list")
    full = get_message(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT, headers="full")
    grouped: dict[str, list[str]] = {}
    for entry in listed["headers"]:
        grouped.setdefault(entry["name"], []).append(entry["value"])
    assert grouped == full["headers"]


def test_an_unusable_mode_is_refused_even_with_an_empty_acl(
    db_conn: psycopg.Connection,
) -> None:
    """A refusal must never be disguised as a 404 (slice A's ordering rule)."""
    mid = _seed_msg(db_conn)
    db_conn.commit()
    with pytest.raises(ValidationFailed) as excinfo:
        get_message(db_conn, mid, allowed_account_ids=[], headers="xyzzy")
    assert "'list'" in str(excinfo.value)


def test_a_header_block_past_the_ceiling_is_re_read_in_full(
    db_conn: psycopg.Connection, monkeypatch, caplog,
) -> None:
    """Never a short list that looks complete."""
    padding = b"".join(
        b"X-Pad-%d: %s\r\n" % (i, b"y" * 200) for i in range(40)
    )
    mid = _seed_msg(
        db_conn,
        raw_bytes=padding + b"Subject: beyond the ceiling\r\n\r\nbody",
    )
    db_conn.commit()
    monkeypatch.setattr("localmail.api.messages.HEADER_BLOCK_READ_BYTES", 512)
    with caplog.at_level(logging.WARNING, logger="localmail.api.messages"):
        msg = get_message(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT, headers="list")
    assert msg["headers"][-1] == {"name": "Subject", "value": "beyond the ceiling"}
    assert any(str(mid) in r.message % r.args for r in caplog.records)
```

Add `import logging` and `from localmail.api.errors import ValidationFailed` to
that file's imports if absent (it already imports `NotFound`).

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_api_messages.py`
Expected: FAIL — `TypeError: get_message() got an unexpected keyword argument 'headers'`

- [ ] **Step 3: Write minimal implementation**

In `src/localmail/api/messages.py`, add to the imports:

```python
import logging

from localmail.api.errors import NotFound, ValidationFailed
from localmail.header_block import (
    HEADER_BLOCK_READ_BYTES,
    HeaderEntry,
    entries_to_wire,
    group_entries,
    header_block,
    header_mode_error,
    parse_header_block,
)

logger = logging.getLogger("localmail.api.messages")
```

Change the signature and the two touched regions:

```python
def get_message(
    conn: psycopg.Connection,
    message_id: int,
    *,
    allowed_account_ids: list[int],
    headers: str = "compact",
    allow_external_images: bool = False,
) -> dict[str, Any]:
```

Immediately after the docstring, before the empty-ACL short-circuit:

```python
    mode_problem = header_mode_error(headers)
    if mode_problem is not None:
        raise ValidationFailed(mode_problem)
```

In the SELECT, replace `m.attachments, m.headers, m.date_sent,` with
`m.attachments, m.date_sent,` and append the prefix column, composed so the
compact path never reads it:

```python
        wants_headers = headers != "compact"
        header_col = (
            ", substring(m.raw_bytes from 1 for %(limit)s) AS header_prefix"
            if wants_headers else ""
        )
        cur.execute(
            f"""
            SELECT m.id, m.account_id, m.subject, m.from_addr, m.from_name,
                   m.to_addrs, m.cc_addrs, m.bcc_addrs, m.body_text, m.body_html,
                   m.attachments, m.date_sent,
                   a.name AS account_name, a.email_address AS account_address
                   {header_col}
              FROM messages m
              JOIN accounts a ON a.id = m.account_id
             WHERE m.id = %(mid)s AND m.account_id = ANY(%(accounts)s)
            """,  # noqa: S608 - header_col is a literal chosen by `wants_headers`
            {
                "mid": message_id,
                "accounts": allowed_account_ids,
                # +1 so a block that fills the ceiling is distinguishable from
                # one that ends exactly at it, without a second query.
                "limit": HEADER_BLOCK_READ_BYTES + 1,
            },
        )
```

Unpack the fixed columns and keep the optional one separate:

```python
    (mid, account_id, subject, from_addr, from_name,
     to_addrs, cc_addrs, bcc_addrs, body_text, body_html,
     attachments, date_sent,
     account_name, account_address) = row[:14]
```

Replace the `full_headers` block at the end with:

```python
    if wants_headers:
        entries = _header_entries(
            conn, message_id,
            allowed_account_ids=allowed_account_ids,
            prefix=bytes(row[14]),
        )
        msg["headers"] = (
            entries_to_wire(entries) if headers == "list" else group_entries(entries)
        )
    return msg
```

And add the helper below `get_message`:

```python
def _header_entries(
    conn: psycopg.Connection,
    message_id: int,
    *,
    allowed_account_ids: list[int],
    prefix: bytes,
) -> list[HeaderEntry]:
    """Occurrences from the prefix, re-reading in full if it was cut short."""
    block = header_block(prefix, truncated=len(prefix) > HEADER_BLOCK_READ_BYTES)
    if block is None:
        logger.warning(
            "header block of message %s exceeds %d bytes; re-reading in full",
            message_id, HEADER_BLOCK_READ_BYTES,
        )
        raw = get_message_raw(
            conn, message_id, allowed_account_ids=allowed_account_ids
        )
        whole = header_block(raw, truncated=False)
        block = b"" if whole is None else whole
    return parse_header_block(block)
```

**Note on the `noqa`:** the repo's ruff baseline is 10 findings (#285). If the
`S608` directive changes that count, drop the directive and re-check — the
interpolated value is one of two literals and never caller-controlled.

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_api_messages.py && uv run mypy src/localmail && uv run ruff check src/localmail/ | tail -1`
Expected: PASS; mypy Success; ruff **10**.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/messages.py tests/test_api_messages.py
git commit -m "feat(api): get_message serves compact/full/list from one parsed block (#379)"
```

---

### Task 4: The route

**Files:**
- Modify: `src/localmail/serve/routes/messages.py:62-79`
- Test: `tests/test_serve_messages_routes.py` (modify `_seed_msg` + one test, add three)

**Interfaces:**
- Consumes: Task 3's `get_message(..., headers=…)`.
- Produces: `GET /v1/messages/{id}?headers=compact|full|list`; any other value is 400 `/problems/validation-failed`.

- [ ] **Step 1: Write the failing test**

Give the fixture a real header block — its `raw_bytes` is the string `'RAW'`
today, which parses to no headers at all:

```python
               VALUES (%s, '<m@x>', 'hello', 'a@x', 'Anna', 'hi', '<p>hi</p>', '[]'::jsonb,
                       %s, %s, 3, %s::jsonb, %s, %s) RETURNING id""",
            (aid,
             b"Received: by 10.0.0.1\r\nFrom: a@x\r\nreceived: from relay\r\n\r\nhi",
             b"\x00" * 32, json.dumps({"From": ["a@x"]}),
             datetime(2026, 3, 4, tzinfo=timezone.utc), now),
```

(The literal `'RAW'` becomes the `%s` bytes parameter; `size_bytes` stays 3,
which nothing here asserts.)

```python
def test_get_message_full_headers(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    mid = _seed_msg(db_conn)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/messages/{mid}?headers=full",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert r.json()["headers"]["From"] == ["a@x"]


def test_get_message_headers_list_is_ordered_per_occurrence(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    mid = _seed_msg(db_conn)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/messages/{mid}?headers=list",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert [e["name"] for e in r.json()["headers"]] == ["Received", "From", "received"]


def test_the_default_mode_still_omits_headers(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    mid = _seed_msg(db_conn)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/messages/{mid}",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert "headers" not in r.json()


def test_an_unknown_headers_mode_is_problem_json_not_a_silent_compact(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    """It used to answer 200 with no headers key — a typo, and a new client's
    `list` against an old server, were indistinguishable from success (#379)."""
    mid = _seed_msg(db_conn)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/messages/{mid}?headers=xyzzy",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["type"] == "/problems/validation-failed"
    assert "'list'" in body["detail"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_messages_routes.py`
Expected: FAIL — `headers=list` returns 200 with no `headers` key; the unknown-mode test gets 200 instead of 400.

- [ ] **Step 3: Write minimal implementation**

In `src/localmail/serve/routes/messages.py::detail`, pass the mode through
instead of collapsing it to a bool:

```python
@router.get("/{message_id}")
def detail(
    message_id: str,
    request: Request,
    headers: str = Query("compact"),
    external_images: bool = Query(False),
    user=Depends(get_authenticated_user),
) -> dict[str, Any]:
    """One message. `headers` is `compact` (default, no headers key), `full`
    (an object keyed by wire spelling) or `list` (one entry per occurrence, in
    wire order); any other value is a 400.
    """
    mid = parse_int_id(message_id, field="message_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        return get_message(
            conn, mid,
            allowed_account_ids=allowed,
            headers=headers,
            allow_external_images=external_images,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_messages_routes.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/routes/messages.py tests/test_serve_messages_routes.py
git commit -m "feat(serve): ?headers=list, and an unknown mode is a 400 (#379)"
```

---

### Task 5: MCP takes the same vocabulary

**Files:**
- Modify: `src/localmail/mcp/tools.py:92-108`, `src/localmail/mcp/server.py:310-342`
- Test: `tests/test_mcp_server_build.py` (add two), `tests/test_mcp_tools.py` (add one)

**Interfaces:**
- Consumes: Task 3's `get_message(..., headers=…)`; Task 1's `HeaderMode`.
- Produces: `tool_get_message(conn, *, message_id, allowed_account_ids, headers="compact")`; the MCP tool parameter `headers`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_server_build.py
def test_get_message_publishes_the_three_header_modes(db_dsn):
    """The agent-facing schema is `server.py`'s, not `tools.py`'s (#308)."""
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        server = build_mcp_server(pool, searcher=None, config=McpConfig(enabled=True))
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
    finally:
        pool.close()
    props = (tools["get_message"].inputSchema or {})["properties"]
    assert "full_headers" not in props, "the removed flag never worked as described"
    assert set(props["headers"]["enum"]) == {"compact", "full", "list"}
    assert props["headers"].get("default") == "compact"


def test_get_message_tells_the_agent_when_order_matters(db_dsn):
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        server = build_mcp_server(pool, searcher=None, config=McpConfig(enabled=True))
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
    finally:
        pool.close()
    description = (tools["get_message"].inputSchema or {})["properties"]["headers"]["description"]
    assert "wire order" in description
    assert "Received" in description
```

```python
# tests/test_mcp_tools.py — beside the existing tool_get_message tests.
# `_insert_message` seeds raw_bytes='r', which has no header block, so this
# seeds its own message in the same style.
def test_tool_get_message_forwards_the_header_mode(db_conn):
    uid = create_user(db_conn, "carol-hdr", "hunter2")
    acct = _insert_account(db_conn, "carol-hdr-acct")
    grant_account(db_conn, uid, acct)
    raw = b"Received: by 10.0.0.1\r\nFrom: a@x\r\nreceived: from relay\r\n\r\nbody"
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages"
            "  (account_id, message_id, raw_sha256, subject, body_text,"
            "   headers, raw_bytes, size_bytes)"
            " VALUES (%s, '<hdr-mcp@x>', %s, 'hi', 'body', '{}'::jsonb, %s, %s)"
            " RETURNING id",
            (acct, b"\x5a" * 32, raw, len(raw)),
        )
        row = cur.fetchone(); assert row is not None
        mid = int(row[0])
    db_conn.commit()
    msg = tools.tool_get_message(
        db_conn, message_id=mid,
        allowed_account_ids=allowed_account_ids(db_conn, uid),
        headers="list",
    )
    assert [e["name"] for e in msg["headers"]] == ["Received", "From", "received"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_mcp_server_build.py tests/test_mcp_tools.py`
Expected: FAIL — `KeyError: 'headers'` (the schema still publishes `full_headers`).

- [ ] **Step 3: Write minimal implementation**

`src/localmail/mcp/tools.py`:

```python
def tool_get_message(
    conn: psycopg.Connection,
    *,
    message_id: int,
    allowed_account_ids: list[int],
    headers: str = "compact",
) -> dict[str, Any]:
    """One message (headers, body, attachment list), ACL-scoped.

    Raises localmail.api.errors.NotFound when the message is absent OR the
    caller lacks a grant on its account (indistinguishable by design).
    """
    return api_get_message(
        conn, message_id,
        allowed_account_ids=allowed_account_ids,
        headers=headers,
    )
```

`src/localmail/mcp/server.py` — replace the `full_headers` parameter and the
forwarding:

```python
        headers: Annotated[HeaderMode, Field(description=(
            "\"compact\" (default) omits headers; \"full\" returns an object "
            "keyed by header name, each value that name's occurrences; "
            "\"list\" returns one {name, value} entry per occurrence in wire "
            "order — use it when order matters, e.g. a Received chain or "
            "Authentication-Results."))] = "compact",
```

```python
                return tools.tool_get_message(
                    conn,
                    message_id=mid,
                    allowed_account_ids=allowed,
                    headers=headers,
                )
```

with `from localmail.header_block import HeaderMode` added to that module's imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_mcp_server_build.py tests/test_mcp_tools.py tests/test_mcp_tool_descriptions.py`
Expected: PASS, including the generic "every parameter is documented" test.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/tools.py src/localmail/mcp/server.py tests/test_mcp_server_build.py tests/test_mcp_tools.py
git commit -m "feat(mcp): get_message takes headers=compact|full|list (#379)"
```

---

### Task 6: `api_minor` 1, and the docs that announce it

**Files:**
- Modify: `src/localmail/serve/routes/version.py:17-18`, `README.md`, `docs/mcp-usage.md:269`, `CLAUDE.md`
- Test: `tests/test_serve_version_route.py` (add one)

**Interfaces:**
- Consumes: nothing.
- Produces: `API_MINOR = 1`, documented as the `headers=list` feature signal.

- [ ] **Step 1: Write the failing test**

```python
def test_api_minor_announces_the_headers_list_mode(db_dsn: str) -> None:
    """An old server cannot refuse `?headers=list`; it answers 200 with no
    headers key. `api_minor >= 1` is the only way a client can ask first."""
    body = _client(db_dsn).get("/v1/version").json()
    assert body["api_minor"] >= 1
```

`/v1/version` is unauthenticated, and `_client` is the helper this file already
defines — do not add a bearer token to the call.

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_version_route.py`
Expected: FAIL — `assert 0 >= 1`

- [ ] **Step 3: Write minimal implementation**

`src/localmail/serve/routes/version.py`:

```python
API_MAJOR = 1
# 1: GET /v1/messages/{id}?headers=list (#379). An older server answers an
# unknown mode with 200 and no headers key, so this is the only feature signal.
API_MINOR = 1
```

README — document the endpoint's modes beside the existing `/v1/messages`
prose, and state the signal:

```markdown
`GET /v1/messages/{id}` takes `headers=compact|full|list`. `compact` (the
default) omits the `headers` key; `full` returns an object keyed by the header
name as spelled on the wire, each value that name's occurrences; `list` returns
one `{name, value}` entry per occurrence **in wire order**, which is what a
`Received` chain or an `Authentication-Results` sequence needs — an object
cannot express order across names, and splits `Received` from `received`. Any
other value is a 400. Grouping `list` by name reproduces `full` exactly.

`headers=list` is announced by `api_minor >= 1` on `GET /v1/version`: an older
server answers the unknown mode with 200 and no `headers` key.
```

`docs/mcp-usage.md:269` — replace the `get_message` row's parameter list:

```markdown
| `get_message` | `message_id`, `headers="compact"\|"full"\|"list"` | Fetch one message's headers, body, and attachment list once search/browse has surfaced its ID. `headers="list"` gives one `{name, value}` entry per occurrence in wire order — use it for `Received` chains and `Authentication-Results`. Each attachment entry carries `filename`, `sha256`, `content_type` (stored MIME type), and `size` (decoded bytes) — enough to decide text-vs-original retrieval and size a download before fetching. |
```

CLAUDE.md — add under the GUI-server/API section, in the house voice:

```markdown
- **Message headers are served per occurrence, in wire order (#379).** `?headers=full`
  emitted the `messages.headers` JSONB verbatim, which groups by wire spelling:
  order across names is lost (89.1% of messages carry a repeated name), case
  variants split into separate keys (5.1%), and the values are frozen at sync
  time (11.6% disagree with a fresh parse — whitespace only, all of it). The
  rule is the pure [src/localmail/header_block.py](src/localmail/header_block.py):
  one sequence of `HeaderEntry`, of which `full` is `group_entries(...)` and
  `list` is `entries_to_wire(...)`, so **the two modes cannot disagree** —
  `parser._headers_dict` is that grouping too, which is what keeps the stored
  column and the wire on one rule. Read as a bounded `raw_bytes` prefix
  (`HEADER_BLOCK_READ_BYTES`, 64 KiB; 0 of 129,590 live messages need more,
  p95 is 8.7 KB) because that column carries the attachments (p99 2.6 MB, max
  35 MB); a block that does not end inside it is **re-read in full with a
  WARNING**, never silently truncated. An unknown mode is a **400** naming the
  three (it used to be a silent compact, indistinguishable from an old server
  ignoring `list`), refused ahead of the empty-ACL short-circuit so it is never
  disguised as a 404. **`api_minor` is 1** — its first move — because an old
  server cannot refuse the new mode.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_serve_version_route.py tests/test_serve_app_baseline.py`
Expected: PASS (the baseline's `api_minor >= 0` is unaffected).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/routes/version.py tests/test_serve_version_route.py README.md docs/mcp-usage.md CLAUDE.md
git commit -m "feat(serve): api_minor 1 announces headers=list, and document it (#379)"
```

---

### Task 7: Acceptance, and the full gates

**Files:**
- Test: `tests/test_headers_list_acceptance.py` (create)

**Interfaces:**
- Consumes: everything above.
- Produces: nothing importable; this is the handoff's stated acceptance.

- [ ] **Step 1: Write the failing test**

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The acceptance stated for slice B: every occurrence, in order, and `full`
unchanged against a golden response (#379)."""
import json
from datetime import datetime, timezone

import psycopg
from fastapi.testclient import TestClient

from localmail.serve.app import create_app

_RAW = (
    b"Received: from a.example (a.example [10.0.0.1]) by mx.example; Tue, 15 Sep 2026 19:18:28 -0700\r\n"
    b"Authentication-Results: mx.example; dkim=pass header.d=a.example\r\n"
    b"received: from b.example (b.example [10.0.0.2]) by a.example; Tue, 15 Sep 2026 19:18:20 -0700\r\n"
    b"From: Anna <anna@example.com>\r\n"
    b"Subject: two hops and a case variant\r\n"
    b"\r\n"
    b"body\r\n"
)

_GOLDEN_FULL = {
    "Received": [
        "from a.example (a.example [10.0.0.1]) by mx.example; Tue, 15 Sep 2026 19:18:28 -0700",
    ],
    "Authentication-Results": ["mx.example; dkim=pass header.d=a.example"],
    "received": [
        "from b.example (b.example [10.0.0.2]) by a.example; Tue, 15 Sep 2026 19:18:20 -0700",
    ],
    "From": ["Anna <anna@example.com>"],
    "Subject": ["two hops and a case variant"],
}


def _seed(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('acc','x@y.test','imap.x','password') RETURNING id"
        )
        row = cur.fetchone(); assert row is not None
        aid = row[0]
        cur.execute(
            "INSERT INTO mailboxes (account_id, name) VALUES (%s,'INBOX') RETURNING id",
            (aid,),
        )
        row = cur.fetchone(); assert row is not None
        mb = row[0]
        now = datetime.now(timezone.utc)
        cur.execute(
            """INSERT INTO messages (account_id, message_id, subject, from_addr,
                                     body_text, attachments, raw_bytes, raw_sha256,
                                     size_bytes, headers, date_sent, date_received)
               VALUES (%s,'<m@x>','two hops and a case variant','anna@example.com',
                       'body','[]'::jsonb,%s,%s,%s,%s::jsonb,%s,%s) RETURNING id""",
            (aid, _RAW, b"\x01" * 32, len(_RAW), json.dumps({}),
             datetime(2026, 9, 15, tzinfo=timezone.utc), now),
        )
        row = cur.fetchone(); assert row is not None
        mid = row[0]
        cur.execute(
            "INSERT INTO message_labels (message_id, mailbox_id, uid) VALUES (%s,%s,%s)",
            (mid, mb, mid),
        )
    conn.commit()
    return mid


def test_two_received_headers_and_a_case_variant_come_back_in_order(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    mid = _seed(db_conn)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/messages/{mid}?headers=list",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert [e["name"] for e in r.json()["headers"]] == [
        "Received", "Authentication-Results", "received", "From", "Subject",
    ]
    assert r.json()["headers"][0]["value"].startswith("from a.example")


def test_full_is_unchanged_against_the_golden_response(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    mid = _seed(db_conn)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/messages/{mid}?headers=full",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert r.json()["headers"] == _GOLDEN_FULL
```

`db_dsn`, `api_token`, `db_conn` and `grant_alice_all_accounts` are all
`tests/conftest.py` fixtures (lines 252, 361, 310, 388) — the same four
`tests/test_serve_messages_routes.py` takes.

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/test_headers_list_acceptance.py`
Expected: PASS if Tasks 1–5 landed. If it fails, the failure is the bug — fix it before continuing.

- [ ] **Step 3: Run every gate**

```bash
unset VIRTUAL_ENV && uv run pytest -q                       # expect > 3682 passed, 2 warnings
unset VIRTUAL_ENV && uv run mypy src/localmail              # Success, 156 files
unset VIRTUAL_ENV && uv run ruff check src/localmail/ | tail -1   # 10
```

No `gui/` gate is needed: no file under `gui/` changes. Confirm with
`git diff --name-only main... | grep '^gui/' || echo "no gui changes"`.

- [ ] **Step 4: Mutation-check the two claims that carry this slice**

Each must fail a test; restore from a file copy, never `git checkout` (it would
discard uncommitted work in the same file).

```bash
cp src/localmail/api/messages.py /tmp/messages.py.bak
# 1. `list` silently answers the `full` shape
python3 - <<'PY'
import pathlib
p = pathlib.Path("src/localmail/api/messages.py"); s = p.read_text()
old = 'entries_to_wire(entries) if headers == "list" else group_entries(entries)'
assert s.count(old) == 1
p.write_text(s.replace(old, "group_entries(entries)"))
PY
unset VIRTUAL_ENV && uv run pytest -q tests/test_headers_list_acceptance.py  # must FAIL
cp /tmp/messages.py.bak src/localmail/api/messages.py

# 2. a truncated block is served as if complete
cp src/localmail/header_block.py /tmp/header_block.py.bak
python3 - <<'PY'
import pathlib
p = pathlib.Path("src/localmail/header_block.py"); s = p.read_text()
old = "return None if truncated else data"
assert s.count(old) == 1
p.write_text(s.replace(old, "return data"))
PY
unset VIRTUAL_ENV && uv run pytest -q tests/test_header_block.py tests/test_api_messages.py  # must FAIL
cp /tmp/header_block.py.bak src/localmail/header_block.py
unset VIRTUAL_ENV && uv run pytest -q tests/test_header_block.py tests/test_api_messages.py  # green again
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_headers_list_acceptance.py
git commit -m "test(headers): the slice B acceptance — order kept, full unchanged (#379)"
```
