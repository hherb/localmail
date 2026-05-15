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


def apply_migrations(dsn: str) -> list[str]:
    """Apply any unapplied .sql migrations. Returns the revisions newly applied."""
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
            revision = path.stem  # e.g. "0001_init"
            if revision in done:
                continue
            sql = path.read_text()
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (revision) VALUES (%s)",
                    (revision,),
                )
            conn.commit()
            applied.append(revision)
    return applied
