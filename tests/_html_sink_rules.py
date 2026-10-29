# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A pure rule forbidding ``{@html}`` sinks in the GUI's Svelte files (#395).

#391 deleted the GUI's only ``{@html}`` sink and pinned its absence with one
component test on one field. That made "no HTML sink in ``gui/src``" a
tree-wide property held by nothing tree-wide: ``{@html subject}`` on another
component would pass ``svelte-check`` and every test. This is the
``_rerank_wiring_rules`` arrangement for the GUI: read every ``.svelte``
file and report each sink.

**Text, not a parse tree**, unlike the Python rules, which walk the AST so
prose about the forbidden spelling is not read as a use. Parsing Svelte
from Python would need a second parser, and the cost of the text match is
one-directional: a ``{@html`` inside an HTML comment or a ``<script>``
string is reported, i.e. the rule fails closed. ``.svelte`` files carry no
such prose today.

The pattern allows whitespace after the brace because Svelte's parser does
(``allow_whitespace()`` right after ``{`` in its ``tag.js``), so ``{ @html
x}`` compiles to the same sink.
"""
from __future__ import annotations

import pathlib
import re
from collections.abc import Mapping

HTML_SINK = re.compile(r"\{\s*@html\b")

#: Files (relative to ``gui/src``) allowed to carry a sink. Empty: rendering
#: message HTML is the reading pane's sandboxed iframe ``srcdoc``, which is
#: the precedent a new HTML surface should follow instead.
ALLOWED_SINKS: frozenset[pathlib.Path] = frozenset()


def svelte_sources(root: pathlib.Path) -> dict[pathlib.Path, str]:
    """Every ``.svelte`` under ``root``, keyed by its path relative to it."""
    return {p.relative_to(root): p.read_text()
            for p in sorted(root.rglob("*.svelte"))}


def html_sink_error(sources: Mapping[pathlib.Path, str], *,
                    allowed: frozenset[pathlib.Path]) -> str | None:
    """Name every ``{@html}`` sink outside ``allowed``, or ``None``.

    An empty ``sources`` is reported rather than passed: a moved ``gui/src``
    would otherwise scan nothing and stay green.
    """
    if not sources:
        return "no .svelte files found to scan for {@html} sinks"
    hits = [f"{path}:{src.count(chr(10), 0, m.start()) + 1}"
            for path, src in sorted(sources.items()) if path not in allowed
            for m in HTML_SINK.finditer(src)]
    if not hits:
        return None
    return ("{@html} renders a string as markup; render it as text, or add "
            "the file to ALLOWED_SINKS with the reason: " + ", ".join(hits))
