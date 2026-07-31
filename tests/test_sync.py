# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from localmail.config import AccountConfig
from localmail.account_seed import account_create_kwargs
from localmail.api.admin.accounts import create_account, get_account_by_name
from localmail.sync import backfill_internal_date, folders_to_sync, sync_account

from . import _eml
from ._fake_imap import FakeIMAPClient


def make_account(**over: Any) -> AccountConfig:
    defaults: dict[str, Any] = dict(
        name="acct",
        email="me@example.com",
        imap_host="imap.example.com",
        imap_port=993,
        auth_method="password",
    )
    defaults.update(over)
    return AccountConfig(**defaults)


def _ensure_account(conn, account: AccountConfig) -> int:
    existing = get_account_by_name(conn, account.name)
    if existing is not None:
        return existing.id
    return create_account(conn, **account_create_kwargs(account)).id


def _sync(conn, imap, *, account: AccountConfig | None = None, **kw):
    account = account or make_account()
    account_id = _ensure_account(conn, account)
    return sync_account(conn, imap, account=account, account_id=account_id, **kw)


# --- folder filter unit tests ------------------------------------------------


def test_folder_filter_excludes_noselect_and_deny():
    folders = [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\Noselect",), b"/", "[Gmail]"),
        ((b"\\All",), b"/", "[Gmail]/All Mail"),
        ((), b"/", "Work"),
    ]
    out = folders_to_sync(folders, allow=None, deny=["[Gmail]/All Mail"])
    assert [name for name, _, _ in out] == ["INBOX", "Work"]


def test_folder_filter_allow_list_restricts():
    folders = [
        ((), b"/", "INBOX"),
        ((), b"/", "Work"),
        ((), b"/", "Personal"),
    ]
    out = folders_to_sync(folders, allow=["INBOX"], deny=None)
    assert [name for name, _, _ in out] == ["INBOX"]


def test_folder_filter_excludes_by_special_use_flag():
    # Gmail tags Trash as [Gmail]/Bin on en-AU but always with the \Trash flag.
    folders = [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\HasNoChildren", b"\\Trash"), b"/", "[Gmail]/Bin"),
        ((b"\\HasNoChildren", b"\\Junk"), b"/", "[Gmail]/Spam"),
        ((b"\\HasNoChildren", b"\\All"), b"/", "[Gmail]/All Mail"),
        ((b"\\HasNoChildren", b"\\Sent"), b"/", "[Gmail]/Sent Mail"),
    ]
    out = folders_to_sync(
        folders,
        allow=None,
        deny=None,
        deny_flags=["\\Trash", "\\Junk", "\\All"],
    )
    assert [name for name, _, _ in out] == ["INBOX", "[Gmail]/Sent Mail"]


# --- end-to-end sync (against real Postgres + fake IMAP) ----------------------


def test_first_sync_imports_all_messages(db_conn, tmp_path: Path):
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())
    imap.append("INBOX", _eml.multipart_alt())

    results = _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    assert results == {"INBOX": 2}
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(*) FROM message_labels")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT uidnext, uidvalidity FROM mailboxes WHERE name='INBOX'")
        uidnext, uidvalidity = cur.fetchone()
        assert uidvalidity == 1
        assert uidnext == 3  # max UID seen (2) + 1


def test_sync_persists_imap_internaldate_to_internal_date_column(
    db_conn, tmp_path: Path,
) -> None:
    """sync.py must thread IMAP INTERNALDATE into `messages.internal_date`.

    Before this fix, `sync_mailbox` fetched INTERNALDATE alongside
    BODY[]/FLAGS but discarded it — every row landed with `date_received
    = DEFAULT now()` and no record of when the email actually arrived at
    the IMAP server. The user-visible symptom was that "newest mail on
    top" surfaced freshly-synced archive backfills above genuinely-recent
    arrivals. This test pins the wiring so a future refactor can't break
    it silently.
    """
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    sent_at_imap = datetime(2024, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
    imap.append("INBOX", _eml.plain(), internal_date=sent_at_imap)

    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    with db_conn.cursor() as cur:
        cur.execute("SELECT internal_date FROM messages")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == sent_at_imap


def test_sync_leaves_internal_date_null_when_imap_omits_it(
    db_conn, tmp_path: Path,
) -> None:
    """Some IMAP servers / fetches may omit INTERNALDATE (or return a
    non-datetime sentinel). The column must accept NULL in that case;
    the backfill CLI can populate it later from a dedicated FETCH pass.
    """
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())  # no internal_date

    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    with db_conn.cursor() as cur:
        cur.execute("SELECT internal_date FROM messages")
        row = cur.fetchone()
        assert row is not None
        assert row[0] is None


def test_backfill_internal_date_fills_nulls_from_imap(
    db_conn, tmp_path: Path,
) -> None:
    """`backfill_internal_date` re-fetches INTERNALDATE for rows where
    the column is NULL and writes it. The legacy archive — synced before
    INTERNALDATE was threaded through — is the primary motivation.
    """
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    # First sync without INTERNALDATE so internal_date lands as NULL —
    # mimics the pre-migration-0018 archive state.
    imap.append("INBOX", _eml.plain())
    imap.append("INBOX", _eml.utf8_subject())
    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages WHERE internal_date IS NULL")
        assert cur.fetchone()[0] == 2

    # Now the IMAP server starts returning INTERNALDATE for those UIDs.
    expected = {
        1: datetime(2022, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        2: datetime(2023, 6, 15, 14, 30, 0, tzinfo=timezone.utc),
    }
    inbox = imap.folders["INBOX"]
    for uid, when in expected.items():
        raw, flags, _ = inbox.messages[uid]
        inbox.messages[uid] = (raw, flags, when)

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM accounts WHERE name = 'acct'")
        account_id = cur.fetchone()[0]
    scanned, updated = backfill_internal_date(
        db_conn, imap, account_id=account_id,
    )
    assert (scanned, updated) == (2, 2)
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT ml.uid, m.internal_date FROM messages m "
            "JOIN message_labels ml ON ml.message_id = m.id ORDER BY ml.uid"
        )
        rows = cur.fetchall()
    assert {uid: when for uid, when in rows} == expected


def test_backfill_internal_date_is_idempotent_and_skips_populated_rows(
    db_conn, tmp_path: Path,
) -> None:
    """Already-populated rows must not be overwritten by a re-run — the
    column may carry a different (more authoritative) value supplied by
    a custom path, and a backfill pass shouldn't clobber it.
    """
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    when_imap = datetime(2025, 1, 1, tzinfo=timezone.utc)
    imap.append("INBOX", _eml.plain(), internal_date=when_imap)
    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    # Sanity: the regular sync already populated internal_date.
    with db_conn.cursor() as cur:
        cur.execute("SELECT internal_date FROM messages")
        assert cur.fetchone()[0] == when_imap

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM accounts WHERE name = 'acct'")
        account_id = cur.fetchone()[0]
    scanned, updated = backfill_internal_date(
        db_conn, imap, account_id=account_id,
    )
    assert (scanned, updated) == (0, 0)


def test_resync_inserts_zero_new_messages(db_conn, tmp_path: Path):
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())

    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)
    second = _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    assert second == {"INBOX": 0}
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 1


def test_incremental_sync_picks_up_only_new_uids(db_conn, tmp_path: Path):
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())  # UID 1
    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    imap.append("INBOX", _eml.multipart_alt())  # UID 2
    imap.append("INBOX", _eml.utf8_subject())   # UID 3

    second = _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)
    assert second == {"INBOX": 2}


def test_same_message_in_two_folders_creates_one_message_two_labels(
    db_conn, tmp_path: Path
):
    raw = _eml.plain()
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.add_folder("Archive")
    imap.append("INBOX", raw)
    imap.append("Archive", raw)

    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT m.name FROM message_labels ml JOIN mailboxes m ON m.id=ml.mailbox_id "
            "ORDER BY m.name"
        )
        assert [row[0] for row in cur.fetchall()] == ["Archive", "INBOX"]


def test_uidvalidity_change_triggers_full_resync(db_conn, tmp_path: Path):
    imap = FakeIMAPClient()
    imap.add_folder("INBOX", uidvalidity=10)
    imap.append("INBOX", _eml.plain())
    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    # Server reassigns UIDs. The same message body re-appears under UID 1.
    imap.folders["INBOX"].messages.clear()
    imap.bump_uidvalidity("INBOX")
    imap.append("INBOX", _eml.plain())

    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 1  # still one — Message-Id dedup kicks in
        cur.execute("SELECT count(*) FROM message_labels")
        # Labels for the *old* mailbox state were cleared on UIDVALIDITY change,
        # then re-linked under the new UID. Final count = 1.
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT uidvalidity FROM mailboxes WHERE name='INBOX'")
        assert cur.fetchone()[0] == 11


def test_attachments_are_written_and_recorded(db_conn, tmp_path: Path):
    from localmail.attachments import blob_path

    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.with_attachment())

    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    with db_conn.cursor() as cur:
        cur.execute("SELECT id, attachments FROM messages")
        rows = cur.fetchall()
    assert len(rows) == 1
    _, atts = rows[0]
    assert isinstance(atts, list) and len(atts) == 1
    entry = atts[0]
    assert entry["filename"] == "pixel.png"
    assert isinstance(entry["sha256"], str) and len(entry["sha256"]) == 64

    on_disk = blob_path(tmp_path, entry["sha256"])
    assert on_disk.exists()

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT mime_type, size_bytes FROM attachment_blobs WHERE sha256=%s",
            (bytes.fromhex(entry["sha256"]),),
        )
        mime, size = cur.fetchone()
        assert mime == "image/png"
        assert size == on_disk.stat().st_size


def test_same_attachment_in_two_messages_dedupes_to_one_blob(db_conn, tmp_path: Path):
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    # Two messages that happen to share the same attachment payload but have
    # distinct Message-Ids so they're two separate messages rows.
    raw_a = _eml.with_attachment()
    raw_b = raw_a.replace(b"<att-789@example.com>", b"<att-790@example.com>")
    imap.append("INBOX", raw_a)
    imap.append("INBOX", raw_b)

    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(*) FROM attachment_blobs")
        assert cur.fetchone()[0] == 1  # only one blob despite two attachments


def test_poison_pill_message_is_skipped_without_breaking_the_batch(
    db_conn, tmp_path: Path, monkeypatch
):
    from localmail import sync as sync_mod

    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())                               # UID 1: good
    imap.append("INBOX", _eml.multipart_alt())                       # UID 2: poison
    imap.append("INBOX", _eml.utf8_subject())                        # UID 3: good

    real_upsert = sync_mod.upsert_message

    def maybe_explode(conn, *, account_id, parsed, internal_date=None):
        if parsed.message_id == "<alt-456@example.com>":
            raise ValueError("simulated psycopg.DataError on the poison message")
        return real_upsert(conn, account_id=account_id, parsed=parsed, internal_date=internal_date)

    monkeypatch.setattr(sync_mod, "upsert_message", maybe_explode)

    results = _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)
    # UID 1 and UID 3 inserted; UID 2 skipped.
    assert results == {"INBOX": 2}

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 2
        # uidnext must advance past the poison pill so we don't re-attempt forever.
        cur.execute("SELECT uidnext FROM mailboxes WHERE name='INBOX'")
        assert cur.fetchone()[0] == 4

        # The poison pill must be in failed_messages with full raw bytes,
        # so a future `retry-failed` can re-attempt it after a fix.
        cur.execute(
            "SELECT uid, error_class, error_message, raw_bytes IS NOT NULL "
            "FROM failed_messages"
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        uid, ecls, emsg, has_raw = rows[0]
        assert uid == 2
        assert ecls == "ValueError"
        assert "poison" in emsg
        assert has_raw is True


def test_retry_failed_messages_recovers_after_parser_fix(
    db_conn, tmp_path: Path, monkeypatch
):
    from localmail import sync as sync_mod
    from localmail.sync import retry_failed_messages

    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())              # UID 1: will fail then succeed

    real_upsert = sync_mod.upsert_message
    explode = {"on": True}

    def maybe_explode(conn, *, account_id, parsed, internal_date=None):
        if explode["on"]:
            raise ValueError("transient parser failure")
        return real_upsert(conn, account_id=account_id, parsed=parsed, internal_date=internal_date)

    monkeypatch.setattr(sync_mod, "upsert_message", maybe_explode)
    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM failed_messages")
        assert cur.fetchone()[0] == 1

    # Now "fix" the parser and retry.
    explode["on"] = False
    ok, still = retry_failed_messages(db_conn, attachments_root=tmp_path)
    assert (ok, still) == (1, 0)

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM failed_messages")
        assert cur.fetchone()[0] == 0


def test_retry_still_failing_bumps_retry_count(db_conn, tmp_path: Path, monkeypatch):
    from localmail import sync as sync_mod
    from localmail.sync import retry_failed_messages

    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())

    def always_explode(conn, *, account_id, parsed):
        raise ValueError("still broken")

    monkeypatch.setattr(sync_mod, "upsert_message", always_explode)
    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    ok, still = retry_failed_messages(db_conn, attachments_root=tmp_path)
    assert (ok, still) == (0, 1)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT retry_count, last_retry_at IS NOT NULL FROM failed_messages"
        )
        retry_count, has_last_retry = cur.fetchone()
        assert retry_count >= 1
        assert has_last_retry is True


def test_max_messages_caps_inserts_and_next_run_resumes(db_conn, tmp_path: Path):
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    for _ in range(5):
        imap.append("INBOX", _eml.plain())
    # Five identical bodies → only one unique by Message-Id, so generate distinct ones:
    imap.folders["INBOX"].messages.clear()
    for i in range(5):
        msg = _eml.plain().replace(b"<plain-123@example.com>", f"<m{i}@e.com>".encode())
        imap.append("INBOX", msg)

    first = _sync(
        db_conn, imap, account=make_account(),
        attachments_root=tmp_path, max_messages=2,
    )
    assert first == {"INBOX": 2}

    with db_conn.cursor() as cur:
        cur.execute("SELECT uidnext FROM mailboxes WHERE name='INBOX'")
        # We processed UIDs 1, 2 → uidnext should advance to 3.
        assert cur.fetchone()[0] == 3

    second = _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)
    assert second == {"INBOX": 3}

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 5


def test_progress_callback_is_invoked(db_conn, tmp_path: Path):
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())

    messages: list[str] = []
    _sync(
        db_conn, imap, account=make_account(),
        attachments_root=tmp_path, progress=messages.append,
    )
    assert any("INBOX" in m and "candidate" in m for m in messages)
    assert any("processed" in m for m in messages)


def test_messages_without_message_id_dedup_via_sha(db_conn, tmp_path: Path):
    raw = _eml.no_message_id()
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", raw)

    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)
    # Re-append the identical bytes under a new UID; should not duplicate.
    imap.append("INBOX", raw)
    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages WHERE message_id IS NULL")
        assert cur.fetchone()[0] == 1




def test_two_messages_with_blank_message_id_stay_distinct(db_conn, tmp_path: Path):
    """#222B: a present-but-blank Message-Id must not collapse distinct mail.

    Both messages carry `Message-Id:` with nothing but whitespace. Before the
    fix the header parsed as a non-None, non-unique string, so the second
    message deduped onto the first's row and its body was discarded.
    """
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.degenerate_message_id("first body"))
    imap.append("INBOX", _eml.degenerate_message_id("second body"))

    results = _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    assert results == {"INBOX": 2}
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages WHERE message_id IS NULL")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(DISTINCT raw_sha256) FROM messages")
        assert cur.fetchone()[0] == 2


# --- empty BODY[] on FETCH: expunged vs transient (#222A) ---------------------


def _uidnext(conn, name: str = "INBOX") -> int:
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SELECT uidnext FROM mailboxes WHERE name=%s", (name,))
        return cur.fetchone()[0]


def test_expunged_uid_does_not_hold_the_resume_watermark(db_conn, tmp_path: Path):
    """A UID swept but since expunged is unrecoverable — advance past it.

    Holding the watermark here would pin the mailbox forever, re-fetching the
    whole tail on every run for a message that no longer exists.
    """
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())          # uid 1
    imap.append("INBOX", _eml.multipart_alt())  # uid 2
    imap.phantom_uids = {3}                     # swept, already gone

    results = _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    assert results == {"INBOX": 2}
    assert _uidnext(db_conn) == 4, "must advance past the expunged UID"


def test_transient_empty_body_holds_the_watermark_and_the_next_run_recovers(
    db_conn, tmp_path: Path,
):
    """A UID still on the server but returning no BODY[] must not be lost.

    uid 2's body is suppressed while uids 1 and 3 ingest normally. Because
    `highest_seen` is a running max, uid 3 would otherwise carry the watermark
    past uid 2 and the message would be skipped permanently.
    """
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())          # uid 1
    imap.append("INBOX", _eml.multipart_alt())  # uid 2 — suppressed
    imap.append("INBOX", _eml.utf8_subject())   # uid 3
    imap.suppress_body = {2}

    first = _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    assert first == {"INBOX": 2}
    assert _uidnext(db_conn) == 2, "the resume point must be clamped to the stuck UID"

    imap.suppress_body = set()  # hiccup clears
    second = _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    assert second == {"INBOX": 1}, "the held-back message is picked up next run"
    assert _uidnext(db_conn) == 4
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 3
        cur.execute("SELECT count(*) FROM failed_messages")
        assert cur.fetchone()[0] == 0


def test_a_failing_existence_probe_is_treated_as_transient(db_conn, tmp_path: Path):
    """If the probe itself errors we cannot tell expunged from transient.

    Assuming "still there" costs one re-fetch next run; assuming "gone" would
    silently drop a real message, so the probe fails safe.
    """
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())          # uid 1
    imap.append("INBOX", _eml.multipart_alt())  # uid 2 — suppressed
    imap.suppress_body = {2}

    real_search = imap.search

    def flaky_search(criteria):
        # Only the single-UID probe fails; the mailbox sweep still works.
        if isinstance(criteria, list) and criteria[:1] == ["UID"] and ":*" not in criteria[1]:
            raise OSError("probe blew up")
        return real_search(criteria)

    imap.search = flaky_search  # type: ignore[method-assign]

    _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    assert _uidnext(db_conn) == 2, "an unknown outcome must hold the resume point"


def test_a_permanently_unfetchable_uid_is_given_up_once_the_window_passes(
    db_conn, tmp_path: Path,
):
    """The hold must be bounded (#222A follow-up).

    A zero-length or corrupt-store message is "still present" forever, so an
    unbounded hold would pin the mailbox and re-fetch its whole tail on every
    run — and the IDLE thread re-syncs INBOX on *every* notification.
    """
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())          # uid 1
    imap.append("INBOX", _eml.multipart_alt())  # uid 2 — never fetchable
    imap.append("INBOX", _eml.utf8_subject())   # uid 3
    imap.suppress_body = {2}

    # A generous window: the hold survives repeated passes, however many.
    for _ in range(3):
        _sync(db_conn, imap, account=make_account(),
              attachments_root=tmp_path, max_body_fetch_hold_s=3600.0)
        assert _uidnext(db_conn) == 2, "held while the window lasts"

    # Same UID, window now elapsed.
    _sync(db_conn, imap, account=make_account(),
          attachments_root=tmp_path, max_body_fetch_hold_s=0.0)

    assert _uidnext(db_conn) == 4, "past the window sync gives up and advances"
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM transient_fetches")
        assert cur.fetchone()[0] == 0, "giving up must not leave the row behind"


def test_a_successful_fetch_clears_the_hold_history(db_conn, tmp_path: Path):
    """The window measures a *continuous* outage, so recovery must reset it."""
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())          # uid 1
    imap.append("INBOX", _eml.multipart_alt())  # uid 2 — suppressed, then not
    imap.suppress_body = {2}

    _sync(db_conn, imap, account=make_account(),
          attachments_root=tmp_path, max_body_fetch_hold_s=3600.0)
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute("SELECT attempt_count FROM transient_fetches")
        assert [r[0] for r in cur.fetchall()] == [1]

    imap.suppress_body = set()
    _sync(db_conn, imap, account=make_account(),
          attachments_root=tmp_path, max_body_fetch_hold_s=3600.0)

    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM transient_fetches")
        assert cur.fetchone()[0] == 0, "history must not survive a good fetch"


def test_a_uidvalidity_reset_drops_stale_hold_history(db_conn, tmp_path: Path):
    """Renumbered UIDs must not inherit an old UID's nearly-expired window.

    Otherwise a brand-new message reusing that number could be given up on at
    its very first sighting.
    """
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())          # uid 1
    imap.append("INBOX", _eml.multipart_alt())  # uid 2 — suppressed
    imap.suppress_body = {2}

    _sync(db_conn, imap, account=make_account(),
          attachments_root=tmp_path, max_body_fetch_hold_s=3600.0)
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM transient_fetches")
        assert cur.fetchone()[0] == 1

    imap.bump_uidvalidity("INBOX")
    imap.suppress_body = set()
    _sync(db_conn, imap, account=make_account(),
          attachments_root=tmp_path, max_body_fetch_hold_s=3600.0)

    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM transient_fetches")
        assert cur.fetchone()[0] == 0


def test_hold_history_below_the_resume_point_is_reclaimed(db_conn, tmp_path: Path):
    """An expunged UID recorded as held — the probe is skipped once the run
    knows the server is emptying bodies — would otherwise leak a row forever."""
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())          # uid 1
    imap.append("INBOX", _eml.multipart_alt())  # uid 2
    imap.suppress_body = {1, 2}

    # Window disabled: both are given up on at once, so the watermark advances
    # past both and no history may survive.
    _sync(db_conn, imap, account=make_account(),
          attachments_root=tmp_path, max_body_fetch_hold_s=0.0)

    assert _uidnext(db_conn) == 3
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM transient_fetches")
        assert cur.fetchone()[0] == 0
