# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Every argument the Searcher refuses is one family, mapped to 400 as one (#344).

``Searcher.search`` raises four sibling exceptions whose entire purpose is
"map me to a 400". They used to derive straight from ``ValueError`` with no
shared base, so ``api/search.py`` enumerated them by name — in **two
different tuples on two branches**, each carrying a comment arguing which
members were unreachable there. A fifth guard added without updating a
tuple is an operator-facing **500**, because ``serve.app`` registers a
handler for ``APIError`` only.

That is not hypothetical. #342 shipped with ``SortNotApplicable`` missing
from the keyset tuple, and its safety rested on ``KEYSET_SORT is
TEXTLESS_SORT`` — an aliasing decision made in ``search_cursor.py``, i.e. in
a different module from the omission it was holding up.

The pins here are deliberately two kinds, because either alone has a hole:

* **Structural** — every exception class in ``argument_errors`` inherits
  the base, so a fifth guard *written in the right place* joins the family
  by construction rather than by someone remembering.
* **Behavioural** — the family is enumerated from the **type**
  (``__subclasses__``, transitively), never from a list here, and every
  member is driven through both ``run_search`` branches. A member added
  later is therefore in scope for **those pins** without anyone editing
  them, which is the property a hand-written list cannot have.

Two tests here *are* hand-written lists, and deliberately: the
``_KNOWN_MEMBERS`` control and the ``label`` mapping both fail on a
fifth member, which is how the author is made to decide its prefix rather
than inherit one silently. So the file does need an edit — the parametrised
pins do not, and they are the ones that would otherwise pass vacuously.

**What none of this reaches** is a fifth member written *outside*
``argument_errors``: the structural pin filters on ``__module__``, so a
``class Foo(ValueError)`` in ``searcher.py`` reproduces #344 verbatim with
every test here green. That reverse cross-check is #347.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search import run_search
from localmail.api.search_cursor import encode_keyset_cursor
from localmail.search import argument_errors
from localmail.search.argument_errors import (
    KeysetCursorUnusable,
    KeysetOrderMismatch,
    SearchArgumentRefused,
    SortNotApplicable,
    SortOrderNotApplicable,
)
from localmail.search.searcher import KeysetCursor

#: The four guards live at four different points of ``Searcher.search`` and
#: were four unrelated types. Spelled out once here so the structural pin
#: below has something to fail against if one is dropped from the module.
_KNOWN_MEMBERS = (
    SortNotApplicable,
    SortOrderNotApplicable,
    KeysetOrderMismatch,
    KeysetCursorUnusable,
)


def _family(
    base: type[SearchArgumentRefused] = SearchArgumentRefused,
) -> list[type[SearchArgumentRefused]]:
    """Every concrete member of the family, transitively.

    Derived from the type rather than listed, so a member added later is
    covered by the parametrised pins below without this file being edited —
    the whole point of giving the four a base class. ``__subclasses__`` is
    direct only, hence the recursion.

    Restricted to classes defined in ``argument_errors``, because
    ``__subclasses__`` is process-global while ``@parametrize`` is evaluated
    at **import**: without the filter the case set would depend on which test
    modules pytest happened to import first, and the day any file defines a
    local subclass as a negative control — a natural thing to write for this
    feature — ``test_the_family_is_exactly_the_four_known_members`` becomes
    order-dependent. The filter costs nothing, since a member belongs in that
    module anyway (#347) and the structural pin below independently requires
    every class defined there to inherit the base.
    """
    out: list[type[SearchArgumentRefused]] = []
    for sub in base.__subclasses__():
        if sub.__module__ == argument_errors.__name__:
            out.append(sub)
        out.extend(_family(sub))
    return out


# --- the family itself ----------------------------------------------------

def test_all_four_refusals_share_one_base() -> None:
    for exc in _KNOWN_MEMBERS:
        assert issubclass(exc, SearchArgumentRefused), exc.__name__


def test_the_base_is_a_value_error() -> None:
    """Kept a ``ValueError`` so nothing that already catches one changes.

    The base is what api/ catches; ``ValueError`` is what it must stay
    narrower than, since psycopg, ``datetime`` and the embedding backends
    raise that and relabelling a real outage as a caller error would send
    the operator to fix a blameless query.
    """
    assert issubclass(SearchArgumentRefused, ValueError)
    assert not issubclass(ValueError, SearchArgumentRefused)


def test_every_exception_in_the_module_inherits_the_base() -> None:
    """The by-construction half: a fifth guard added here joins the family.

    Without it the base is just a convention, and #344's defect — a member
    the boundary does not catch — returns as "someone forgot to inherit"
    instead of "someone forgot to widen a tuple".
    """
    defined = [
        obj for _name, obj in vars(argument_errors).items()
        if inspect.isclass(obj)
        and issubclass(obj, BaseException)
        and obj.__module__ == argument_errors.__name__
        and obj is not SearchArgumentRefused
    ]
    assert defined, "the module must define the refusals it is named for"
    for exc in defined:
        assert issubclass(exc, SearchArgumentRefused), exc.__name__


def test_the_family_is_exactly_the_four_known_members() -> None:
    """A negative control on ``_family``.

    Every parametrised test below is driven by it, so a ``_family`` that
    silently returned ``[]`` would make them all pass vacuously.
    """
    assert set(_family()) == set(_KNOWN_MEMBERS)


# --- both api boundaries map the whole family -----------------------------

def _searcher(raising: BaseException) -> MagicMock:
    s = MagicMock()
    s.search.side_effect = raising
    s.smart_available = False
    return s


def _keyset_cursor() -> str:
    return encode_keyset_cursor(
        KeysetCursor(ts=None, id=7, order="desc", walk="archive")
    )


@pytest.mark.parametrize("exc_type", _family(), ids=lambda t: t.__name__)
def test_the_fresh_branch_maps_every_family_member_to_a_400(
    exc_type: type[Exception],
) -> None:
    """``ValidationFailed`` alone would not pin this — ``run_search`` raises
    it from at least three sites that never touch the searcher, so the call
    is asserted as well as the outcome."""
    s = _searcher(exc_type("refused"))
    with pytest.raises(ValidationFailed):
        run_search(searcher=s, free_text="invoice", filters={}, limit=5,
                   allowed_account_ids=[1], user_id=1)
    s.search.assert_called_once()
    assert "keyset_cursor" not in s.search.call_args.kwargs


@pytest.mark.parametrize("exc_type", _family(), ids=lambda t: t.__name__)
def test_the_keyset_branch_maps_every_family_member_to_a_400(
    exc_type: type[Exception],
) -> None:
    """The branch #342 shipped with ``SortNotApplicable`` missing.

    It was safe only because ``KEYSET_SORT is TEXTLESS_SORT``, i.e. by an
    aliasing decision made in a different module — the kind of non-local
    reasoning a base class removes.

    The branch is asserted, not assumed. On ``pytest.raises(ValidationFailed)``
    alone this test passed with ``elif plan.mode == "keyset"`` mutated to
    ``elif False`` — control fell through to the pool branch, where
    ``decode_search_cursor`` raises a *different* ``ValidationFailed`` over
    the same cursor. A test named for a branch that passes while the branch
    never runs is the shape this file exists to remove.
    """
    s = _searcher(exc_type("refused"))
    with pytest.raises(ValidationFailed):
        run_search(searcher=s, free_text="invoice", filters={}, limit=5,
                   allowed_account_ids=[1], user_id=1,
                   cursor=_keyset_cursor())
    s.search.assert_called_once()
    assert s.search.call_args.kwargs["keyset_cursor"] is not None


def test_an_unrelated_value_error_still_escapes_as_itself() -> None:
    """The positive control, and the reason the base is not ``ValueError``.

    A boundary that widened to bare ``ValueError`` would pass every test
    above while relabelling a psycopg or embedding-backend failure as a
    caller error.
    """
    s = _searcher(ValueError("the backend fell over"))
    with pytest.raises(ValueError) as exc:
        run_search(searcher=s, free_text="invoice", filters={}, limit=5,
                   allowed_account_ids=[1], user_id=1)
    assert not isinstance(exc.value, ValidationFailed)


def test_the_keyset_branch_keeps_naming_the_cursor() -> None:
    """A cursor refusal still reads as one — the shipped, reachable wording
    on that branch, which the prefix rule below must not change."""
    s = _searcher(KeysetCursorUnusable("needs its query back"))
    with pytest.raises(ValidationFailed, match="^cursor: needs its query back"):
        run_search(searcher=s, free_text="invoice", filters={}, limit=5,
                   allowed_account_ids=[1], user_id=1,
                   cursor=_keyset_cursor())


# --- the prefix says what the refusal is about (#331 point 3) -------------

def test_the_prefix_rides_the_exception_not_the_branch() -> None:
    """A non-cursor refusal caught on the keyset branch is not labelled one.

    The prefix used to be written into that branch's f-string, so it was
    applied to everything the branch caught. Widening the catch to the whole
    family (#344) would have made the mislabel reachable by more members —
    the category error #331 point 3 filed, arriving via its own fix.
    """
    s = _searcher(SortOrderNotApplicable("sort_order='asc' is not applicable"))
    with pytest.raises(ValidationFailed) as exc:
        run_search(searcher=s, free_text="invoice", filters={}, limit=5,
                   allowed_account_ids=[1], user_id=1,
                   cursor=_keyset_cursor())
    assert "cursor:" not in str(exc.value)


def test_a_cursor_refusal_is_labelled_on_the_fresh_branch_too() -> None:
    """The other half: the prefix follows the cause, so it appears wherever
    the cause does — not only where a branch happened to write it."""
    s = _searcher(KeysetCursorUnusable("needs its query back"))
    with pytest.raises(ValidationFailed, match="^cursor: "):
        run_search(searcher=s, free_text="invoice", filters={}, limit=5,
                   allowed_account_ids=[1], user_id=1)


def test_only_the_cursor_refusals_carry_a_prefix() -> None:
    """Pins the mapping itself, so a member added later must decide.

    An empty default fails harmlessly (a lost word of context); an inherited
    *wrong* one makes a false claim, which is why the two cursor members set
    it explicitly rather than the sort members clearing it.
    """
    carried = {e for e in _family() if e.label}
    assert carried == {KeysetCursorUnusable, KeysetOrderMismatch}
    for exc in carried:
        assert exc.label == "cursor"


# --- the join belongs to the type, not to the member or the boundary (#350) --

def test_the_separator_is_owned_by_the_type_not_written_into_each_label() -> None:
    """A member declares *what* the refusal is about; the type spells the join.

    ``wire_prefix = "cursor: "`` made the correctness of the join a property
    of a member's **spelling**, invisible at the join site.

    #350 states that "nothing catches it", and that is **wrong as written** —
    measured against the pre-#350 tree, dropping the space from a shipped
    member fails three tests, one of them a literal
    ``assert exc.wire_prefix == "cursor: "``. The defect it names is real but
    narrower, and this test is aimed at the narrower one: what nothing
    checked is the **form**. A fifth member spelled ``"filter"`` and one
    spelled ``"filter: "`` fail the *same two* enumeration tests
    (``..._is_exactly_the_four_known_members`` and
    ``..._only_the_cursor_refusals_carry_a_prefix``), because both fail for
    being new rather than for being misspelled — so whoever updates those two
    to admit the member, as they must, gets no signal about the separator.
    And it is spelled wrong by default: the trailing space in a sibling is
    the only place the convention is written down.
    """
    for exc in _family():
        assert SearchArgumentRefused._SEPARATOR not in exc.label, (
            f"{exc.__name__}.label spells the separator itself; the type "
            "owns it")
        assert exc.label == exc.label.strip()


def test_a_labelled_member_joins_its_label_to_its_message() -> None:
    """The shipped wire form, asserted against the *rule* rather than a
    literal, so the two cannot drift apart."""
    exc = KeysetCursorUnusable("needs its query back")
    assert exc.wire_message() == (
        f"{exc.label}{SearchArgumentRefused._SEPARATOR}needs its query back")


def test_an_unlabelled_member_renders_its_bare_message() -> None:
    """No label, no separator — the sort members' shipped form."""
    exc = SortNotApplicable("sort='rank' is not applicable")
    assert exc.wire_message() == "sort='rank' is not applicable"


@pytest.mark.parametrize("message", ["", "   ", "\t\n"])
def test_a_labelled_member_with_no_message_renders_no_dangling_separator(
    message: str,
) -> None:
    """``SearchArgumentRefused()`` used to render ``"cursor: "`` — a label
    with nothing after it, which reads as a detail withheld.

    Unreachable today, since all five raise sites pass a message, which is
    exactly when such things rot. ``ResolvedVersion.__post_init__`` *rejects*
    a blank detail for the same reason; here the refusal is still worth
    delivering, so the label alone is the answer.

    Tested on ``.strip()`` rather than truthiness for the reason
    ``version_report.unreadable``'s fallback is: ``"   "`` is truthy and
    would sail through a bare ``or`` into the dangling form.
    """
    assert KeysetCursorUnusable(message).wire_message() == "cursor"


def test_a_member_with_neither_label_nor_message_renders_empty() -> None:
    """The base itself, which no boundary raises. Pinned so the two
    independent branches of the join are both decided rather than one of
    them being an accident of the other."""
    assert SearchArgumentRefused().wire_message() == ""
