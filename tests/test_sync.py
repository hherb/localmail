from pathlib import Path
from typing import Any

from localmail.config import AccountConfig
from localmail.sync import folders_to_sync, sync_account

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

    results = sync_account(
        db_conn, imap, account=make_account(), attachments_root=tmp_path
    )

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


def test_resync_inserts_zero_new_messages(db_conn, tmp_path: Path):
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())

    sync_account(db_conn, imap, account=make_account(), attachments_root=tmp_path)
    second = sync_account(
        db_conn, imap, account=make_account(), attachments_root=tmp_path
    )

    assert second == {"INBOX": 0}
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 1


def test_incremental_sync_picks_up_only_new_uids(db_conn, tmp_path: Path):
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())  # UID 1
    sync_account(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    imap.append("INBOX", _eml.multipart_alt())  # UID 2
    imap.append("INBOX", _eml.utf8_subject())   # UID 3

    second = sync_account(
        db_conn, imap, account=make_account(), attachments_root=tmp_path
    )
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

    sync_account(db_conn, imap, account=make_account(), attachments_root=tmp_path)

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
    sync_account(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    # Server reassigns UIDs. The same message body re-appears under UID 1.
    imap.folders["INBOX"].messages.clear()
    imap.bump_uidvalidity("INBOX")
    imap.append("INBOX", _eml.plain())

    sync_account(db_conn, imap, account=make_account(), attachments_root=tmp_path)

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

    sync_account(db_conn, imap, account=make_account(), attachments_root=tmp_path)

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

    sync_account(db_conn, imap, account=make_account(), attachments_root=tmp_path)

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

    def maybe_explode(conn, *, account_id, parsed):
        if parsed.message_id == "<alt-456@example.com>":
            raise ValueError("simulated psycopg.DataError on the poison message")
        return real_upsert(conn, account_id=account_id, parsed=parsed)

    monkeypatch.setattr(sync_mod, "upsert_message", maybe_explode)

    results = sync_account(
        db_conn, imap, account=make_account(), attachments_root=tmp_path
    )
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

    def maybe_explode(conn, *, account_id, parsed):
        if explode["on"]:
            raise ValueError("transient parser failure")
        return real_upsert(conn, account_id=account_id, parsed=parsed)

    monkeypatch.setattr(sync_mod, "upsert_message", maybe_explode)
    sync_account(db_conn, imap, account=make_account(), attachments_root=tmp_path)

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
    sync_account(db_conn, imap, account=make_account(), attachments_root=tmp_path)

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

    first = sync_account(
        db_conn, imap, account=make_account(),
        attachments_root=tmp_path, max_messages=2,
    )
    assert first == {"INBOX": 2}

    with db_conn.cursor() as cur:
        cur.execute("SELECT uidnext FROM mailboxes WHERE name='INBOX'")
        # We processed UIDs 1, 2 → uidnext should advance to 3.
        assert cur.fetchone()[0] == 3

    second = sync_account(
        db_conn, imap, account=make_account(), attachments_root=tmp_path,
    )
    assert second == {"INBOX": 3}

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 5


def test_progress_callback_is_invoked(db_conn, tmp_path: Path):
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())

    messages: list[str] = []
    sync_account(
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

    sync_account(db_conn, imap, account=make_account(), attachments_root=tmp_path)
    # Re-append the identical bytes under a new UID; should not duplicate.
    imap.append("INBOX", raw)
    sync_account(db_conn, imap, account=make_account(), attachments_root=tmp_path)

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages WHERE message_id IS NULL")
        assert cur.fetchone()[0] == 1
