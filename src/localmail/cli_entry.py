# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The `localmail` console script: the CLI, imported inside a guard (#305).

`cli.py` imports the daemon — and so `sqlparse`, `psycopg`, `keyring` — at
module scope, so a partial `uv sync` that dropped any third-party dependency
killed every command with a bare traceback before click parsed anything.
That included `--version`, the manual's install-verification step, i.e. the
command an operator runs *because* the install looks broken.

The fix is here rather than in `cli.py`'s 38 commands: deferring those imports
into each command body would move the failure from startup to first use, and
would have to be re-learned by every command added later. Guarding the one
import keeps the failure at startup and makes it legible for all of them.

This module must import nothing third-party: `localmail` and
`version_report` are stdlib-only, which is what lets it run on the very
install it is diagnosing.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass

from . import __version__, __version_diagnostic__
from .version_report import render_exception_chain

#: The repair, worded as every other remedy in the tree words it — `--extra`
#: matters, since a bare `uv sync` uninstalls the extras (see CLAUDE.md).
REPAIR_COMMAND = "uv sync --extra extraction --extra mcp"

#: A command that cannot start. `--version` keeps exit 0 regardless: scripts
#: use it as a liveness check, and its stderr carries the diagnosis (#291).
DEPENDENCY_EXIT_CODE = 1

#: click's own prog name for the console script. `python -m localmail` would
#: read differently under click, but the manual's check runs the script.
_PROG = "localmail"

_OWN_PACKAGE = "localmail"


@dataclass(frozen=True)
class StartupFailure:
    """What to print, and how to exit, when the CLI could not be imported."""

    stdout: str | None
    stderr: str
    exit_code: int


def _is_dependency(exc: ImportError) -> bool:
    """True when the module that failed is not ours.

    `ImportError.name` names the module that could not be imported — or, for
    `from psycopg import x`, the package `x` was missing from. A failure inside
    localmail is a bug in this tree, and `uv sync` would not fix it, so it is
    not reported as one; a nameless `ImportError` is not either.
    """
    if not exc.name:
        return False
    return exc.name.split(".", 1)[0] != _OWN_PACKAGE


def _asks_for_version(argv: Sequence[str]) -> bool:
    # A token after a bare `--` is an argument, never the flag. Imprecise in
    # one direction only: `localmail sync --version` prints the version here,
    # where click would refuse the option.
    before = argv[: argv.index("--")] if "--" in argv else argv
    return "--version" in before


def startup_failure_report(
    exc: ImportError,
    *,
    argv: Sequence[str],
    version: str,
    version_diagnostic: str | None,
) -> StartupFailure | None:
    """Decide what a failed CLI import tells the operator. Pure.

    Returns `None` when the failure should keep its traceback: a non-version
    command hit an import bug in localmail itself.
    """
    dependency = _is_dependency(exc)
    cause = render_exception_chain(exc)
    line = (
        f"{_PROG}: cannot start: {cause}. A dependency is missing or broken; "
        f"repair the install with `{REPAIR_COMMAND}`."
        if dependency
        else f"{_PROG}: cannot start: {cause}"
    )
    if _asks_for_version(argv):
        lines = [line] if version_diagnostic is None else [version_diagnostic, line]
        return StartupFailure(
            stdout=f"{_PROG}, version {version}",
            stderr="\n".join(lines),
            exit_code=0,
        )
    if not dependency:
        return None
    return StartupFailure(stdout=None, stderr=line, exit_code=DEPENDENCY_EXIT_CODE)


def main() -> None:
    """The console script and `python -m localmail`."""
    try:
        from .cli import main as cli_main
    except ImportError as exc:
        report = startup_failure_report(
            exc,
            argv=sys.argv[1:],
            version=__version__,
            version_diagnostic=__version_diagnostic__,
        )
        if report is None:
            raise
        if report.stdout is not None:
            print(report.stdout)
        print(report.stderr, file=sys.stderr)
        sys.exit(report.exit_code)
    cli_main()
