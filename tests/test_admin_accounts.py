"""Service-layer tests for localmail.api.admin.accounts."""

import pytest

from localmail.api.admin.accounts import (
    Account, AccountSummary,
    list_accounts, get_account,
)
from localmail.api.errors import NotFound


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
