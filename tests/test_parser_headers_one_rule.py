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
    """A policy whose `Subject` parse raises, like older stdlibs can for any header.

    CPython's structured-header parser is **not total**. On 3.12.3 various
    malformed headers can raise `IndexError`, `AttributeError`, or
    `HeaderParseError`. 3.13 guards some cases but not all, so on a current
    interpreter edge cases may still exist -- and a fixture-driven test would
    silently stop exercising the guard on the interpreter that still needs it.
    Raising at the policy seam is where the stdlib itself fails, so the pin
    holds on every interpreter.
    """

    def header_fetch_parse(self, name, value):  # type: ignore[no-untyped-def]
        if name.lower() == "subject":
            raise IndexError("stdlib cannot parse this one")
        return super().header_fetch_parse(name, value)


def test_a_header_the_stdlib_cannot_parse_degrades_alone(monkeypatch) -> None:
    """#314: the guard lives in `entries_from_message` now, and still holds.

    Raised at the policy seam rather than from a fixture: on CPython 3.13 no
    real input provokes it, so a fixture-driven test would silently stop
    exercising the guard on the interpreter that still needs it.
    """
    monkeypatch.setattr(email.policy, "default", _SubjectParseRaises())
    msg = email.message_from_bytes(
        b"From: a@x\r\nSubject: s\r\n\r\nbody",
        _class=EmailMessage,
        policy=email.policy.default,
    )
    out = group_entries(entries_from_message(msg))
    assert out["Subject"] == ["s"]
    assert out["From"] == ["a@x"]
