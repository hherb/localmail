from localmail.api.sanitize import sanitize_html


def test_script_tag_stripped() -> None:
    html = "<p>hi</p><script>alert(1)</script>"
    out = sanitize_html(html, cid_to_sha={})
    assert "<script>" not in out
    assert "alert" not in out
    assert "<p>hi</p>" in out


def test_event_handlers_stripped() -> None:
    html = '<a href="x" onclick="bad()">click</a>'
    out = sanitize_html(html, cid_to_sha={})
    assert "onclick" not in out
    assert ">click</a>" in out


def test_external_image_src_stripped() -> None:
    html = '<img src="https://tracker.example/pixel.gif">'
    out = sanitize_html(html, cid_to_sha={})
    assert "tracker.example" not in out
    assert "src=" not in out or "src=\"\"" in out


def test_cid_image_rewritten_to_attachment_url() -> None:
    cid_to_sha = {"image1@example": "deadbeef" * 8}
    html = '<img src="cid:image1@example">'
    out = sanitize_html(html, cid_to_sha=cid_to_sha)
    assert "/v1/attachments/" + ("deadbeef" * 8) in out
    assert "cid:" not in out


def test_unknown_cid_stripped() -> None:
    html = '<img src="cid:missing@example">'
    out = sanitize_html(html, cid_to_sha={})
    assert "cid:" not in out


def test_safe_styles_passed_through_inline() -> None:
    html = '<p style="color: red">x</p>'
    out = sanitize_html(html, cid_to_sha={})
    assert "<p" in out
    assert "x</p>" in out


def test_data_uri_image_allowed() -> None:
    html = '<img src="data:image/png;base64,AAAA">'
    out = sanitize_html(html, cid_to_sha={})
    assert "data:image/png" in out


def test_style_color_kept() -> None:
    """Safe CSS — colors, fonts, borders — must survive the CSS sanitizer.

    Whitespace inside a style declaration is implementation-defined
    (``bleach`` emits ``color: red``; ``nh3`` normalises to ``color:red``).
    The security invariant is that the property and value survive together
    — the assertions accept both forms.
    """
    html = '<p style="color: red; font-weight: bold">x</p>'
    out = sanitize_html(html, cid_to_sha={})
    assert "color" in out and "red" in out
    assert "font-weight" in out and "bold" in out


def test_style_background_image_url_dropped() -> None:
    """background-image is not in bleach's default property allowlist."""
    html = '<p style="background-image: url(https://tracker.example/p.png)">x</p>'
    out = sanitize_html(html, cid_to_sha={})
    assert "tracker.example" not in out
    assert "background-image" not in out


def test_style_position_fixed_dropped() -> None:
    """``position`` is omitted from the allowlist — blocks overlay/clickjacking via inline style.

    Surviving properties (``color: red``) should remain regardless of the
    sanitiser's whitespace normalisation.
    """
    html = '<div style="position: fixed; top: 0; left: 0; color: red">overlay</div>'
    out = sanitize_html(html, cid_to_sha={})
    assert "position" not in out
    assert "color" in out and "red" in out


def test_style_javascript_url_dropped() -> None:
    """A `javascript:` URL inside CSS (e.g. background) must not survive."""
    html = '<p style="background: url(javascript:alert(1))">x</p>'
    out = sanitize_html(html, cid_to_sha={})
    assert "javascript:" not in out
    assert "alert" not in out


def test_external_image_unquoted_src_stripped() -> None:
    """Unquoted srcs (`src=foo`) must also be neutralised."""
    html = "<img src=https://tracker.example/pixel.gif>"
    out = sanitize_html(html, cid_to_sha={})
    assert "tracker.example" not in out


def test_external_image_single_quoted_src_stripped() -> None:
    """Single-quoted srcs must also be neutralised."""
    html = "<img src='https://tracker.example/pixel.gif'>"
    out = sanitize_html(html, cid_to_sha={})
    assert "tracker.example" not in out


def test_cid_unquoted_src_rewritten() -> None:
    """cid: references work regardless of quoting style."""
    cid_to_sha = {"image1@example": "cafef00d" * 8}
    html = "<img src=cid:image1@example>"
    out = sanitize_html(html, cid_to_sha=cid_to_sha)
    assert "/v1/attachments/" + ("cafef00d" * 8) in out
    assert "cid:" not in out


def test_anchor_gains_rel_noopener_noreferrer() -> None:
    """nh3 auto-injects ``rel="noopener noreferrer"`` on every ``<a>`` tag.

    This is cheap tabnabbing protection — even if the message renderer
    later opens a sanitised link in a new context, the opener reference
    cannot be hijacked.
    """
    html = '<a href="https://example.com/foo">visit</a>'
    out = sanitize_html(html, cid_to_sha={})
    assert "noopener" in out
    assert "noreferrer" in out
    assert "https://example.com/foo" in out


def test_script_src_cid_does_not_leak_attachment_url() -> None:
    """``<script src="cid:...">`` must not surface a rewritten attachment URL.

    ``_rewrite_image_srcs`` runs before the sanitiser, so it could in
    principle rewrite the script's ``src`` to ``/v1/attachments/<sha>``.
    nh3's ``clean_content_tags`` then removes the whole tag (and any
    content), so neither the rewritten URL nor the inner text escape.
    """
    cid_to_sha = {"evil@example": "deadbeef" * 8}
    html = '<p>before</p><script src="cid:evil@example">alert(1)</script><p>after</p>'
    out = sanitize_html(html, cid_to_sha=cid_to_sha)
    assert "deadbeef" not in out
    assert "/v1/attachments/" not in out
    assert "alert" not in out
    assert "<script" not in out
    assert "<p>before</p>" in out
    assert "<p>after</p>" in out


def test_iframe_content_fully_stripped() -> None:
    """``<iframe>`` content must be removed along with the tag itself."""
    html = '<p>ok</p><iframe src="javascript:alert(1)">fallback text</iframe><p>tail</p>'
    out = sanitize_html(html, cid_to_sha={})
    assert "<iframe" not in out
    assert "javascript:" not in out
    assert "alert" not in out
    assert "fallback text" not in out
    assert "<p>ok</p>" in out
    assert "<p>tail</p>" in out


def test_unclosed_script_tag_content_stripped() -> None:
    """An unclosed ``<script>`` at end-of-doc must still be neutralised.

    The previous regex pre-pass required a matching ``</script>``; nh3
    delegates to ``html5ever`` which treats everything after ``<script>``
    as script content until EOF.
    """
    html = "<p>visible</p><script>alert('escaped')"
    out = sanitize_html(html, cid_to_sha={})
    assert "<script" not in out
    assert "alert" not in out
    assert "<p>visible</p>" in out


def test_comments_stripped() -> None:
    """HTML comments must not survive (could leak server-side notes)."""
    html = "<p>shown</p><!-- secret server comment --><p>also shown</p>"
    out = sanitize_html(html, cid_to_sha={})
    assert "secret" not in out
    assert "<!--" not in out
    assert "<p>shown</p>" in out
    assert "<p>also shown</p>" in out
