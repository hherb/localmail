"""`localmail search` composes its flags ahead of the free text (#374).

The CLI used to append its flag tokens after the free text, so an unclosed
``"`` in the query swallowed every flag into free text and the command exited
0 with unfiltered results. These pins are #367's for the HTTP composer: the
filters a composed query parses to do not depend on the free text, and the
free text survives unchanged.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from localmail.cli_search_query import compose_cli_query
from localmail.search.query import parse_query, quoted_filter_value

_FLAGS = dict(
    accounts=("gmail", "work mail"),
    folders=("INBOX", "a b"),
    after=datetime(2024, 1, 1),
    before=datetime(2024, 12, 31),
    from_substr="alice",
    to_substr="bob smith",
    subject_substr="invoice",
    has_attachment=True,
    label="Important",
)

_HOSTILE_TEXT = [
    "",
    "plain words",
    "O'Brien contract",
    'unclosed "quote here',
    '"',
    'from:"',
    "-acme OR beta",
    '"a phrase" don\'t',
]


@pytest.mark.parametrize("text", _HOSTILE_TEXT)
def test_the_flags_parse_the_same_whatever_the_free_text(text):
    composed = compose_cli_query(text, **_FLAGS)
    baseline = compose_cli_query("", **_FLAGS)
    assert parse_query(composed).filters == parse_query(baseline).filters


@pytest.mark.parametrize("text", _HOSTILE_TEXT)
def test_the_free_text_survives_composition(text):
    composed = compose_cli_query(text, **_FLAGS)
    assert parse_query(composed).free_text == parse_query(text).free_text


def test_every_flag_reaches_its_filter():
    f = parse_query(compose_cli_query('"unclosed', **_FLAGS)).filters
    assert f.account_names == ["gmail", "work mail"]
    assert f.folders == ["INBOX", "a b"]
    assert f.from_substr == "alice"
    assert f.to_substr == "bob smith"
    assert f.subject_substr == "invoice"
    assert f.label == "Important"
    assert f.has_attachment is True
    assert str(f.after) == "2024-01-01"
    assert str(f.before) == "2024-12-31"


def test_no_flags_is_the_free_text_alone():
    assert compose_cli_query("O'Brien", **_no_flags()) == "O'Brien"


@pytest.mark.parametrize(
    "flag,value",
    [("label", "foo bar"), ("label", "don't"), ("folders", ('a"b',)),
     ("from_substr", 'x" has:attachment "y')],
)
def test_a_flag_value_cannot_swallow_or_inject(flag, value):
    """A bad flag value must not eat the free text, nor add an operator."""
    kwargs = _no_flags()
    kwargs[flag] = value
    parsed = parse_query(compose_cli_query("contract", **kwargs))
    assert parsed.free_text == "contract"
    assert parsed.filters.has_attachment is None


@pytest.mark.parametrize("flag", ["from_substr", "to_substr", "subject_substr", "label"])
def test_a_value_that_is_only_quotes_is_refused(flag):
    kwargs = _no_flags()
    kwargs[flag] = '""'
    with pytest.raises(ValueError, match="empty once its quotes are removed"):
        compose_cli_query("x", **kwargs)


def test_quoted_filter_value_strips_quotes_and_newlines():
    assert quoted_filter_value('a"b\nc') == '"ab c"'
    assert quoted_filter_value('""') is None


def _no_flags() -> dict:
    return dict(accounts=(), folders=(), after=None, before=None,
                from_substr=None, to_substr=None, subject_substr=None,
                has_attachment=False, label=None)
