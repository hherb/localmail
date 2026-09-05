# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Pure AST rules keeping the ``SearchArgumentRefused`` family enforceable (#347).

#344 gave the four Searcher argument refusals a base so ``api/search.py``
could catch the family instead of enumerating members. The enforcement that
shipped with it is one-directional: ``_family()`` derives its cases from
``SearchArgumentRefused.__subclasses__()`` filtered on
``__module__ == argument_errors``, which asks only about classes *already*
declared there. That is ``missing_seam_error`` asking whether a name is
present — the half this tree has twice ruled insufficient
(``_pool_leaks.pool_constructor_calls``,
``_harness_lock.acceptance_coverage_error``).

Two rules, because the two shapes cost different things:

``misplaced_member_error``
    A class inheriting a family member must be declared in the family
    module. Such a class is still mapped to 400 correctly — verified, so
    #347's title ("reproduces #344") is wrong — but ``_family()``'s module
    filter excludes it, so it silently joins none of the derived pins: not
    the 400-mapping cases, not #349's pre-IO ones. Coverage shrinking with
    every test green.

``foreign_refusal_error``
    A **named** exception class raised from ``Searcher.search`` must be a
    family member. This is the shape that actually reproduces #344 —
    ``class SomethingRefused(ValueError)`` reaches a caller as an
    operator-facing 500, ``serve.app`` handling ``APIError`` only — and it is
    not caught by the rule above, since it inherits nothing from the family.

**Neither rule judges a decision.** #347 warns that the raise rule must not
become "a second authority on which ``ValueError``s are deliberately outside
the family". It is not: a raise spelled with a bare builtin is out of scope
by construction, and one spelled with a named class must be a member. #348's
decision — the membership checks stay a plain ``ValueError`` — is recorded in
``argument_errors``' docstring, and this rule reads the *spelling* that
decision produced, never the reasoning behind it.

Both walk the AST rather than the text, for ``_mentions_version_option``'s
reason (#291): every forbidden shape here is named in prose in this module,
in the test file, in ``argument_errors``' docstring and in the issue, and a
substring scan reads those as violations.

Both take the family as a **parameter** rather than importing it, so the
rules can be driven against contrived inputs — the real four are structurally
incapable of violating either, which is exactly why nothing tested this.
"""
from __future__ import annotations

import ast
import pathlib
from collections.abc import Mapping

#: Where a family member must be declared, relative to ``src/localmail``.
FAMILY_MODULE = "search/argument_errors.py"

#: The root of the family.
FAMILY_BASE = "SearchArgumentRefused"

#: The class and method whose raises are governed by ``foreign_refusal_error``.
#: Scoped to ``search`` rather than the whole class because ``continue_page``
#: and ``grow_pool`` raise ``CacheMissError``/``PageOutOfPoolError`` on
#: purpose — a 409 with its own mapping, not a refused argument.
SEARCH_CLASS = "Searcher"
SEARCH_METHOD = "search"

#: Exceptions ``Searcher.search`` may raise without joining the family.
#: Bare builtins only: they carry no claim of being a refusal type, so
#: spelling one is a visible opt-out rather than a silent omission. The two
#: live uses are #333's membership checks (``ValueError``, kept outside the
#: family by #348) and ``--smart`` with no rewriter configured
#: (``RuntimeError``).
#:
#: **The exemption is conditional, and this constant cannot express the
#: condition.** Both live uses are safe only because a boundary answers them
#: first — ``run_search`` mirrors the same membership rule ahead of the call,
#: and computes ``effective_smart = smart and searcher.smart_available`` — so
#: neither reaches a catch site. A *new* guard spelled ``raise
#: ValueError(...)``, which is the shape both live examples model, passes
#: this rule and is an unhandled 500 at both boundaries, exactly as the
#: message below says. Adding a name here means checking that a boundary
#: refuses it first; the rule reads spelling and cannot check that for you.
ALLOWED_BARE_RAISES = frozenset({"ValueError", "RuntimeError"})


def source_files(root: pathlib.Path) -> dict[pathlib.Path, str]:
    """Every ``.py`` under ``root``, keyed by its path relative to it."""
    return {p.relative_to(root): p.read_text()
            for p in sorted(root.rglob("*.py"))}


def _base_names(node: ast.ClassDef) -> set[str]:
    """The bases of ``node``, by their final name component.

    ``Attribute`` is folded to its attribute so a qualified base
    (``argument_errors.SearchArgumentRefused``) is read the same as a bare
    one — otherwise the import style decides whether the rule applies.
    """
    names: set[str] = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def family_names(family_module_source: str) -> frozenset[str]:
    """The family's member names, read out of the family module itself.

    Returned as a set so the callers below need no import of the real
    exceptions, which keeps them drivable against contrived families. Starts
    from ``FAMILY_BASE`` and closes over inheritance within the module, so a
    member added there is covered without this function being edited.
    """
    tree = ast.parse(family_module_source)
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    found = {FAMILY_BASE}
    changed = True
    while changed:
        changed = False
        for node in classes:
            if node.name not in found and _base_names(node) & found:
                found.add(node.name)
                changed = True
    return frozenset(found)


def misplaced_member_error(
    sources: Mapping[pathlib.Path, str],
    *, family: frozenset[str] | None = None,
) -> str | None:
    """Report any family member declared outside the family module, or ``None``.

    ``family`` defaults to the names read from ``sources``' own copy of the
    family module, so the caller normally passes the tree and nothing else.

    The closure is a **fixpoint across files**, not one pass: a class
    deriving from a class that derives from a member is still a member, and
    the two can live in different modules. Iterating to a fixpoint also makes
    the result independent of the order ``sources`` happens to be in.
    """
    trees = {path: ast.parse(src) for path, src in sources.items()}
    if family is None:
        family_src = sources.get(pathlib.Path(FAMILY_MODULE))
        if family_src is None:
            return (f"{FAMILY_MODULE} was not among the sources, so the family "
                    "resolved empty and this rule inspected nothing")
        family = family_names(family_src)
    if not family:
        return (f"the family resolved empty, so this rule inspected nothing; "
                f"check FAMILY_BASE ({FAMILY_BASE!r}) against {FAMILY_MODULE}")

    known = set(family)
    offenders: dict[str, pathlib.Path] = {}
    changed = True
    while changed:
        changed = False
        for path, tree in trees.items():
            if path == pathlib.Path(FAMILY_MODULE):
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.ClassDef) and node.name not in known
                        and _base_names(node) & known):
                    known.add(node.name)
                    offenders[node.name] = path
                    changed = True
    if not offenders:
        return None
    listed = ", ".join(f"{name} ({path})" for name, path in sorted(offenders.items()))
    return (
        f"{FAMILY_BASE} members must be declared in {FAMILY_MODULE}, so that "
        f"_family() covers them and the derived 400-mapping and pre-IO pins "
        f"apply; found outside it: {listed}"
    )


def _searcher_methods(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    """Every method of ``SEARCH_CLASS``, keyed by name, **last binding wins**.

    Last rather than first, because that is what Python binds:
    ``_harness_lock._local_functions`` learned this on ``def main`` and the
    first version of this rule had the bug it was fixed for — a redefinition
    (a merge artefact, a conditional definition, a stub kept above the real
    one) meant auditing a method that never runs and reporting the tree
    healthy. Classes are folded the same way for the same reason.
    """
    methods: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == SEARCH_CLASS:
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    methods[item.name] = item
    return methods


def _self_call_names(fn: ast.FunctionDef) -> set[str]:
    """The ``self.foo(...)`` methods ``fn`` calls."""
    return {
        node.func.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    }


def _reachable_from_search(
    methods: Mapping[str, ast.FunctionDef],
) -> list[ast.FunctionDef]:
    """``SEARCH_METHOD`` and every method it transitively calls on ``self``.

    **Following helpers is the rule, not a refinement of it.** Reading
    ``search`` alone leaves the rule blind to the most natural refactor there
    is — extract the guards — and ``Searcher.search`` is 300+ lines, so that
    refactor is a matter of when. A guard moved into
    ``Searcher._check_hybrid_cursor`` raising a non-member is #344 verbatim,
    and the rule written to end it would have reported the tree healthy.
    ``_harness_lock.harness_lock_error`` follows ``main`` into its helpers
    for exactly this reason, having found a harness that already had the
    shape.

    Bounded by ``seen``, so mutual recursion terminates. Measured against the
    real tree when this landed: the six helpers reachable from ``search``
    raise nothing named, so following them reports nothing new — it closes
    the door before anyone walks through it.
    """
    seen: set[str] = set()
    order: list[ast.FunctionDef] = []
    stack = [SEARCH_METHOD]
    while stack:
        name = stack.pop()
        if name in seen or name not in methods:
            continue
        seen.add(name)
        order.append(methods[name])
        stack.extend(_self_call_names(methods[name]))
    return order


def _raised_name(node: ast.Raise) -> str | None:
    """The exception class name a ``raise`` names, or ``None``.

    ``None`` covers the two shapes that declare no type: a bare ``raise``
    (re-raising the active exception, which reading as a violation would
    forbid every ``except ...: raise`` in the method) and a raise of an
    expression naming no identifier at all — ``raise errs[0]``, say, which
    this rule cannot resolve and does not guess at.

    **Known imprecision, deliberate**: ``raise err`` on a local *is* an
    ``ast.Name``, so it reads as the class name ``err`` and is reported. An
    earlier wording here claimed the opposite. Left as it is because it
    fails closed — a spurious report, never a missed one — and because
    judging otherwise means resolving names, a second binding analysis to
    keep in step with Python's. Rename the local or raise the class.

    Both a ``Call`` and a bare class are read (``raise Foo("x")`` and
    ``raise Foo``), and a dotted spelling folds to its attribute
    (``raise argument_errors.Foo``) so the import style does not decide
    whether the rule applies — the ``_base_names`` arrangement.

    One function rather than two conditions at the call site: the second was
    redundant with the first, so no mutation could distinguish them, and an
    arm no test can reach is the kind this tree removes rather than
    documents.
    """
    if node.exc is None:
        return None
    exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
    if isinstance(exc, ast.Name):
        return exc.id
    if isinstance(exc, ast.Attribute):
        return exc.attr
    return None


def family_raise_sites(
    searcher_source: str, *, family: frozenset[str],
) -> dict[str, int]:
    """How many times each family member is raised, reachable from ``search``.

    The *site*-keyed counterpart to ``foreign_refusal_error``'s name-keyed
    question, and the reverse cross-check
    ``test_searcher_guards_precede_io.py``'s provocation table needs. That
    file makes site-keying its central claim — ``KeysetCursorUnusable`` has
    two raise sites and "covering the type once would leave the site that
    most plausibly drifts untested" — but its own completeness check compares
    *types*, so a sixth site of an already-provoked member joins nothing and
    nothing fails. ``missing_seam_error``'s "asks only whether a name is
    present" half, one file over.

    Same reachable set as the raise rule, so a guard extracted into a helper
    still counts. Returns data rather than a message because the caller's
    question is "do these two agree", which only it can answer.
    """
    tree = ast.parse(searcher_source)
    methods = _searcher_methods(tree)
    counts: dict[str, int] = {}
    for fn in _reachable_from_search(methods):
        for node in ast.walk(fn):
            if not isinstance(node, ast.Raise):
                continue
            name = _raised_name(node)
            if name is not None and name in family:
                counts[name] = counts.get(name, 0) + 1
    return counts


def foreign_refusal_error(
    searcher_source: str, *, family: frozenset[str],
) -> str | None:
    """Report a named non-member raised from ``Searcher.search``, or ``None``.

    "From ``search``" means the method **and every method it transitively
    calls on ``self``** — see ``_reachable_from_search`` for why reading the
    method alone is not the rule but a hole in it.

    A bare ``raise`` (re-raising the active exception) declares no type and is
    ignored; so is any name in ``ALLOWED_BARE_RAISES``.

    A missing ``Searcher.search`` is itself reported. Without that, a rename
    would leave this rule inspecting nothing and reporting every tree
    healthy — the vacuity ``_family()`` returning ``[]`` would cause one file
    over, and the reason both this and ``misplaced_member_error`` are pinned
    with negative controls rather than only positive ones.
    """
    tree = ast.parse(searcher_source)
    methods = _searcher_methods(tree)
    if SEARCH_METHOD not in methods:
        return (f"{SEARCH_CLASS}.{SEARCH_METHOD} was not found, so this rule "
                "inspected nothing; update SEARCH_CLASS/SEARCH_METHOD if the "
                "guards moved")

    offenders: list[str] = []
    for fn in _reachable_from_search(methods):
        for node in ast.walk(fn):
            if not isinstance(node, ast.Raise):
                continue
            name = _raised_name(node)
            if name is None or name in ALLOWED_BARE_RAISES or name in family:
                continue
            where = ("" if fn.name == SEARCH_METHOD
                     else f", via {SEARCH_CLASS}.{fn.name}")
            offenders.append(f"{name} (line {node.lineno}{where})")
    if not offenders:
        return None
    return (
        f"every named exception raised from {SEARCH_CLASS}.{SEARCH_METHOD} "
        f"must subclass {FAMILY_BASE}, or both api boundaries answer it as a "
        f"500 rather than a 400; found: {', '.join(offenders)}"
    )
