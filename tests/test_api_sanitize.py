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
