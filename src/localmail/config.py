"""Config loading and validation for localmail."""

from __future__ import annotations

import os
import tomllib
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, field_validator

TrustedProxies = tuple[IPv4Network | IPv6Network, ...]


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
    # When None, the daemon picks `db.compute_daemon_pool_size(...)` based on
    # the number of configured accounts and which optional workers are on.
    # Set explicitly to override for tight Postgres `max_connections` budgets
    # or for operators who want more concurrency than the formula gives.
    pool_max_size: int | None = None


class ServeConfig(BaseModel):
    """Tunables for the GUI HTTP server (localmail.serve)."""
    pool_min_size: int = 1
    pool_max_size: int = 4
    # /v1/changes excludes messages newer than `now() - changes_safe_horizon_s`
    # so a concurrent sync transaction that allocates `messages.id = N+1` and
    # commits before another tx with id=N can't make the client advance past
    # N+1 and miss N when its commit lands. The default 5 s is much longer
    # than any reasonable per-message sync transaction; bump if you observe
    # sync transactions that genuinely take longer.
    changes_safe_horizon_s: int = 5


class AuthConfig(BaseModel):
    """Tunables for the login rate limiter (Postgres-backed)."""

    login_per_user_max: int = 5
    login_per_user_window_s: int = 60

    login_per_ip_max: int = 20
    login_per_ip_window_s: int = 60

    login_global_max: int = 30
    login_global_window_s: int = 60

    # Best-effort retention: rows older than this are deleted by the
    # in-process sweep. Independent of the sliding-window caps above —
    # raise to keep audit history further back without affecting limits.
    login_attempt_retention_s: int = 86400

    # Per-worker cadence for the sweep. Gated by a PG advisory lock so
    # concurrent workers don't pile up DELETEs.
    login_cleanup_interval_s: int = 300

    # Reverse-proxy support for the login rate limiter. Empty (default) =
    # historic behaviour: client_ip is the socket peer (request.client.host).
    # When non-empty, an X-Forwarded-For header is peeled right-to-left,
    # skipping entries in trusted_proxies, to find the originating client.
    # The same list governs both:
    #   (a) admission: is the immediate socket peer a trusted proxy?
    #   (b) peeling:   which XFF entries are proxies vs the client?
    # See docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md
    trusted_proxies: list[str] = Field(default_factory=list)

    # Hard cap on XFF entries we walk before giving up — bounds CPU on an
    # attacker-supplied giant XFF header and bounds the chain depth we
    # claim to support. Three is enough for client → CDN → ALB → app.
    trusted_proxies_max_hops: int = Field(default=3, ge=1, le=10)

    _trusted_proxies_parsed: TrustedProxies = PrivateAttr(default=())

    @field_validator("trusted_proxies")
    @classmethod
    def _validate_trusted_proxies(cls, v: list[str]) -> list[str]:
        # Parse-and-discard: fail LOUD at config-load on a bad CIDR.
        # The real parse runs once in model_post_init and is read by
        # trusted_proxies_parsed.
        for s in v:
            ip_network(s, strict=False)
        return v

    def model_post_init(self, __context: object) -> None:
        # PrivateAttr assignment via object.__setattr__ works regardless
        # of any future model_config = ConfigDict(frozen=...) change.
        object.__setattr__(
            self,
            "_trusted_proxies_parsed",
            tuple(
                ip_network(s, strict=False) for s in self.trusted_proxies
            ),
        )

    @property
    def trusted_proxies_parsed(self) -> TrustedProxies:
        return self._trusted_proxies_parsed


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
    # Default pool large enough that the first sort=rank page fills (the
    # GUI sends limit=50) and "Load more" can serve at least one
    # additional page from the cache without firing grow_pool. Smaller
    # pools cause the very first "Load more" to re-run the whole
    # retrieval pipeline and surface mostly-duplicate top hits.
    rerank_pool_size: int = 100
    # Cap for transparent grow_pool growth driven by the /v1/search cursor
    # path. When the page cursor would advance past the current cached pool
    # and `can_grow_pool=True`, the route doubles candidates_per_arm up to
    # this ceiling; once the ceiling is hit, next_cursor flips to null.
    candidates_per_arm_max: int = 800
    page_size_default: int = 20
    page_size_max: int = 200
    hnsw_ef_search: int = 64
    snippet_width_chars: int = 200

    # --- reranker ---
    # Default OFF. The cross-encoder rerank pass is O(pool size) and the
    # search cursor's grow_pool path doubles the pool on each page advance
    # past the cache (50 → 100 → 200 → 400 → 800). On CPU this overruns
    # request timeouts. Flip to true on GPU hosts via config.toml.
    reranker_enabled: bool = False
    reranker_backend: Literal["fastembed"] = "fastembed"
    reranker_model: str = "jinaai/jina-reranker-v2-base-multilingual"
    rerank_max_chars: int = 1500

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
    # Floor for the chunk-pass batch so that embed_worker_batch_size (the
    # embedding batch) can be tuned smaller without accidentally starving the
    # chunking pass — chunking is cheap relative to embedding.
    embed_worker_chunk_batch_size: int = 50

    # --- Phase 1+: per-message body language detection ---
    # Populates `messages.body_lang` (ISO 639-1 lowercase). Required for the
    # `lang:` search DSL token / `/v1/search?languages=` filter to return any
    # rows. Embed worker calls the detector on its sweep when one is provided;
    # `localmail lang-backfill` runs the same pass standalone for the archive.
    body_lang_enabled: bool = True
    # Minimum top-language probability. Lingua's confidence values sum to 1.0
    # over the active language set; below this threshold, store NULL ("unknown")
    # rather than guess. 0.65 keeps de/en/es/ja/no fixtures correct while
    # rejecting noise from very short / multilingual bodies.
    body_lang_min_confidence: float = 0.65
    # Bodies shorter than this many characters are skipped — single-line bodies
    # are too short for reliable detection in any language.
    body_lang_min_text_chars: int = 20
    # Use lingua's low-accuracy mode (~100MB resident vs ~1GB). Email bodies
    # are usually long enough that the accuracy hit is not measurable.
    body_lang_low_accuracy: bool = True
    # How many messages to claim per detection pass.
    body_lang_detect_batch_size: int = 200

    # --- Phase 2: extraction worker ---
    # Controls the background thread that extracts text from attachment blobs and
    # writes the results to attachment_text for Arm 4 retrieval.
    run_extract_worker: bool = True
    extract_worker_poll_interval_s: float = 30.0
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
    # Chardet confidence threshold for text-encoding detection. Below
    # this, fall back to latin-1 rather than trust a low-confidence guess.
    extractor_chardet_confidence_min: float = 0.5

    # --- Pre-existing placeholder fields, will be consumed by extract_worker.py in Task 13.
    # Not part of the Phase 2 spec field set; kept for forward compatibility.
    extractor_backend: Literal["docling", "lightweight"] = "docling"
    extractor_per_blob_timeout_s: int = 300

    # --- Phase 2: Arm 4 ---
    # arm4_fanout_cap limits how many blob rows Arm 4 fetches per query so that
    # a single message with many attachments cannot dominate the result pool.
    arm4_fanout_cap: int = 10
    # arm4_chunk_prefetch_multiplier: how many times the per-message limit to
    # fetch from attachment_chunks before fan-out to messages. A multiplier > 1
    # gives headroom so that after fan-out and capping, there are still enough
    # distinct message candidates to fill the output budget.
    arm4_chunk_prefetch_multiplier: int = 3

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
    serve: ServeConfig = Field(default_factory=ServeConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
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
