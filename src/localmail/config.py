# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Config loading and validation for localmail."""

from __future__ import annotations

import os
import tomllib
from collections import Counter
from ipaddress import ip_network
from pathlib import Path
from typing import Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

# Imported (not redefined) so client_ip.py is the single source of truth for
# the TrustedProxies alias. client_ip.py must remain free of any
# localmail.config import — adding one would close a cycle through this line.
# (Other localmail.api.* modules import from localmail.config freely; only
# client_ip.py is constrained, because this is the one config→api edge.)
from localmail.api.client_ip import TrustedProxies


class DatabaseConfig(BaseModel):
    dsn: str


class AttachmentsConfig(BaseModel):
    root: Path = Path("~/localmail")

    @field_validator("root", mode="after")
    @classmethod
    def expand(cls, v: Path) -> Path:
        return Path(os.path.expanduser(str(v))).resolve()


class ImportsConfig(BaseModel):
    """Tunables for the /admin/imports archive-import feature (Sub-plan 2A.5).

    `roots` is the allowlist of directories the import UI may read archives
    from; an empty list disables imports. Each entry is user-expanded and
    resolved to an absolute path so the path-allowlist guard compares
    realpaths. No magic numbers live in importer code — they live here.
    """
    roots: list[Path] = Field(default_factory=list)
    checkpoint_every: int = 50  # flush progress + commit to DB every N messages
    checkpoint_seconds: float = 2.0  # also flush + poll cancel every N seconds (issue #163)
    stale_seconds: int = 60  # a running job idle longer than this is shown stalled in the panel

    @field_validator("roots", mode="after")
    @classmethod
    def _resolve_roots(cls, v: list[Path]) -> list[Path]:
        return [Path(os.path.expanduser(str(p))).resolve() for p in v]


class DaemonConfig(BaseModel):
    idle_renew_seconds: int = 1740
    poll_seconds: int = 300
    # When None, the daemon picks `db.compute_daemon_pool_size(...)` based on
    # the number of configured accounts and which optional workers are on.
    # Set explicitly to override for tight Postgres `max_connections` budgets
    # or for operators who want more concurrency than the formula gives.
    pool_max_size: int | None = None
    # Startup backoff (#133): if Postgres is briefly unreachable when the
    # daemon launches (DB still coming up under systemd, transient blip), the
    # construction-time DB touches retry with exponential backoff between
    # `startup_backoff_initial_s` and `startup_backoff_max_s` rather than
    # crashing. Mirrors the 1s→60s shape the IDLE/poll worker loops use.
    startup_backoff_initial_s: float = 1.0
    startup_backoff_max_s: float = 60.0
    # How often the running daemon re-reads the account set and reconciles
    # its per-account threads (seconds). Hot-reload latency upper bound.
    reload_seconds: int = 30
    # Per-thread join timeout on teardown / shutdown (seconds). Reused by the
    # 2B.4 supervisor's stop() (SIGTERM -> wait -> SIGKILL).
    shutdown_grace_seconds: float = 30.0
    # 2B.2 heartbeats: a worker's heartbeat is "stale" when
    # now() - last_heartbeat_at exceeds this. Both the IDLE thread and the poll
    # thread re-beat every ~30s (idle.HEARTBEAT_SECONDS / poller.HEARTBEAT_SECONDS,
    # the latter even while idling between passes), so the default comfortably
    # exceeds the beat interval and a healthy worker is never flagged stale by
    # jitter — independent of [daemon] poll_seconds.
    heartbeat_stale_seconds: int = 120
    # Bound for the daemon's *fresh* (non-pool) psycopg connects —
    # `_load_syncable_accounts`, `reconcile`, `_clear_heartbeats` (#140). Without
    # it a network black-hole (host up, packets dropped) blocks the connect for
    # the OS TCP default (minutes), stalling startup and hot-reload. Integer
    # seconds because libpq's `connect_timeout` is integer-valued (a float would
    # serialise to "10.0" in the conninfo string).
    db_connect_timeout_s: int = 10
    # Companion bound for the *query* phase of those same fresh connects (#142).
    # This is `statement_timeout`, a *server-side* GUC: Postgres aborts a query
    # that runs longer than this *on the server*. It bounds a slow / stuck query
    # (lock contention, a pathological plan) — NOT a network black-hole. If
    # request packets are dropped in transit the server never starts the query
    # (so the timer never arms), and if the reply is dropped the client blocks on
    # recv regardless; that post-connect black-hole case is bounded by
    # `db_tcp_user_timeout_ms` below, not here. Threaded into `Daemon._connect()`
    # as libpq `options='-c statement_timeout=<N>s'`. Integer seconds (passed with
    # the GUC `s` unit suffix, so no s->ms magic conversion); these queries are
    # sub-ms so the default is generous headroom over real load yet finite. `0`
    # disables the bound (libpq/Postgres semantics).
    db_statement_timeout_s: int = 30
    # Bound for the *post-connect network black-hole* on those same fresh connects
    # (#142): host up, packets silently dropped *after* the TCP connect succeeds.
    # `db_connect_timeout_s` only covers the connect handshake and
    # `db_statement_timeout_s` is server-side (useless when the server never sees
    # the query / the reply is dropped), so neither breaks a client stuck in recv.
    # libpq's `tcp_user_timeout` forces the connection closed after this many
    # milliseconds of unacknowledged transmitted data — the OS-level escape the
    # other two can't provide. Integer **milliseconds** (libpq's native unit for
    # this parameter — distinct from the `_s` knobs above; do not reuse those
    # naively). Effective on Linux (the systemd deploy target); libpq silently
    # ignores it on platforms without `TCP_USER_TIMEOUT` (e.g. macOS dev). `0`
    # uses the OS default (disables the bound).
    db_tcp_user_timeout_ms: int = 30000
    # 2B.3 command queue: the daemon LISTENs the `daemon_commands` channel on a
    # dedicated connection so an enqueue's NOTIFY wakes the reconcile loop early
    # (reload-now converges immediately instead of waiting out reload_seconds).
    # The poll path (reload_seconds) is authoritative and correct on its own;
    # the listener is a pure latency optimization. Disable it where LISTEN is
    # undesirable — the daemon then still consumes commands on the next tick.
    command_listen_enabled: bool = True
    # How long the listener blocks in notifies() before re-checking the stop
    # event (seconds). Bounds shutdown latency of the listener thread; small so
    # a stopping daemon's listener exits promptly, large enough not to busy-spin.
    command_listen_poll_seconds: float = 5.0


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

    # `GET /v1/changes?subscription=` and `POST /v1/changes/ack` both create a
    # `channel_subscriptions` row on first use of a name, so a client that
    # derives the name from a UUID or timestamp would grow the table without
    # bound. A polling deployment needs a handful of stable names; the cap only
    # has to be generous enough that a legitimate client never trips it.
    max_subscriptions_per_user: int = 32

    # Admin UI signing keys. Empty default = admin UI disabled; populated
    # only when the operator opts in by setting them in config.toml. Both
    # keys must be at least 32 base64url characters (~24 bytes decoded).
    # See docs/superpowers/specs/2026-05-28-admin-ui-design.md §3.
    session_signing_key: str = ""
    state_signing_key: str = ""

    # OAuth2 callback URL the admin UI redirects to after Google consent.
    # Must match an Authorized redirect URI registered in Google Cloud
    # Console for the localmail OAuth client. Empty default = OAuth web
    # flow disabled; CLI desktop loopback flow remains available.
    oauth_callback_url: str = ""

    # Mark admin session cookies as Secure. Default True: behind a
    # TLS-terminating reverse proxy the wire scheme is HTTPS even when the
    # uvicorn socket sees plain HTTP, so we can't infer Secure from
    # request.url.scheme. Operators running 127.0.0.1 dev with --no-tls
    # must set this to False or the browser will reject the cookie.
    cookie_secure: bool = True

    # 2B.4 daemon supervision. When True (default) the serve process owns
    # `localmail run` as a child subprocess (Plane B: start/stop/restart) and
    # binds a Unix control socket the CLI can talk to. Set False for the
    # systemd deployment where the supervisor owns `localmail run`
    # independently — Plane A (reload / restart-account via the command queue)
    # and read-only status still work; lifecycle ops report `external`.
    supervise_daemon: bool = True
    # Directory for the supervisor's Unix control socket
    # (`localmail-supervisor.sock`, mode 0600). Empty default = resolve at run
    # time from $XDG_RUNTIME_DIR, falling back to the platform temp dir. Set
    # explicitly to pin the socket location (e.g. `/run/localmail`).
    runtime_dir: str = ""

    @field_validator("session_signing_key", "state_signing_key")
    @classmethod
    def _validate_signing_key(cls, v: str) -> str:
        if v == "":
            return v
        if len(v) < 32:
            raise ValueError(
                "signing key must be at least 32 characters "
                "(generate with `python -c 'import secrets; "
                "print(secrets.token_urlsafe(32))'`)"
            )
        return v


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
        self._trusted_proxies_parsed = tuple(
            ip_network(s, strict=False) for s in self.trusted_proxies
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
    """One IMAP account.

    As of Sub-plan 2A.2d the database is canonical for accounts: these
    ``[[accounts]]`` TOML blocks are read only as the ``init-db`` seed and as
    the seed-from-TOML source for ``add-account`` / ``oauth-login`` when the
    named DB row does not yet exist. No account command (nor the daemon, nor
    the one-shot ``sync``) reads TOML at runtime once the DB row exists.
    Field validation is shared with the admin service layer in
    ``api/admin/accounts.py``.
    """

    name: str
    email: str
    imap_host: str
    imap_port: int = 993
    auth_method: Literal["password", "oauth2"]
    oauth_provider: Literal["gmail"] | None = None
    folder_allow: list[str] = Field(default_factory=list)
    folder_deny: list[str] = Field(default_factory=list)
    folder_deny_flags: list[str] = Field(default_factory=list)
    # No longer consumed as of Sub-plan 2A.2b: the daemon enumerates accounts
    # from the DB (no per-account poll column) and uses the daemon-wide
    # `[daemon].poll_seconds`. Kept parseable for back-compat only — an
    # existing per-account value is silently ignored, never an error.
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
    rewriter_backend: Literal["ollama", "openai", "anthropic"] = "ollama"
    rewriter_model: str = "granite4.1:3b-q8_0"
    rewriter_timeout_s: float = 10.0
    rewriter_max_expansion_terms: int = 8
    # Generated-token cap for the OpenAI and Anthropic backends. Anthropic's
    # Messages API requires max_tokens; OpenAI treats it as optional. The
    # Ollama backend ignores it (it uses /api/generate's own options). The
    # rewrite output is a small JSON object, so the default is generous but bounded.
    # Note: the OpenAI backend sends `max_tokens` (correct for classic
    # /chat/completions and every OpenAI-compatible server — vLLM, llama.cpp,
    # Ollama's /v1). OpenAI's own reasoning models (o1/o3-family) reject
    # `max_tokens` in favour of `max_completion_tokens`; point those at a
    # non-reasoning model or a compatible proxy.
    rewriter_max_tokens: int = 1024
    # OpenAI-compatible backend (rewriter_backend = "openai"). Any server
    # speaking /chat/completions works (OpenAI, vLLM, Ollama's own /v1, etc.).
    # The API key is read at construction from the named environment variable
    # (never config/DB).
    # base_url INCLUDES /v1 (OpenAI SDK convention); the client appends only
    # /chat/completions. For Ollama's own OpenAI-compat endpoint use
    # "http://localhost:11434/v1".
    rewriter_openai_base_url: str = "https://api.openai.com/v1"
    rewriter_openai_api_key_env: str = "OPENAI_API_KEY"
    # Anthropic backend (rewriter_backend = "anthropic"). The version string is
    # the anthropic-version request header.
    # Pin a known-good anthropic-version; bump when adopting newer API features.
    # base_url is the ORIGIN only (no /v1 suffix); the client appends
    # /v1/messages. This is the opposite convention from the OpenAI field above.
    rewriter_anthropic_base_url: str = "https://api.anthropic.com"
    rewriter_anthropic_api_key_env: str = "ANTHROPIC_API_KEY"
    rewriter_anthropic_version: str = "2023-06-01"
    # Bounded per-process LRU+TTL cache for `--smart` rewrite results, keyed on
    # (today, free_text). Repeated identical smart queries skip a fresh Ollama
    # call. Entries are tiny, so the size can exceed page_cache_size. 0 disables.
    rewriter_cache_size: int = 128
    rewriter_cache_ttl_s: int = 1200

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
    # Cap on *consecutive* transient extraction failures (docling third-party
    # network errors, OOM blips) before a blob stops being re-attempted (#153).
    # Independent of extract_worker_max_retries, which stays reserved for
    # poison-pill (failed_extractions) semantics. Larger than the poison cap
    # because transients are often genuinely recoverable, but now bounded so a
    # permanently-failing network error can't loop the worker forever. Reset to
    # 0 on the first successful extraction of the blob.
    extract_worker_max_transient_retries: int = 5

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


class UpgradeEstimateConfig(BaseModel):
    """Throughput rates used by `localmail estimate-upgrade` to project
    lock-holding duration for lock-heavy migrations against a populated
    `messages` table. See docs/operations/upgrade-runbook.md.

    All four constants are tunable per-installation: an operator on slow
    storage (HDD, low-memory) should halve the throughput rates; an
    operator on NVMe with abundant RAM may double them. The runbook
    documents a calibration procedure for operators who care about
    accuracy.
    """

    # tsvector stores tokens + positions, so the stored column is larger
    # than the raw concatenated text it indexes. 1.5x is the rule of
    # thumb for English / Western European text; languages with longer
    # average tokens (German compounds, Finnish) trend toward 1.7x.
    fts_v2_blowup_factor: float = 1.5

    # Typical GIN-on-tsvector ratio: the index is ~40% of the column it
    # covers. Varies with token uniqueness (higher for diverse vocab).
    gin_size_factor: float = 0.4

    # SSD baseline for `ALTER TABLE ... ADD COLUMN ... GENERATED ALWAYS
    # AS ... STORED` — Postgres rewrites the whole table heap. HDD is
    # ~10x slower; NVMe is ~2x faster.
    table_rewrite_mb_per_sec: float = 80.0

    # SSD baseline for `CREATE INDEX ... USING GIN`. GIN builds are
    # CPU-bound (token enumeration) more than I/O-bound; the rate is
    # less hardware-sensitive than table_rewrite_mb_per_sec.
    gin_build_mb_per_sec: float = 30.0


class McpConfig(BaseModel):
    """Model Context Protocol server settings.

    The MCP server is mounted into `localmail serve` at `/mcp` only when
    `enabled` is true AND the optional `mcp` extra is installed. Disabled by
    default; set `enabled = true` to opt in, mirroring `search.reranker_enabled`.

    `issuer_url` / `resource_server_url` are advertised in the RFC 9728
    protected-resource metadata. `resource_server_url` is the **public origin**
    of the serve deployment (no `/mcp` suffix — the mount path is appended
    internally); set it to the externally reachable URL so the metadata served
    at `/.well-known/oauth-protected-resource/mcp` and the `WWW-Authenticate`
    challenge agree. Tokens stay opaque-bearer, obtained out-of-band via
    `/v1/auth/login`; localmail is not an OAuth authorization server.

    `authorization_servers` is advertised in the metadata's required
    `authorization_servers` field. `None` falls back to `[issuer_url]`; set an
    explicit list to point spec-strict clients at a real external authorization
    server whose tokens `LocalmailTokenVerifier` accepts.

    Setting `authorization_server_enabled = true` turns localmail itself into an
    OAuth 2.1 authorization server for MCP clients and requires
    `[serve].state_signing_key` to be set.

    `resource_indicators` is the RFC 8707 accepted-resource set; `None` falls
    back to `[mcp_resource_url(resource_server_url)]`. `oauth_require_resource_indicator`
    rejects an /authorize request that omits `resource`.
    """

    enabled: bool = False
    issuer_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8443")
    resource_server_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8443")
    authorization_servers: list[AnyHttpUrl] | None = None
    resource_indicators: list[AnyHttpUrl] | None = None

    # OAuth 2.1 authorization server (MCP "Approach B"). Opt-in; when off the
    # MCP server stays opaque-bearer + discovery only (today's behaviour). All
    # tunables defaulted so the provider carries no magic numbers.
    authorization_server_enabled: bool = False
    oauth_require_resource_indicator: bool = False
    oauth_access_token_ttl_s: int = 3600
    oauth_refresh_token_ttl_s: int = 2592000
    oauth_authorization_code_ttl_s: int = 60
    oauth_consent_state_ttl_s: int = 300
    oauth_registration_window_s: int = 3600
    oauth_registration_max: int = 20
    oauth_client_unused_retention_s: int = 86400


class Config(BaseModel):
    database: DatabaseConfig
    attachments: AttachmentsConfig = AttachmentsConfig()
    daemon: DaemonConfig = DaemonConfig()
    serve: ServeConfig = Field(default_factory=ServeConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    gmail_oauth: GmailOAuthConfig | None = None
    accounts: list[AccountConfig] = Field(default_factory=list)
    search: SearchConfig = Field(default_factory=SearchConfig)
    imports: ImportsConfig = Field(default_factory=ImportsConfig)
    upgrade: UpgradeEstimateConfig = Field(default_factory=UpgradeEstimateConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)

    @model_validator(mode="after")
    def _reject_duplicate_account_names(self) -> Config:
        # `name` is the canonical account key everywhere (keyring username, DB
        # `accounts.name` unique constraint, the init-db seed's dedup key), so
        # a duplicate is never valid. Fail loud here rather than let it surface
        # opaquely in a downstream consumer (issue #129).
        duplicates = sorted(
            name for name, count in Counter(a.name for a in self.accounts).items()
            if count > 1
        )
        if duplicates:
            raise ValueError(
                "duplicate account name(s) in config: " + ", ".join(duplicates)
            )
        return self


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
