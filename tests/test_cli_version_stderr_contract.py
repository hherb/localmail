# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""`localmail --version` already has a machine-readable channel (#300).

stderr is non-empty if and only if the version could not be resolved. That has
been true since #291 but was an accident of implementation rather than a stated
contract, and nothing pinned it — so a future refactor could move the line to
stdout, or drop it, without a failing test, and every script checking it would
silently start reporting healthy.

No behaviour changes here. stdout stays the single machine-readable line and
the exit status stays 0, both of which scripts depend on.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from localmail.cli import main
from localmail.version_report import VersionSource, unknown_version_diagnostic

_UNRESOLVABLE = [
    VersionSource.NOT_INSTALLED,
    VersionSource.METADATA_INCOMPLETE,
    VersionSource.METADATA_UNREADABLE,
]


def test_a_healthy_resolution_says_nothing_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("localmail.cli.__version_diagnostic__", None)
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.strip()


@pytest.mark.parametrize("source", _UNRESOLVABLE, ids=lambda s: s.value)
def test_every_unresolvable_source_writes_to_stderr(
    source: VersionSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    detail = "boom" if source is VersionSource.METADATA_UNREADABLE else None
    diagnostic = unknown_version_diagnostic(source, detail=detail)
    monkeypatch.setattr("localmail.cli.__version_diagnostic__", diagnostic)

    result = CliRunner().invoke(main, ["--version"])

    assert result.exit_code == 0, "exit 0 is contract — scripts use it as liveness"
    assert result.stderr.strip(), f"{source.value} must be reported on stderr"


def test_the_diagnostic_never_reaches_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdout is parsed by the manual's install-verification step."""
    diagnostic = unknown_version_diagnostic(VersionSource.NOT_INSTALLED, detail=None)
    monkeypatch.setattr("localmail.cli.__version_diagnostic__", diagnostic)

    result = CliRunner().invoke(main, ["--version"])

    assert diagnostic not in result.stdout
    assert len(result.stdout.strip().splitlines()) == 1
