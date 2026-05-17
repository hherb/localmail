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

    # --- Phase 1 embed worker ---
    # Controls the background thread that chunks messages and writes embeddings.
    run_embed_worker: bool = True
    embed_worker_batch_size: int = 100
    embed_worker_poll_interval_s: float = 5.0
    embed_worker_max_chunk_retries: int = 3

    # --- Phase 2: extraction worker ---
    # Controls the background thread that extracts text from attachment blobs and
    # writes the results to attachment_text for Arm 4 retrieval.
    run_extract_worker: bool = True
    extract_worker_poll_interval_s: int = 30
    extract_worker_batch_size: int = 20
    extract_worker_max_retries: int = 3

    # --- Phase 2: extractor policy ---
    # Allowlists control which blobs are eligible for text extraction; blobs
    # outside these sets are silently skipped rather than attempted and failed.
    extractor_mime_allowlist: list[str] = Field(
        default_factory=lambda: [
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.oasis.opendocument.text",
            "application/rtf",
            "text/plain",
            "text/markdown",
            "text/html",
            "text/csv",
            "text/calendar",
        ]
    )
    extractor_extension_allowlist: list[str] = Field(
        default_factory=lambda: [
            ".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".rtf",
            ".txt", ".md", ".html", ".htm", ".csv", ".ics",
        ]
    )
    extractor_max_blob_bytes: int = 50 * 1024 * 1024
    extractor_max_extracted_chars: int = 1_000_000
    extractor_docling_max_pages: int = 200
    extractor_ocr_languages: list[str] = Field(default_factory=lambda: ["en"])

    # --- attachment extraction backend (Phase 2) ---
    extractor_backend: Literal["docling", "lightweight"] = "docling"
    extractor_max_file_size_mb: int = 100
    extractor_per_blob_timeout_s: int = 300

    # --- Phase 2: Arm 4 ---
    # arm4_fanout_cap limits how many blob rows Arm 4 fetches per query so that
    # a single message with many attachments cannot dominate the result pool.
    arm4_fanout_cap: int = 10

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
