# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Every rerank call goes through ``_safe_rerank`` (#361 review follow-up).

The structural half. ``_safe_rerank`` holds both degradation guards, and
the argument for putting them there is that one helper covers both call
sites by construction — which was untrue when #361 shipped: replacing the
call in ``Searcher.search`` with a direct ``reranker.rerank(...)`` left the
whole suite green. The rule under test is
``_rerank_wiring_rules.rerank_wiring_error``; the behavioural half lives in
``test_rerank_end_to_end.py``.

The contrived-source tests matter more than the one against ``src/``: the
real tree cannot violate this rule today, so a rule that always returned
``None`` would pass the production check and nothing else.
"""
from __future__ import annotations

import pathlib

from tests._rerank_wiring_rules import (
    EXEMPT_MODULES,
    GUARD_MODULE,
    rerank_wiring_error,
    source_files,
    unguarded_rerank_calls,
)

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "localmail"

_GUARDED = '''
def _safe_rerank(reranker, query, snippets, *, fallback):
    try:
        raw = reranker.rerank(query, snippets)
    except Exception:
        return list(fallback)
    return finite_scores(raw, fallback=fallback).scores
'''

#: The mutation that was green on the shipped branch.
_BYPASS = _GUARDED + '''
class Searcher:
    def search(self, q):
        return self._reranker.rerank(q, self._snippets)
'''


def test_the_shipped_tree_routes_every_rerank_through_the_guard() -> None:
    """The production check. Weak on its own — see the module docstring."""
    assert rerank_wiring_error(source_files(_SRC)) is None


def test_the_bypass_that_was_green_on_the_shipped_branch_is_reported() -> None:
    """The exact mutation two reviewers ran independently: call the reranker
    directly from ``Searcher.search``, discarding both guards."""
    problem = rerank_wiring_error({pathlib.Path(GUARD_MODULE): _BYPASS})
    assert problem is not None
    assert "rerank called outside _safe_rerank" in problem
    # The report must name the offending line, not just the file — derived
    # from the fixture so editing it cannot silently weaken the assertion.
    bypass_line = 1 + next(
        i for i, text in enumerate(_BYPASS.splitlines())
        if "self._reranker.rerank" in text
    )
    assert f"{GUARD_MODULE}:{bypass_line}" in problem, problem


def test_the_guard_s_own_call_is_not_reported() -> None:
    """The positive control. A rule that reported every ``.rerank(`` would
    fail the production check above, but would also 'catch' the mutation
    for the wrong reason — so both directions are pinned."""
    assert rerank_wiring_error({pathlib.Path(GUARD_MODULE): _GUARDED}) is None


def test_the_guard_is_only_privileged_in_its_own_module() -> None:
    """``_safe_rerank`` is a module-level function in ``searcher.py``. A
    same-named function elsewhere is a different function and gets no
    exemption — otherwise the rule is defeated by copying a name."""
    other = pathlib.Path("search/somewhere_else.py")
    problem = rerank_wiring_error({other: _GUARDED})
    assert problem is not None
    assert other.as_posix() in problem


def test_a_call_in_an_exempt_module_is_not_reported() -> None:
    """``reranker.py`` implements the call and ``cli.py`` warms the model,
    discarding the result. Driven through the parameter so the real
    exemptions are not what is being asserted."""
    warmup = pathlib.Path("cli.py")
    assert unguarded_rerank_calls({warmup: _BYPASS}, exempt=frozenset({"cli.py"})) == []
    assert unguarded_rerank_calls({warmup: _BYPASS}, exempt=frozenset()) != []


def test_the_real_exemptions_are_the_two_that_earn_it() -> None:
    """A third exemption is how this rule would be silently switched off, so
    the set is asserted rather than trusted. ``reranker.py`` implements the
    call; ``cli.py`` discards its result."""
    assert EXEMPT_MODULES == frozenset({"search/reranker.py", "cli.py"})


def test_a_tree_with_no_rerank_call_at_all_is_reported_not_passed() -> None:
    """A rename retires this rule silently otherwise — the failure mode
    ``_harness_lock`` records, where a guard goes quiet at the moment it
    stops applying rather than when it starts being violated."""
    problem = rerank_wiring_error({pathlib.Path("search/searcher.py"): "x = 1\n"})
    assert problem is not None
    assert "retired by a rename" in problem


def test_a_nested_helper_inside_the_guard_is_still_reported() -> None:
    """Deliberate conservatism: attribution follows the innermost function,
    so a closure declared inside ``_safe_rerank`` is reported and its author
    says why. Fails closed, which is the direction to fail in."""
    nested = '''
def _safe_rerank(reranker, query, snippets, *, fallback):
    def _inner():
        return reranker.rerank(query, snippets)
    return _inner()
'''
    assert unguarded_rerank_calls({pathlib.Path(GUARD_MODULE): nested}) != []
