# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Integration tests for `localmail estimate-upgrade` (issue #2)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from localmail.cli import main


def test_cli_estimate_upgrade_human_output(cli_config, db_conn):
    """Default text output is human-readable and contains the revision
    name, status, and a duration string."""
    runner = CliRunner()
    result = runner.invoke(main, ["estimate-upgrade"])
    assert result.exit_code == 0, result.output
    assert "0006_search_indexes" in result.output
    # Either "applied" (fixture state) or "pending" (clean DB) is fine.
    assert ("applied" in result.output) or ("pending" in result.output)


def test_cli_estimate_upgrade_json_output(cli_config, db_conn):
    """--format json emits a parseable list with all wire fields present."""
    runner = CliRunner()
    result = runner.invoke(main, ["estimate-upgrade", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) >= 1
    row = payload[0]
    for key in (
        "revision",
        "status",
        "current_bytes",
        "projected_bytes",
        "projected_duration_s",
        "warnings",
    ):
        assert key in row, f"missing key {key!r} in {row!r}"
    assert row["revision"] == "0006_search_indexes"


def test_cli_estimate_upgrade_db_unreachable(monkeypatch, tmp_path):
    """Bad DSN -> exit 1 (Click idiom) + clear error on stderr.

    Pins the wire contract documented in the Failure modes table of
    docs/superpowers/specs/2026-05-27-large-archive-upgrade-estimator-design.md:
    `click.ClickException` exits 1, matching every other `localmail`
    subcommand. Scripts that need a structured channel use
    `--format json`; the exit code is not differentiated from other
    CLI errors.
    """
    stub = tmp_path / "config.toml"
    stub.write_text('[database]\ndsn = "postgresql://unreachable:1/no_such_db"\n')
    monkeypatch.setenv("LOCALMAIL_CONFIG", str(stub))

    runner = CliRunner()
    result = runner.invoke(main, ["estimate-upgrade"])
    assert result.exit_code == 1, (
        f"expected exit 1 (Click idiom), got {result.exit_code}: {result.output!r}"
    )
    # Don't pin the exact wording — psycopg's connection-error string
    # varies by platform. Broaden the check to cover common forms:
    # "connect", "could not", "resolve", "no such host", etc.
    output_lower = result.output.lower()
    assert any(
        kw in output_lower
        for kw in ("connect", "could not", "resolve", "no such host", "error")
    )
