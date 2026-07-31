# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Helpers that build RFC822 byte streams for tests, so fixtures are self-contained."""

from __future__ import annotations

from email.message import EmailMessage


def plain() -> bytes:
    msg = EmailMessage()
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "Hello"
    msg["Date"] = "Wed, 01 Jan 2025 12:00:00 +0000"
    msg["Message-Id"] = "<plain-123@example.com>"
    msg.set_content("Hello Bob\n")
    return msg.as_bytes()


def multipart_alt() -> bytes:
    msg = EmailMessage()
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "Bob <bob@example.com>, Carol <carol@example.com>"
    msg["Cc"] = "watch@example.com"
    msg["Subject"] = "Pretty hello"
    msg["Date"] = "Wed, 01 Jan 2025 12:00:00 +0000"
    msg["Message-Id"] = "<alt-456@example.com>"
    msg["In-Reply-To"] = "<earlier@example.com>"
    msg["References"] = "<root@example.com> <earlier@example.com>"
    msg.set_content("Hello (text)")
    msg.add_alternative("<p>Hello (html)</p>", subtype="html")
    return msg.as_bytes()


def with_attachment() -> bytes:
    # 1x1 transparent PNG.
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "Photo"
    msg["Date"] = "Wed, 15 Jan 2025 09:30:00 +0000"
    msg["Message-Id"] = "<att-789@example.com>"
    msg.set_content("See attached.")
    msg.add_attachment(png_bytes, maintype="image", subtype="png", filename="pixel.png")
    return msg.as_bytes()


def utf8_subject() -> bytes:
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "Tést héllo"
    msg["Date"] = "Wed, 01 Jan 2025 12:00:00 +0000"
    msg["Message-Id"] = "<u8@example.com>"
    msg.set_content("Hello")
    return msg.as_bytes()


def no_message_id() -> bytes:
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "no msgid"
    msg["Date"] = "Wed, 01 Jan 2025 12:00:00 +0000"
    msg.set_content("Hello")
    return msg.as_bytes()


def degenerate_message_id(body: str, *, header_value: str = "<>") -> bytes:
    """A message whose Message-Id header is present but carries no identity (#222B).

    Built by hand: `EmailMessage.__setitem__` would not emit these forms.

    The default is an **empty angle-addr**, which is the form that actually
    reaches the parser intact -- `email.policy.default` already collapses a
    whitespace-only header body to `""` (so the pre-fix `if message_id` guard
    caught those), but `<>` survives as a truthy, non-unique string.
    """
    return (
        b"From: alice@example.com\r\n"
        b"To: bob@example.com\r\n"
        b"Subject: degenerate\r\n"
        b"Date: Wed, 01 Jan 2025 12:00:00 +0000\r\n"
        b"Message-Id: " + header_value.encode() + b"\r\n"
        b"\r\n" + body.encode() + b"\r\n"
    )


def two_attachments_same_name() -> bytes:
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "Two files, same name"
    msg["Date"] = "Wed, 15 Jan 2025 09:30:00 +0000"
    msg["Message-Id"] = "<dup-001@example.com>"
    msg.set_content("Body")
    msg.add_attachment(b"file-one", maintype="text", subtype="plain", filename="note.txt")
    msg.add_attachment(b"file-two", maintype="text", subtype="plain", filename="note.txt")
    return msg.as_bytes()


def html_with_inline_image() -> bytes:
    """HTML message with an inline image referenced by Content-Id."""
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "Inline image"
    msg["Date"] = "Wed, 15 Jan 2025 09:30:00 +0000"
    msg["Message-Id"] = "<inline-1@example.com>"
    msg.set_content("plain text fallback")
    msg.add_alternative(
        '<html><body><img src="cid:inline-pixel@example"></body></html>',
        subtype="html",
    )
    msg.add_attachment(
        png_bytes,
        maintype="image",
        subtype="png",
        filename="inline.png",
        cid="<inline-pixel@example>",
        disposition="inline",
    )
    return msg.as_bytes()
