# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Is the pending invocation a request for help? (#307)

Since #304 the ``main`` group callback reports an unresolvable version on
behalf of the 36 commands that do not report for themselves. click resolves
the subcommand *before* applying its ``--help``, so that report landed ahead
of the help text an operator had explicitly asked to read — while bare
``localmail``, ``localmail --help`` and an unknown command stayed quiet,
because ``no_args_is_help`` short-circuits ahead of the callback. Help does
no archive work and touches neither config nor database; the decision was to
make all four shapes quiet rather than the other three loud.

Pure, and separate from ``cli.py``, because the question is worth asking in
isolation: the callback that consumes it cannot see the arguments at all (by
the time click runs it, the subcommand's own arguments have been taken off
the context), so the plumbing that fetches them and the rule that judges them
are best able to be wrong independently.
"""
from __future__ import annotations

from collections.abc import Sequence

#: click's own end-of-options marker. Past it every token is a value.
_END_OF_OPTIONS = "--"


def is_help_request(
    args: Sequence[str], help_option_names: Sequence[str]
) -> bool:
    """True when ``args`` asks click for help rather than for work.

    ``args`` is the *pending* argument list — what the resolved subcommand has
    not parsed yet — and ``help_option_names`` is ``ctx.help_option_names``,
    read from the context rather than hardcoded: click lets a project add
    ``-h``, and a rule spelling ``--help`` itself would stay silent for
    whichever spelling that project's operators actually type.

    The scan stops at a bare ``--``, since click would not treat a later token
    as the help option either.

    **Known imprecision, deliberate.** A help token consumed as an option
    *value* (``localmail sync --account --help``) reads as a help request
    here, because judging otherwise means knowing every option's arity — i.e.
    re-implementing click's parser against a command set this module cannot
    see. The cost is one suppressed diagnostic on a pathological invocation;
    the alternative is a second parser to keep in step with the first.
    """
    wanted = frozenset(help_option_names)
    for arg in args:
        if arg == _END_OF_OPTIONS:
            return False
        if arg in wanted:
            return True
    return False
