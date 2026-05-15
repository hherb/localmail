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


def test_two_attachments_same_filename_both_captured():
    p = parse_message(_eml.two_attachments_same_name())
    assert [a.filename for a in p.attachments] == ["note.txt", "note.txt"]
    assert p.attachments[0].payload == b"file-one"
    assert p.attachments[1].payload == b"file-two"
