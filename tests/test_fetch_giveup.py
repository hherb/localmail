# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Giving up on an unfetchable ``BODY[]`` leaves a queryable, re-drivable row (#239).

Pre-#238 an empty ``BODY[]`` was always silently lost. #238 bounded the hold,
but the give-up itself still left only a WARNING: no table state, no CLI, no way
to find or re-drive the message. Every sibling failure path here keeps a row
precisely so an operator can — ``failed_messages`` / ``retry-failed``,
``failed_extractions`` / ``retry-failed-extractions``. These tests pin the
tombstone (``transient_fetches.gave_up_at``) and its two commands.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from localmail.fetch_retry import (
    GaveUpFetch,
    arm_for_retry,
    clear_attempts,
    list_gave_up,
    load_attempts,
    mark_gave_up,
    plan_uidnext_rewind,
    purge_gave_up,
    reclaim_below,
    record_attempt,
)

# --- pure: the rewind planner ----------------------------------------------


class TestPlanUidnextRewind:
    """Re-driving a given-up UID means rewinding the mailbox watermark to it.

    Idempotent by construction — sync's existing-id check plus
    ``ON CONFLICT DO NOTHING`` make the re-scan of everything above it a no-op.
    """

    def test_rewinds_to_the_lowest_tombstoned_uid_per_mailbox(self):
        plan = plan_uidnext_rewind([(1, 40), (1, 12), (2, 7)], {1: 100, 2: 90})
        assert plan == {1: 12, 2: 7}

    def test_a_watermark_already_at_or_below_the_uid_is_not_rewound(self):
        """Sync will re-see it anyway; moving the watermark forward would skip."""
        assert plan_uidnext_rewind([(1, 40)], {1: 40}) == {}
        assert plan_uidnext_rewind([(1, 40)], {1: 3}) == {}

    def test_no_tombstones_is_an_empty_plan(self):
        assert plan_uidnext_rewind([], {1: 100}) == {}

    def test_a_mailbox_with_no_known_watermark_is_skipped(self):
        assert plan_uidnext_rewind([(9, 5)], {1: 100}) == {}


# --- DB: the tombstone ------------------------------------------------------


def _mailbox(conn, *, account: str = "a", mailbox: str = "INBOX") -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM accounts WHERE name = %s", (account,))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO accounts (name, email_address, auth_method, imap_host,"
                " imap_port, config) VALUES (%s, 'a@b.test', 'password', 'h', 993,"
                " '{}') RETURNING id",
                (account,),
            )
            row = cur.fetchone()
        assert row is not None
        cur.execute(
            "INSERT INTO mailboxes (account_id, name, uidnext) VALUES (%s, %s, 1)"
            " RETURNING id",
            (row[0], mailbox),
        )
        mb = cur.fetchone()
        assert mb is not None
    conn.commit()
    return int(mb[0])


def test_marking_a_give_up_tombstones_the_existing_hold_row(db_conn):
    mb = _mailbox(db_conn)
    record_attempt(db_conn, mailbox_id=mb, uid=7)

    mark_gave_up(db_conn, mailbox_id=mb, uid=7)

    rows = list_gave_up(db_conn)
    assert [(r.mailbox_name, r.uid) for r in rows] == [("INBOX", 7)]
    assert rows[0].attempt_count == 1
    assert rows[0].gave_up_at is not None


def test_re_marking_keeps_the_first_give_up_moment(db_conn):
    """A lower held UID keeps an expired one reachable, so it is re-seen every
    pass. Each re-sighting must NOT restamp the tombstone."""
    mb = _mailbox(db_conn)
    record_attempt(db_conn, mailbox_id=mb, uid=7)
    mark_gave_up(db_conn, mailbox_id=mb, uid=7)
    first = list_gave_up(db_conn)[0].gave_up_at

    record_attempt(db_conn, mailbox_id=mb, uid=7)
    mark_gave_up(db_conn, mailbox_id=mb, uid=7)

    again = list_gave_up(db_conn)[0]
    assert again.gave_up_at == first
    assert again.attempt_count == 2


def test_reclaim_below_preserves_tombstones_but_drops_live_holds(db_conn):
    """`reclaim_below` was the tombstone's only collector — it must not be."""
    mb = _mailbox(db_conn)
    record_attempt(db_conn, mailbox_id=mb, uid=3)
    record_attempt(db_conn, mailbox_id=mb, uid=5)
    mark_gave_up(db_conn, mailbox_id=mb, uid=5)

    reclaim_below(db_conn, mailbox_id=mb, uid=99)

    with db_conn.cursor() as cur:
        cur.execute("SELECT uid FROM transient_fetches WHERE mailbox_id = %s", (mb,))
        assert [r[0] for r in cur.fetchall()] == [5]


def test_a_recovered_fetch_clears_the_tombstone(db_conn):
    """After a retry rewinds the watermark, a successful fetch removes the row."""
    mb = _mailbox(db_conn)
    record_attempt(db_conn, mailbox_id=mb, uid=7)
    mark_gave_up(db_conn, mailbox_id=mb, uid=7)

    clear_attempts(db_conn, mailbox_id=mb, uid=7)

    assert list_gave_up(db_conn) == []


def test_list_gave_up_reports_account_and_mailbox_and_filters_by_account(db_conn):
    mb_a = _mailbox(db_conn, account="acct-a", mailbox="INBOX")
    mb_b = _mailbox(db_conn, account="acct-b", mailbox="Archive")
    for mb in (mb_a, mb_b):
        record_attempt(db_conn, mailbox_id=mb, uid=4)
        mark_gave_up(db_conn, mailbox_id=mb, uid=4)

    everything = list_gave_up(db_conn)
    assert {r.account_name for r in everything} == {"acct-a", "acct-b"}

    only_b = list_gave_up(db_conn, account_name="acct-b")
    assert [(r.account_name, r.mailbox_name, r.uid) for r in only_b] == [
        ("acct-b", "Archive", 4)
    ]
    assert isinstance(only_b[0], GaveUpFetch)


def test_list_gave_up_ignores_live_holds(db_conn):
    mb = _mailbox(db_conn)
    record_attempt(db_conn, mailbox_id=mb, uid=7)
    assert list_gave_up(db_conn) == []


def test_purge_removes_tombstones_and_reports_how_many(db_conn):
    mb = _mailbox(db_conn)
    record_attempt(db_conn, mailbox_id=mb, uid=3)          # live hold, must survive
    for uid in (7, 8):
        record_attempt(db_conn, mailbox_id=mb, uid=uid)
        mark_gave_up(db_conn, mailbox_id=mb, uid=uid)

    assert purge_gave_up(db_conn) == 2
    assert list_gave_up(db_conn) == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT uid FROM transient_fetches WHERE mailbox_id = %s", (mb,))
        assert [r[0] for r in cur.fetchall()] == [3]


def test_purge_can_be_scoped_to_one_account(db_conn):
    mb_a = _mailbox(db_conn, account="acct-a")
    mb_b = _mailbox(db_conn, account="acct-b")
    for mb in (mb_a, mb_b):
        record_attempt(db_conn, mailbox_id=mb, uid=4)
        mark_gave_up(db_conn, mailbox_id=mb, uid=4)

    assert purge_gave_up(db_conn, account_name="acct-a") == 1
    assert [r.account_name for r in list_gave_up(db_conn)] == ["acct-b"]


def test_arming_reopens_the_hold_window_without_dropping_the_record(db_conn):
    """Re-driving must not delete the tombstone before the re-fetch has happened.

    Deleting it and rewinding the watermark are one transaction, but the
    watermark is not ours alone: `update_mailbox_progress` writes it
    unconditionally from a value the daemon computed at the *top* of its pass,
    so a daemon mid-pass on this mailbox overwrites the rewind at its next
    checkpoint. With a purge that costs the record too, and the operator was
    told it worked. Arming leaves the row, so a clobbered rewind is merely a
    re-run.

    Reopening the window is what makes the retry a retry at all: `load_attempts`
    does not filter tombstones, so a row carrying its original `first_seen_at`
    expires on sight and sync would give up again without ever re-fetching.
    """
    mb = _mailbox(db_conn)
    record_attempt(db_conn, mailbox_id=mb, uid=7)
    record_attempt(db_conn, mailbox_id=mb, uid=7)
    mark_gave_up(db_conn, mailbox_id=mb, uid=7)
    stamped = list_gave_up(db_conn)[0].gave_up_at
    # These timestamps are `now()`, i.e. transaction time — the arming has to
    # land in a later transaction for the reopened window to be observable.
    db_conn.commit()

    assert arm_for_retry(db_conn) == 1

    still_there = list_gave_up(db_conn)
    assert [r.uid for r in still_there] == [7], "the record must survive the arming"
    assert still_there[0].gave_up_at == stamped, "still absent since the same moment"
    assert load_attempts(db_conn, mb)[7].first_seen_at > stamped, "fresh window"
    assert still_there[0].attempt_count == 0


def test_arming_can_be_scoped_to_one_account(db_conn):
    mb_a = _mailbox(db_conn, account="acct-a")
    mb_b = _mailbox(db_conn, account="acct-b")
    for mb in (mb_a, mb_b):
        record_attempt(db_conn, mailbox_id=mb, uid=4)
        mark_gave_up(db_conn, mailbox_id=mb, uid=4)
    before = {mb: load_attempts(db_conn, mb)[4].first_seen_at for mb in (mb_a, mb_b)}
    db_conn.commit()

    assert arm_for_retry(db_conn, account_name="acct-a") == 1

    assert load_attempts(db_conn, mb_a)[4].first_seen_at > before[mb_a]
    assert load_attempts(db_conn, mb_b)[4].first_seen_at == before[mb_b]


def test_arming_leaves_live_holds_alone(db_conn):
    """A live hold is already inside its window; restarting it would extend the
    very pin the bounded hold exists to end."""
    mb = _mailbox(db_conn)
    record_attempt(db_conn, mailbox_id=mb, uid=3)
    before = load_attempts(db_conn, mb)[3]

    assert arm_for_retry(db_conn) == 0

    assert load_attempts(db_conn, mb)[3] == before


def test_purge_can_drop_only_tombstones_older_than_a_cutoff(db_conn):
    """The retention mechanism: opt-in, never automatic — see the CLI docstring."""
    mb = _mailbox(db_conn)
    for uid in (7, 8):
        record_attempt(db_conn, mailbox_id=mb, uid=uid)
        mark_gave_up(db_conn, mailbox_id=mb, uid=uid)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE transient_fetches SET gave_up_at = now() - interval '40 days'"
            " WHERE uid = 7"
        )
    db_conn.commit()

    assert purge_gave_up(db_conn, older_than_s=30 * 86_400) == 1
    assert [r.uid for r in list_gave_up(db_conn)] == [8]


# --- sync writes the tombstone ----------------------------------------------


def test_sync_tombstones_the_uid_it_gives_up_on(db_conn, tmp_path: Path):
    from tests import _eml
    from tests._fake_imap import FakeIMAPClient
    from tests.test_sync import _sync, make_account

    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())
    imap.append("INBOX", _eml.multipart_alt())
    imap.suppress_body = {1}

    # Window disabled -> the first sighting is already expired: give up at once.
    _sync(db_conn, imap, account=make_account(),
          attachments_root=tmp_path, max_body_fetch_hold_s=0.0)

    db_conn.rollback()
    rows = list_gave_up(db_conn)
    assert [(r.mailbox_name, r.uid) for r in rows] == [("INBOX", 1)]


def test_a_given_up_uid_survives_the_checkpoint_that_passes_it(db_conn, tmp_path: Path):
    """The watermark advancing past it is exactly when the record must persist."""
    from tests import _eml
    from tests._fake_imap import FakeIMAPClient
    from tests.test_sync import _sync, _uidnext, make_account

    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())
    imap.append("INBOX", _eml.multipart_alt())
    imap.suppress_body = {1}

    _sync(db_conn, imap, account=make_account(),
          attachments_root=tmp_path, max_body_fetch_hold_s=0.0)

    assert _uidnext(db_conn) == 3, "sync advanced past the unfetchable UID"
    assert [r.uid for r in list_gave_up(db_conn)] == [1]


def test_an_armed_tombstone_gets_a_real_retry_not_an_instant_give_up(
    db_conn, tmp_path: Path
):
    """The whole point of arming, end to end.

    `load_attempts` does not filter tombstones, so a rewound-but-unarmed row
    arrives at the next pass carrying its original `first_seen_at`, expires on
    sight, and sync gives up again without ever re-fetching — the retry command
    would be a no-op that costs a full re-scan. Armed, the UID gets a genuine
    fresh window and the watermark stays clamped to it.
    """
    from tests import _eml
    from tests._fake_imap import FakeIMAPClient
    from tests.test_sync import _sync, _uidnext, make_account

    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())
    imap.append("INBOX", _eml.multipart_alt())
    imap.suppress_body = {1}

    _sync(db_conn, imap, account=make_account(),
          attachments_root=tmp_path, max_body_fetch_hold_s=0.0)
    assert _uidnext(db_conn) == 3

    # What `retry-failed-fetches` does: rewind, then arm.
    with db_conn.cursor() as cur:
        cur.execute("UPDATE mailboxes SET uidnext = 1")
    arm_for_retry(db_conn)
    db_conn.commit()

    _sync(db_conn, imap, account=make_account(),
          attachments_root=tmp_path, max_body_fetch_hold_s=1800.0)

    assert _uidnext(db_conn) == 1, "the re-driven UID is held, not given up on again"
    assert [r.uid for r in list_gave_up(db_conn)] == [1], "still absent, still recorded"


def test_a_re_driven_uid_that_finally_fetches_clears_its_record(
    db_conn, tmp_path: Path
):
    """The one moment the archive may stop reporting the message as missing."""
    from tests import _eml
    from tests._fake_imap import FakeIMAPClient
    from tests.test_sync import _sync, make_account

    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())
    imap.suppress_body = {1}

    _sync(db_conn, imap, account=make_account(),
          attachments_root=tmp_path, max_body_fetch_hold_s=0.0)
    assert [r.uid for r in list_gave_up(db_conn)] == [1]

    with db_conn.cursor() as cur:
        cur.execute("UPDATE mailboxes SET uidnext = 1")
    arm_for_retry(db_conn)
    db_conn.commit()
    imap.suppress_body = set()

    _sync(db_conn, imap, account=make_account(),
          attachments_root=tmp_path, max_body_fetch_hold_s=1800.0)

    assert list_gave_up(db_conn) == []


# --- CLI --------------------------------------------------------------------


@pytest.fixture
def cli(cli_config):
    from click.testing import CliRunner

    from localmail.cli import main

    def run(*args: str):
        return CliRunner().invoke(main, ["--config", str(cli_config), *args])

    return run


def test_cli_list_failed_fetches_shows_the_tombstone(db_conn, cli):
    mb = _mailbox(db_conn, account="acct-a")
    record_attempt(db_conn, mailbox_id=mb, uid=7)
    mark_gave_up(db_conn, mailbox_id=mb, uid=7)
    db_conn.commit()

    result = cli("list-failed-fetches")

    assert result.exit_code == 0, result.output
    assert "acct-a" in result.output
    assert "INBOX" in result.output
    assert "uid=7" in result.output


def test_cli_list_failed_fetches_says_so_when_empty(db_conn, cli):
    result = cli("list-failed-fetches")
    assert result.exit_code == 0, result.output
    assert "no " in result.output.lower()


def test_cli_retry_rewinds_the_watermark_and_arms_the_tombstone(db_conn, cli):
    """The record is *armed*, not dropped — see `arm_for_retry`'s rationale.

    It clears when the message is actually fetched (`clear_attempts`), which is
    the only moment the archive can honestly stop reporting it as missing.
    """
    mb = _mailbox(db_conn, account="acct-a")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE mailboxes SET uidnext = 100 WHERE id = %s", (mb,))
    record_attempt(db_conn, mailbox_id=mb, uid=7)
    mark_gave_up(db_conn, mailbox_id=mb, uid=7)
    db_conn.commit()

    result = cli("retry-failed-fetches")

    assert result.exit_code == 0, result.output
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute("SELECT uidnext FROM mailboxes WHERE id = %s", (mb,))
        row = cur.fetchone()
        assert row is not None and row[0] == 7
    assert [r.uid for r in list_gave_up(db_conn)] == [7]
    assert load_attempts(db_conn, mb)[7].attempt_count == 0, "window reopened"


def test_cli_retry_reports_the_watermark_move_per_mailbox(db_conn, cli):
    """A rewind is the expensive half of this command; name what it moved."""
    mb = _mailbox(db_conn, account="acct-a")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE mailboxes SET uidnext = 100 WHERE id = %s", (mb,))
    record_attempt(db_conn, mailbox_id=mb, uid=7)
    mark_gave_up(db_conn, mailbox_id=mb, uid=7)
    db_conn.commit()

    result = cli("retry-failed-fetches")

    assert result.exit_code == 0, result.output
    assert "acct-a/INBOX" in result.output
    assert "100" in result.output and "7" in result.output


def test_cli_retry_dry_run_reports_the_plan_and_changes_nothing(db_conn, cli):
    mb = _mailbox(db_conn, account="acct-a")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE mailboxes SET uidnext = 100 WHERE id = %s", (mb,))
    record_attempt(db_conn, mailbox_id=mb, uid=7)
    mark_gave_up(db_conn, mailbox_id=mb, uid=7)
    db_conn.commit()
    before = load_attempts(db_conn, mb)[7]

    result = cli("retry-failed-fetches", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "acct-a/INBOX" in result.output
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute("SELECT uidnext FROM mailboxes WHERE id = %s", (mb,))
        row = cur.fetchone()
        assert row is not None and row[0] == 100, "dry run must not rewind"
    assert load_attempts(db_conn, mb)[7] == before


def test_cli_forget_dry_run_reports_without_dropping(db_conn, cli):
    mb = _mailbox(db_conn, account="acct-a")
    record_attempt(db_conn, mailbox_id=mb, uid=7)
    mark_gave_up(db_conn, mailbox_id=mb, uid=7)
    db_conn.commit()

    result = cli("retry-failed-fetches", "--forget", "--dry-run")

    assert result.exit_code == 0, result.output
    db_conn.rollback()
    assert [r.uid for r in list_gave_up(db_conn)] == [7]


def test_cli_retry_tolerates_a_mailbox_with_no_watermark_yet(db_conn, cli):
    """`mailboxes.uidnext` is nullable — a row exists before its first sync.

    Reading it as `int(None)` would crash the command outright; there is simply
    nothing to rewind to, which is the planner's existing "no known watermark"
    branch.
    """
    mb = _mailbox(db_conn, account="acct-a")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE mailboxes SET uidnext = NULL WHERE id = %s", (mb,))
    record_attempt(db_conn, mailbox_id=mb, uid=7)
    mark_gave_up(db_conn, mailbox_id=mb, uid=7)
    db_conn.commit()

    result = cli("retry-failed-fetches")

    assert result.exit_code == 0, result.output
    assert "rewound 0 mailbox(es)" in result.output
    db_conn.rollback()
    assert [r.uid for r in list_gave_up(db_conn)] == [7], "the record is kept"


def test_cli_list_failed_fetches_rejects_a_non_positive_limit(db_conn, cli):
    """`--limit 0` used to emit `LIMIT 0` and print the same 'nothing here' line
    as a genuinely empty table."""
    assert cli("list-failed-fetches", "--limit", "0").exit_code != 0


def test_cli_forget_purges_without_rewinding(db_conn, cli):
    """The operator accepting the loss must not trigger a full re-scan."""
    mb = _mailbox(db_conn, account="acct-a")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE mailboxes SET uidnext = 100 WHERE id = %s", (mb,))
    record_attempt(db_conn, mailbox_id=mb, uid=7)
    mark_gave_up(db_conn, mailbox_id=mb, uid=7)
    db_conn.commit()

    result = cli("retry-failed-fetches", "--forget")

    assert result.exit_code == 0, result.output
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute("SELECT uidnext FROM mailboxes WHERE id = %s", (mb,))
        row = cur.fetchone()
        assert row is not None and row[0] == 100
    assert list_gave_up(db_conn) == []


def test_cli_retry_rejects_an_unknown_account(db_conn, cli):
    result = cli("retry-failed-fetches", "--account", "nope")
    assert result.exit_code != 0
    assert "nope" in result.output
