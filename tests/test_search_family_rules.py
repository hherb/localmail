# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The family is enforced against ``src/``, not only against itself (#347).

#344's enforcement is one-directional.
``test_search_argument_errors.py::_family()`` filters on ``obj.__module__ ==
argument_errors.__name__``, so it checks only classes **already defined in**
the family module — ``missing_seam_error`` asking whether a name is present,
one module over.

**#347's title and its body describe different defects, and only one of them
is #344.** Measured, not read:

* A fifth member that *inherits the base* but lives in ``searcher.py`` is
  mapped to **400** by both boundaries exactly as intended — the title's
  "reproduces #344" is wrong. What it really costs is the derived pins: the
  ``__module__`` filter excludes it, so it silently joins neither the
  400-mapping cases nor #349's pre-IO ones. Coverage shrinking with every
  test green, which is the same class of defect one level down.
* A fifth guard written ``class SomethingRefused(ValueError)`` **is** #344
  verbatim — an operator-facing 500, ``serve.app`` handling ``APIError``
  only. And the acceptance #347 states (a location rule over classes that
  inherit the base) does not catch it, because it does not inherit the base.

So there are two rules here, not one. The location rule is what #347 asks
for and keeps ``_family()`` honest; the raise rule is the "stronger variant"
it weighs, and is the one that closes the 500.

The raise rule deliberately does **not** become "a second authority on which
``ValueError``s are outside the family", which #347 warns against. It never
judges a *decision*: a raise spelled with a bare builtin (``ValueError``,
``RuntimeError``) is out of scope by construction, and any raise spelled with
a **named** class must be a family member. #348's decision — that the
membership checks stay a plain ``ValueError`` — is recorded in
``argument_errors``' docstring and is what makes those two raise sites
spelled that way; this rule reads the spelling, not the reasoning.

Both walk the **AST**, never the text: this module, the issue, and
``argument_errors``' own docstring all name the forbidden shapes in prose,
and a substring scan reads those as violations (``_mentions_version_option``,
#291).
"""
from __future__ import annotations

import pathlib

import pytest

from tests._search_family_rules import (
    ALLOWED_BARE_RAISES,
    FAMILY_MODULE,
    family_names,
    foreign_refusal_error,
    misplaced_member_error,
    source_files,
)

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "localmail"


# --- the rules hold over the real tree ------------------------------------

def test_no_family_member_is_declared_outside_the_family_module() -> None:
    assert misplaced_member_error(source_files(_SRC)) is None


def test_every_named_refusal_raised_from_search_is_a_family_member() -> None:
    searcher = _SRC / "search" / "searcher.py"
    assert foreign_refusal_error(searcher.read_text(),
                                 family=family_names(
                                     (_SRC / FAMILY_MODULE).read_text())) is None


def test_the_family_module_is_where_the_members_actually_live() -> None:
    """The rules above are vacuous if this path is wrong — a typo'd
    ``FAMILY_MODULE`` makes ``family_names`` empty, and an empty family makes
    both rules pass over any tree at all."""
    names = family_names((_SRC / FAMILY_MODULE).read_text())
    assert names >= {"SearchArgumentRefused", "SortNotApplicable",
                     "SortOrderNotApplicable", "KeysetOrderMismatch",
                     "KeysetCursorUnusable"}


# --- the location rule ----------------------------------------------------

_LOCATION_FAMILY = frozenset({"SearchArgumentRefused", "SortNotApplicable",
                              "KeysetCursorUnusable"})


def test_a_member_declared_elsewhere_is_reported() -> None:
    """The defect #347 asks for: it keeps ``_family()``'s module filter
    honest, so a member cannot escape every derived pin."""
    problem = misplaced_member_error({
        pathlib.Path("search/searcher.py"):
            "class FilterRefused(SearchArgumentRefused):\n    pass\n",
    }, family=_LOCATION_FAMILY)
    assert problem is not None
    assert "FilterRefused" in problem
    assert "searcher.py" in problem


def test_a_member_inheriting_another_member_is_reported_too() -> None:
    """Inheritance is transitive, so the rule must be. A class deriving from
    ``KeysetCursorUnusable`` is as much a family member as one deriving from
    the base, and is caught by the boundaries identically."""
    problem = misplaced_member_error({
        pathlib.Path("search/date_keyset.py"):
            "class StaleCursor(KeysetCursorUnusable):\n    pass\n",
    }, family=_LOCATION_FAMILY)
    assert problem is not None
    assert "StaleCursor" in problem


def test_a_two_hop_chain_outside_the_module_is_reported() -> None:
    """The fixpoint, not one pass: a class deriving from a class that derives
    from a member is still a member. Written as two files so the rule cannot
    pass by seeing both bases in one AST."""
    problem = misplaced_member_error({
        pathlib.Path("search/a.py"): "class Mid(SortNotApplicable):\n    pass\n",
        pathlib.Path("search/b.py"): "class Leaf(Mid):\n    pass\n",
    }, family=_LOCATION_FAMILY)
    assert problem is not None
    assert "Leaf" in problem


def test_a_qualified_base_is_reported() -> None:
    """``argument_errors.SearchArgumentRefused`` is the same declaration
    spelled through the module, and dodges a rule that reads ``Name`` only."""
    problem = misplaced_member_error({
        pathlib.Path("search/searcher.py"):
            "class X(argument_errors.SearchArgumentRefused):\n    pass\n",
    }, family=_LOCATION_FAMILY)
    assert problem is not None


def test_the_family_module_itself_is_exempt() -> None:
    """The positive control: the real members live there and must not be
    reported, or the rule is unusable and would be deleted."""
    assert misplaced_member_error({
        pathlib.Path(FAMILY_MODULE):
            "class KeysetCursorUnusable(SearchArgumentRefused):\n    pass\n",
    }, family=_LOCATION_FAMILY) is None


def test_an_unrelated_exception_elsewhere_is_not_reported() -> None:
    """The other positive control: the rule must not claim every exception in
    ``src/``. ``CacheMissError`` and friends are none of its business."""
    assert misplaced_member_error({
        pathlib.Path("search/page_cache.py"):
            "class CacheMissError(RuntimeError):\n    pass\n",
    }, family=_LOCATION_FAMILY) is None


# --- the raise rule -------------------------------------------------------

_FAMILY = frozenset({"SearchArgumentRefused", "SortNotApplicable",
                     "KeysetCursorUnusable"})


def _searcher_source(body: str) -> str:
    return f"class Searcher:\n    def search(self):\n{body}"


def test_a_named_non_member_raised_from_search_is_reported() -> None:
    """#344 verbatim: this is the shape that reaches a caller as a 500."""
    problem = foreign_refusal_error(
        _searcher_source('        raise SomethingRefused("nope")\n'),
        family=_FAMILY)
    assert problem is not None
    assert "SomethingRefused" in problem


@pytest.mark.parametrize("name", sorted(ALLOWED_BARE_RAISES))
def test_a_bare_builtin_raise_is_out_of_scope(name: str) -> None:
    """The escape hatch, and the reason this rule is not a second authority
    on #348's decision: it reads the spelling, never the reasoning."""
    assert foreign_refusal_error(
        _searcher_source(f'        raise {name}("membership")\n'),
        family=_FAMILY) is None


@pytest.mark.parametrize("member", sorted(_FAMILY))
def test_a_family_member_raised_from_search_is_fine(member: str) -> None:
    assert foreign_refusal_error(
        _searcher_source(f'        raise {member}("refused")\n'),
        family=_FAMILY) is None


def test_a_raise_outside_search_is_out_of_scope() -> None:
    """``continue_page`` raises ``CacheMissError`` on purpose — a 409, with
    its own mapping. A rule scoped to the whole class would forbid it.

    ``continue_page`` is written **first** deliberately: with ``search`` first,
    a rule that resolved the class's *first* method rather than the named one
    would still land on ``search`` and pass. Mutation-proven — that ordering
    let ``item.name == SEARCH_METHOD`` be deleted with the file green.
    """
    src = ("class Searcher:\n"
           "    def continue_page(self):\n"
           "        raise CacheMissError('evicted')\n"
           "    def search(self):\n"
           "        pass\n")
    assert foreign_refusal_error(src, family=_FAMILY) is None


def test_a_qualified_raise_is_reported() -> None:
    """``raise argument_errors.SomethingRefused(...)`` — the ``ast.Attribute``
    arm of ``_raised_name``.

    Untested until the #350 review, and deleting that arm left the file
    green, so the import style decided whether the rule applied. The
    *location* rule has had ``test_a_qualified_base_is_reported`` for the
    identical case in ``_base_names`` since #347; this is its counterpart.
    """
    problem = foreign_refusal_error(
        _searcher_source('        raise argument_errors.SomethingRefused("x")\n'),
        family=_FAMILY)
    assert problem is not None
    assert "SomethingRefused" in problem


def test_a_raise_of_a_bare_class_is_reported() -> None:
    """``raise SomethingRefused`` — no parentheses, so ``node.exc`` is a
    ``Name`` rather than a ``Call``. Python instantiates it for you, so it is
    a raise of that class in every sense that matters here; deleting the
    non-``Call`` arm of ``_raised_name`` left the file green."""
    problem = foreign_refusal_error(
        _searcher_source("        raise SomethingRefused\n"), family=_FAMILY)
    assert problem is not None
    assert "SomethingRefused" in problem


def test_an_expression_naming_no_identifier_is_not_guessed_at() -> None:
    """The genuine ``None`` arm. ``raise errs[0]`` names no class this rule
    can resolve, and guessing would be a binding analysis."""
    assert foreign_refusal_error(
        _searcher_source("        raise errs[0]\n"), family=_FAMILY) is None


def test_a_guard_extracted_into_a_helper_is_still_in_scope() -> None:
    """The hole #347 shipped with: reading ``search`` alone (#350 review).

    "Extract the guards" is the most natural refactor of a 300-line method,
    and ``_harness_lock`` already found a harness that had exactly that
    shape. A non-member raised from ``Searcher._check_cursor`` is #344
    verbatim — a 500 at both boundaries — and the rule was green for it.
    """
    src = ("class Searcher:\n"
           "    def search(self):\n"
           "        self._check_cursor()\n"
           "    def _check_cursor(self):\n"
           "        raise SomethingRefused('nope')\n")
    problem = foreign_refusal_error(src, family=_FAMILY)
    assert problem is not None
    assert "SomethingRefused" in problem
    assert "_check_cursor" in problem


def test_a_family_member_raised_from_a_helper_is_fine() -> None:
    """The control: following helpers must not report the compliant form of
    the same refactor, or the rule buys a false positive rather than a
    closed hole."""
    src = ("class Searcher:\n"
           "    def search(self):\n"
           "        self._check_cursor()\n"
           "    def _check_cursor(self):\n"
           "        raise KeysetCursorUnusable('refused')\n")
    assert foreign_refusal_error(src, family=_FAMILY) is None


def test_a_helper_no_one_calls_from_search_stays_out_of_scope() -> None:
    """The other control. ``continue_page``'s helpers are as out of scope as
    ``continue_page`` — reachability is the rule, not "any private method"."""
    src = ("class Searcher:\n"
           "    def search(self):\n"
           "        pass\n"
           "    def continue_page(self):\n"
           "        self._evict()\n"
           "    def _evict(self):\n"
           "        raise CacheMissError('evicted')\n")
    assert foreign_refusal_error(src, family=_FAMILY) is None


def test_mutually_recursive_helpers_terminate() -> None:
    """``seen`` bounds the walk; without it this hangs rather than fails."""
    src = ("class Searcher:\n"
           "    def search(self):\n"
           "        self._a()\n"
           "    def _a(self):\n"
           "        self._b()\n"
           "    def _b(self):\n"
           "        self._a()\n"
           "        raise SomethingRefused('nope')\n")
    problem = foreign_refusal_error(src, family=_FAMILY)
    assert problem is not None
    assert "SomethingRefused" in problem


def test_the_last_binding_of_search_is_the_one_audited() -> None:
    """Python binds the last ``def``; auditing the first means auditing a
    method that never runs.

    ``_harness_lock._local_functions`` records this lesson on ``def main``
    and this rule shipped with the bug it was fixed for.
    """
    src = ("class Searcher:\n"
           "    def search(self):\n"
           "        pass\n"
           "    def search(self):\n"
           "        raise SomethingRefused('nope')\n")
    problem = foreign_refusal_error(src, family=_FAMILY)
    assert problem is not None
    assert "SomethingRefused" in problem


def test_a_two_hop_chain_inside_the_family_module_is_a_member() -> None:
    """``family_names`` closes over inheritance *within* the module, and that
    closure was a single pass' worth of untested: no fixture had a two-hop
    chain inside the family module, so collapsing it to a base-only read left
    the file green.

    ``misplaced_member_error`` has ``test_a_two_hop_chain_outside_the_module_
    is_reported`` for the cross-file fixpoint; this is the in-module half.

    **Declared innermost-first, which is the whole point.** With the chain
    written base-downwards a single pass over the classes finds all three in
    declaration order, so the fixpoint mutation survives — measured. It is
    order-independence that ``while changed`` buys, and only a hostile order
    demonstrates it.
    """
    src = ("class KeysetWalkUnusable(KeysetCursorUnusable):\n    pass\n"
           "class KeysetCursorUnusable(SearchArgumentRefused):\n    pass\n"
           "class SearchArgumentRefused(ValueError):\n    pass\n")
    assert family_names(src) == frozenset({
        "SearchArgumentRefused", "KeysetCursorUnusable", "KeysetWalkUnusable"})


def test_a_bare_reraise_is_not_a_declaration() -> None:
    """``raise`` with no expression re-raises the active exception; reading it
    as a violation would forbid every ``except ...: raise`` in the method."""
    src = _searcher_source("        try:\n            pass\n"
                           "        except Exception:\n            raise\n")
    assert foreign_refusal_error(src, family=_FAMILY) is None


def test_a_missing_search_method_is_reported_not_passed() -> None:
    """The vacuity control. A rename that moved the guards out of
    ``Searcher.search`` would otherwise make this rule silently inspect
    nothing and report every tree healthy — the ``_family()`` returning ``[]``
    trap, one file over."""
    problem = foreign_refusal_error("class Searcher:\n    pass\n",
                                    family=_FAMILY)
    assert problem is not None
    assert "search" in problem


def test_an_empty_family_is_reported_not_passed() -> None:
    """The other vacuity control: both rules take the family as a parameter,
    so a resolution that silently yielded nothing would make them assert
    nothing at all."""
    problem = foreign_refusal_error(
        _searcher_source('        raise SortNotApplicable("x")\n'),
        family=frozenset())
    assert problem is not None


def test_a_tree_without_the_family_module_is_reported_not_passed() -> None:
    """The location rule's vacuity control, matching the raise rule's.

    Resolving the family from the tree is the convenient default, so a moved
    or renamed ``argument_errors.py`` would otherwise leave the rule
    inspecting nothing and reporting every tree healthy."""
    problem = misplaced_member_error({
        pathlib.Path("search/searcher.py"):
            "class FilterRefused(SearchArgumentRefused):\n    pass\n",
    })
    assert problem is not None
    assert FAMILY_MODULE in problem


def test_an_empty_stated_family_is_reported_not_passed() -> None:
    """And the same when the family is stated rather than resolved."""
    problem = misplaced_member_error(
        {pathlib.Path("search/searcher.py"): "class X(Y):\n    pass\n"},
        family=frozenset())
    assert problem is not None
