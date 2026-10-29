# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""No ``{@html}`` sink in the GUI's Svelte components (#395).

#391 deleted the only one and pinned its absence with a single component
test on a single field, so ``{@html subject}`` on another component was
caught by nothing. The rule under test is
``_html_sink_rules.html_sink_error``.

The contrived-source tests matter more than the one against ``gui/src``:
the real tree has zero sinks today, so a rule that always returned ``None``
would pass the production check and nothing else.
"""
from __future__ import annotations

import pathlib

from tests._html_sink_rules import ALLOWED_SINKS, html_sink_error, svelte_sources

_GUI_SRC = pathlib.Path(__file__).resolve().parents[1] / "gui" / "src"
_ROW = pathlib.Path("components/Row.svelte")


def test_the_gui_has_no_html_sink() -> None:
    sources = svelte_sources(_GUI_SRC)
    # Non-vacuous: a moved tree would scan nothing and pass.
    assert pathlib.Path("components/MessageListRow.svelte") in sources
    assert html_sink_error(sources, allowed=ALLOWED_SINKS) is None


def test_the_allowlist_is_empty_today() -> None:
    assert ALLOWED_SINKS == frozenset()


def test_a_sink_is_reported_with_its_line() -> None:
    src = "<script>let s = '';</script>\n<p>{@html s}</p>\n"
    problem = html_sink_error({_ROW: src}, allowed=frozenset())
    assert problem is not None
    assert "components/Row.svelte:2" in problem


def test_whitespace_after_the_brace_is_still_a_sink() -> None:
    # Svelte's parser calls allow_whitespace() after `{`, so this compiles.
    problem = html_sink_error({_ROW: "<p>{ @html s}</p>"}, allowed=frozenset())
    assert problem is not None
    assert "components/Row.svelte:1" in problem


def test_every_sink_is_reported_not_just_the_first() -> None:
    src = "{@html a}\n{@html b}\n"
    problem = html_sink_error({_ROW: src}, allowed=frozenset())
    assert problem is not None
    assert "Row.svelte:1" in problem and "Row.svelte:2" in problem


def test_binding_inner_or_outer_html_is_a_sink() -> None:
    src = ("<div contenteditable bind:innerHTML={a}></div>\n"
           "<div contenteditable bind:outerHTML={b}></div>\n")
    problem = html_sink_error({_ROW: src}, allowed=frozenset())
    assert problem is not None
    assert "Row.svelte:1" in problem and "Row.svelte:2" in problem


def test_binding_text_content_is_not_a_sink() -> None:
    src = "<div contenteditable bind:textContent={a}></div>\n"
    assert html_sink_error({_ROW: src}, allowed=frozenset()) is None


def test_text_interpolation_is_not_a_sink() -> None:
    src = "<p>{snippet}</p>\n<p>email me @html.example</p>\n"
    assert html_sink_error({_ROW: src}, allowed=frozenset()) is None


def test_an_allowlisted_file_is_not_reported() -> None:
    assert html_sink_error({_ROW: "{@html s}"}, allowed=frozenset({_ROW})) is None


def test_an_empty_scan_is_reported_rather_than_passed() -> None:
    problem = html_sink_error({}, allowed=frozenset())
    assert problem is not None
    assert "no .svelte" in problem
