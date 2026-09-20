# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The one snippet-width rule, and that both layers really read it (#390).

Slice E stated the type check and the floor twice, in two wordings — one in
``api/search_projection.py`` for network callers and one in
``Searcher._snippet_width`` for library callers — so `"10"` earned a
different sentence for an identical mistake, and a future decision to accept
(say) an integral ``float`` could land at one layer and silently not at the
other. The differential tests at the bottom are what bind them now; they are
the ``ALLOWLISTED_WHERE_SQL`` ↔ ``is_allowlisted`` arrangement.
"""
from __future__ import annotations

import inspect

import pytest

from localmail.api.errors import ValidationFailed
from localmail.config import SearchConfig
from localmail.search.snippet_width import snippet_width_error

_CAP = 1000

#: Every value that is not an integer window, whatever the cap. ``bool`` is
#: here because it is an ``int`` subclass, so ``True`` would otherwise read
#: as a one-character window.
NON_INTEGERS: tuple[object, ...] = (True, False, 1.5, 5.0, "10", "", None, [], {}, object())


@pytest.mark.parametrize("value", [1, 2, _CAP // 2, _CAP])
def test_an_integer_inside_the_cap_is_accepted(value: int) -> None:
    assert snippet_width_error(value, max_chars=_CAP) is None


@pytest.mark.parametrize("value", [1, _CAP, _CAP + 1, 10**9])
def test_uncapped_accepts_any_positive_integer(value: int) -> None:
    """A library caller asking for a 5,000-character window is not doing
    anything wrong — the cap is an operator's bound for *network* callers."""
    assert snippet_width_error(value, max_chars=None) is None


@pytest.mark.parametrize("value", NON_INTEGERS)
@pytest.mark.parametrize("cap", [_CAP, None])
def test_a_non_integer_is_refused_under_either_cap(value: object, cap: int | None) -> None:
    msg = snippet_width_error(value, max_chars=cap)
    assert msg is not None
    assert "integer" in msg


@pytest.mark.parametrize("value", NON_INTEGERS)
def test_the_type_refusal_is_worded_identically_with_and_without_a_cap(value: object) -> None:
    """The type fault is the same fault at both layers, so it is the same
    sentence — the drift #390 was filed about."""
    assert snippet_width_error(value, max_chars=_CAP) == snippet_width_error(
        value, max_chars=None)


@pytest.mark.parametrize("value", [0, -1, -1000])
@pytest.mark.parametrize("cap", [_CAP, None])
def test_below_the_floor_is_refused_under_either_cap(value: int, cap: int | None) -> None:
    assert snippet_width_error(value, max_chars=cap) is not None


def test_the_capped_refusal_names_the_range() -> None:
    for value in (0, _CAP + 1):
        msg = snippet_width_error(value, max_chars=_CAP)
        assert msg is not None
        assert "1" in msg and str(_CAP) in msg, msg


def test_the_uncapped_floor_refusal_does_not_invent_a_ceiling() -> None:
    """An uncapped caller has no upper bound, so naming one would send them
    to a limit that does not apply to them."""
    msg = snippet_width_error(0, max_chars=None)
    assert msg is not None
    assert str(_CAP) not in msg


def test_a_value_over_the_cap_is_refused_only_when_a_cap_was_given() -> None:
    assert snippet_width_error(_CAP + 1, max_chars=_CAP) is not None
    assert snippet_width_error(_CAP + 1, max_chars=None) is None


def test_max_chars_is_keyword_only_with_no_default() -> None:
    """``None`` is the *permissive* reading, so it must be written at the
    call site rather than arrived at by forgetting the argument (#234's
    shape). Forgetting it is a ``TypeError``, not a silently uncapped
    network boundary."""
    param = inspect.signature(snippet_width_error).parameters["max_chars"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        snippet_width_error(5)  # type: ignore[call-arg]


# --- the two layers read this rule, and are not a second copy of it -------


def _run_search_with_searcher(searcher: object, *, snippet_chars: object) -> None:
    from localmail.api.search import run_search

    run_search(searcher=searcher, free_text="q", filters={}, limit=10,  # type: ignore[arg-type]
               allowed_account_ids=[1], user_id=1, snippet_chars=snippet_chars)


def _run_search_with(snippet_chars: object, *, cap: int) -> None:
    """Drive ``run_search``'s gate and nothing else.

    The searcher is a mock whose ``search`` raises, so a value that gets
    *past* the gate fails loudly rather than being reported as accepted.
    """
    from unittest.mock import MagicMock

    searcher = MagicMock()
    searcher.config = SearchConfig(snippet_max_chars=cap)
    searcher.search.side_effect = AssertionError("reached retrieval past the gate")
    _run_search_with_searcher(searcher, snippet_chars=snippet_chars)


@pytest.mark.parametrize("value", [*NON_INTEGERS, 0, -1, _CAP + 1])
def test_the_api_boundary_refuses_with_the_rules_own_wording(value: object) -> None:
    """Differential: the network gate raises iff the *capped* rule reports a
    message, and raises exactly that message.

    ``None`` is the one value the gate is allowed to disagree about — it
    means "unstated", which never reaches the rule.
    """
    expected = snippet_width_error(value, max_chars=_CAP)
    if value is None:
        assert expected is not None  # the rule refuses it; the gate skips it
        return
    assert expected is not None
    with pytest.raises(ValidationFailed) as excinfo:
        _run_search_with(value, cap=_CAP)
    assert str(excinfo.value) == expected


@pytest.mark.parametrize("value", [1, _CAP // 2, _CAP])
def test_the_api_boundary_admits_exactly_what_the_capped_rule_admits(value: int) -> None:
    assert snippet_width_error(value, max_chars=_CAP) is None
    with pytest.raises(AssertionError, match="reached retrieval past the gate"):
        _run_search_with(value, cap=_CAP)


@pytest.mark.parametrize("value", [*NON_INTEGERS, 0, -1])
def test_the_searcher_refuses_exactly_what_the_uncapped_rule_refuses(value: object) -> None:
    """Differential: ``Searcher._snippet_width`` raises iff the uncapped
    rule reports a message, and raises *that* message.

    ``None`` is the one value both layers are allowed to disagree about,
    and they disagree for the same reason: it means "unstated" at the call
    site — the configured default here, a skipped gate at the api boundary
    — so it never reaches the rule.
    """
    from localmail.search.searcher import Searcher

    expected = snippet_width_error(value, max_chars=None)
    assert expected is not None
    searcher = Searcher.__new__(Searcher)
    if value is None:
        return
    with pytest.raises(ValueError) as excinfo:
        searcher._snippet_width(value)  # type: ignore[arg-type]
    assert str(excinfo.value) == expected


def test_an_unstated_width_is_the_configured_default_at_both_layers() -> None:
    """``None`` never reaches the rule: the Searcher answers with its
    config, and the api gate skips the check entirely."""
    from unittest.mock import MagicMock

    from localmail.search.searcher import Searcher

    searcher = Searcher.__new__(Searcher)
    searcher._cfg = SearchConfig(snippet_width_chars=123)  # type: ignore[attr-defined]
    assert searcher._snippet_width(None) == 123

    gated = MagicMock()
    gated.config = SearchConfig(snippet_max_chars=_CAP)
    gated.search.side_effect = AssertionError("reached retrieval past the gate")
    with pytest.raises(AssertionError, match="reached retrieval past the gate"):
        _run_search_with_searcher(gated, snippet_chars=None)


@pytest.mark.parametrize("value", [1, 2, 10**6])
def test_the_searcher_accepts_exactly_what_the_uncapped_rule_accepts(value: int) -> None:
    from localmail.search.searcher import Searcher

    assert snippet_width_error(value, max_chars=None) is None
    searcher = Searcher.__new__(Searcher)
    assert searcher._snippet_width(value) == value


def test_neither_layer_restates_the_rule_in_its_own_source() -> None:
    """The fault #390 names is a hand-written second copy, so read the
    sources: neither may test the type or the floor itself.

    Structural because the differential tests above cannot see a *third*
    copy added beside the delegation — they only prove the verdicts agree
    today, which a duplicated-but-still-correct copy also satisfies.
    """
    import localmail.api.search_projection as projection
    import localmail.search.searcher as searcher_mod

    for module in (projection, searcher_mod):
        source = inspect.getsource(module)
        assert "must be a positive integer (got" not in source, module.__name__
        assert "snippet_chars must be an integer" not in source, module.__name__
