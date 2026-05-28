"""Service-layer tests for localmail.api.admin.accounts."""

import keyring
import pytest

from localmail.api.admin.accounts import (
    Account, AccountSummary,
    AccountFieldError, AccountInUse,
    FolderInfo,
    clear_secret, create_account, delete_account, get_account,
    list_accounts, probe_connection, store_password, update_account,
)
from localmail.api.errors import NotFound
from tests._fake_imap import FakeIMAPClient


def _insert_account(conn, *, name, email='x@y.test', method='password',
                    host='imap.example', port=993, oauth_provider=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "imap_host, imap_port, oauth_provider, config) "
            "VALUES (%s, %s, %s, %s, %s, %s, '{}'::jsonb) RETURNING id",
            (name, email, method, host, port, oauth_provider),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


def test_list_accounts_returns_summaries_in_id_order(db_conn):
    id_a = _insert_account(db_conn, name='alpha')
    id_b = _insert_account(db_conn, name='beta')
    summaries = list_accounts(db_conn)
    assert [s.id for s in summaries] == [id_a, id_b]
    assert all(isinstance(s, AccountSummary) for s in summaries)
    assert summaries[0].name == 'alpha'
    assert summaries[0].auth_method == 'password'


def test_get_account_returns_full_record(db_conn):
    aid = _insert_account(db_conn, name='gamma',
                          email='g@example.test', method='oauth2',
                          host='imap.gmail.com', port=993,
                          oauth_provider='gmail')
    acct = get_account(db_conn, aid)
    assert isinstance(acct, Account)
    assert acct.id == aid
    assert acct.name == 'gamma'
    assert acct.email_address == 'g@example.test'
    assert acct.auth_method == 'oauth2'
    assert acct.oauth_provider == 'gmail'
    assert acct.imap_host == 'imap.gmail.com'
    assert acct.imap_port == 993
    assert acct.sync_enabled is True
    assert acct.folder_allow is None
    assert acct.folder_deny is None
    assert acct.folder_deny_flags is None


def test_get_account_missing_raises_not_found(db_conn):
    with pytest.raises(NotFound):
        get_account(db_conn, 9999)


def test_create_account_password_round_trip(db_conn):
    acct = create_account(
        db_conn,
        name='work',
        email_address='work@example.test',
        auth_method='password',
        imap_host='imap.example',
        imap_port=993,
        oauth_provider=None,
        folder_allow=None,
        folder_deny=['Spam'],
        folder_deny_flags=['\\Junk'],
    )
    assert acct.id > 0 and acct.name == 'work'
    assert acct.folder_deny == ['Spam']
    fetched = get_account(db_conn, acct.id)
    assert fetched == acct


def test_create_account_archive_has_null_host(db_conn):
    acct = create_account(
        db_conn,
        name='legacy-2017',
        email_address='archive@local.test',
        auth_method='archive',
        imap_host=None,
        imap_port=None,
        oauth_provider=None,
        folder_allow=None,
        folder_deny=None,
        folder_deny_flags=None,
    )
    assert acct.auth_method == 'archive'
    assert acct.imap_host is None and acct.imap_port is None


def test_create_account_rejects_blank_name(db_conn):
    with pytest.raises(AccountFieldError):
        create_account(
            db_conn,
            name='',
            email_address='x@y.test',
            auth_method='password',
            imap_host='h', imap_port=993,
            oauth_provider=None,
            folder_allow=None, folder_deny=None, folder_deny_flags=None,
        )


def test_create_account_rejects_password_without_host(db_conn):
    with pytest.raises(AccountFieldError):
        create_account(
            db_conn,
            name='x',
            email_address='x@y.test',
            auth_method='password',
            imap_host=None, imap_port=None,
            oauth_provider=None,
            folder_allow=None, folder_deny=None, folder_deny_flags=None,
        )


def test_update_account_changes_folders_and_bumps_updated_at(db_conn):
    aid = _insert_account(db_conn, name='u')
    before = get_account(db_conn, aid)
    updated = update_account(
        db_conn,
        aid,
        folder_deny=['Trash', 'Bin'],
        sync_enabled=False,
    )
    assert updated.folder_deny == ['Trash', 'Bin']
    assert updated.sync_enabled is False
    assert updated.updated_at >= before.updated_at


def test_update_account_missing_raises_not_found(db_conn):
    with pytest.raises(NotFound):
        update_account(db_conn, 9999, sync_enabled=False)


def test_update_account_rejects_changing_auth_method_to_archive_with_host(db_conn):
    aid = _insert_account(db_conn, name='live')
    with pytest.raises(AccountFieldError):
        update_account(db_conn, aid, auth_method='archive')


def test_update_account_violation_raises_field_error(db_conn):
    """If an UPDATE violates accounts_live_requires_host, the service layer
    surfaces AccountFieldError rather than the raw psycopg CheckViolation."""
    aid = _insert_account(db_conn, name='constraint-target')
    with pytest.raises(AccountFieldError):
        update_account(db_conn, aid, imap_host=None)


def test_delete_empty_account_succeeds(db_conn):
    aid = _insert_account(db_conn, name='empty')
    delete_account(db_conn, aid)
    with pytest.raises(NotFound):
        get_account(db_conn, aid)


def test_delete_account_with_messages_refuses_without_force(db_conn):
    aid = _insert_account(db_conn, name='busy')
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (account_id, raw_bytes, raw_sha256, "
            "size_bytes, headers, attachments) "
            "VALUES (%s, %s, %s, %s, '{}'::jsonb, '[]'::jsonb)",
            (aid, b'x', b'a'*32, 1))
    with pytest.raises(AccountInUse):
        delete_account(db_conn, aid)


def test_delete_account_with_messages_force_cascades(db_conn):
    aid = _insert_account(db_conn, name='busy2')
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (account_id, raw_bytes, raw_sha256, "
            "size_bytes, headers, attachments) "
            "VALUES (%s, %s, %s, %s, '{}'::jsonb, '[]'::jsonb)",
            (aid, b'x', b'b'*32, 1))
    delete_account(db_conn, aid, force=True)
    with pytest.raises(NotFound):
        get_account(db_conn, aid)


def test_store_password_writes_keyring(db_conn):
    aid = _insert_account(db_conn, name='kring')
    acct = get_account(db_conn, aid)
    store_password(acct, 'sekret')
    assert keyring.get_password('localmail', 'kring') == 'sekret'


def test_clear_secret_removes_keyring_entries(db_conn):
    aid = _insert_account(db_conn, name='kring2')
    acct = get_account(db_conn, aid)
    store_password(acct, 'sekret')
    keyring.set_password('localmail', 'kring2:refresh', 'refr')
    clear_secret(acct)
    assert keyring.get_password('localmail', 'kring2') is None
    assert keyring.get_password('localmail', 'kring2:refresh') is None


def test_clear_secret_tolerates_missing_keyring_entries(db_conn):
    aid = _insert_account(db_conn, name='kring3')
    acct = get_account(db_conn, aid)
    clear_secret(acct)  # no-op, no raise


def test_probe_connection_returns_folder_list(db_conn, monkeypatch):
    from contextlib import contextmanager

    aid = _insert_account(db_conn, name='tc')

    fake = FakeIMAPClient.with_folders(['INBOX', '[Gmail]/All Mail', 'Sent'])

    @contextmanager
    def fake_open_connection(account):
        yield fake

    monkeypatch.setattr(
        'localmail.api.admin.accounts._open_imap_connection',
        fake_open_connection,
    )
    folders = probe_connection(db_conn, aid)
    assert [f.name for f in folders] == ['INBOX', '[Gmail]/All Mail', 'Sent']
    assert all(isinstance(f, FolderInfo) for f in folders)


def test_probe_connection_archive_raises_field_error(db_conn):
    aid = _insert_account(db_conn, name='arch-probe', method='archive',
                          host=None, port=None)
    with pytest.raises(AccountFieldError):
        probe_connection(db_conn, aid)
