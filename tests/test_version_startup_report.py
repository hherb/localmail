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
import inspect
import logging
import textwrap
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from localmail.cli import SELF_REPORTING_COMMANDS, main
from localmail.config import LocalmailConfig, McpConfig, ServeConfig
from localmail.daemon import Daemon
from localmail.retry import RetryAborted
from localmail.serve.app import create_app
from localmail.version_report import VersionSource, log_version_diagnostic

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
#: `cli.py` is here because it is the reader this pin most needs: every test in
#: this file and in `test_version_single_source.py` rebinds
#: `localmail.cli.__version_diagnostic__`, so replacing its package import with
#: a module-local `= None` left `--version` (#291) *and* `serve` (#295)
#: permanently silent with the entire suite green. Verified by mutation.
_READER_SOURCES = {
    "src/localmail/cli.py": "localmail",
    "src/localmail/daemon.py": "localmail",
    "src/localmail/serve/app.py": "localmail",
}

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _construct(reader: str, db_dsn: str) -> None:
    """Build the entry point under test, then release whatever it opened.

    `finally`, not a trailing `.close()`: both constructors open a pool part-way
    through, so a raise after that point would leak it along with its background
    threads, which then outlive this test (#321).

    An earlier wording here said a leaked writer is what makes `db_conn`'s
    TRUNCATE deadlock in a later test. That was #335's theory and it was
    measured false — with a `lock_timeout` armed on the truncate, ten
    instrumented runs recorded zero blocked truncates; the corruption came from
    a second pytest session, which `db_session_lock` now excludes. Closing the
    pool is still right, just for the ordinary reason.
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


@pytest.mark.parametrize("quiet", [None, ""], ids=["none", "empty"])
def test_a_healthy_install_logs_nothing(quiet) -> None:
    """`None` is the healthy answer and must not become an empty WARNING — a
    line that fires on every start is a line operators learn to skip.

    `""` is parametrised beside it because the guard is deliberately falsy
    rather than `is None`, and narrowing it to `is None` otherwise passed every
    test in this file while emitting a blank ERROR at every startup.
    """
    assert _records_from(quiet) == []


def _records_from_calls(*diagnostics: str | None) -> list[logging.LogRecord]:
    """Everything `log_version_diagnostic` emits across successive calls on one
    logger — i.e. within one process, which is the scope the dedup is keyed to."""
    log = logging.getLogger("localmail.test.version-repeat")
    records: list[logging.LogRecord] = []
    log.addHandler(_Collect(records))
    log.setLevel(logging.DEBUG)
    try:
        for diagnostic in diagnostics:
            log_version_diagnostic(log, diagnostic)
    finally:
        log.handlers.clear()
    return records


def test_the_same_diagnostic_is_reported_once_per_process() -> None:
    """The mechanism that makes the two `serve` call sites safe.

    `serve_cmd` reports, then `create_app` reports again on the same startup;
    without the dedup an operator sees the same eight-line block twice and
    learns to skip it. Nothing pinned this — deleting the dedup outright left
    the whole suite green, which is what let the comment at `cli.py`'s call site
    assert "create_app's call below stays silent" with nothing behind it.
    """
    assert [r.getMessage() for r in _records_from_calls(_PROBE, _PROBE)] == [_PROBE]


def test_a_second_distinct_diagnostic_is_still_reported() -> None:
    """The dedup is keyed on the diagnostic, not on "have we said anything yet".

    A bool flag passes the test above and is the mutation that matters: it
    silences a *different* second problem for the life of the process — a
    suppression bug inside the module written to end suppression bugs. Verified
    by mutation: the flag form left the entire suite green.
    """
    other = _PROBE + " (a different cause)"
    assert [r.getMessage() for r in _records_from_calls(_PROBE, other, _PROBE)] == [
        _PROBE,
        other,
    ]


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


def test_the_severity_word_matches_the_level_the_line_is_logged_at() -> None:
    """#302: the text said `warning:` while the record was an ERROR.

    One string serves two consumers. `--version` writes it to stderr through
    click, where there is no level and the word is the only severity marker; the
    startup callers hand it to `logging`, where the level is. They disagreed, so
    journald showed `ERROR ... warning: ...` and an operator told to grep for
    one found the other.

    Asserted as a **relation** — the word is read back off a record this module
    actually emitted, not compared against a literal `"error"` — so the two
    cannot be changed apart. A literal would pass against a remedy set and a
    level that agree with the literal and not with each other.
    """
    record = _records_from(_PROBE)[0]
    expected = f"{record.levelname.lower()}: "
    for source in VersionSource:
        if source.diagnostic is None:
            continue
        assert source.diagnostic.startswith(expected), (
            f"{source.value}'s remedy opens with "
            f"{source.diagnostic.split(':')[0]!r} but the line is logged at "
            f"{record.levelname}"
        )


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
    expected_module = _READER_SOURCES[module]
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
    matching = [r for r in caplog.records if _PROBE in r.getMessage()]
    assert matching, (
        "serve must report an unresolvable version before its schema check, "
        "not from inside create_app which that check prevents it from reaching"
    )
    # The logger name is the operator's grep target, and `caplog` collects at the
    # root regardless of it — the same reason the reach tests assert it.
    assert {r.name for r in matching} == {"localmail.serve"}


#: A config path nothing answers on. `load_config` raises `FileNotFoundError`
#: for an explicitly-named file that is absent, which is the gate below.
_MISSING_CONFIG = "/nonexistent/localmail/config.toml"

#: The name the group callback reports under — the package logger, since the
#: report is now made on behalf of whichever of the 38 commands is running.
_GROUP_LOGGER = "localmail"


def _invoke_reporting(argv, monkeypatch: pytest.MonkeyPatch, caplog):
    """Run `argv` with an unresolvable version and no reachable config.

    Every command resolves its DSN through `load_config`, which raises on a
    named-but-absent file — so each one fails before it can touch Postgres,
    while still passing through the group callback the report now lives in.
    """
    monkeypatch.setattr("localmail.cli.__version_diagnostic__", _PROBE)
    monkeypatch.setenv("LOCALMAIL_CONFIG", _MISSING_CONFIG)
    monkeypatch.delenv("LOCALMAIL_DSN_OVERRIDE", raising=False)
    with caplog.at_level("ERROR", logger=_GROUP_LOGGER):
        CliRunner().invoke(main, argv)
    return [r for r in caplog.records if _PROBE in r.getMessage()]


#: Every command that does *not* report for itself — i.e. the 36 that #304 found
#: reporting nowhere. Derived from the registered commands rather than listed,
#: so a command added later is covered without anyone remembering to add it.
_DELEGATING_COMMANDS = sorted(set(main.commands) - SELF_REPORTING_COMMANDS)


@pytest.mark.parametrize("command", _DELEGATING_COMMANDS)
def test_every_other_command_reports_an_unresolvable_version(
    command: str, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """#304: of 38 commands, exactly three surfaced it — `--version`, `serve`,
    `run`. The other 36 caught the failure and reported it nowhere.

    That matters more than it looks, because #296 deliberately traded a loud
    crash for graceful degradation: before it, an unreadable `METADATA`
    propagated out of `import localmail` and killed the command with a
    traceback. The compensating report was only ever wired to the two entry
    points #295 named — so for a cron `localmail sync` on a host whose
    `site-packages` mount has started raising EIO, #296 converted a loud nightly
    failure into a silent success with exit 0.

    Parametrised over every command rather than a sample: the defect was a *set*
    of commands, so a sample would let the next one added rejoin the silent 36.
    """
    assert _invoke_reporting([command], monkeypatch, caplog), (
        f"`localmail {command}` reports an unresolvable version nowhere"
    )


#: The reporter, named once so the AST walk below and the code it checks cannot
#: be renamed apart.
_REPORTER = log_version_diagnostic.__name__


def _commands_that_report() -> set[str]:
    """Every registered `main` subcommand whose own body calls the reporter.

    Read off the live command registry (so a command added later is in scope
    without anyone updating a list) and decided by walking each callback's AST
    rather than grepping its source — the distinction #291 already paid for
    here: the *reason* for a rule is written in comments and docstrings beside
    it, and a text match cannot tell prose from code.
    """
    reporting = set()
    for name, command in main.commands.items():
        if command.callback is None:
            continue
        tree = ast.parse(textwrap.dedent(inspect.getsource(command.callback)))
        if any(
            isinstance(node, ast.Call) and getattr(node.func, "id", None) == _REPORTER
            for node in ast.walk(tree)
        ):
            reporting.add(name)
    return reporting


def test_the_skip_set_is_exactly_the_commands_that_report_themselves() -> None:
    """The skip-set is a hardcoded pair, and only one of its drift directions is
    survivable — so the pair is derived from the code and compared, not trusted.

    A command **listed but not reporting** goes silent: the group callback steps
    aside for it and nothing takes its place, which is #304 reopened for exactly
    the long-running processes #295 was about. A command **reporting but not
    listed** merely loses its formatting, because the group callback's earlier
    line wins the per-process dedup.

    Stated as set equality rather than two containments so both are one
    assertion, and so the failure message names the offending command.
    """
    assert _commands_that_report() == set(SELF_REPORTING_COMMANDS)


def test_a_command_stays_quiet_when_the_version_is_known(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The common case, pinned separately: "report unconditionally" satisfies
    every assertion above and would put an ERROR in front of an operator on
    every healthy invocation of all 38 commands."""
    monkeypatch.setattr("localmail.cli.__version_diagnostic__", None)
    monkeypatch.setenv("LOCALMAIL_CONFIG", _MISSING_CONFIG)
    with caplog.at_level("ERROR", logger=_GROUP_LOGGER):
        CliRunner().invoke(main, ["list-accounts"])
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize("argv", [
    ["sync", "--help"],
    ["list-accounts", "--help"],
    ["daemon", "status", "--help"],
], ids=["sync", "list-accounts", "nested-group"])
def test_a_help_request_stays_quiet(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """#307: `--help` does no archive work, touches no config and no DB.

    click resolves the subcommand before applying its `--help`, so #304's
    group-callback report ran for `<cmd> --help` and put an ERROR ahead of the
    text the operator had explicitly asked to read — while bare `localmail`,
    `localmail --help` and an unknown command stayed quiet, because
    `no_args_is_help` short-circuits ahead of the callback. The decision
    (option 2) was to make all four shapes quiet rather than make the other
    three loud.

    The nested case is parametrised too: `daemon` is a second group mounted on
    `main`, so its help arrives as `['status', '--help']` on the root context
    and a rule reading only the first pending argument would miss it.
    """
    records = _invoke_reporting(argv, monkeypatch, caplog)
    assert records == [], f"`localmail {' '.join(argv)}` reported on a help screen"


@pytest.mark.parametrize("argv", [
    [],
    ["--help"],
    ["nosuchcommand"],
], ids=["bare", "group-help", "unknown-command"])
def test_the_shapes_that_never_reached_the_callback_are_still_quiet(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The other three of #307's four shapes, pinned rather than assumed.

    Their silence is a side effect of click's parse order (`no_args_is_help`
    short-circuits before the callback), which is exactly why the issue asked
    for a pin: nothing asserted it, so a future `invoke_without_command=True`
    would flip them loud without a failing test.
    """
    assert _invoke_reporting(argv, monkeypatch, caplog) == []


def test_a_real_invocation_of_the_same_command_still_reports(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The positive control for the two above.

    Skipping on a help request must not become skipping on the command: a
    rule that matched too broadly would return #304 for the 36 commands it was
    filed about, and every assertion above would still pass.
    """
    assert _invoke_reporting(["sync"], monkeypatch, caplog)


def test_the_version_flag_is_not_a_group_callback_reporter(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """`--version` must keep reporting through click, and must not also log.

    Its stdout is the machine-readable line the manual's install-verification
    step parses, and click is what keeps the diagnostic off it. The flag is
    eager and exits inside its own callback, so the group callback never runs —
    this pins that, because a report added there *without* the eager exit would
    duplicate the stderr line and a report moved *out* of `_print_version` would
    leave `--version` silent again, which is #291.
    """
    monkeypatch.setattr("localmail.cli.__version_diagnostic__", _PROBE)
    with caplog.at_level("ERROR", logger=_GROUP_LOGGER):
        result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert _PROBE in result.stderr
    assert _PROBE not in result.stdout
    assert [r for r in caplog.records if _PROBE in r.getMessage()] == []


@pytest.mark.parametrize(
    ("argv", "logger_name"),
    [
        (["serve", "--no-tls", "--bind", "127.0.0.1"], "localmail.serve"),
        (["run"], "localmail.daemon"),
    ],
    ids=["serve", "run"],
)
def test_a_startup_path_reports_before_its_config_gate(
    argv, logger_name, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The gate ahead of the two both commands already pin.

    `serve_cmd` reported after `load_config`, and `run_cmd` reported only from
    inside `Daemon.__init__` — one gate later still. A missing or malformed
    config raises there, so on a host mid-deploy neither command said anything,
    which is the same hole #295 closed one gate further down. The test that
    pinned the schema gate could not see it: it drives the
    `LOCALMAIL_DSN_OVERRIDE` branch, which skips `load_config` entirely.

    A host damaged enough to lose its version metadata is exactly a host whose
    config may not be there either, which is why CLAUDE.md states the rule
    without qualification: every call runs before the gate it precedes.
    """
    monkeypatch.setattr("localmail.cli.__version_diagnostic__", _PROBE)
    monkeypatch.setenv("LOCALMAIL_CONFIG", _MISSING_CONFIG)
    monkeypatch.delenv("LOCALMAIL_DSN_OVERRIDE", raising=False)
    with caplog.at_level("ERROR", logger=logger_name):
        result = CliRunner().invoke(main, argv)
    assert result.exit_code != 0
    assert isinstance(result.exception, (FileNotFoundError, SystemExit))
    matching = [r for r in caplog.records if _PROBE in r.getMessage()]
    assert matching, (
        f"{argv[0]} must report an unresolvable version before it loads config"
    )
    assert {r.name for r in matching} == {logger_name}
