# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The header block: where it ends, what occurrences it holds, how they group."""
from typing import get_args

import pytest

from localmail.header_block import (
    HEADER_BLOCK_READ_BYTES,
    HEADER_MODES,
    PREFIX_READ_BYTES,
    HeaderEntry,
    HeaderMode,
    entries_to_wire,
    group_entries,
    header_block,
    header_block_of_whole,
    header_mode_error,
    parse_header_block,
    prefix_is_truncated,
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


def test_a_truncated_prefix_that_still_holds_the_separator_is_served_from_it():
    """The ordinary big message: cut short, but the block ended before the cut.

    This is every message over the ceiling — i.e. every attachment-bearing one
    — so it is the common path, not an edge. Its negative control is the test
    above: without this one, deciding on `truncated` alone and never searching
    passes the whole file, and every such message pays a full `raw_bytes`
    re-read under a WARNING that states the opposite of the truth.
    """
    assert header_block(_INTERLEAVED + b"\r\n body", truncated=True) == _INTERLEAVED


def test_a_whole_message_always_yields_a_block_even_with_no_separator():
    """`header_block_of_whole` is total, which is what the re-read path needs.

    An empty block would serve an empty header list — indistinguishable from a
    message that genuinely has none.
    """
    assert header_block_of_whole(_INTERLEAVED + b"\r\n body") == _INTERLEAVED
    assert header_block_of_whole(b"From: a@x\r\n") == b"From: a@x\r\n"
    assert header_block_of_whole(b"") == b""


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"From: a@x",
        b"From: a@x\r\n",
        b"From: a@x\rSubject: s\r\rbody",          # bare CR endings
        b"From: a@x\nSubject: s\n\r\nbody",        # LF header, CRLF blank line
        b"\r",
        b"\x00" * 8,
    ],
)
def test_an_untruncated_read_never_reports_none(raw):
    """What makes `header_block_of_whole` total, and the re-read branch-free.

    Pinned because the re-read path relies on it: were this to start returning
    None, a caller would have to invent a fallback, and the obvious one (b"")
    is the empty-list-looks-complete failure.
    """
    assert header_block(raw, truncated=False) is not None


def test_the_prefix_ceiling_and_the_read_length_stay_one_rule():
    """`PREFIX_READ_BYTES` is the ceiling plus one, and the judge agrees.

    Split apart, the SQL reads the ceiling exactly and a block that fills it
    reads as complete. The boundary is what matters, so it is asserted at the
    boundary rather than by restating the arithmetic.
    """
    assert PREFIX_READ_BYTES == HEADER_BLOCK_READ_BYTES + 1
    assert not prefix_is_truncated(b"x" * HEADER_BLOCK_READ_BYTES)
    assert prefix_is_truncated(b"x" * PREFIX_READ_BYTES)


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


def test_the_mode_vocabulary_is_derived_from_the_literal():
    """Hand-typing the tuple is what `HEADER_MODES`' comment forbids.

    The parametrised tests above read `HEADER_MODES`, so they move with it and
    cannot see it drift from the Literal that MCP's `Field` validates against.
    """
    assert HEADER_MODES == get_args(HeaderMode)


@pytest.mark.parametrize(
    "raw",
    [
        b"From: a@x\r\nSubject: s\r\n\r\nbody\r\n",      # CRLF
        b"From: a@x\nSubject: s\n\nbody\n",              # LF
        b"From: a@x\rSubject: s\r\rbody\r",              # bare CR — unrecognised
        b"From: a@x\nSubject: s\n\r\nbody",              # LF header, CRLF blank
        b"From: a@x\r\nSubject: s\r\n\nbody",            # CRLF header, LF blank
        b"From: a@x\r\nthis is body\r\nSubject: hidden\r\n",  # no separator
    ],
)
def test_the_block_scan_never_disagrees_with_the_stdlib_parse(raw):
    """Our scan may cut earlier than the stdlib; it must never cut differently.

    `_SEPARATORS` recognises only `\\r\\n\\r\\n` and `\\n\\n`, so odd spellings
    fall through to the whole message — which is safe exactly because
    `email.message_from_bytes` finds the real boundary itself. That safety is
    accidental and load-bearing, so it is pinned rather than assumed: no body
    line may ever surface as a header.
    """
    import email
    import email.policy
    from email.message import EmailMessage

    from localmail.header_block import entries_from_message

    whole = email.message_from_bytes(
        raw, _class=EmailMessage, policy=email.policy.default
    )
    assert parse_header_block(header_block_of_whole(raw)) == entries_from_message(whole)
