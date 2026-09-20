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
from localmail.search.snippet_width import MIN_WIDTH, snippet_width_error
from tests._snippet_width_rules import (
    CONSUMER_MODULES,
    snippet_width_duplication_error,
)

_CAP = 1000

#: Every value that is not an integer window, whatever the cap. ``bool`` is
#: here because it is an ``int`` subclass, so ``True`` would otherwise read
#: as a one-character window.
NON_INTEGERS: tuple[object, ...] = (True, False, 1.5, 5.0, "10", "", None, [], {}, object())

#: The same set minus ``None``, for the two layer-differentials below.
#: ``None`` means *unstated* at a call site — the configured default in the
#: Searcher, a skipped gate at the api boundary — so it never reaches the
#: rule and neither layer refuses it. Carrying it there produced a row that
#: read "the layer refuses None", which is the opposite of the truth, and
#: asserted only what ``test_a_non_integer_is_refused_under_either_cap``
#: already does. The real ``None`` behaviour is
#: ``test_an_unstated_width_is_the_configured_default_at_both_layers``.
STATED_NON_INTEGERS: tuple[object, ...] = tuple(
    v for v in NON_INTEGERS if v is not None)


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


def test_the_capped_wording_is_the_one_the_wire_has_always_carried() -> None:
    """#390's whole claim is that only the *Searcher's* wording moved.

    Literal, because that is the claim. The route-level refusals assert
    loose needles (`"snippet_chars"`, `"1000"`), all of which survive any
    rewording — so before this test, rewriting the sentence a network
    caller reads left 154 tests passing. The repo's own convention for a
    sentence that is on the wire is to assert it whole
    (`test_the_keyset_branch_keeps_naming_the_cursor`).
    """
    assert snippet_width_error(0, max_chars=_CAP) == (
        "snippet_chars must be between 1 and 1000 (got 0)")
    assert snippet_width_error(_CAP + 1, max_chars=_CAP) == (
        "snippet_chars must be between 1 and 1000 (got 1001)")
    assert snippet_width_error("5", max_chars=_CAP) == (
        "snippet_chars must be an integer (got '5')")


def test_the_capped_refusal_names_both_ends_of_the_range() -> None:
    """The floor and the ceiling, separately.

    This asserted `"1" in msg and str(_CAP) in msg`, and `"1000"` contains
    `"1"` — so the first conjunct was implied by the second and the test
    did not check what its name said. Dropping the floor from the sentence
    entirely (`"must be at most 1000"`) passed it.
    """
    for value in (0, _CAP + 1):
        msg = snippet_width_error(value, max_chars=_CAP)
        assert msg is not None
        assert str(MIN_WIDTH) in msg.split(" and ")[0], msg
        assert str(_CAP) in msg.split(" and ")[1], msg


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


@pytest.mark.parametrize("value", [*STATED_NON_INTEGERS, 0, -1, _CAP + 1])
def test_the_api_boundary_refuses_with_the_rules_own_wording(value: object) -> None:
    """Differential: the network gate raises iff the *capped* rule reports a
    message, and raises exactly that message.

    ``None`` is excluded — see ``STATED_NON_INTEGERS``.
    """
    expected = snippet_width_error(value, max_chars=_CAP)
    assert expected is not None
    with pytest.raises(ValidationFailed) as excinfo:
        _run_search_with(value, cap=_CAP)
    assert str(excinfo.value) == expected


@pytest.mark.parametrize("value", [1, _CAP // 2, _CAP])
def test_the_api_boundary_admits_exactly_what_the_capped_rule_admits(value: int) -> None:
    assert snippet_width_error(value, max_chars=_CAP) is None
    with pytest.raises(AssertionError, match="reached retrieval past the gate"):
        _run_search_with(value, cap=_CAP)


@pytest.mark.parametrize("value", [*STATED_NON_INTEGERS, 0, -1])
def test_the_searcher_refuses_exactly_what_the_uncapped_rule_refuses(value: object) -> None:
    """Differential: ``Searcher._snippet_width`` raises iff the uncapped
    rule reports a message, and raises *that* message.

    ``None`` is excluded — see ``STATED_NON_INTEGERS``.
    """
    from localmail.search.searcher import Searcher

    expected = snippet_width_error(value, max_chars=None)
    assert expected is not None
    searcher = Searcher.__new__(Searcher)
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


def _consumer_sources() -> dict[str, str]:
    import importlib

    return {name: inspect.getsource(importlib.import_module(name))
            for name in CONSUMER_MODULES}


def test_no_consumer_restates_the_rule_in_its_own_source() -> None:
    """No consumer may test the type or the floor itself.

    Structural because the differential tests above cannot see a *third*
    copy added beside the delegation — they only prove the verdicts agree
    today, which a duplicated-but-still-correct copy also satisfies.

    This was a grep for two message literals across two modules, and it had
    two holes, each found by mutation rather than by reading. It scanned
    `api/search_projection.py` — which #390 moved the rule *out of* — and
    not `api/search.py`, which now holds the capped gate, so an
    identically-worded copy at the real boundary passed it. And its
    forbidden set held the two *type* wordings and neither **floor**
    wording, so a verbatim third copy of the floor rule passed it too.
    `_snippet_width_rules` reads the AST instead: whatever a copy is
    worded, it has to ask `isinstance` or compare against a number.
    """
    assert snippet_width_duplication_error(_consumer_sources()) is None


def test_the_rule_reports_a_consumer_it_was_not_given() -> None:
    """A rename that shrinks the scanned set must fail, not pass quietly —
    that is exactly how the predecessor came to read the one module which
    could no longer hold a copy."""
    sources = _consumer_sources()
    dropped = sources.pop("localmail.api.search")
    problem = snippet_width_duplication_error(sources)
    assert problem is not None and "localmail.api.search" in problem
    sources["localmail.api.search"] = dropped
    assert snippet_width_duplication_error(sources) is None


@pytest.mark.parametrize("copy", [
    # The floor, which the predecessor's forbidden set did not cover.
    "def f(snippet_chars):\n    if snippet_chars < 1:\n        raise ValueError('x')\n",
    # The type check, however it is worded.
    "def f(snippet_chars):\n    if isinstance(snippet_chars, bool):\n        raise ValueError('x')\n",
    # A cap read off config rather than a bare name.
    "def f(cfg, n):\n    if n > cfg.snippet_max_chars:\n        raise ValueError('x')\n",
])
def test_a_hand_written_copy_is_reported_wherever_it_is_spelled(copy: str) -> None:
    sources = _consumer_sources()
    sources["localmail.api.search"] += "\n" + copy
    problem = snippet_width_duplication_error(sources)
    assert problem is not None and "localmail.api.search" in problem


def test_the_spellings_the_gates_actually_use_are_not_reported() -> None:
    """Positive control: a rule that fires on `is not None` (how both gates
    spell "unstated") or on the delegation itself would fail every
    consumer, and the assertion above would still read as passing."""
    sources = _consumer_sources()
    sources["localmail.api.search"] += (
        "\ndef f(snippet_chars, cfg):\n"
        "    if snippet_chars is not None:\n"
        "        return snippet_width_error(snippet_chars,"
        " max_chars=cfg.snippet_max_chars)\n"
        "    return None\n")
    assert snippet_width_duplication_error(sources) is None


def test_the_floor_is_the_one_the_config_fields_are_bounded_by() -> None:
    """`MIN_WIDTH` and `SearchConfig`'s two `ge=1` bounds are three
    statements of one floor. Nothing bound them, so they could drift into a
    config whose default the rule refuses."""
    for field in ("snippet_width_chars", "snippet_max_chars"):
        bounds = [m for m in SearchConfig.model_fields[field].metadata
                  if getattr(m, "ge", None) is not None]
        assert bounds, field
        assert bounds[0].ge == MIN_WIDTH, field
