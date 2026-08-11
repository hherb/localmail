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
emitted, with five test files mocking it into looking covered). One WARNING at
startup puts the signal where the operator of a headless host actually looks.

These tests pin the **reach** — that each entry point consults the diagnostic.
What the diagnostic *says* is pinned in `test_version_report.py`; that the CLI
puts it on stderr rather than stdout is pinned in `test_version_single_source.py`.
"""
from __future__ import annotations

import logging
import threading

import pytest

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


def _construct(reader: str, db_dsn: str) -> None:
    """Build the entry point under test, then release whatever it opened."""
    if reader == "daemon":
        cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
        cfg.search.run_embed_worker = False
        cfg.search.run_extract_worker = False
        Daemon(cfg, dsn=db_dsn, ssl=False).pool.close()
        return
    create_app(db_dsn=db_dsn, searcher=None).state.pool.close()


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


def test_an_unresolvable_version_is_reported_at_warning() -> None:
    """WARNING, not INFO: an unresolvable version means the running deploy
    cannot be identified, and INFO sits below most supervisors' threshold."""
    records = _records_from(_PROBE)
    assert [r.levelno for r in records] == [logging.WARNING]
    assert _PROBE in records[0].getMessage()


@pytest.mark.parametrize("reader", sorted(_READERS))
def test_a_startup_path_reports_an_unresolvable_version(
    reader: str, db_dsn: str, db_conn, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The #295 defect itself: `serve` and `run` were silent on exactly the
    path (`NOT_INSTALLED`) the remedy text was written for."""
    attribute, logger_name = _READERS[reader]
    monkeypatch.setattr(attribute, _PROBE)
    with caplog.at_level("WARNING", logger=logger_name):
        _construct(reader, db_dsn)
    assert [r for r in caplog.records if _PROBE in r.getMessage()]


@pytest.mark.parametrize("reader", sorted(_READERS))
def test_a_startup_path_stays_quiet_when_the_version_is_known(
    reader: str, db_dsn: str, db_conn, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The overwhelmingly common case, pinned separately: "log it
    unconditionally" satisfies the test above and is the mutation that matters.
    """
    attribute, logger_name = _READERS[reader]
    monkeypatch.setattr(attribute, None)
    with caplog.at_level("WARNING", logger=logger_name):
        _construct(reader, db_dsn)
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
    dead = "postgresql://localmail@127.0.0.1:1/nope"
    cfg = LocalmailConfig.model_validate(
        {
            "database": {"dsn": dead},
            "daemon": {"startup_backoff_initial_s": 0.01, "startup_backoff_max_s": 0.02},
        }
    )
    stop = threading.Event()
    stop.set()
    with caplog.at_level("WARNING", logger="localmail.daemon"):
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
    with caplog.at_level("WARNING", logger="localmail.serve"):
        with pytest.raises(ValueError, match="state_signing_key"):
            create_app(
                db_dsn="postgresql://localmail@127.0.0.1:1/nope",
                searcher=None,
                serve_config=ServeConfig(),
                mcp_config=McpConfig(authorization_server_enabled=True),
                enable_mcp=True,
            )
    assert [r for r in caplog.records if _PROBE in r.getMessage()], (
        "the version diagnostic must be logged before the config check, not after"
    )
