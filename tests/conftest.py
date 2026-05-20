import os
from dataclasses import dataclass

import keyring
import psycopg
import pytest
from keyring.backend import KeyringBackend

from localmail.db import apply_migrations

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


def _db_available() -> bool:
    try:
        with psycopg.connect(TEST_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


_DB_OK = _db_available()


@pytest.fixture(scope="session")
def db_dsn():
    """Apply migrations once per session; skip dependent tests if no DB."""
    if not _DB_OK:
        pytest.skip(f"no Postgres reachable at {TEST_DSN}")
    apply_migrations(TEST_DSN)
    return TEST_DSN


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
                "api_users, api_tokens, user_accounts, api_login_attempts "
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
    reset_login_rate_limiter()
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
