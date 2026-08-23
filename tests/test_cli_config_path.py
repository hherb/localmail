# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Every CLI command that opens the database must honour `--config` (#245).

The bug these pin: `main` resolved `--config` into `ctx.obj["config_path"]`, but
the shared `_dsn()` helper called `load_config()` with **no argument** — so nine
commands silently ran against `~/.config/localmail/config.toml`, i.e. a
different database, a different attachment root, a different everything.

The probe fixture below trips the first DB call each command makes and records
the DSN it was handed, which is the only observable that distinguishes the two
configs.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from localmail.cli import main

#: Distinguishable DSNs. Neither is ever connected to — the probe raises first.
NAMED_DSN = "postgresql:///localmail_named_config"
DEFAULT_DSN = "postgresql:///localmail_default_config"

#: Every command whose handler resolves a DSN. The issue named five (the ones
#: that call `load_config()` *as well*); these four also route through `_dsn()`
#: and were equally affected.
# Entries are argv fragments, not bare names: the API-key and API-user commands
# take a NAME, and click would reject the invocation before the DSN is resolved.
DSN_CONSUMING_COMMANDS = [
    "extract-backfill",
    "embed-backfill",
    "lang-backfill",
    "search-status",
    "estimate-upgrade",
    "list-failed-embeddings",
    "retry-failed-embeddings",
    "list-failed-extractions",
    "retry-failed-extractions",
    "add-api-key probe_bot",
    "list-api-keys",
    "revoke-api-key probe_bot",
    "remove-api-key probe_bot",
    "add-api-user probe_user --password pw",
    "list-api-users",
    "remove-api-user probe_user",
]


class _ProbeTripped(RuntimeError):
    """Raised by the fake DB entry points instead of connecting."""


@pytest.fixture
def dsn_probe(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the DSN handed to whichever DB entry point the command reaches.

    Two entry points because `estimate-upgrade` uses `psycopg.connect` directly
    while the rest go through `open_pool`. Both raise rather than connect, so no
    command runs past the one thing under test.
    """
    seen: list[str] = []

    def _record(dsn: str, *args: object, **kwargs: object) -> None:
        seen.append(dsn)
        raise _ProbeTripped(dsn)

    monkeypatch.setattr("localmail.db.open_pool", _record)
    monkeypatch.setattr("psycopg.connect", _record)
    # `embed-backfill` and `lang-backfill` build a model before touching the DB.
    # Neither is what is under test, and loading them costs hundreds of MB.
    monkeypatch.setattr(
        "localmail.search.lang_detect.make_detector", lambda cfg: object()
    )
    monkeypatch.setattr("localmail.cli._make_backend", lambda cfg: object())
    return seen


@pytest.fixture
def two_configs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point `$LOCALMAIL_CONFIG` at one config and return the path of another.

    `$LOCALMAIL_CONFIG` is what `default_config_path()` resolves to, so it plays
    the role of "the config the operator did *not* ask for".
    """
    default = tmp_path / "default.toml"
    default.write_text(f'[database]\ndsn = "{DEFAULT_DSN}"\n')
    monkeypatch.setenv("LOCALMAIL_CONFIG", str(default))
    named = tmp_path / "named.toml"
    named.write_text(f'[database]\ndsn = "{NAMED_DSN}"\n')
    return named


@pytest.mark.parametrize("command", DSN_CONSUMING_COMMANDS)
def test_config_option_selects_the_database(
    command: str, dsn_probe: list[str], two_configs: Path
) -> None:
    """`localmail --config PATH <command>` must reach the DB named by PATH."""
    result = CliRunner().invoke(main, ["--config", str(two_configs), *command.split()])
    assert isinstance(result.exception, _ProbeTripped), result.output
    assert dsn_probe == [NAMED_DSN]


@pytest.mark.parametrize("command", DSN_CONSUMING_COMMANDS)
def test_without_the_option_the_default_config_is_still_used(
    command: str, dsn_probe: list[str], two_configs: Path
) -> None:
    """The counterpart: threading `--config` through must not break the far more
    common invocation that omits it."""
    result = CliRunner().invoke(main, command.split())
    assert isinstance(result.exception, _ProbeTripped), result.output
    assert dsn_probe == [DEFAULT_DSN]
