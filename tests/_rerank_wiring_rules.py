# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A pure AST rule keeping every rerank call behind ``_safe_rerank`` (#361).

``_safe_rerank`` holds both degradation guards — the whole-batch fallback
when the reranker raises, and #361's per-row demotion when it returns a
score that cannot be ordered. The correctness argument for putting them
*there* is that one helper covers both call sites "by construction".

It did not. Measured on the branch that shipped #361: replacing the call in
``Searcher.search`` with a direct ``reranker.rerank(...)`` — discarding
**both** guards from the primary user-facing search path — left the entire
suite green, 3422 passed. Nothing pinned the wiring at all; every test of
``_safe_rerank`` calls it directly, so the helper was covered and its use
was not. That is CLAUDE.md's #278 shape: a surface several test files make
look covered.

"By construction" in this tree means the mistake cannot be made
(``_pool_leaks.pool_constructor_calls`` walks ``src/`` for constructors,
``_harness_lock.harness_lock_error`` walks each entry point for unlocked
database work). This is the same rule for the same reason: read ``src/``
and report any rerank call that is not the guarded one.

**It reads spelling, not intent**, like ``_search_family_rules``. A call
spelled ``<something>.rerank(...)`` outside ``_safe_rerank`` is reported;
one spelled ``_safe_rerank(...)`` is not. It cannot see a caller that
bypasses the helper by other means — assigning pre-computed scores, say —
so it is paired with a behavioural pin driving ``Searcher.search`` with a
NaN-emitting reranker, for the reason this tree pairs structural and
behavioural pins everywhere: either alone has a hole.

The AST, not the text, for ``_mentions_version_option``'s reason (#291):
the forbidden spelling is named in prose in this module, in its test file,
in ``_safe_rerank``'s own docstring and in the issue.
"""
from __future__ import annotations

import ast
import pathlib
from collections.abc import Mapping

#: The method name whose calls are governed. Both ``Reranker`` protocol
#: implementations and the stub rerankers in tests spell it this way.
RERANK_METHOD = "rerank"

#: The one function allowed to call it, and the module it lives in.
GUARD = "_safe_rerank"
GUARD_MODULE = "search/searcher.py"

#: Modules whose rerank calls are exempt, with the reason each is.
#:
#: ``search/reranker.py`` is where the call is *implemented* — the backend
#: delegates to fastembed's own ``rerank``, which is the thing being
#: guarded, not a use of it.
#:
#: ``cli.py`` warms the model at startup and **discards the result**, so no
#: score reaches a sort key. That exemption is conditional and this constant
#: cannot express the condition: if a warmup ever fed a page, it would need
#: the guard like any other caller.
EXEMPT_MODULES = frozenset({
    "search/reranker.py",
    "cli.py",
})


def source_files(root: pathlib.Path) -> dict[pathlib.Path, str]:
    """Every ``.py`` under ``root``, keyed by its path relative to it."""
    return {p.relative_to(root): p.read_text()
            for p in sorted(root.rglob("*.py"))}


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, str]:
    """Map every node to the name of the function that lexically contains it.

    Nested functions win over their parents, so a call inside a closure
    declared in ``_safe_rerank`` is attributed to the closure — which is the
    conservative reading: it is reported, and the author says why.
    """
    owner: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                owner[child] = node.name
    return owner


def unguarded_rerank_calls(
    sources: Mapping[pathlib.Path, str],
    *, exempt: frozenset[str] | None = None,
) -> list[str]:
    """Every ``.rerank(...)`` call that is not ``_safe_rerank``'s own.

    Returns ``"<path>:<line>"`` strings, sorted, so the report names the
    line to look at rather than only the file.

    ``exempt`` is a parameter rather than a direct read of
    :data:`EXEMPT_MODULES` so the rule can be driven against contrived
    trees — the real ``src/`` cannot violate it today, which is exactly why
    nothing tested this the first time.
    """
    skip = EXEMPT_MODULES if exempt is None else exempt
    found: list[str] = []
    for path, source in sources.items():
        if path.as_posix() in skip:
            continue
        tree = ast.parse(source)
        owner = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != RERANK_METHOD:
                continue
            if path.as_posix() == GUARD_MODULE and owner.get(node) == GUARD:
                continue
            found.append(f"{path.as_posix()}:{node.lineno}")
    return sorted(found)


def rerank_wiring_error(
    sources: Mapping[pathlib.Path, str],
    *, exempt: frozenset[str] | None = None,
) -> str | None:
    """Report rerank calls that bypass the guard, or ``None``.

    Shaped like ``account_names.account_name_error`` — a message, or
    ``None``, with the caller deciding what an error *is*.

    **Reports rather than passes when it would inspect nothing.** A rule
    that silently finds no rerank calls at all has been retired by a rename,
    which is the failure ``_harness_lock`` records: the guard would go quiet
    at the moment it stopped applying, rather than at the moment it started
    being violated.
    """
    if not any(RERANK_METHOD in source for source in sources.values()):
        return (
            f"no {RERANK_METHOD!r} call found anywhere in the tree — this rule "
            f"has been retired by a rename rather than satisfied; re-point it "
            f"at whatever {GUARD} now guards"
        )
    offenders = unguarded_rerank_calls(sources, exempt=exempt)
    if not offenders:
        return None
    return (
        f"rerank called outside {GUARD}: {', '.join(offenders)} — every rerank "
        f"must go through {GUARD}, which holds both degradation guards (the "
        f"whole-batch fallback on a raise, and the per-row demotion of a "
        f"score that cannot be ordered, #361). Calling the reranker directly "
        f"discards both, silently."
    )
