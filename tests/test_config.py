from pathlib import Path

import pytest
from pydantic import ValidationError

from localmail.config import Config, LocalmailConfig, SearchConfig, load_config


def write(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


def test_minimal_config(tmp_path: Path):
    p = write(
        tmp_path / "c.toml",
        """
        [database]
        dsn = "postgresql:///localmail"
        """,
    )
    cfg = load_config(p)
    assert cfg.database.dsn == "postgresql:///localmail"
    assert cfg.accounts == []
    assert cfg.daemon.poll_seconds == 300


def test_attachments_root_expands_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", "/Users/example")
    p = write(
        tmp_path / "c.toml",
        """
        [database]
        dsn = "postgresql:///localmail"
        [attachments]
        root = "~/mailarchive"
        """,
    )
    cfg = load_config(p)
    assert str(cfg.attachments.root) == "/Users/example/mailarchive"


def test_account_requires_known_auth_method(tmp_path: Path):
    p = write(
        tmp_path / "c.toml",
        """
        [database]
        dsn = "postgresql:///localmail"

        [[accounts]]
        name = "x"
        email = "x@example.com"
        imap_host = "imap.example.com"
        auth_method = "magic"
        """,
    )
    with pytest.raises(ValidationError):
        load_config(p)


def test_full_account(tmp_path: Path):
    p = write(
        tmp_path / "c.toml",
        """
        [database]
        dsn = "postgresql:///localmail"

        [[accounts]]
        name = "gm"
        email = "a@gmail.com"
        imap_host = "imap.gmail.com"
        auth_method = "oauth2"
        oauth_provider = "gmail"
        folder_deny = ["[Gmail]/All Mail"]
        """,
    )
    cfg = load_config(p)
    assert len(cfg.accounts) == 1
    a = cfg.accounts[0]
    assert a.auth_method == "oauth2"
    assert a.oauth_provider == "gmail"
    assert a.folder_deny == ["[Gmail]/All Mail"]


def test_model_default_daemon_values():
    cfg = Config.model_validate({"database": {"dsn": "x"}})
    assert cfg.daemon.idle_renew_seconds == 1740
    assert cfg.daemon.poll_seconds == 300


def test_search_config_has_sane_defaults():
    cfg = SearchConfig()
    assert cfg.embedding_backend == "fastembed"
    assert cfg.embedding_model == "embeddinggemma"
    assert cfg.embedding_dim == 768
    assert cfg.candidates_per_arm == 50
    assert cfg.rrf_k == 60
    assert cfg.rerank_pool_size == 50
    assert cfg.page_size_default == 20
    assert cfg.page_size_max == 200
    assert cfg.snippet_width_chars == 200
    assert cfg.run_embed_worker is True
    assert cfg.chunk_size_tokens == 512
    assert cfg.chunk_overlap_tokens == 64


def test_search_config_attached_to_localmail_config():
    cfg = LocalmailConfig.model_validate({"database": {"dsn": "x"}})
    assert isinstance(cfg.search, SearchConfig)
    assert cfg.search.embedding_backend == "fastembed"


def test_search_config_overrides_via_dict():
    cfg = SearchConfig.model_validate({
        "embedding_backend": "ollama",
        "candidates_per_arm": 100,
        "bm25_field_boosts": {"subject": 5.0},
    })
    assert cfg.embedding_backend == "ollama"
    assert cfg.candidates_per_arm == 100
    assert cfg.bm25_field_boosts["subject"] == 5.0
