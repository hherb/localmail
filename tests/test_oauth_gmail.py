import json
from pathlib import Path
from unittest.mock import patch

import pytest

from localmail.oauth_gmail import (
    GMAIL_SCOPES,
    build_xoauth2_string,
    credentials_from_refresh,
    fresh_access_token,
)


def _write_client_json(path: Path, key: str = "installed") -> Path:
    path.write_text(
        json.dumps(
            {
                key: {
                    "client_id": "cid.apps.googleusercontent.com",
                    "client_secret": "csecret",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                }
            }
        )
    )
    return path


def test_build_xoauth2_string_uses_sasl_separator():
    s = build_xoauth2_string("user@example.com", "ya29.tok")
    assert s == "user=user@example.com\x01auth=Bearer ya29.tok\x01\x01"


def test_credentials_from_refresh_with_installed_block(tmp_path: Path):
    p = _write_client_json(tmp_path / "client.json", "installed")
    creds = credentials_from_refresh("refresh-abc", p)
    assert creds.refresh_token == "refresh-abc"
    assert creds.client_id == "cid.apps.googleusercontent.com"
    assert creds.client_secret == "csecret"
    assert creds.token_uri == "https://oauth2.googleapis.com/token"
    assert creds.scopes == GMAIL_SCOPES
    assert creds.token is None


def test_credentials_from_refresh_with_web_block(tmp_path: Path):
    p = _write_client_json(tmp_path / "client.json", "web")
    creds = credentials_from_refresh("rt", p)
    assert creds.client_id == "cid.apps.googleusercontent.com"


def test_credentials_from_refresh_rejects_unknown_format(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"unexpected_key": {}}))
    with pytest.raises(ValueError, match="installed.*web"):
        credentials_from_refresh("rt", p)


def test_credentials_from_refresh_rejects_missing_keys(tmp_path: Path):
    p = tmp_path / "partial.json"
    p.write_text(json.dumps({"installed": {"client_id": "x"}}))
    with pytest.raises(ValueError, match="missing keys"):
        credentials_from_refresh("rt", p)


def test_fresh_access_token_calls_refresh_and_returns_token(tmp_path: Path):
    p = _write_client_json(tmp_path / "client.json")

    def fake_refresh(self, request):  # noqa: ARG001
        self.token = "ya29.new-token"

    with patch(
        "google.oauth2.credentials.Credentials.refresh", new=fake_refresh
    ):
        token = fresh_access_token("refresh-token", p)
    assert token == "ya29.new-token"


def test_fresh_access_token_raises_if_refresh_returns_nothing(tmp_path: Path):
    p = _write_client_json(tmp_path / "client.json")

    def fake_refresh(self, request):  # noqa: ARG001
        pass  # leave self.token unset

    with patch("google.oauth2.credentials.Credentials.refresh", new=fake_refresh):
        with pytest.raises(RuntimeError, match="no access_token"):
            fresh_access_token("rt", p)
