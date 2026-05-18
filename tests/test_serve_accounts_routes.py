import psycopg
from fastapi.testclient import TestClient

from localmail.serve.app import create_app


def _client(db_dsn: str) -> TestClient:
    return TestClient(create_app(db_dsn=db_dsn, searcher=None))


def _seed_account_and_folder(conn: psycopg.Connection) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            ("a1", "h@x.test", "imap.x.test", "password"),
        )
        row = cur.fetchone()
        assert row is not None
        a = row[0]
        cur.execute("INSERT INTO mailboxes (account_id, name) VALUES (%s, %s) RETURNING id",
                    (a, "INBOX"))
        row = cur.fetchone()
        assert row is not None
        f = row[0]
    conn.commit()
    return a, f


def test_list_accounts_auth_required(db_dsn: str) -> None:
    r = _client(db_dsn).get("/v1/accounts")
    assert r.status_code == 401


def test_list_accounts_returns_array(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    _seed_account_and_folder(db_conn)
    grant_alice_all_accounts()
    r = _client(db_dsn).get("/v1/accounts", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["name"] == "a1"


def test_list_folders_for_account(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    aid, _ = _seed_account_and_folder(db_conn)
    grant_alice_all_accounts()
    r = _client(db_dsn).get(
        f"/v1/accounts/{aid}/folders",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["name"] == "INBOX"
