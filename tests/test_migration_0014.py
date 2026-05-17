"""Schema smoke test for migration 0014 (api_users, api_tokens)."""
import psycopg


def test_api_users_table_exists(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = 'api_users' "
            "ORDER BY ordinal_position"
        )
        rows = cur.fetchall()
    cols = {r[0]: (r[1], r[2]) for r in rows}
    assert cols["id"][0] == "bigint"
    assert cols["username"] == ("text", "NO")
    assert cols["password_hash"] == ("text", "NO")
    assert cols["created_at"][1] == "NO"
    assert cols["disabled_at"][1] == "YES"


def test_api_users_username_is_unique(db_conn: psycopg.Connection) -> None:
    raised = False
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash) VALUES (%s, %s)",
            ("alice", "$argon2id$dummy"),
        )
    db_conn.commit()
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_users (username, password_hash) VALUES (%s, %s)",
                ("alice", "$argon2id$other"),
            )
        db_conn.commit()
    except psycopg.errors.UniqueViolation:
        raised = True
        db_conn.rollback()
    assert raised, "duplicate username should violate unique constraint"


def test_api_tokens_table_exists(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_name = 'api_tokens' "
            "ORDER BY ordinal_position"
        )
        cols = dict(cur.fetchall())
    assert cols["token_sha256"] == "bytea"
    assert cols["user_id"] == "bigint"
    assert cols["expires_at"].startswith("timestamp")
    assert cols["last_used_at"].startswith("timestamp")


def test_api_tokens_cascades_on_user_delete(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash) VALUES (%s, %s) RETURNING id",
            ("bob", "$argon2id$dummy"),
        )
        row = cur.fetchone()
        assert row is not None
        uid = row[0]
        cur.execute(
            "INSERT INTO api_tokens (token_sha256, user_id, expires_at) "
            "VALUES (%s, %s, now() + interval '30 days')",
            (b"\x01" * 32, uid),
        )
        cur.execute("DELETE FROM api_users WHERE id = %s", (uid,))
        cur.execute("SELECT count(*) FROM api_tokens WHERE user_id = %s", (uid,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0
