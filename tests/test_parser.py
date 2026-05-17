import hashlib
from datetime import timezone

from localmail.parser import parse_message

from . import _eml


def test_plain_message():
    raw = _eml.plain()
    p = parse_message(raw)

    assert p.message_id == "<plain-123@example.com>"
    assert p.subject == "Hello"
    assert p.from_addr == "alice@example.com"
    assert p.from_name == "Alice"
    assert p.to_addrs == ["bob@example.com"]
    assert p.cc_addrs == []
    assert p.bcc_addrs == []
    assert p.body_text.strip() == "Hello Bob"
    assert p.body_html is None
    assert p.attachments == []
    assert p.date_sent is not None
    assert p.date_sent.tzinfo is not None
    assert p.date_sent.astimezone(timezone.utc).year == 2025
    assert p.raw_bytes == raw
    assert p.size_bytes == len(raw)
    assert p.raw_sha256 == hashlib.sha256(raw).digest()


def test_multipart_alternative_keeps_both_bodies():
    p = parse_message(_eml.multipart_alt())
    assert "Hello (text)" in p.body_text
    assert "<p>Hello (html)</p>" in p.body_html
    assert p.to_addrs == ["bob@example.com", "carol@example.com"]
    assert p.cc_addrs == ["watch@example.com"]
    assert p.in_reply_to == "<earlier@example.com>"
    assert p.refs == ["<root@example.com>", "<earlier@example.com>"]


def test_attachment_is_extracted():
    p = parse_message(_eml.with_attachment())
    assert len(p.attachments) == 1
    a = p.attachments[0]
    assert a.filename == "pixel.png"
    assert a.mime_type == "image/png"
    assert a.payload.startswith(b"\x89PNG")
    assert p.body_text.strip() == "See attached."


def test_utf8_subject_is_decoded():
    p = parse_message(_eml.utf8_subject())
    assert p.subject == "Tést héllo"


def test_missing_message_id_returns_none_but_has_sha():
    p = parse_message(_eml.no_message_id())
    assert p.message_id is None
    assert len(p.raw_sha256) == 32


def test_headers_are_dict_of_lists_and_jsonable():
    import json

    p = parse_message(_eml.multipart_alt())
    assert isinstance(p.headers, dict)
    for v in p.headers.values():
        assert isinstance(v, list)
        for item in v:
            assert isinstance(item, str)
    # Must round-trip through JSON for JSONB insertion.
    json.dumps(p.headers)


def test_attachment_only_message_gets_synthesized_subject_and_body():
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Date"] = "Wed, 01 Jan 2025 12:00:00 +0000"
    msg["Message-Id"] = "<atts-only@example.com>"
    # No Subject, no body text — only attachments.
    msg.make_mixed()
    msg.add_attachment(
        b"pdfbytes", maintype="application", subtype="pdf", filename="invoice.pdf"
    )
    msg.add_attachment(
        b"imgbytes", maintype="image", subtype="jpeg", filename="receipt.jpg"
    )

    p = parse_message(msg.as_bytes())

    assert p.subject == "{attachments only}"
    assert p.body_text == "{attachments: invoice.pdf, receipt.jpg}"
    assert len(p.attachments) == 2  # real attachments still intact


def test_attachments_present_but_subject_already_set_is_not_overwritten():
    p = parse_message(_eml.with_attachment())  # has Subject: "Photo" + body "See attached."
    assert p.subject == "Photo"
    assert "See attached" in (p.body_text or "")


def test_no_attachments_means_no_synthesis():
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Date"] = "Wed, 01 Jan 2025 12:00:00 +0000"
    msg["Message-Id"] = "<empty@example.com>"
    # Deliberately empty: no subject, no body, no attachments.
    p = parse_message(msg.as_bytes())
    # subject/body stay None — Postgres TEXT accepts NULL just fine.
    assert p.subject is None
    assert p.body_text is None
    assert p.attachments == []


def test_nul_bytes_in_text_fields_are_stripped():
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "before\x00after"
    msg["Date"] = "Wed, 01 Jan 2025 12:00:00 +0000"
    msg["Message-Id"] = "<nul-1@example.com>"
    msg.set_content("body before\x00body after")
    raw = msg.as_bytes()

    p = parse_message(raw)
    # Postgres TEXT rejects NUL bytes; the parser is the right place to strip them.
    assert "\x00" not in (p.subject or "")
    assert p.subject == "beforeafter"
    assert "\x00" not in (p.body_text or "")
    assert "body before" in p.body_text and "body after" in p.body_text


def test_two_attachments_same_filename_both_captured():
    p = parse_message(_eml.two_attachments_same_name())
    assert [a.filename for a in p.attachments] == ["note.txt", "note.txt"]
    assert p.attachments[0].payload == b"file-one"
    assert p.attachments[1].payload == b"file-two"


def test_inline_image_content_id_is_captured():
    """Inline images carry a Content-Id header — the parser must preserve it
    (sans angle brackets) so HTML rendering can rewrite cid: references."""
    p = parse_message(_eml.html_with_inline_image())
    assert len(p.attachments) == 1
    att = p.attachments[0]
    assert att.content_id == "inline-pixel@example"
    assert att.mime_type == "image/png"
    assert att.payload.startswith(b"\x89PNG")


def test_non_inline_attachment_has_none_content_id():
    p = parse_message(_eml.with_attachment())
    assert len(p.attachments) == 1
    assert p.attachments[0].content_id is None
