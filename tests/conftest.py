# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

import os
import sys
from dataclasses import dataclass

import keyring
import psycopg
import pytest
from keyring.backend import KeyringBackend

from localmail import secrets
from localmail.db import apply_migrations
from tests._db_session_lock import acquire_exclusive

TEST_DSN = os.environ.get(
    "LOCALMAIL_TEST_DSN",
    "postgresql://localmail:local%40%40mail@localhost:5532/localmail_test",
)


class InMemoryKeyring(KeyringBackend):
    """Keyring backend that holds secrets in a process-local dict."""

    priority = 100  # type: ignore[assignment]

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, servicename: str, username: str, password: str) -> None:
        self.store[(servicename, username)] = password

    def get_password(self, servicename: str, username: str) -> str | None:
        return self.store.get((servicename, username))

    def delete_password(self, servicename: str, username: str) -> None:
        if (servicename, username) not in self.store:
            raise keyring.errors.PasswordDeleteError(f"no such entry: {servicename}/{username}")
        del self.store[(servicename, username)]


@pytest.fixture(autouse=True)
def memory_keyring():
    """Swap in an in-memory keyring backend for the duration of each test."""
    original = keyring.get_keyring()
    backend = InMemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(original)


@pytest.fixture(autouse=True)
def fresh_embed_failure_log():
    """Clear the embed worker's process-wide failure log between tests (#267).

    It throttles repeated backend-failure WARNINGs, so a test that breaks the
    backend would otherwise silence the *next* test's first failure — the
    logging analogue of the leaked secret backend below.

    Imported inside the fixture so collecting a suite that never touches search
    doesn't pull the embedding stack in.
    """
    from localmail.search.embed_worker import reset_failure_log

    reset_failure_log()
    yield
    reset_failure_log()


@pytest.fixture(autouse=True)
def fresh_version_reports():
    """Clear the process-wide record of reported version diagnostics (#295).

    `log_version_diagnostic` reports each distinct diagnostic once per process,
    so `serve_cmd` and `create_app` on one startup path do not print the same
    line twice. Without this reset the *first* test to construct an entry point
    would silence every later one — the same shape as the embed worker's
    failure log above.
    """
    from localmail.version_report import reset_version_reports

    reset_version_reports()
    yield
    reset_version_reports()


@pytest.fixture(autouse=True)
def fresh_build_info():
    """Clear the process-wide build-identity cache between tests (#278).

    `resolve_build_info` caches for the life of the process — correct in
    production, where the answer must not change under a running daemon, and
    wrong across tests, where one test's monkeypatched resolver would otherwise
    be the answer every later test sees. The `fresh_version_reports` shape.
    """
    from localmail.build_report import reset_build_info

    reset_build_info()
    yield
    reset_build_info()


@pytest.fixture(autouse=True)
def default_secret_backend():
    """Restore the keyring secret backend after every test.

    `load_config` installs the backend its `[secrets]` block names, so any test
    that loads a config with `backend = "file"` would otherwise point every
    later test at a tmp_path that no longer exists.
    """
    yield
    secrets.reset_to_default()


def _db_available() -> bool:
    try:
        with psycopg.connect(TEST_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


_DB_OK = _db_available()


def _announce(request, message: str) -> None:
    """Put `message` in front of the operator during session setup.

    Fixture-setup output is captured, so a plain `print` is invisible for as
    long as the wait lasts — which is exactly the window where silence reads
    as a hung run. The terminal reporter writes past the capture; stderr is
    the fallback for a `-p no:terminal` invocation.
    """
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"\n{message}", yellow=True)
        return
    print(message, file=sys.stderr, flush=True)


@pytest.fixture(scope="session")
def db_dsn(request):
    """Apply migrations once per session; skip dependent tests if no DB.

    Holds the per-database session lock for the whole run (#335). Every test
    truncates every table, so two pytest sessions sharing one test database
    delete each other's seeded rows — silently, because the truncate
    succeeds. The lock is taken *before* `apply_migrations` so two sessions
    cannot race the migration runner either.
    """
    if not _DB_OK:
        pytest.skip(f"no Postgres reachable at {TEST_DSN}")
    holder = acquire_exclusive(
        TEST_DSN, on_wait=lambda msg: _announce(request, msg)
    )
    try:
        apply_migrations(TEST_DSN)
        yield TEST_DSN
    finally:
        holder.close()


_EMBEDDING_PROBE: tuple[bool, str] | None = None


def _embedding_model_available() -> tuple[bool, str]:
    """Lazily probe (once per session) whether the real embedding model loads.

    Unlike the cheap DB probe, this can trigger a ~1.2 GB download, so it runs
    only when a test actually requests `require_real_embedding_model` — never at
    import/collection time. A HuggingFace download failure (a 429 on shared CI
    IPs, an offline runner, a CDN hiccup — fastembed raises a bare
    ``ValueError: Could not load model … from any source.``) is reported as
    *unavailable* so the dependent tests SKIP rather than fail on infrastructure
    we don't control. Any other error (misconfig, wrong dimension, a missing
    ``query_embed``) propagates as a genuine failure. A successful probe also
    warms the model cache for the test that follows.
    """
    global _EMBEDDING_PROBE
    if _EMBEDDING_PROBE is None:
        from localmail.config import SearchConfig
        from localmail.search.embeddings import FastEmbedBackend
        try:
            FastEmbedBackend(cfg=SearchConfig())
        except ValueError as e:
            if "from any source" in str(e):
                _EMBEDDING_PROBE = (False, f"embedding model unavailable (download failed): {e}")
            else:
                raise
        except OSError as e:
            _EMBEDDING_PROBE = (False, f"embedding model unavailable (network/IO error): {e}")
        else:
            _EMBEDDING_PROBE = (True, "")
    return _EMBEDDING_PROBE


@pytest.fixture
def require_real_embedding_model():
    """Skip a real-model test when the model can't be downloaded/loaded.

    Turns a transient HuggingFace outage (notably the 429s GitHub Actions'
    shared IPs draw) into a SKIP instead of a flaky red. Pair with
    ``@pytest.mark.slow`` — every real-model test is opt-in and resilient.
    """
    available, reason = _embedding_model_available()
    if not available:
        pytest.skip(reason)


@pytest.fixture
def db_conn(db_dsn):
    """Yield a clean connection. Truncates all data tables before each test."""
    conn = psycopg.connect(db_dsn, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE accounts, mailboxes, messages, message_labels, "
                "attachment_blobs, failed_messages, message_chunks, "
                "failed_embeddings, embedding_models, failed_chunkings, "
                "attachment_text, attachment_chunks, failed_extractions, "
                "api_users, api_tokens, user_accounts, api_login_attempts, "
                "daemon_commands, daemon_heartbeats, import_jobs, "
                "oauth_clients, oauth_registration_attempts, "
                "channel_subscriptions, transient_fetches "
                "RESTART IDENTITY CASCADE"
            )
        conn.commit()
        yield conn
    finally:
        conn.close()


@dataclass(frozen=True)
class SeededUser:
    id: int
    username: str
    password: str


@pytest.fixture
def api_user(db_conn):
    """Create a single API user, return SeededUser."""
    from localmail.api.auth import create_user, reset_login_rate_limiter
    reset_login_rate_limiter(db_conn)
    db_conn.commit()
    username = "alice"
    password = "hunter2"
    uid = create_user(db_conn, username, password)
    db_conn.commit()
    return SeededUser(id=uid, username=username, password=password)


@pytest.fixture
def api_token(db_conn, api_user):
    """Mint a valid bearer token for `api_user`."""
    from localmail.api.auth import login
    token, _expires = login(db_conn, api_user.username, api_user.password)
    db_conn.commit()
    return token


@pytest.fixture
def cli_config(monkeypatch, tmp_path, db_dsn):
    """Make `localmail.config.load_config()` resolvable without `$HOME/.config/localmail`.

    Writes a stub `config.toml` (only the mandatory `[database].dsn` key) to
    `tmp_path` and points `LOCALMAIL_CONFIG` at it for the test's lifetime.
    Tests that exercise CLI subcommands whose handlers call `load_config()`
    must depend on this fixture so they pass on a clean CI runner — see
    GitHub issue #100. Tests that additionally need to override the DSN
    used for actual SQL still monkeypatch `localmail.cli._dsn`; this
    fixture only ensures the config file *parses*.
    """
    stub = tmp_path / "config.toml"
    stub.write_text(f'[database]\ndsn = "{db_dsn}"\n')
    monkeypatch.setenv("LOCALMAIL_CONFIG", str(stub))
    return stub


@pytest.fixture
def grant_alice_all_accounts(db_conn, api_user):
    """Returns a callable that grants `api_user` access to every account
    currently seeded in the DB.

    Most pre-ACL route tests seed an account, then call the route under the
    `api_token` fixture and expect to read it back. Call this fixture's
    returned callable after seeding so the route's ACL filter sees the user
    as authorised.
    """
    def _grant() -> None:
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_accounts (user_id, account_id) "
                "SELECT %s, id FROM accounts "
                "ON CONFLICT DO NOTHING",
                (api_user.id,),
            )
        db_conn.commit()
    return _grant
