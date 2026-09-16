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
