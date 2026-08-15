# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The rule that keeps the version diagnostic off a help screen (#307).

Since #304 the `main` group callback reports an unresolvable version on
behalf of the 36 commands that do not report for themselves — and click
resolves the subcommand *before* applying its `--help`, so the line landed
ahead of the help text an operator had explicitly asked to read. Bare
`localmail`, `localmail --help` and an unknown command were already quiet
(`no_args_is_help` short-circuits ahead of the callback), so the reporting
`<cmd> --help` was the odd one out.
"""
from __future__ import annotations

import pytest

from localmail.cli_help_request import is_help_request

_NAMES = ("--help",)


@pytest.mark.parametrize("args", [
    ["--help"],
    ["--account", "work", "--help"],
    ["status", "--help"],          # a nested group's help
])
def test_a_pending_help_option_is_a_help_request(args: list[str]) -> None:
    assert is_help_request(args, _NAMES) is True


@pytest.mark.parametrize("args", [
    [],                            # `localmail sync`
    ["--account", "work"],
    ["--limit-per-folder", "50"],
    ["status"],
])
def test_an_ordinary_invocation_is_not(args: list[str]) -> None:
    assert is_help_request(args, _NAMES) is False


def test_it_honours_the_context_s_own_help_option_names() -> None:
    """Read from `ctx.help_option_names`, never hardcoded.

    click lets a project add `-h`, and a rule that hardcoded `--help` would
    stay silent for the spelling that project's operators actually type.
    """
    assert is_help_request(["-h"], ("-h", "--help")) is True
    assert is_help_request(["-h"], _NAMES) is False


def test_the_scan_stops_at_a_bare_double_dash() -> None:
    """`--` ends option parsing, so a later `--help` is a value, not a flag.

    The only place this rule can be honest for free: past `--` click itself
    would never treat the token as the help option, so neither may we.
    """
    assert is_help_request(["--", "--help"], _NAMES) is False
    assert is_help_request(["--help", "--"], _NAMES) is True
