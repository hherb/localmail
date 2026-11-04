# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The `localmail` console script: the CLI, imported inside a guard (#305).

`cli.py` imports the daemon — and so `sqlparse`, `psycopg`, `keyring` — at
module scope, so a partial `uv sync` that dropped any third-party dependency
killed every command with a bare traceback before click parsed anything.
That included `--version`, the manual's install-verification step, i.e. the
command an operator runs *because* the install looks broken.

The fix is here rather than in `cli.py`'s 42 commands: deferring those imports
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
    """What to print, and how to exit, when the CLI could not be imported.

    `exit_code` is `None` when the failure must keep its traceback: print
    `stderr` (if any) and re-raise.
    """

    stdout: str | None
    stderr: str
    exit_code: int | None


def _top_package(module: str) -> str:
    return module.split(".", 1)[0]


def _raising_module(exc: BaseException) -> str | None:
    """The module whose code raised `exc`, skipping the import machinery.

    psycopg reports a missing wheel or libpq as an `ImportError` with no
    `.name` ("no pq wrapper available"), and pydantic a version mismatch as a
    `SystemError`; in both the raise sits in the dependency's own frame.
    """
    module = None
    tb = exc.__traceback__
    while tb is not None:
        name = tb.tb_frame.f_globals.get("__name__")
        if isinstance(name, str) and _top_package(name) != "importlib":
            module = name
        tb = tb.tb_next
    return module


def _is_dependency(exc: Exception) -> bool:
    """True when the failure is in a third-party package, not in localmail.

    `ImportError.name` names the module that could not be imported — or, for
    `from psycopg import x`, the package `x` was missing from. Without one,
    the module that raised decides. A failure inside localmail is a bug in
    this tree that `uv sync` would not fix, so it keeps its traceback; so does
    a failure the machinery raised on our import of a dependency (a truncated
    or unreadable file), since its last non-importlib frame is ours.

    Imprecise in one direction, deliberately: a localmail import naming a
    package that is not there (a typo like `from psycopg_pool import Typo`, or
    a dependency never declared) reads as a broken dependency. The line still
    names the module, and `uv sync` failing to fix it is the tell.
    """
    if isinstance(exc, ImportError) and exc.name:
        module: str | None = exc.name
    else:
        module = _raising_module(exc)
    return module is not None and _top_package(module) != _OWN_PACKAGE


def _asks_for_version(argv: Sequence[str]) -> bool:
    # A token after a bare `--` is an argument, never the flag. Imprecise in
    # one direction only: `localmail sync --version` prints the version here,
    # where click would refuse the option.
    before = argv[: argv.index("--")] if "--" in argv else argv
    return "--version" in before


def _render(exc: Exception) -> str:
    # This runs on a path that must not fail; render_exception_chain's
    # contract leaves guarding the walk to its caller.
    try:
        return render_exception_chain(exc)
    except Exception:
        return type(exc).__name__


def startup_failure_report(
    exc: Exception,
    *,
    argv: Sequence[str],
    version: str,
    version_diagnostic: str | None,
) -> StartupFailure:
    """Decide what a failed CLI import tells the operator. Pure.

    An unresolvable version is reported on every branch (#304): the group
    callback that normally does it never runs when the CLI cannot import.
    """
    dependency = _is_dependency(exc)
    cause = _render(exc)
    line = (
        f"{_PROG}: cannot start: {cause}. A dependency is missing or broken; "
        f"repair the install with `{REPAIR_COMMAND}`."
        if dependency
        else f"{_PROG}: cannot start: {cause}"
    )
    prefix = [] if version_diagnostic is None else [version_diagnostic]
    if _asks_for_version(argv):
        return StartupFailure(
            stdout=f"{_PROG}, version {version}",
            stderr="\n".join([*prefix, line]),
            exit_code=0,
        )
    if not dependency:
        return StartupFailure(stdout=None, stderr="\n".join(prefix), exit_code=None)
    return StartupFailure(
        stdout=None,
        stderr="\n".join([*prefix, line]),
        exit_code=DEPENDENCY_EXIT_CODE,
    )


def main() -> None:
    """The console script and `python -m localmail`."""
    try:
        from .cli import main as cli_main
    # Not just ImportError: a half-upgraded package can raise anything at
    # import (pydantic raises SystemError), and --version must still answer.
    except Exception as exc:
        report = startup_failure_report(
            exc,
            argv=sys.argv[1:],
            version=__version__,
            version_diagnostic=__version_diagnostic__,
        )
        if report.stdout is not None:
            print(report.stdout)
        if report.stderr:
            print(report.stderr, file=sys.stderr)
        if report.exit_code is None:
            raise
        sys.exit(report.exit_code)
    cli_main()
