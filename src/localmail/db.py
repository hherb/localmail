"""Database connection + migration runner."""

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool


def open_pool(dsn: str, *, min_size: int = 1, max_size: int = 4) -> ConnectionPool:
    return ConnectionPool(conninfo=dsn, min_size=min_size, max_size=max_size, open=True)


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
    """Split a SQL file into individual statements on semicolons.

    Strips comment-only lines and blank lines. Needed for non-transactional
    migrations where each statement must be executed separately (e.g.
    CREATE INDEX CONCURRENTLY cannot share a multi-statement execute call).
    """
    stmts = []
    for raw in sql.split(";"):
        stmt = raw.strip()
        # Remove comment lines, keep actual SQL
        non_comment = "\n".join(
            line for line in stmt.splitlines() if not line.strip().startswith("--")
        ).strip()
        if non_comment:
            stmts.append(stmt)
    return stmts


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
