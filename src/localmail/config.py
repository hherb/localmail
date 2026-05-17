"""Config loading and validation for localmail."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class DatabaseConfig(BaseModel):
    dsn: str


class AttachmentsConfig(BaseModel):
    root: Path = Path("~/localmail")

    @field_validator("root", mode="after")
    @classmethod
    def expand(cls, v: Path) -> Path:
        return Path(os.path.expanduser(str(v))).resolve()


class DaemonConfig(BaseModel):
    idle_renew_seconds: int = 1740
    poll_seconds: int = 300


class GmailOAuthConfig(BaseModel):
    client_secrets_file: Path

    @field_validator("client_secrets_file", mode="after")
    @classmethod
    def expand(cls, v: Path) -> Path:
        return Path(os.path.expanduser(str(v))).resolve()


class AccountConfig(BaseModel):
    name: str
    email: str
    imap_host: str
    imap_port: int = 993
    auth_method: Literal["password", "oauth2"]
    oauth_provider: Literal["gmail"] | None = None
    folder_allow: list[str] = Field(default_factory=list)
    folder_deny: list[str] = Field(default_factory=list)
    folder_deny_flags: list[str] = Field(default_factory=list)
    poll_seconds: int | None = None


class SearchConfig(BaseModel):
    """All tunables for the search subsystem. No magic numbers elsewhere."""

    # --- embedding ---
    embedding_backend: Literal["fastembed", "ollama", "auto"] = "fastembed"
    embedding_model: str = "embeddinggemma"
    # Optional override: full provider model path (e.g. "google/embeddinggemma-300m").
    # When None, the backend resolves `embedding_model` through its own
    # registry; setting this lets operators pin a sibling model size
    # ("-instruct", "-1b", …) without a code change.
    embedding_model_path: str | None = None
    embedding_dim: int = 768
    ollama_host: str = "http://localhost:11434"
    ollama_retry_max_attempts: int = 5
    ollama_retry_initial_backoff_s: float = 1.0
    ollama_retry_max_backoff_s: float = 60.0
    fastembed_cache_dir: Path | None = None
    fastembed_threads: int = Field(
        default_factory=lambda: min(4, max(1, (os.cpu_count() or 2) // 2))
    )

    # --- chunking ---
    chunk_size_tokens: int = 512
    chunk_overlap_tokens: int = 64
    chunk_strip_quoted_replies: bool = True
    chunk_strip_signatures: bool = True

    # --- BM25 ---
    bm25_field_boosts: dict[str, float] = Field(default_factory=lambda: {
        "subject": 3.0, "from": 2.0, "body": 1.0, "to": 0.5,
    })

    # --- retrieval / fusion ---
    candidates_per_arm: int = 50
    rrf_k: int = 60
    rerank_pool_size: int = 50
    page_size_default: int = 20
    page_size_max: int = 200
    hnsw_ef_search: int = 64
    snippet_width_chars: int = 200

    # --- reranker ---
    reranker_enabled: bool = True
    reranker_backend: Literal["fastembed"] = "fastembed"
    reranker_model: str = "Xenova/bge-reranker-v2-m3"
    rerank_max_chars: int = 4000

    # --- query rewriter (Phase 4) ---
    rewriter_enabled_by_default: bool = False
    rewriter_backend: Literal["ollama"] = "ollama"
    rewriter_model: str = "qwen2.5:3b"
    rewriter_timeout_s: float = 10.0

    # --- attachment extraction (Phase 2) ---
    extractor_backend: Literal["docling", "lightweight"] = "docling"
    extractor_max_file_size_mb: int = 100
    extractor_per_blob_timeout_s: int = 300
    extractor_ocr_languages: list[str] = Field(
        default_factory=lambda: ["eng", "deu", "spa", "nor", "jpn"]
    )

    # --- workers ---
    run_embed_worker: bool = True
    embed_worker_batch_size: int = 100
    embed_worker_poll_interval_s: float = 5.0
    embed_worker_max_chunk_retries: int = 3

    run_extract_worker: bool = True
    extract_worker_batch_size: int = 4
    extract_worker_poll_interval_s: float = 30.0
    extract_worker_max_blob_retries: int = 2

    # --- index build ---
    index_build_maintenance_work_mem_mb: int = 2048

    # --- pagination cache ---
    page_cache_size: int = 16
    page_cache_ttl_s: int = 1200

    # --- evaluation / logging (Phase 5) ---
    log_queries: bool = False


class Config(BaseModel):
    database: DatabaseConfig
    attachments: AttachmentsConfig = AttachmentsConfig()
    daemon: DaemonConfig = DaemonConfig()
    gmail_oauth: GmailOAuthConfig | None = None
    accounts: list[AccountConfig] = Field(default_factory=list)
    search: SearchConfig = Field(default_factory=SearchConfig)


# Alias for plans/future code that reference LocalmailConfig
LocalmailConfig = Config


def default_config_path() -> Path:
    env = os.environ.get("LOCALMAIL_CONFIG")
    if env:
        return Path(os.path.expanduser(env))
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(os.path.expanduser(base)) / "localmail" / "config.toml"


def load_config(path: Path | None = None) -> Config:
    path = path or default_config_path()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return Config.model_validate(data)
