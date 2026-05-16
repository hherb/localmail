"""localmail CLI entry point."""

from __future__ import annotations

import json as _json
import sys
from dataclasses import asdict
from pathlib import Path

import click
import psycopg

import logging

from . import secrets
from .config import AccountConfig, Config, default_config_path, load_config
from .daemon import Daemon
from .db import apply_migrations
from .imap_client import open_connection
from .oauth_gmail import run_consent_flow
from .search import create_searcher
from .sync import retry_failed_messages, sync_account


def _account_or_die(cfg: Config, name: str) -> AccountConfig:
    for a in cfg.accounts:
        if a.name == name:
            return a
    raise click.ClickException(
        f"account {name!r} is not declared in config.toml; "
        f"add an [[accounts]] block with name = {name!r} first"
    )


@click.group()
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Path to config.toml (default: $LOCALMAIL_CONFIG or ~/.config/localmail/config.toml).",
)
@click.pass_context
def main(ctx: click.Context, config_path: Path | None) -> None:
    """Local PostgreSQL archive of one or more IMAP accounts."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path or default_config_path()


@main.command("init-db")
@click.pass_context
def init_db(ctx: click.Context) -> None:
    """Apply pending schema migrations to the database."""
    cfg = load_config(ctx.obj["config_path"])
    applied = apply_migrations(cfg.database.dsn)
    if applied:
        for rev in applied:
            click.echo(f"applied {rev}")
    else:
        click.echo("schema already up to date")


@main.command("list-accounts")
@click.pass_context
def list_accounts(ctx: click.Context) -> None:
    """Show accounts configured in config.toml and whether a secret is stored."""
    cfg = load_config(ctx.obj["config_path"])
    if not cfg.accounts:
        click.echo("no accounts configured")
        return
    for a in cfg.accounts:
        if a.auth_method == "password":
            has_secret = secrets.get_password(a.name) is not None
            secret_label = "password" if has_secret else "MISSING"
        else:
            has_secret = secrets.get_refresh_token(a.name) is not None
            secret_label = "oauth-token" if has_secret else "MISSING"
        click.echo(
            f"{a.name}\t{a.email}\t{a.imap_host}:{a.imap_port}\t{a.auth_method}\t[{secret_label}]"
        )


@main.command("add-account")
@click.argument("name")
@click.option(
    "--password",
    "password_opt",
    default=None,
    help="Password (prompted securely if omitted). Only used for auth_method='password'.",
)
@click.pass_context
def add_account(ctx: click.Context, name: str, password_opt: str | None) -> None:
    """Store the IMAP password (or refresh-token slot for OAuth) for an account.

    The account must already be declared in config.toml.
    """
    cfg = load_config(ctx.obj["config_path"])
    account = _account_or_die(cfg, name)

    if account.auth_method == "password":
        pw = password_opt or click.prompt(
            f"IMAP password for {account.email}", hide_input=True, confirmation_prompt=True
        )
        secrets.set_password(name, pw)
        click.echo(f"stored password for {name} in keyring")
    else:
        raise click.ClickException(
            f"account {name!r} uses {account.auth_method!r}; "
            f"run `localmail oauth-login {name}` instead"
        )


@main.command("remove-account")
@click.argument("name")
@click.pass_context
def remove_account(ctx: click.Context, name: str) -> None:
    """Remove any stored secret for an account from the keyring."""
    secrets.delete_password(name)
    secrets.delete_refresh_token(name)
    click.echo(f"cleared secrets for {name}")


@main.command("oauth-login")
@click.argument("name")
@click.pass_context
def oauth_login(ctx: click.Context, name: str) -> None:
    """Run the Gmail OAuth2 consent flow and store the refresh token in the keyring.

    Opens a local browser. The account must be declared as auth_method='oauth2'
    in config.toml and [gmail_oauth] client_secrets_file must point to the
    Google Cloud Desktop OAuth client JSON.
    """
    cfg = load_config(ctx.obj["config_path"])
    account = _account_or_die(cfg, name)
    if account.auth_method != "oauth2":
        raise click.ClickException(
            f"account {name!r} uses auth_method={account.auth_method!r}; "
            f"oauth-login only applies to OAuth2 accounts"
        )
    if account.oauth_provider != "gmail":
        raise click.ClickException(
            f"unsupported oauth_provider: {account.oauth_provider!r}"
        )
    if cfg.gmail_oauth is None:
        raise click.ClickException(
            "config.toml is missing [gmail_oauth] client_secrets_file"
        )

    click.echo("opening browser for Google consent ...")
    creds = run_consent_flow(cfg.gmail_oauth.client_secrets_file)
    secrets.set_refresh_token(name, creds.refresh_token)
    click.echo(f"stored OAuth refresh token for {name} in keyring")


@main.command("sync")
@click.option("--account", "account_name", default=None,
              help="Sync only this account (default: all accounts in config).")
@click.option("--no-ssl", is_flag=True, default=False,
              help="Disable TLS — for testing against a local IMAP server only.")
@click.option("--limit-per-folder", "limit_per_folder", type=int, default=None,
              help="Fetch at most N new UIDs per folder in this run. "
                   "Useful for smoke-testing; the next run resumes from the checkpoint.")
@click.pass_context
def sync_cmd(
    ctx: click.Context,
    account_name: str | None,
    no_ssl: bool,
    limit_per_folder: int | None,
) -> None:
    """One-shot incremental sync. Useful for cron and manual testing."""
    cfg = load_config(ctx.obj["config_path"])
    accounts = (
        [_account_or_die(cfg, account_name)] if account_name else cfg.accounts
    )
    if not accounts:
        raise click.ClickException("no accounts configured")

    gmail_secrets = cfg.gmail_oauth.client_secrets_file if cfg.gmail_oauth else None
    with psycopg.connect(cfg.database.dsn, autocommit=False) as conn:
        for account in accounts:
            click.echo(f"--- syncing {account.name} ---")
            with open_connection(
                account, ssl=not no_ssl, gmail_client_secrets=gmail_secrets
            ) as imap:
                results = sync_account(
                    conn,
                    imap,
                    account=account,
                    attachments_root=cfg.attachments.root,
                    max_messages=limit_per_folder,
                    progress=click.echo,
                )
            for folder, n in results.items():
                click.echo(f"  {folder}: +{n} new")


@main.command("list-failed")
@click.option("--account", "account_name", default=None,
              help="Restrict to one account (default: all).")
@click.option("--limit", type=int, default=50, show_default=True,
              help="Maximum rows to display.")
@click.pass_context
def list_failed(ctx: click.Context, account_name: str | None, limit: int) -> None:
    """Show messages that sync skipped due to errors (with raw bytes preserved)."""
    cfg = load_config(ctx.obj["config_path"])
    with psycopg.connect(cfg.database.dsn) as conn, conn.cursor() as cur:
        if account_name:
            cur.execute(
                """
                SELECT f.id, a.name, m.name, f.uid, f.error_class,
                       left(f.error_message, 80), f.retry_count, f.failed_at
                FROM failed_messages f
                JOIN accounts a  ON a.id = f.account_id
                JOIN mailboxes m ON m.id = f.mailbox_id
                WHERE a.name = %s
                ORDER BY f.failed_at DESC
                LIMIT %s
                """,
                (account_name, limit),
            )
        else:
            cur.execute(
                """
                SELECT f.id, a.name, m.name, f.uid, f.error_class,
                       left(f.error_message, 80), f.retry_count, f.failed_at
                FROM failed_messages f
                JOIN accounts a  ON a.id = f.account_id
                JOIN mailboxes m ON m.id = f.mailbox_id
                ORDER BY f.failed_at DESC
                LIMIT %s
                """,
                (limit,),
            )
        rows = cur.fetchall()

    if not rows:
        click.echo("no failed messages")
        return
    for row in rows:
        rid, acct, mbox, uid, ecls, emsg, retries, when = row
        click.echo(
            f"#{rid} {acct}/{mbox} uid={uid} retries={retries} "
            f"{when:%Y-%m-%d %H:%M} {ecls}: {emsg}"
        )


@main.command("retry-failed")
@click.option("--account", "account_name", default=None,
              help="Restrict to one account (default: all).")
@click.pass_context
def retry_failed(ctx: click.Context, account_name: str | None) -> None:
    """Re-attempt every failed message with the current parser. Successful
    rows are deleted from failed_messages and inserted into messages."""
    cfg = load_config(ctx.obj["config_path"])
    with psycopg.connect(cfg.database.dsn) as conn:
        acct_id: int | None = None
        if account_name:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM accounts WHERE name = %s", (account_name,))
                row = cur.fetchone()
                if not row:
                    raise click.ClickException(f"no such account: {account_name!r}")
                acct_id = row[0]
        ok, still = retry_failed_messages(
            conn,
            attachments_root=cfg.attachments.root,
            account_id=acct_id,
        )
    click.echo(f"recovered: {ok}    still failing: {still}")


@main.command("run")
@click.option("--no-ssl", is_flag=True, default=False,
              help="Disable TLS — for local-test IMAP servers only.")
@click.option("--log-level", default="INFO",
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
              help="Logging verbosity (default: INFO).")
@click.pass_context
def run_cmd(ctx: click.Context, no_ssl: bool, log_level: str) -> None:
    """Run the localmail daemon in the foreground.

    Maintains one IMAP IDLE connection per account (on INBOX) plus a periodic
    poll loop for the remaining folders. Intended to be supervised by systemd
    or launchd; SIGTERM and SIGINT shut it down cleanly.
    """
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s %(levelname)s %(threadName)s %(name)s %(message)s",
    )
    cfg = load_config(ctx.obj["config_path"])
    daemon = Daemon(cfg, ssl=not no_ssl)
    daemon.run_forever()


def _page_to_dict(page) -> dict:
    """Convert a SearchPage into a JSON-serializable dict."""
    out = asdict(page)
    return _json.loads(_json.dumps(out, default=str))


def _print_text_page(page) -> None:
    if not page.results:
        click.echo("no results")
        return
    for r in page.results:
        click.echo(f"[{r.rank}] {r.date_sent or '-'}  {r.from_addr or '-':40.40s}  "
                   f"{r.subject or '(no subject)':60.60s}")
        click.echo(f"    score={r.score:.3f} (rrf={r.rrf_score:.4f})  {r.snippet_source}")
        if r.attachment_filename:
            click.echo(f"    [{r.attachment_filename}]")
        click.echo(f"    {r.snippet}")
        click.echo("")
    if page.search_token:
        click.echo(f"token: {page.search_token}   "
                   f"(page {page.page}, pool {page.pool_size})")
        if page.has_more_in_pool:
            click.echo(f"hint: localmail search-page {page.search_token} {page.page + 1}")
        if page.can_grow_pool:
            click.echo(f"hint: localmail search-grow {page.search_token} --candidates 200")


@main.command()
@click.argument("query", nargs=-1, required=True)
@click.option("--account", "accounts", multiple=True, help="restrict to account name(s)")
@click.option("--folder", "folders", multiple=True)
@click.option("--after", type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option("--before", type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option("--from", "from_substr")
@click.option("--to", "to_substr")
@click.option("--subject", "subject_substr")
@click.option("--has-attachment", is_flag=True, default=None)
@click.option("--label")
@click.option("--page-size", type=int)
@click.option("--candidates-per-arm", type=int)
@click.option("--rerank-pool", type=int)
@click.option("--no-rerank", is_flag=True)
@click.option("--smart", is_flag=True)
@click.option("--no-cache", is_flag=True)
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
@click.option("--verbose", is_flag=True)
def search(query, accounts, folders, after, before, from_substr, to_substr,
           subject_substr, has_attachment, label, page_size, candidates_per_arm,
           rerank_pool, no_rerank, smart, no_cache, fmt, verbose):
    """Hybrid BM25 + vector search over the local archive."""
    text_q = " ".join(query)
    extra: list[str] = []
    for a in accounts:
        extra.append(f"account:{a}")
    for f in folders:
        extra.append(f'folder:"{f}"')
    if from_substr:
        extra.append(f'from:"{from_substr}"')
    if to_substr:
        extra.append(f'to:"{to_substr}"')
    if subject_substr:
        extra.append(f'subject:"{subject_substr}"')
    if after:
        extra.append(f"after:{after.date().isoformat()}")
    if before:
        extra.append(f"before:{before.date().isoformat()}")
    if has_attachment:
        extra.append("has:attachment")
    if label:
        extra.append(f"label:{label}")
    if extra:
        text_q = f"{text_q} {' '.join(extra)}".strip()

    searcher = create_searcher()
    try:
        if no_rerank:
            searcher._reranker = None  # documented internal — Phase 5 can promote
        page = searcher.search(
            text_q, page_size=page_size, candidates_per_arm=candidates_per_arm,
            rerank_pool_size=rerank_pool, use_cache=not no_cache, smart=smart,
        )
    except RuntimeError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)

    if verbose:
        click.echo(f"timing(ms): {page.timing_ms}", err=True)
    if fmt == "json":
        click.echo(_json.dumps(_page_to_dict(page), default=str))
    else:
        _print_text_page(page)


_CACHE_HINT = (
    "the page cache lives in-process and isn't shared across CLI invocations. "
    "For deep pagination, use the Python API (localmail.search.create_searcher) "
    "or the MCP server (Phase 3). For one-shot follow-up, re-run with "
    "`localmail search ... --candidates-per-arm 200 --rerank-pool 200`."
)


@main.command("search-page")
@click.argument("token")
@click.argument("page", type=int)
def search_page(token, page):
    """Fetch a follow-up page from an earlier `localmail search` token.

    Not supported across separate CLI invocations — see message.
    """
    click.echo(_CACHE_HINT, err=True)
    sys.exit(2)


@main.command("search-grow")
@click.argument("token")
@click.option("--candidates", type=int, required=True)
def search_grow(token, candidates):
    """Re-run with a larger candidate pool — see CLI cache limitation."""
    click.echo(_CACHE_HINT, err=True)
    sys.exit(2)


def _dsn() -> str:
    """Resolve DSN from the existing localmail config."""
    return load_config().database.dsn


def _make_backend(cfg):
    """Build the configured EmbeddingBackend. Override via monkeypatch in tests."""
    from localmail.search.embeddings import FastEmbedBackend
    return FastEmbedBackend(cfg.search)


@main.command("embed-backfill")
@click.option("--account", "account_name")
@click.option("--no-progress", is_flag=True)
def embed_backfill(account_name, no_progress):
    """Drain the embedding queue in the foreground; exit when empty."""
    from localmail.db import open_pool
    from localmail.search.embed_worker import run_embed_worker_once
    cfg = load_config()
    backend = _make_backend(cfg)
    pool = open_pool(_dsn())
    try:
        total = 0
        while True:
            with pool.connection() as conn:
                wrote = run_embed_worker_once(conn, cfg.search, backend)
            if wrote == 0:
                break
            total += wrote
            if not no_progress:
                click.echo(f"embedded {wrote} chunks (total {total})", err=True)
    finally:
        pool.close()
    click.echo(f"done: {total} chunks embedded")


@main.command("search-status")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def search_status(fmt):
    """Show progress: how many chunks remain to be embedded, failures, etc."""
    from localmail.db import open_pool
    pool = open_pool(_dsn())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM messages")
            row = cur.fetchone()
            assert row is not None
            messages_total = row[0]
            cur.execute("SELECT COUNT(*) FROM message_chunks")
            row = cur.fetchone()
            assert row is not None
            chunks_total = row[0]
            cur.execute("SELECT COUNT(*) FROM message_chunks WHERE embedding_v1 IS NOT NULL")
            row = cur.fetchone()
            assert row is not None
            chunks_embedded = row[0]
            cur.execute("SELECT COUNT(*) FROM failed_embeddings")
            row = cur.fetchone()
            assert row is not None
            failed = row[0]
    finally:
        pool.close()
    payload = {
        "messages_total": messages_total,
        "chunks_total": chunks_total,
        "chunks_embedded": chunks_embedded,
        "chunks_pending": chunks_total - chunks_embedded,
        "failed_embeddings": failed,
    }
    if fmt == "json":
        click.echo(_json.dumps(payload))
    else:
        for k, v in payload.items():
            click.echo(f"{k:24s} {v}")


@main.command("list-failed-embeddings")
@click.option("--limit", type=int, default=50)
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def list_failed_embeddings(limit, fmt):
    """Show recent failed_embeddings rows."""
    from localmail.db import open_pool
    pool = open_pool(_dsn())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, chunk_table, chunk_id, error_class, error_message,"
                " retry_count, failed_at, last_retry_at FROM failed_embeddings"
                " ORDER BY failed_at DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
    finally:
        pool.close()
    cols = ["id", "chunk_table", "chunk_id", "error_class", "error_message",
            "retry_count", "failed_at", "last_retry_at"]
    payload = [dict(zip(cols, r, strict=True)) for r in rows]
    if fmt == "json":
        click.echo(_json.dumps(payload, default=str))
    else:
        for p in payload:
            click.echo(f"#{p['id']:6d}  {p['chunk_table']}:{p['chunk_id']}  "
                       f"{p['error_class']}  retries={p['retry_count']}  "
                       f"{p['failed_at']}")
            click.echo(f"        {p['error_message']}")


@main.command("retry-failed-embeddings")
@click.option("--chunk-table", default=None,
              help="restrict to message_chunks or attachment_chunks")
def retry_failed_embeddings(chunk_table):
    """Clear failed_embeddings rows so the embed worker re-attempts them."""
    from localmail.db import open_pool
    pool = open_pool(_dsn())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            if chunk_table:
                cur.execute("DELETE FROM failed_embeddings WHERE chunk_table = %s",
                            (chunk_table,))
            else:
                cur.execute("DELETE FROM failed_embeddings")
            n = cur.rowcount
        conn.commit()
    finally:
        pool.close()
    click.echo(f"cleared {n} failed_embeddings rows")


if __name__ == "__main__":
    main()
