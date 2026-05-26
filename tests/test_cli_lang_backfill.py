"""Tests for the `lang-backfill` CLI verb + body_lang fields in `search-status`."""

from __future__ import annotations

import json

from click.testing import CliRunner

from localmail.cli import main
from localmail.search.lang_detect import FixedDetector


def _seed_messages(conn, bodies: list[str | None]) -> list[int]:
    """Insert one account + N messages; return message IDs."""
    ids: list[int] = []
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES ('a', 'a@x', 'h', 'password') RETURNING id"
        )
        acct = cur.fetchone()[0]
        for i, body in enumerate(bodies):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, 's', %s, '{}'::jsonb, %s, %s) RETURNING id",
                (acct, f"<m{i}>", bytes([i + 1]) * 32, body, b"raw", 1),
            )
            ids.append(cur.fetchone()[0])
    conn.commit()
    return ids


def test_cli_lang_backfill_populates_body_lang(
    monkeypatch, db_dsn, db_conn, cli_config
) -> None:
    ids = _seed_messages(db_conn, ["alpha body", "beta body", "gamma body"])
    detector = FixedDetector({"alpha body": "en", "beta body": "de", "gamma body": "es"})

    monkeypatch.setattr("localmail.cli._dsn", lambda: db_dsn)
    monkeypatch.setattr("localmail.search.lang_detect.make_detector", lambda cfg: detector)

    runner = CliRunner()
    result = runner.invoke(main, ["lang-backfill", "--no-progress"])

    assert result.exit_code == 0, result.output
    assert "3 messages processed" in result.output

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, body_lang FROM messages WHERE id = ANY(%s) ORDER BY id", (ids,),
        )
        seen = dict(cur.fetchall())
    assert seen[ids[0]] == "en"
    assert seen[ids[1]] == "de"
    assert seen[ids[2]] == "es"


def test_cli_lang_backfill_no_op_when_disabled(
    monkeypatch, db_dsn, db_conn, cli_config
) -> None:
    """`body_lang_enabled=False` → exits cleanly without touching rows."""
    ids = _seed_messages(db_conn, ["anything"])

    monkeypatch.setattr("localmail.cli._dsn", lambda: db_dsn)
    # Force make_detector to return None (the disabled path).
    monkeypatch.setattr("localmail.search.lang_detect.make_detector", lambda cfg: None)

    runner = CliRunner()
    result = runner.invoke(main, ["lang-backfill"])

    assert result.exit_code == 0
    assert "disabled" in (result.output + (result.stderr_bytes or b"").decode())

    with db_conn.cursor() as cur:
        cur.execute("SELECT body_lang FROM messages WHERE id = %s", (ids[0],))
        assert cur.fetchone()[0] is None


def test_cli_search_status_reports_body_lang_counts(
    monkeypatch, db_dsn, db_conn, cli_config
) -> None:
    ids = _seed_messages(db_conn, ["one body", "two body"])
    with db_conn.cursor() as cur:
        cur.execute("UPDATE messages SET body_lang = 'en' WHERE id = %s", (ids[0],))
    db_conn.commit()

    monkeypatch.setattr("localmail.cli._dsn", lambda: db_dsn)
    runner = CliRunner()
    result = runner.invoke(main, ["search-status", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["body_lang_populated"] == 1
    assert payload["body_lang_pending"] == 1
