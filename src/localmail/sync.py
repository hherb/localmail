# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Sync an IMAP account into PostgreSQL.

Per-mailbox algorithm:
    1. SELECT the mailbox; capture server UIDVALIDITY and UIDNEXT.
    2. If UIDVALIDITY changed (or we've never synced this mailbox) -> drop the
       message_labels rows for this mailbox and re-link by re-fetching all UIDs.
    3. Otherwise fetch only UIDs > stored uidnext.
    4. Per message: upsert into `messages` (per-account dedup by Message-Id, or
       by raw SHA-256 when Message-Id is absent); upsert `message_labels` row;
       on first insertion of a message that has attachments, write them under
       the attachments root and store the JSONB index.
    5. Checkpoint mailbox.uidnext every batch so a crash doesn't lose progress.
"""

from __future__ import annotations

import hashlib
import logging
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Protocol

import psycopg
from psycopg.types.json import Jsonb

from .attachments import write_attachments
from .config import AccountConfig
from .parser import ParsedMessage, parse_message

log = logging.getLogger(__name__)

BATCH_SIZE = 50


# --- IMAP client surface we depend on ----------------------------------------


class ImapLike(Protocol):
    """The subset of imapclient.IMAPClient that sync.py actually calls.

    Defined as a Protocol so tests can pass in an in-memory fake.
    """

    def list_folders(self) -> list[tuple]: ...
    def select_folder(self, folder: str) -> dict: ...
    def search(self, criteria) -> list[int]: ...
    def fetch(self, uids: list[int], data: list) -> dict: ...


# --- DB helpers ---------------------------------------------------------------


@dataclass
class MailboxRow:
    id: int
    name: str
    uidvalidity: int | None
    uidnext: int | None


def upsert_mailbox(
    conn: psycopg.Connection,
    *,
    account_id: int,
    name: str,
    delimiter: str | None,
    flags: list[str],
) -> MailboxRow:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mailboxes (account_id, name, delimiter, flags)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (account_id, name) DO UPDATE SET
                delimiter = EXCLUDED.delimiter,
                flags     = EXCLUDED.flags
            RETURNING id, name, uidvalidity, uidnext
            """,
            (account_id, name, delimiter, flags),
        )
        row = cur.fetchone()
        assert row is not None
        return MailboxRow(id=row[0], name=row[1], uidvalidity=row[2], uidnext=row[3])


def _existing_message_id(
    cur: psycopg.Cursor, *, account_id: int, message_id: str | None, raw_sha256: bytes
) -> int | None:
    if message_id is not None:
        cur.execute(
            "SELECT id FROM messages WHERE account_id=%s AND message_id=%s",
            (account_id, message_id),
        )
    else:
        cur.execute(
            """
            SELECT id FROM messages
            WHERE account_id=%s AND message_id IS NULL AND raw_sha256=%s
            """,
            (account_id, raw_sha256),
        )
    row = cur.fetchone()
    return row[0] if row else None


def upsert_message(
    conn: psycopg.Connection,
    *,
    account_id: int,
    parsed: ParsedMessage,
    internal_date: datetime | None = None,
) -> tuple[int, bool]:
    """Return (message_db_id, inserted_now).

    ``internal_date`` is the IMAP server's INTERNALDATE for the message
    (RFC 3501) — "when did this email arrive at the mailbox". When
    supplied, it lands in ``messages.internal_date`` and drives the
    canonical "newest first" sort. None is acceptable (parser-only
    callers, retry-failed paths) and leaves the column NULL until a
    later ``localmail backfill-internal-date`` pass populates it.
    """
    with conn.cursor() as cur:
        existing = _existing_message_id(
            cur,
            account_id=account_id,
            message_id=parsed.message_id,
            raw_sha256=parsed.raw_sha256,
        )
        if existing is not None:
            return existing, False

        cur.execute(
            """
            INSERT INTO messages (
                account_id, message_id, raw_sha256, in_reply_to, refs,
                subject, from_addr, from_name, to_addrs, cc_addrs, bcc_addrs,
                date_sent, internal_date, headers, body_text, body_html,
                raw_bytes, size_bytes
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s
            )
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (
                account_id,
                parsed.message_id,
                parsed.raw_sha256,
                parsed.in_reply_to,
                parsed.refs,
                parsed.subject,
                parsed.from_addr,
                parsed.from_name,
                parsed.to_addrs,
                parsed.cc_addrs,
                parsed.bcc_addrs,
                parsed.date_sent,
                internal_date,
                Jsonb(parsed.headers),
                parsed.body_text,
                parsed.body_html,
                parsed.raw_bytes,
                parsed.size_bytes,
            ),
        )
        row = cur.fetchone()
        if row is not None:
            return row[0], True
        # Lost a race: a concurrent writer (the daemon runs an IDLE thread on
        # INBOX and a poll thread on other folders per account, and Gmail
        # delivers the same Message-Id to several labels at once) inserted this
        # message between our existence check and INSERT. ON CONFLICT DO NOTHING
        # suppressed the duplicate insert; re-read the winner's id so this call
        # returns the shared row instead of raising UniqueViolation and being
        # recorded as a spurious poison-pill in failed_messages.
        existing = _existing_message_id(
            cur,
            account_id=account_id,
            message_id=parsed.message_id,
            raw_sha256=parsed.raw_sha256,
        )
        assert existing is not None
        return existing, False


def upsert_label(
    conn: psycopg.Connection,
    *,
    message_db_id: int,
    mailbox_id: int,
    uid: int,
    flags: list[str],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO message_labels (message_id, mailbox_id, uid, flags)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (message_id, mailbox_id) DO UPDATE SET
                uid   = EXCLUDED.uid,
                flags = EXCLUDED.flags
            """,
            (message_db_id, mailbox_id, uid, flags),
        )


def set_message_attachments(
    conn: psycopg.Connection, *, message_db_id: int, rows: list[dict]
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE messages SET attachments = %s WHERE id = %s",
            (Jsonb(rows), message_db_id),
        )


def update_mailbox_progress(
    conn: psycopg.Connection,
    *,
    mailbox_id: int,
    uidvalidity: int,
    uidnext: int,
    last_sync_at: datetime | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mailboxes
               SET uidvalidity  = %s,
                   uidnext      = %s,
                   last_sync_at = COALESCE(%s, last_sync_at)
             WHERE id = %s
            """,
            (uidvalidity, uidnext, last_sync_at, mailbox_id),
        )


def clear_mailbox_labels(conn: psycopg.Connection, mailbox_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM message_labels WHERE mailbox_id = %s", (mailbox_id,))


# --- per-message processing (shared by live sync and retry) ------------------


def process_one_message(
    conn: psycopg.Connection,
    *,
    account_id: int,
    mailbox_id: int,
    uid: int,
    raw: bytes,
    flags: list[str],
    attachments_root: Path,
    internal_date: datetime | None = None,
) -> tuple[int, bool]:
    """Parse one IMAP message, upsert it, link the label, write attachments.

    Returns `(message_db_id, did_insert)`. Caller is responsible for any
    surrounding transaction / savepoint. ``internal_date`` is the IMAP
    INTERNALDATE for new inserts; pass None when retrying a previously
    failed message (the original INTERNALDATE wasn't captured in
    ``failed_messages``), and the column will be left NULL for a later
    backfill pass.
    """
    parsed = parse_message(raw)
    db_id, did_insert = upsert_message(
        conn, account_id=account_id, parsed=parsed, internal_date=internal_date,
    )
    upsert_label(
        conn,
        message_db_id=db_id,
        mailbox_id=mailbox_id,
        uid=uid,
        flags=flags,
    )
    if did_insert and parsed.attachments:
        rows = write_attachments(conn, parsed, root=attachments_root)
        set_message_attachments(conn, message_db_id=db_id, rows=rows)
    return db_id, did_insert


def record_failed_message(
    conn: psycopg.Connection,
    *,
    account_id: int,
    mailbox_id: int,
    uid: int,
    raw: bytes,
    exc: BaseException,
) -> None:
    """Persist the raw bytes + exception details so a future `retry-failed`
    can re-attempt the message after a parser/sync fix, without re-fetching
    from IMAP.

    Idempotent on `(account_id, mailbox_id, uid)`: a re-raised failure
    overwrites the prior record and bumps `retry_count`.
    """
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    sha = hashlib.sha256(raw).digest()
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT fail_log")
        try:
            cur.execute(
                """
                INSERT INTO failed_messages
                    (account_id, mailbox_id, uid, raw_bytes, raw_sha256,
                     error_class, error_message, error_traceback)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_id, mailbox_id, uid) DO UPDATE SET
                    raw_bytes       = EXCLUDED.raw_bytes,
                    raw_sha256      = EXCLUDED.raw_sha256,
                    error_class     = EXCLUDED.error_class,
                    error_message   = EXCLUDED.error_message,
                    error_traceback = EXCLUDED.error_traceback,
                    failed_at       = now(),
                    retry_count     = failed_messages.retry_count + 1,
                    last_retry_at   = now()
                """,
                (
                    account_id, mailbox_id, uid, raw, sha,
                    type(exc).__name__, str(exc), tb,
                ),
            )
            cur.execute("RELEASE SAVEPOINT fail_log")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT fail_log")
            cur.execute("RELEASE SAVEPOINT fail_log")
            log.exception(
                "could not record failed message UID %s in mailbox %s; "
                "skipping log row",
                uid, mailbox_id,
            )


def retry_failed_messages(
    conn: psycopg.Connection,
    *,
    attachments_root: Path,
    account_id: int | None = None,
) -> tuple[int, int]:
    """Re-attempt every row in `failed_messages` (optionally scoped to one
    account). Successful re-imports DELETE the row; failures bump retry_count.
    Returns `(succeeded, still_failing)`.
    """
    sql = """
        SELECT id, account_id, mailbox_id, uid, raw_bytes
        FROM failed_messages
        {where}
        ORDER BY id
    """.format(where="WHERE account_id = %s" if account_id is not None else "")
    params: tuple = (account_id,) if account_id is not None else ()

    succeeded = 0
    still_failing = 0
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    for row_id, acct_id, mailbox_id, uid, raw in rows:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT retry")
        try:
            process_one_message(
                conn,
                account_id=acct_id,
                mailbox_id=mailbox_id,
                uid=int(uid),
                raw=bytes(raw),
                flags=[],  # flags weren't stored; safe default
                attachments_root=attachments_root,
            )
            with conn.cursor() as cur:
                cur.execute("RELEASE SAVEPOINT retry")
                cur.execute("DELETE FROM failed_messages WHERE id = %s", (row_id,))
            conn.commit()
            succeeded += 1
        except Exception as exc:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT retry")
                cur.execute("RELEASE SAVEPOINT retry")
            record_failed_message(
                conn,
                account_id=acct_id,
                mailbox_id=mailbox_id,
                uid=int(uid),
                raw=bytes(raw),
                exc=exc,
            )
            conn.commit()
            still_failing += 1
    return succeeded, still_failing


def backfill_internal_date(
    conn: psycopg.Connection,
    imap: "ImapLike",
    *,
    account_id: int,
    progress: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """Populate ``messages.internal_date`` for existing rows by re-fetching
    INTERNALDATE from IMAP. Returns ``(scanned, updated)``.

    Rationale: pre-migration-0018 syncs didn't store INTERNALDATE, and the
    legacy ``date_received`` column held sync time instead. After the
    migration, those rows have ``internal_date IS NULL``. This pass walks
    every mailbox of the account, fetches INTERNALDATE for the UIDs we
    know about (body bytes are not refetched — the FETCH is cheap), and
    fills the column.

    Idempotent: only NULL rows are updated. A row that gained
    ``internal_date`` via incremental sync since the migration is left
    alone. Mailboxes whose ``uidvalidity`` no longer matches what we have
    on disk are skipped — those UIDs are stale and the next regular sync
    will pick them up cleanly.
    """
    def _emit(msg: str) -> None:
        if progress is not None:
            progress(msg)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, uidvalidity FROM mailboxes WHERE account_id = %s",
            (account_id,),
        )
        mailboxes = cur.fetchall()

    scanned = 0
    updated = 0
    for mb_id, mb_name, expected_uidvalidity in mailboxes:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT m.id, ml.uid
                     FROM messages m
                     JOIN message_labels ml ON ml.message_id = m.id
                    WHERE ml.mailbox_id = %s
                      AND m.internal_date IS NULL""",
                (mb_id,),
            )
            msg_uid_pairs = cur.fetchall()
        if not msg_uid_pairs:
            continue

        try:
            select_resp = imap.select_folder(mb_name)
        except Exception as exc:
            log.warning("could not select %s for backfill: %s", mb_name, exc)
            continue
        server_uidvalidity = (
            select_resp.get(b"UIDVALIDITY") or select_resp.get("UIDVALIDITY")
        )
        if server_uidvalidity is not None and expected_uidvalidity is not None \
                and int(server_uidvalidity) != int(expected_uidvalidity):
            log.warning(
                "skipping %s: uidvalidity changed (%s -> %s); next regular "
                "sync will refresh",
                mb_name, expected_uidvalidity, server_uidvalidity,
            )
            continue

        msg_ids_by_uid = {int(uid): int(mid) for mid, uid in msg_uid_pairs}
        uids = sorted(msg_ids_by_uid.keys())
        scanned += len(uids)

        mailbox_updated = 0
        for chunk in _batches(uids, BATCH_SIZE):
            fetched = imap.fetch(chunk, [b"INTERNALDATE"])
            with conn.cursor() as cur:
                for uid_key, data in fetched.items():
                    int_date = (
                        data.get(b"INTERNALDATE") or data.get("INTERNALDATE")
                    )
                    if not isinstance(int_date, datetime):
                        continue
                    mid = msg_ids_by_uid.get(int(uid_key))
                    if mid is None:
                        continue
                    cur.execute(
                        "UPDATE messages SET internal_date = %s "
                        "WHERE id = %s AND internal_date IS NULL",
                        (int_date, mid),
                    )
                    if cur.rowcount > 0:
                        mailbox_updated += 1
            conn.commit()
        updated += mailbox_updated
        _emit(f"  {mb_name}: {mailbox_updated}/{len(uids)} backfilled")

    return scanned, updated


# --- folder filtering --------------------------------------------------------


def folders_to_sync(
    folders: Iterable[tuple],
    *,
    allow: list[str] | None,
    deny: list[str] | None,
    deny_flags: list[str] | None = None,
) -> list[tuple[str, str | None, list[str]]]:
    """Filter the IMAP LIST response into a set of folders we should sync.

    - `\\Noselect` folders are always excluded (they can't be SELECTed).
    - `allow` (if non-empty) restricts to exactly the named folders.
    - `deny` excludes folders by exact name (case-sensitive).
    - `deny_flags` excludes folders that carry any of these IMAP special-use
      flags (RFC 6154). Robust to provider locale: e.g. Gmail Trash is
      "[Gmail]/Bin" on en-AU but always carries `\\Trash`.
    """
    out: list[tuple[str, str | None, list[str]]] = []
    deny_set = set(deny or [])
    allow_set = set(allow or [])
    deny_flag_set = set(deny_flags or [])
    for entry in folders:
        flags_raw, delimiter, name = entry
        flag_strs = [
            f.decode() if isinstance(f, (bytes, bytearray)) else str(f)
            for f in (flags_raw or ())
        ]
        if "\\Noselect" in flag_strs:
            continue
        if deny_flag_set and any(f in deny_flag_set for f in flag_strs):
            continue
        if allow_set and name not in allow_set:
            continue
        if name in deny_set:
            continue
        delim = (
            delimiter.decode() if isinstance(delimiter, (bytes, bytearray)) else delimiter
        )
        out.append((name, delim, flag_strs))
    return out


# --- sync core ---------------------------------------------------------------


def _decode_flags(flags) -> list[str]:
    if not flags:
        return []
    return [
        f.decode() if isinstance(f, (bytes, bytearray)) else str(f) for f in flags
    ]


def _filter_new_uids(uids: list[int], known_uidnext: int | None) -> list[int]:
    if known_uidnext is None:
        return sorted(uids)
    return sorted(u for u in uids if u >= known_uidnext)


def sync_mailbox(
    conn: psycopg.Connection,
    imap: ImapLike,
    *,
    account_id: int,
    mailbox: MailboxRow,
    attachments_root: Path,
    max_messages: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> int:
    """Sync a single mailbox. Returns number of newly inserted messages.

    If `max_messages` is set, fetch at most that many UIDs in this run. The
    mailbox's `uidnext` checkpoint advances to the highest UID processed, so
    the next run resumes where this one stopped.
    """

    def _emit(msg: str) -> None:
        if progress is not None:
            progress(msg)

    status = imap.select_folder(mailbox.name)
    server_uidvalidity = int(status.get(b"UIDVALIDITY") or status.get("UIDVALIDITY") or 0)
    server_uidnext = int(status.get(b"UIDNEXT") or status.get("UIDNEXT") or 0)

    full_sync = mailbox.uidvalidity != server_uidvalidity
    if full_sync and mailbox.uidvalidity is not None:
        log.info(
            "uidvalidity changed for mailbox %s (%s -> %s); resyncing",
            mailbox.name, mailbox.uidvalidity, server_uidvalidity,
        )
        clear_mailbox_labels(conn, mailbox.id)
        conn.commit()

    if full_sync:
        uids = sorted(imap.search("ALL"))
    else:
        last = mailbox.uidnext or 1
        # SEARCH "UID N:*" always returns at least one result on a non-empty
        # mailbox, even if no messages have UID >= N. Filter manually below.
        candidate = imap.search(["UID", f"{last}:*"])
        uids = _filter_new_uids(candidate, last)

    total_candidates = len(uids)
    if max_messages is not None and total_candidates > max_messages:
        uids = uids[:max_messages]

    _emit(
        f"  {mailbox.name}: {len(uids)} of {total_candidates} candidate UID(s) "
        f"this run"
    )

    if not uids:
        update_mailbox_progress(
            conn,
            mailbox_id=mailbox.id,
            uidvalidity=server_uidvalidity,
            uidnext=server_uidnext or (mailbox.uidnext or 1),
            last_sync_at=datetime.now().astimezone(),
        )
        conn.commit()
        return 0

    inserted = 0
    skipped = 0
    seen = 0
    highest_seen = (mailbox.uidnext or 1) - 1
    for chunk in _batches(uids, BATCH_SIZE):
        fetched = imap.fetch(chunk, [b"BODY.PEEK[]", b"FLAGS", b"INTERNALDATE"])
        for uid in chunk:
            data = fetched.get(uid) or fetched.get(int(uid)) or {}
            raw = data.get(b"BODY[]") or data.get("BODY[]")
            if not raw:
                log.warning("UID %s in %s returned no body; skipping", uid, mailbox.name)
                highest_seen = max(highest_seen, int(uid))
                seen += 1
                continue

            flags_list = _decode_flags(data.get(b"FLAGS") or data.get("FLAGS"))
            internal_date_raw = data.get(b"INTERNALDATE") or data.get("INTERNALDATE")
            internal_date = internal_date_raw if isinstance(internal_date_raw, datetime) else None
            # Per-message SAVEPOINT — a single poison-pill row (e.g. an
            # unexpected encoding the parser/DB chokes on) only loses itself,
            # not the surrounding 49 messages' worth of work. On failure we
            # persist the raw bytes to failed_messages so `localmail
            # retry-failed` can re-attempt after a fix without re-fetching
            # from IMAP.
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT msg")
            try:
                _, did_insert = process_one_message(
                    conn,
                    account_id=account_id,
                    mailbox_id=mailbox.id,
                    uid=int(uid),
                    raw=raw,
                    flags=flags_list,
                    attachments_root=attachments_root,
                    internal_date=internal_date,
                )
                with conn.cursor() as cur:
                    cur.execute("RELEASE SAVEPOINT msg")
                if did_insert:
                    inserted += 1
            except Exception as exc:
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK TO SAVEPOINT msg")
                    cur.execute("RELEASE SAVEPOINT msg")
                log.exception(
                    "skipping poison-pill UID %s in %s/%s",
                    uid, account_id, mailbox.name,
                )
                record_failed_message(
                    conn,
                    account_id=account_id,
                    mailbox_id=mailbox.id,
                    uid=int(uid),
                    raw=raw,
                    exc=exc,
                )
                skipped += 1

            highest_seen = max(highest_seen, int(uid))
            seen += 1

        update_mailbox_progress(
            conn,
            mailbox_id=mailbox.id,
            uidvalidity=server_uidvalidity,
            uidnext=highest_seen + 1,
            last_sync_at=datetime.now().astimezone(),
        )
        conn.commit()
        suffix = f", {skipped} skipped" if skipped else ""
        _emit(
            f"  {mailbox.name}: {seen}/{len(uids)} processed, "
            f"+{inserted} new{suffix}"
        )

    return inserted


def sync_account(
    conn: psycopg.Connection,
    imap: ImapLike,
    *,
    account: AccountConfig,
    account_id: int,
    attachments_root: Path,
    max_messages: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Sync every mailbox of an account. Returns {mailbox_name: inserted}.

    The caller resolves `account_id` from the DB (the DB is canonical for
    accounts — Sub-plan 2A.2d). This function never creates the account row.
    """
    folders = imap.list_folders()
    selectable = folders_to_sync(
        folders,
        allow=account.folder_allow,
        deny=account.folder_deny,
        deny_flags=account.folder_deny_flags,
    )

    results: dict[str, int] = {}
    for name, delimiter, flags in selectable:
        mailbox = upsert_mailbox(
            conn,
            account_id=account_id,
            name=name,
            delimiter=delimiter,
            flags=flags,
        )
        conn.commit()
        results[name] = sync_mailbox(
            conn,
            imap,
            account_id=account_id,
            mailbox=mailbox,
            attachments_root=attachments_root,
            max_messages=max_messages,
            progress=progress,
        )
    return results


def _batches(items: list[int], size: int) -> Iterable[list[int]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
