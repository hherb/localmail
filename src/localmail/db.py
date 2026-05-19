"""Database connection + migration runner."""

from __future__ import annotations

from pathlib import Path

import psycopg
import sqlparse
from psycopg_pool import ConnectionPool


def open_pool(dsn: str, *, min_size: int = 1, max_size: int = 4) -> ConnectionPool:
    return ConnectionPool(conninfo=dsn, min_size=min_size, max_size=max_size, open=True)


POOL_BASELINE_MIN = 4
"""Floor for ``compute_daemon_pool_size`` — every daemon gets at least this
many slots so single-account / no-worker deployments still have headroom
for ad-hoc CLI commands that grab a connection alongside the sync loop."""

POOL_HEADROOM = 2
"""Extra slots over the (2N + workers) computed budget. Reserved for
short-lived ad-hoc operations like ``localmail retry-failed`` or
migrations that may run alongside a live daemon."""

_SLOTS_PER_ACCOUNT = 2
"""Each account runs one IDLE thread on INBOX and one poll thread on the
remaining folders; both hold a connection from the shared pool."""


def compute_daemon_pool_size(
    *,
    n_accounts: int,
    run_embed: bool,
    run_extract: bool,
    baseline_min: int = POOL_BASELINE_MIN,
    headroom: int = POOL_HEADROOM,
) -> int:
    """Return the recommended ``ConnectionPool.max_size`` for a daemon.

    The daemon shares one pool across every long-running thread it spawns:
    two per account (IDLE + poll), one for the embed worker (if enabled),
    and one for the extract worker (if enabled). On top of that we keep a
    small headroom for ad-hoc operations. The floor (``baseline_min``)
    guarantees even an idle daemon can service an ad-hoc CLI invocation.

    This function is pure and side-effect-free so it can be unit-tested
    without touching Postgres.
    """
    workers = (1 if run_embed else 0) + (1 if run_extract else 0)
    return max(baseline_min, _SLOTS_PER_ACCOUNT * n_accounts + workers + headroom)


def _migrations_dir() -> Path:
    # migrations/ sits at the repo root, next to pyproject.toml.
    # When installed as a wheel the directory is shipped via tool.hatch (see below).
    pkg_root = Path(__file__).resolve().parent
    # src/localmail/db.py -> repo root is parent.parent.parent
    repo_root = pkg_root.parent.parent
    return repo_root / "migrations"


def list_migrations() -> list[Path]:
    d = _migrations_dir()
    if not d.exists():
        raise FileNotFoundError(f"migrations directory not found: {d}")
    return sorted(p for p in d.iterdir() if p.suffix == ".sql")


def _is_non_transactional(sql: str) -> bool:
    """True if the migration's leading comment block contains the marker.

    Format expected at the very top of a .sql file:
        -- @non-transactional
    Lines after the first non-comment, non-blank line are ignored — the
    marker is treated as a directive, not a free-floating comment.
    """
    for raw in sql.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("--"):
            return False
        if "@non-transactional" in line:
            return True
    return False


def _split_statements(sql: str) -> list[str]:
    """Split a SQL file into individual statements.

    Uses sqlparse so dollar-quoted bodies ($$ ... $$ / $tag$ ... $tag$),
    single-quoted string literals, and -- / /* */ comments don't trip the
    splitter on embedded semicolons. Needed for non-transactional migrations
    where each statement must be executed separately (e.g.
    CREATE INDEX CONCURRENTLY cannot share a multi-statement execute call).

    Returns each statement with its trailing semicolon stripped and any
    leading/trailing whitespace removed; pure-comment / blank fragments are
    dropped.
    """
    stmts: list[str] = []
    for raw in sqlparse.split(sql):
        stmt = raw.strip()
        if stmt.endswith(";"):
            stmt = stmt[:-1].rstrip()
        if not stmt:
            continue
        non_comment = "\n".join(
            line for line in stmt.splitlines() if not line.strip().startswith("--")
        ).strip()
        if not non_comment:
            continue
        stmts.append(stmt)
    return stmts


def pending_migrations(dsn: str) -> list[str]:
    """Return revisions present on disk but missing from `schema_migrations`.

    Returns an empty list if `schema_migrations` doesn't exist yet (treated as
    "everything pending") OR if every migration on disk is already applied.
    Used by `localmail serve` to fail fast when the DB is out of sync.
    """
    on_disk = [p.stem for p in list_migrations()]
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = 'schema_migrations'"
                )
                if cur.fetchone() is None:
                    return on_disk
                cur.execute("SELECT revision FROM schema_migrations")
                done = {row[0] for row in cur.fetchall()}
    except psycopg.OperationalError:
        raise
    return [rev for rev in on_disk if rev not in done]


def apply_migrations(
    dsn: str,
    *,
    index_build_work_mem_mb: int = 2048,
) -> list[str]:
    """Apply any unapplied .sql migrations. Returns the revisions newly applied.

    A migration whose first comment block contains '@non-transactional'
    runs in autocommit mode on its own connection so it can use
    CREATE INDEX CONCURRENTLY or other transaction-incompatible DDL.

    `index_build_work_mem_mb` is set as `maintenance_work_mem` for the
    non-transactional session — HNSW + GIN builds benefit from a large value.
    """
    work_mem_mb = int(index_build_work_mem_mb)
    applied: list[str] = []
    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    revision   TEXT        PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute("SELECT revision FROM schema_migrations")
            done = {row[0] for row in cur.fetchall()}
        conn.commit()

    for path in list_migrations():
        revision = path.stem
        if revision in done:
            continue
        sql = path.read_text()
        if _is_non_transactional(sql):
            with psycopg.connect(dsn, autocommit=True) as nc:
                with nc.cursor() as cur:
                    cur.execute(f"SET maintenance_work_mem = '{work_mem_mb}MB'")
                    for stmt in _split_statements(sql):
                        cur.execute(stmt)
                    cur.execute(
                        "INSERT INTO schema_migrations (revision) VALUES (%s)",
                        (revision,),
                    )
        else:
            with psycopg.connect(dsn, autocommit=False) as tc:
                with tc.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (revision) VALUES (%s)",
                        (revision,),
                    )
                tc.commit()
        applied.append(revision)

    return applied
