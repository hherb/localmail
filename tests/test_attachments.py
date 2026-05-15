import hashlib
from pathlib import Path

from localmail.attachments import blob_path, sanitize_filename, write_attachments
from localmail.parser import Attachment, parse_message

from . import _eml


def test_blob_path_uses_two_level_hex_fanout(tmp_path: Path):
    p = blob_path(tmp_path, "3fa2c8" + "0" * 58)
    assert p.relative_to(tmp_path).parts == ("blobs", "3f", "a2", "3fa2c8" + "0" * 58)


def test_write_one_attachment_writes_blob_and_inserts_row(db_conn, tmp_path: Path):
    parsed = parse_message(_eml.with_attachment())
    rows = write_attachments(db_conn, parsed, root=tmp_path)
    db_conn.commit()

    assert len(rows) == 1
    row = rows[0]
    assert row["filename"] == "pixel.png"
    assert isinstance(row["sha256"], str) and len(row["sha256"]) == 64

    digest = bytes.fromhex(row["sha256"])
    assert digest == hashlib.sha256(parsed.attachments[0].payload).digest()

    on_disk = blob_path(tmp_path, row["sha256"])
    assert on_disk.exists()
    assert on_disk.read_bytes() == parsed.attachments[0].payload

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT path, mime_type, size_bytes FROM attachment_blobs WHERE sha256=%s",
            (digest,),
        )
        path, mime, size = cur.fetchone()
        assert Path(path) == on_disk.resolve()
        assert mime == "image/png"
        assert size == len(parsed.attachments[0].payload)


def test_same_blob_from_two_messages_writes_file_and_row_only_once(
    db_conn, tmp_path: Path
):
    parsed = parse_message(_eml.with_attachment())

    write_attachments(db_conn, parsed, root=tmp_path)
    rows = write_attachments(db_conn, parsed, root=tmp_path)
    db_conn.commit()

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM attachment_blobs")
        assert cur.fetchone()[0] == 1

    # Both calls return the same shape so the message's attachments JSONB is the
    # same regardless of who saw the bytes first.
    assert rows[0]["sha256"] == hashlib.sha256(parsed.attachments[0].payload).digest().hex()


def test_two_attachments_same_name_different_bytes_create_two_blobs(
    db_conn, tmp_path: Path
):
    parsed = parse_message(_eml.two_attachments_same_name())
    rows = write_attachments(db_conn, parsed, root=tmp_path)
    db_conn.commit()

    assert len(rows) == 2
    assert rows[0]["filename"] == "note.txt"
    assert rows[1]["filename"] == "note.txt"
    assert rows[0]["sha256"] != rows[1]["sha256"]  # different bytes -> different blobs

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM attachment_blobs")
        assert cur.fetchone()[0] == 2


def test_original_filename_preserved_per_message(db_conn, tmp_path: Path):
    payload = b"identical bytes across two emails"
    parsed_a = parse_message(_eml.plain())
    parsed_a.attachments = [Attachment(filename="invoice-Q1.pdf", mime_type="application/pdf", payload=payload)]
    parsed_b = parse_message(_eml.multipart_alt())
    parsed_b.attachments = [Attachment(filename="Invoice 2024.pdf", mime_type="application/pdf", payload=payload)]

    rows_a = write_attachments(db_conn, parsed_a, root=tmp_path)
    rows_b = write_attachments(db_conn, parsed_b, root=tmp_path)
    db_conn.commit()

    # One shared blob.
    assert rows_a[0]["sha256"] == rows_b[0]["sha256"]
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM attachment_blobs")
        assert cur.fetchone()[0] == 1

    # But each message's JSONB keeps its own filename — that's what allows
    # restoring with the original names.
    assert rows_a[0]["filename"] == "invoice-Q1.pdf"
    assert rows_b[0]["filename"] == "Invoice 2024.pdf"


def test_no_attachments_returns_empty_list_and_writes_nothing(db_conn, tmp_path: Path):
    parsed = parse_message(_eml.plain())
    rows = write_attachments(db_conn, parsed, root=tmp_path)
    db_conn.commit()

    assert rows == []
    assert not (tmp_path / "blobs").exists()
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM attachment_blobs")
        assert cur.fetchone()[0] == 0


def test_sanitize_filename_strips_path_separators_and_control_chars():
    assert sanitize_filename("../etc/passwd") == "etc_passwd"
    assert sanitize_filename("a/b\\c") == "a_b_c"
    assert sanitize_filename("foo\x00bar") == "foo_bar"
    assert sanitize_filename(".hidden") == "hidden"
    assert sanitize_filename("") == "attachment"
    assert sanitize_filename("   ") == "attachment"
