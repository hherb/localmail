from pathlib import Path

import pytest
from pydantic import ValidationError

from localmail.config import AuthConfig, Config, LocalmailConfig, SearchConfig, load_config


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
    assert cfg.rerank_pool_size == 20
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


def test_search_config_phase2_defaults() -> None:
    """Verify all Phase 2 SearchConfig fields exist and carry the correct defaults."""
    cfg = SearchConfig()

    # Extraction worker
    assert cfg.run_extract_worker is True
    assert cfg.extract_worker_poll_interval_s == 30.0
    assert cfg.extract_worker_batch_size == 20
    assert cfg.extract_worker_max_retries == 3

    # Extractor policy
    assert "application/pdf" in cfg.extractor_mime_allowlist
    assert "text/calendar" in cfg.extractor_mime_allowlist
    assert ".pdf" in cfg.extractor_extension_allowlist
    assert ".ics" in cfg.extractor_extension_allowlist
    assert cfg.extractor_max_blob_bytes == 50 * 1024 * 1024
    assert cfg.extractor_max_extracted_chars == 1_000_000
    assert cfg.extractor_docling_max_pages == 200
    assert cfg.extractor_ocr_languages == ["en"]
    assert cfg.extractor_chardet_confidence_min == 0.5

    # Arm 4
    assert cfg.arm4_fanout_cap == 10


def test_search_config_embed_worker_chunk_batch_size_default() -> None:
    cfg = SearchConfig()
    assert cfg.embed_worker_chunk_batch_size == 50


def test_search_config_arm4_chunk_prefetch_default() -> None:
    cfg = SearchConfig()
    assert cfg.arm4_chunk_prefetch_multiplier == 3


def test_search_config_phase2_custom_overrides() -> None:
    """Pydantic accepts overrides for the Phase 2 fields with correct types."""
    cfg = SearchConfig(
        run_extract_worker=False,
        extract_worker_batch_size=100,
        extractor_max_blob_bytes=10 * 1024 * 1024,
        extractor_ocr_languages=["en", "de", "ja"],
        arm4_fanout_cap=25,
    )
    assert cfg.run_extract_worker is False
    assert cfg.extract_worker_batch_size == 100
    assert cfg.extractor_max_blob_bytes == 10 * 1024 * 1024
    assert cfg.extractor_ocr_languages == ["en", "de", "ja"]
    assert cfg.arm4_fanout_cap == 25


def test_auth_config_defaults_preserve_pre_pg_thresholds() -> None:
    cfg = AuthConfig()
    assert cfg.login_per_user_max == 5
    assert cfg.login_per_user_window_s == 60
    assert cfg.login_per_ip_max == 20
    assert cfg.login_per_ip_window_s == 60
    assert cfg.login_global_max == 30
    assert cfg.login_global_window_s == 60
    assert cfg.login_attempt_retention_s == 86400
    assert cfg.login_cleanup_interval_s == 300


def test_auth_config_round_trip_from_toml(tmp_path: Path) -> None:
    toml_text = """
[database]
dsn = "postgresql:///localmail_test"

[auth]
login_per_user_max = 3
login_per_ip_max = 7
login_global_max = 11
login_attempt_retention_s = 3600
"""
    p = tmp_path / "config.toml"
    p.write_text(toml_text)
    cfg = load_config(p)
    assert cfg.auth.login_per_user_max == 3
    assert cfg.auth.login_per_ip_max == 7
    assert cfg.auth.login_global_max == 11
    assert cfg.auth.login_attempt_retention_s == 3600
    # Defaults fill in the rest.
    assert cfg.auth.login_per_user_window_s == 60
