# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Tests for the `lang-backfill` CLI verb + body_lang fields in `search-status`."""

from __future__ import annotations

import json

from click.testing import CliRunner

from localmail.cli import main
from localmail.config import SearchConfig
from localmail.search.lang_detect import FixedDetector, run_lang_detect_pass


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

    monkeypatch.setattr("localmail.cli._dsn", lambda ctx: db_dsn)
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

    monkeypatch.setattr("localmail.cli._dsn", lambda ctx: db_dsn)
    # Force make_detector to return None (the disabled path).
    monkeypatch.setattr("localmail.search.lang_detect.make_detector", lambda cfg: None)

    runner = CliRunner()
    result = runner.invoke(main, ["lang-backfill"])

    assert result.exit_code == 0
    assert "disabled" in (result.output + (result.stderr_bytes or b"").decode())

    with db_conn.cursor() as cur:
        cur.execute("SELECT body_lang FROM messages WHERE id = %s", (ids[0],))
        assert cur.fetchone()[0] is None


def test_cli_lang_backfill_drains_past_an_undetectable_head(
    monkeypatch, db_dsn, db_conn, cli_config
) -> None:
    """#251: the drain loop must not stop on a batch that labelled nothing.

    The loop used to break on the labelled count, so a leading batch of bodies
    the detector declines ended the run with every message behind them still
    unlabelled — and reported success. Termination now comes from the batch
    coming back empty.
    """
    ids = _seed_messages(db_conn, ["junk a", "junk b", "real body"])
    detector = FixedDetector({"real body": "en"})

    monkeypatch.setattr("localmail.cli._dsn", lambda ctx: db_dsn)
    monkeypatch.setattr("localmail.search.lang_detect.make_detector", lambda cfg: detector)
    monkeypatch.setattr(
        "localmail.config.SearchConfig.body_lang_detect_batch_size", 2, raising=False
    )

    runner = CliRunner()
    result = runner.invoke(main, ["lang-backfill", "--no-progress"])

    assert result.exit_code == 0, result.output
    with db_conn.cursor() as cur:
        cur.execute("SELECT body_lang FROM messages WHERE id = %s", (ids[2],))
        assert cur.fetchone()[0] == "en"


def test_cli_lang_backfill_retry_declined_reopens_and_relabels(
    monkeypatch, db_dsn, db_conn, cli_config
) -> None:
    """`--retry-declined` re-opens what a stricter policy turned away."""
    ids = _seed_messages(db_conn, ["was undetectable"])
    monkeypatch.setattr("localmail.cli._dsn", lambda ctx: db_dsn)

    monkeypatch.setattr(
        "localmail.search.lang_detect.make_detector", lambda cfg: FixedDetector({})
    )
    runner = CliRunner()
    assert runner.invoke(main, ["lang-backfill", "--no-progress"]).exit_code == 0
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT body_lang_attempted_at FROM messages WHERE id = %s", (ids[0],)
        )
        assert cur.fetchone()[0] is not None

    # A looser detector plus --retry-declined reaches the row.
    monkeypatch.setattr(
        "localmail.search.lang_detect.make_detector",
        lambda cfg: FixedDetector({"was undetectable": "en"}),
    )
    result = runner.invoke(main, ["lang-backfill", "--no-progress", "--retry-declined"])

    assert result.exit_code == 0, result.output
    assert "re-opened 1" in result.output
    with db_conn.cursor() as cur:
        cur.execute("SELECT body_lang FROM messages WHERE id = %s", (ids[0],))
        assert cur.fetchone()[0] == "en"


def test_cli_lang_backfill_retry_declined_is_inert_when_disabled(
    monkeypatch, db_dsn, db_conn, cli_config
) -> None:
    """Re-opening rows nothing will then process only inflates the queue."""
    ids = _seed_messages(db_conn, ["undetectable"])
    monkeypatch.setattr("localmail.cli._dsn", lambda ctx: db_dsn)
    monkeypatch.setattr(
        "localmail.search.lang_detect.make_detector", lambda cfg: FixedDetector({})
    )
    runner = CliRunner()
    runner.invoke(main, ["lang-backfill", "--no-progress"])

    monkeypatch.setattr("localmail.search.lang_detect.make_detector", lambda cfg: None)
    result = runner.invoke(main, ["lang-backfill", "--retry-declined"])

    assert result.exit_code == 0
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT body_lang_attempted_at FROM messages WHERE id = %s", (ids[0],)
        )
        # Still stamped: the disabled guard short-circuits before re-opening.
        assert cur.fetchone()[0] is not None


def test_cli_search_status_reports_body_lang_counts(
    monkeypatch, db_dsn, db_conn, cli_config
) -> None:
    ids = _seed_messages(db_conn, ["one body", "two body"])
    with db_conn.cursor() as cur:
        cur.execute("UPDATE messages SET body_lang = 'en' WHERE id = %s", (ids[0],))
    db_conn.commit()

    monkeypatch.setattr("localmail.cli._dsn", lambda ctx: db_dsn)
    runner = CliRunner()
    result = runner.invoke(main, ["search-status", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["body_lang_populated"] == 1
    assert payload["body_lang_pending"] == 1


def test_cli_search_status_separates_pending_from_declined(
    monkeypatch, db_dsn, db_conn, cli_config
) -> None:
    """#251: `pending` must mean claimable work, not "every unlabelled row".

    Before the fix it reported 100020 rows on the live archive that the worker
    would never reach — the number that made a wedged queue look like a busy
    one. Declined rows now have their own counter, so the operator can see the
    genuinely-unlabelable remainder instead of it hiding inside `pending`.
    """
    _seed_messages(db_conn, ["labelled", "declined", "untouched"])
    monkeypatch.setattr("localmail.cli._dsn", lambda ctx: db_dsn)

    # One pass over a batch of two: labels the first, declines the second,
    # never reaches the third.
    run_lang_detect_pass(
        db_conn, SearchConfig(), FixedDetector({"labelled": "en"}), batch=2
    )

    result = CliRunner().invoke(main, ["search-status", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["body_lang_populated"] == 1
    assert payload["body_lang_declined"] == 1
    assert payload["body_lang_pending"] == 1
