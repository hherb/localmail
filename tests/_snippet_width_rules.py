# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The snippet-width rule has one implementation: the AST says so (#390).

Pure — no IO, no pytest. ``snippet_width_duplication_error`` reports any
hand-written type or floor check on a snippet-width value in the modules
that consume the rule, or ``None`` when there is none.

**Why the AST, not a text scan.** The first cut of this pin grepped two
message literals in two modules. It had two holes, both found by mutation
rather than by reading: it scanned ``api/search_projection.py``, which this
change *moved the rule out of*, and not ``api/search.py``, which now holds
the capped gate — so an identically-worded copy at the real boundary passed
it; and its forbidden set held the two *type* wordings and neither **floor**
wording, so a verbatim third copy of the floor rule passed it too. A text
scan also cannot tell prose from code, and every module here explains #390
in its own docstring — the ``_mentions_version_option`` lesson this repo
already paid for once.

So the rule reads the *shape* a second copy must take. Whatever it is
worded, a hand-written check has to ask one of two questions about the
value: is it the right type (``isinstance``), or is it above the floor (an
ordering comparison against a number). Both are reported. The legitimate
answers live in ``search/snippet_width.py``, which is deliberately not
scanned.
"""
from __future__ import annotations

import ast
from collections.abc import Mapping

#: The modules that consume the rule and could therefore hold a second copy
#: of it: the capped gate, the uncapped gate, the boundary the rule used to
#: live in, and the route that owns the `Any` typing rationale.
CONSUMER_MODULES: tuple[str, ...] = (
    "localmail.api.search",
    "localmail.api.search_projection",
    "localmail.search.searcher",
    "localmail.serve.routes.search",
)

#: Ordering only. `snippet_chars is not None` is how both gates spell
#: "unstated", and an identity test is not a floor check.
_ORDERING = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)

_SUBJECT = "snippet"


def _is_subject(node: ast.expr) -> bool:
    """Does ``node`` name a snippet-width value?

    Covers a bare name (``snippet_chars``) and an attribute read
    (``cfg.snippet_max_chars``), which is how a copy at the route or in the
    Searcher would most naturally be spelled.
    """
    if isinstance(node, ast.Name):
        return _SUBJECT in node.id.lower()
    if isinstance(node, ast.Attribute):
        return _SUBJECT in node.attr.lower()
    return False


def _hand_checks(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "isinstance"
                and node.args and _is_subject(node.args[0])):
            found.append((node.lineno, "isinstance() type check"))
        elif isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators, strict=True):
                if not isinstance(op, _ORDERING):
                    continue
                # Either side: `snippet_chars < 1` and `n > cfg.snippet_max_chars`
                # are the same hand-written check, and requiring a literal on
                # the other side missed the second — a cap read off config,
                # which is how a copy at the route would most naturally read.
                if _is_subject(node.left) or _is_subject(right):
                    found.append((node.lineno, "ordering comparison (a floor or cap)"))
    return found


def snippet_width_duplication_error(sources: Mapping[str, str]) -> str | None:
    """Report a hand-written snippet-width check, or ``None``.

    ``sources`` maps module name to source text. A module named in
    ``CONSUMER_MODULES`` but absent from ``sources`` is **reported**, not
    skipped: a rename that silently shrinks the scanned set is the way this
    pin stops holding while staying green — which is how its predecessor
    came to read the one module that could no longer hold a copy.
    """
    missing = [name for name in CONSUMER_MODULES if name not in sources]
    if missing:
        return ("snippet width: these consumers were not scanned, so the rule "
                f"holds for less than it claims: {', '.join(missing)}")
    problems: list[str] = []
    for name in CONSUMER_MODULES:
        try:
            tree = ast.parse(sources[name])
        except SyntaxError as exc:  # pragma: no cover - a broken tree fails everywhere
            return f"snippet width: {name} does not parse: {exc}"
        problems += [f"{name}:{line}: {what}" for line, what in _hand_checks(tree)]
    if not problems:
        return None
    return ("snippet width: hand-written check(s) outside "
            "search/snippet_width.py — the rule is snippet_width_error, and a "
            "second copy is what #390 removed:\n  " + "\n  ".join(problems))
