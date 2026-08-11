# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Every long-running process says so when it could not resolve its version (#295).

#291 fixed `localmail --version`, and only that. It left `__version_diagnostic__`
with exactly **one** reader, so on a headless host — the case the remedy text was
written for — an operator running `serve` or `run` got nothing: `/v1/version`
answered `0.0.0+unknown` as if it were a version, the GUI decoded it and rendered
it in the About tab, and the server log said nothing at all. That is #291
verbatim, one reader over.

The fix is deliberately **not** a wire change. The GUI's connect probe decodes
`server_version` as a non-optional String, which is precisely *why* the sentinel
exists rather than a null; and a new key nothing renders is how #278 happened
from the other end (the About tab renders a `build_hash` the server has never
emitted, with four test files and a Rust `#[cfg(test)]` module mocking it into
looking covered). One ERROR at startup puts the signal where the operator of a
headless host actually looks.

These tests pin the **reach** — that each entry point consults the diagnostic.
What the diagnostic *says* is pinned in `test_version_report.py`; that the CLI
puts it on stderr rather than stdout is pinned in `test_version_single_source.py`.
"""
from __future__ import annotations

import ast
import logging
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from localmail.cli import main
from localmail.config import LocalmailConfig, McpConfig, ServeConfig
from localmail.daemon import Daemon
from localmail.retry import RetryAborted
from localmail.serve.app import create_app
from localmail.version_report import log_version_diagnostic

#: Stands in for a rendered diagnostic. Deliberately **not** a real one: these
#: tests assert the string is carried, and rebuilding it here from
#: `unknown_version_diagnostic` would let a startup path that reports the wrong
#: source still pass.
_PROBE = "warning: version unresolvable <startup-reach-probe>"

#: Each entry point imports the attribute into its own module namespace with
#: `from … import`, so a test must rebind the binding the code actually reads —
#: rebinding `localmail.__version_diagnostic__` reaches neither. The same trap
#: `_print_version`'s docstring records for `__version__`.
_READERS = {
    "daemon": ("localmail.daemon.__version_diagnostic__", "localmail.daemon"),
    "serve": ("localmail.serve.app.__version_diagnostic__", "localmail.serve"),
}

#: A DSN nothing answers on. `create_app` never dials it during construction;
#: `Daemon.__init__` does, which is what the ordering test below exploits.
_DEAD_DSN = "postgresql://localmail@127.0.0.1:1/nope"

#: Where each reader must get the diagnostic *from*. Rebinding a module global
#: is satisfied by any attribute of that name, so the reach tests below cannot
#: see the difference between importing the package's value and defining a
#: module-local `None` — see the AST pin.
_READER_SOURCES = {
    "src/localmail/daemon.py": ("localmail", None),
    "src/localmail/serve/app.py": ("localmail", None),
}

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _construct(reader: str, db_dsn: str) -> None:
    """Build the entry point under test, then release whatever it opened.

    `finally`, not a trailing `.close()`: both constructors open a pool part-way
    through, so a raise after that point would leak it along with its background
    threads — and a leaked writer is what makes `db_conn`'s TRUNCATE deadlock in
    a later test rather than here.
    """
    if reader == "daemon":
        cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
        cfg.search.run_embed_worker = False
        cfg.search.run_extract_worker = False
        daemon = Daemon(cfg, dsn=db_dsn, ssl=False)
        try:
            return
        finally:
            daemon.pool.close()
    app = create_app(db_dsn=db_dsn, searcher=None)
    try:
        return
    finally:
        app.state.pool.close()


def _dsn_for(reader: str, request: pytest.FixtureRequest) -> str:
    """The DSN each reader needs to construct.

    `serve` needs none — `create_app` opens its pool lazily, so a dead DSN
    constructs fine. Requesting `db_dsn` there bought a skip on a PG-less
    checkout and a TRUNCATE this file has no use for, for nothing. Only the
    daemon genuinely touches Postgres during construction
    (`_load_syncable_accounts` sizes the pool from the account count), so only
    the daemon parametrisation skips without one.
    """
    return request.getfixturevalue("db_dsn") if reader == "daemon" else _DEAD_DSN


class _Collect(logging.Handler):
    """Attach directly rather than via `caplog` so the *level* can be asserted
    without depending on the root logger's configuration."""

    def __init__(self, sink: list[logging.LogRecord]) -> None:
        super().__init__(level=logging.DEBUG)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self._sink.append(record)


def _records_from(diagnostic: str | None) -> list[logging.LogRecord]:
    """Everything `log_version_diagnostic` emits for `diagnostic`, on a throwaway
    logger so no other test's handlers see it."""
    log = logging.getLogger(f"localmail.test.version-{diagnostic is None}")
    records: list[logging.LogRecord] = []
    log.addHandler(_Collect(records))
    log.setLevel(logging.DEBUG)
    try:
        log_version_diagnostic(log, diagnostic)
    finally:
        log.handlers.clear()
    return records


def test_a_healthy_install_logs_nothing() -> None:
    """`None` is the healthy answer and must not become an empty WARNING — a
    line that fires on every start is a line operators learn to skip."""
    assert _records_from(None) == []


def test_an_unresolvable_version_is_reported_at_error() -> None:
    """ERROR, not WARNING, and not INFO.

    `localmail run --log-level ERROR` is an offered `click.Choice`, and
    `run_cmd` calls `basicConfig` with it *before* constructing the daemon — so
    at WARNING this line was filtered out entirely, with `basicConfig`'s root
    handler also removing the `logging.lastResort` escape that saves the serve
    path. A report the process can be told to discard is not a report, and this
    is the level that the CLI cannot be asked to go above.
    """
    records = _records_from(_PROBE)
    assert [r.levelno for r in records] == [logging.ERROR]
    assert _PROBE in records[0].getMessage()


def test_the_report_survives_the_quietest_log_level_the_cli_offers() -> None:
    """The defect above, stated as the operator sees it rather than as a level.

    Reads the choices off `run_cmd` itself, so adding a quieter one (`CRITICAL`)
    fails here rather than silently reintroducing the hole.
    """
    from localmail.cli import run_cmd

    choices = next(
        p.type.choices for p in run_cmd.params if p.name == "log_level"
    )
    quietest = max(choices, key=lambda name: getattr(logging, name))
    log = logging.getLogger("localmail.test.version-quietest")
    records: list[logging.LogRecord] = []
    log.addHandler(_Collect(records))
    log.setLevel(getattr(logging, quietest))
    try:
        log_version_diagnostic(log, _PROBE)
    finally:
        log.handlers.clear()
    assert [r.getMessage() for r in records] == [_PROBE], (
        f"the diagnostic is filtered out at --log-level {quietest}"
    )


@pytest.mark.parametrize("module", sorted(_READER_SOURCES))
def test_each_startup_reader_takes_the_diagnostic_from_the_package(
    module: str,
) -> None:
    """The reach tests rebind each reader's module global, and `monkeypatch.
    setattr` is satisfied by *any* attribute of that name — so they pass equally
    against a module-local `__version_diagnostic__ = None` that never came from
    the package. Both readers could ship permanently silent with the whole file
    green, which is the "#278 mocked into looking covered" shape this module's
    own docstring cites.

    An identity assertion cannot close it either: on a healthy install both
    sides are `None`, so it passes against that mutation too. Source is the only
    place the derivation is visible, which is why
    `test_cli_does_not_reintroduce_click_version_option` walks the AST as well.
    """
    expected_module, _ = _READER_SOURCES[module]
    tree = ast.parse((_REPO_ROOT / module).read_text())
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and any(a.name == "__version_diagnostic__" for a in node.names)
    ]
    assert imports, f"{module} does not import __version_diagnostic__ at all"
    # `from . import …` inside the package, or `from localmail import …`.
    assert all(
        node.module in (None, expected_module) for node in imports
    ), f"{module} imports __version_diagnostic__ from somewhere other than the package"


@pytest.mark.parametrize("reader", sorted(_READERS))
def test_a_startup_path_reports_an_unresolvable_version(
    reader: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The #295 defect itself: `serve` and `run` were silent on exactly the
    path (`NOT_INSTALLED`) the remedy text was written for."""
    attribute, logger_name = _READERS[reader]
    monkeypatch.setattr(attribute, _PROBE)
    with caplog.at_level("ERROR", logger=logger_name):
        _construct(reader, _dsn_for(reader, request))
    matching = [r for r in caplog.records if _PROBE in r.getMessage()]
    assert matching
    # The logger name is the operator's grep target, and `caplog` collects at
    # the root regardless of it — so without this, logging the line under any
    # other name passes every assertion in this file.
    assert {r.name for r in matching} == {logger_name}


@pytest.mark.parametrize("reader", sorted(_READERS))
def test_a_startup_path_stays_quiet_when_the_version_is_known(
    reader: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The overwhelmingly common case, pinned separately: "log it
    unconditionally" satisfies the test above and is the mutation that matters.
    """
    attribute, logger_name = _READERS[reader]
    monkeypatch.setattr(attribute, None)
    with caplog.at_level("ERROR", logger=logger_name):
        _construct(reader, _dsn_for(reader, request))
    assert [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith(logger_name)
    ] == []


def test_the_daemon_reports_before_it_waits_for_postgres(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """Ordering, not just presence — and this is a claim the comment makes.

    `Daemon.__init__` blocks in `retry_with_backoff` until Postgres answers. An
    operator whose install is damaged enough to lose its version has a fair
    chance of a host that is broken in other ways too, so reporting *after* that
    gate would withhold the one line explaining why the running deploy cannot be
    identified for as long as the DB stays down — which is unbounded.

    Driven by pointing the daemon at a closed port with the stop event already
    set, so the first attempt fails and the retry aborts immediately.
    """
    monkeypatch.setattr(_READERS["daemon"][0], _PROBE)
    dead = _DEAD_DSN
    cfg = LocalmailConfig.model_validate(
        {
            "database": {"dsn": dead},
            "daemon": {"startup_backoff_initial_s": 0.01, "startup_backoff_max_s": 0.02},
        }
    )
    stop = threading.Event()
    stop.set()
    with caplog.at_level("ERROR", logger="localmail.daemon"):
        with pytest.raises(RetryAborted):
            Daemon(cfg, dsn=dead, ssl=False, stop_event=stop)
    assert [r for r in caplog.records if _PROBE in r.getMessage()], (
        "the version diagnostic must be logged before the Postgres wait, not after"
    )


def test_serve_reports_before_it_rejects_a_misconfiguration(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """Same ordering claim on the serve half.

    `create_app` fails loud on `authorization_server_enabled` without a
    `state_signing_key`, before opening the pool. An operator reading that
    startup failure needs to know the version reported beside it is a sentinel —
    a diagnostic emitted after the raise is a diagnostic never emitted.
    """
    monkeypatch.setattr(_READERS["serve"][0], _PROBE)
    with caplog.at_level("ERROR", logger="localmail.serve"):
        with pytest.raises(ValueError, match="state_signing_key"):
            create_app(
                db_dsn=_DEAD_DSN,
                searcher=None,
                serve_config=ServeConfig(),
                mcp_config=McpConfig(authorization_server_enabled=True),
                enable_mcp=True,
            )
    assert [r for r in caplog.records if _PROBE in r.getMessage()], (
        "the version diagnostic must be logged before the config check, not after"
    )


def test_serve_reports_before_its_schema_check_rejects_an_unreachable_db(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """`create_app` being first is not enough — `serve_cmd` never reaches it.

    The command checks `pending_migrations(dsn)` first and raises
    `ClickException("… Is Postgres reachable?")` on failure, so on an
    unreachable database the version diagnostic inside `create_app` was never
    emitted at all. That is the half of #295 a headless host is most likely to
    hit, and it is the same reasoning `Daemon.__init__` already acts on: an
    install damaged enough to lose its version often sits on a host that is
    broken in other ways too.

    Driven through the CLI rather than by calling `create_app`, because the
    defect lives in the ordering *of the command*, not of the factory.
    """
    monkeypatch.setattr("localmail.cli.__version_diagnostic__", _PROBE)
    monkeypatch.setenv("LOCALMAIL_DSN_OVERRIDE", _DEAD_DSN)
    with caplog.at_level("ERROR", logger="localmail.serve"):
        result = CliRunner().invoke(main, ["serve", "--no-tls", "--bind", "127.0.0.1"])
    assert result.exit_code != 0
    assert "Is Postgres reachable?" in result.output
    assert [r for r in caplog.records if _PROBE in r.getMessage()], (
        "serve must report an unresolvable version before its schema check, "
        "not from inside create_app which that check prevents it from reaching"
    )
