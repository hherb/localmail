# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""localmail CLI entry point."""

from __future__ import annotations

import json as _json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import click
import psycopg

import logging

from . import secrets
from .config import AccountConfig, Config, default_config_path, load_config
from .daemon import Daemon
from .daemon_accounts import account_config_from_row
from .daemon_cli import daemon_group
from .account_seed import account_create_kwargs, seed_accounts
from .api.admin.accounts import (
    Account,
    AccountFieldError,
    AccountInUse,
    create_account,
    delete_account,
    get_account_by_name,
    list_accounts_full,
    list_syncable_accounts,
    touch_account_updated_at,
    update_account,
)
from .cli_account_resolve import Found, NotFound, plan_account_resolution
from .cli_sync_toggle import plan_sync_toggle
from .db import apply_migrations
from .imap_client import open_connection
from .oauth_gmail import run_consent_flow
from .search import create_searcher
from .sync import backfill_internal_date, retry_failed_messages, sync_account
from .upgrade_estimate import ESTIMATORS, EstimateResult


def _is_loopback_bind(bind: str) -> bool:
    """Return True iff `bind` is unambiguously a loopback interface.

    Accepts literal `localhost`, any literal IPv4/IPv6 address whose
    `.is_loopback` is True (e.g. `127.0.0.1`, `127.0.0.5`, `::1`), and
    rejects everything else — including `0.0.0.0`, public IPs, and DNS
    names that might resolve to non-loopback addresses.
    """
    import ipaddress

    if bind == "localhost":
        return True
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


def _warm_reranker_in_background(reranker) -> None:
    """Pre-load the ONNX session so the first user search doesn't pay
    session-init cost on top of inference. Runs in a daemon thread so
    `serve` returns immediately.

    This is a marginal speedup (~0.5s on first query). The dominant cost
    of reranking — the per-pair forward pass — is paid on every query;
    that's controlled by ``search.rerank_pool_size`` /
    ``search.rerank_max_chars``, not by warmup.

    Also serves as a startup smoke test: if the model can't load at all,
    the WARNING fires here rather than waiting for the first user query.
    """
    import threading
    import time

    def _warm() -> None:
        log = logging.getLogger("localmail.search")
        model = getattr(reranker, "model", type(reranker).__name__)
        log.info("warming reranker %r (this loads the ONNX session)", model)
        t0 = time.monotonic()
        try:
            reranker.rerank("warmup", ["the quick brown fox"])
        except Exception as exc:
            log.warning("reranker warmup failed (%s) — first user search may be slow", exc)
            return
        log.info("reranker %r warm in %.1fs", model, time.monotonic() - t0)

    threading.Thread(target=_warm, name="reranker-warmup", daemon=True).start()


def _account_or_die(cfg: Config, name: str) -> AccountConfig:
    for a in cfg.accounts:
        if a.name == name:
            return a
    raise click.ClickException(
        f"account {name!r} is not declared in config.toml; "
        f"add an [[accounts]] block with name = {name!r} first"
    )


def _resolve_account_row(conn: psycopg.Connection, cfg: Config, name: str) -> Account:
    """Resolve `name` to an Account row, seeding it from TOML if absent.

    Returns the DB Account. Raises click.ClickException when the name is in
    neither the DB nor config.toml, or when a malformed TOML block fails
    create_account validation. The caller owns the transaction (commit).
    """
    existing = {row.name: row for row in list_accounts_full(conn)}
    res = plan_account_resolution(name, cfg.accounts, existing)
    if isinstance(res, Found):
        return res.account
    if isinstance(res, NotFound):
        raise click.ClickException(
            f"unknown account {name!r}: not in the DB and no matching "
            f"[[accounts]] block in config.toml"
        )
    try:
        return create_account(conn, **account_create_kwargs(res.config))
    except AccountFieldError as exc:
        raise click.ClickException(
            f"cannot create account {name!r}: {exc}"
        ) from exc


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


main.add_command(daemon_group)


@main.command("init-db")
@click.pass_context
def init_db(ctx: click.Context) -> None:
    """Apply pending schema migrations, then seed accounts from config.toml."""
    cfg = load_config(ctx.obj["config_path"])
    applied = apply_migrations(
        cfg.database.dsn,
        index_build_work_mem_mb=cfg.search.index_build_maintenance_work_mem_mb,
    )
    if applied:
        for rev in applied:
            click.echo(f"applied {rev}")
    else:
        click.echo("schema already up to date")

    try:
        with psycopg.connect(cfg.database.dsn) as conn:
            result = seed_accounts(conn, cfg.accounts)
            conn.commit()
    except AccountFieldError as exc:
        raise click.ClickException(f"account seed failed: {exc}") from exc
    click.echo(
        f"seeded accounts: inserted={result.inserted} "
        f"skipped={result.skipped} drifted={result.drifted}"
    )


@main.command("list-accounts")
@click.pass_context
def list_accounts(ctx: click.Context) -> None:
    """Show accounts in the DB and whether a secret is stored."""
    cfg = load_config(ctx.obj["config_path"])
    with psycopg.connect(cfg.database.dsn) as conn:
        rows = list_accounts_full(conn)
    if not rows:
        click.echo("no accounts")
        return
    for a in rows:
        if a.auth_method == "archive":
            endpoint, secret_label = "archive", "n/a"
        elif a.auth_method == "password":
            endpoint = f"{a.imap_host}:{a.imap_port}"
            secret_label = "password" if secrets.get_password(a.name) else "MISSING"
        else:
            endpoint = f"{a.imap_host}:{a.imap_port}"
            secret_label = "oauth-token" if secrets.get_refresh_token(a.name) else "MISSING"
        click.echo(
            f"{a.name}\t{a.email_address}\t{endpoint}\t{a.auth_method}"
            f"\tsync={a.sync_enabled}\t[{secret_label}]"
        )


@main.command("add-account")
@click.argument("name")
@click.option(
    "--password",
    "password_opt",
    default=None,
    help="Password (prompted securely if omitted). Only for auth_method='password'.",
)
@click.pass_context
def add_account(ctx: click.Context, name: str, password_opt: str | None) -> None:
    """Store the IMAP password for an account in the keyring.

    Resolves NAME against the DB; if absent but declared in config.toml, the
    DB row is created from that block first, then the password is stored.
    """
    cfg = load_config(ctx.obj["config_path"])
    with psycopg.connect(cfg.database.dsn) as conn:
        account = _resolve_account_row(conn, cfg, name)
        # Validate before commit: a mismatched command must not leave a
        # newly-seeded row behind. Raising here rolls back the seed.
        if account.auth_method == "oauth2":
            raise click.ClickException(
                f"account {name!r} uses oauth2; run `localmail oauth-login {name}` instead"
            )
        if account.auth_method != "password":
            raise click.ClickException(
                f"account {name!r} is an archive account; it has no IMAP secret"
            )
        conn.commit()
    pw = password_opt or click.prompt(
        f"IMAP password for {account.email_address}",
        hide_input=True, confirmation_prompt=True,
    )
    secrets.set_password(name, pw)
    with psycopg.connect(cfg.database.dsn) as conn:
        touch_account_updated_at(conn, account.id)
        conn.commit()
    click.echo(f"stored password for {name} in keyring")


@main.command("remove-account")
@click.argument("name")
@click.option("--delete-row", is_flag=True, default=False,
              help="Also delete the account row from the DB (not just secrets).")
@click.option("--force", is_flag=True, default=False,
              help="With --delete-row: cascade-delete even if messages exist.")
@click.pass_context
def remove_account(ctx: click.Context, name: str,
                   delete_row: bool, force: bool) -> None:
    """Clear stored secrets for an account. With --delete-row, also remove
    the DB row (refusing if messages reference it unless --force)."""
    if force and not delete_row:
        raise click.ClickException("--force only applies with --delete-row")
    cfg = load_config(ctx.obj["config_path"])
    if not delete_row:
        secrets.delete_password(name)
        secrets.delete_refresh_token(name)
        click.echo(f"cleared secrets for {name}")
        return
    with psycopg.connect(cfg.database.dsn) as conn:
        account = get_account_by_name(conn, name)
        if account is None:
            secrets.delete_password(name)
            secrets.delete_refresh_token(name)
            click.echo(f"no DB row for {name}; cleared keyring only")
            return
        try:
            delete_account(conn, account.id, force=force)
        except AccountInUse as exc:
            raise click.ClickException(
                f"{exc}; pass --force to delete it and its messages"
            ) from exc
        conn.commit()
    secrets.delete_password(name)
    secrets.delete_refresh_token(name)
    click.echo(f"deleted account {name} and cleared its secrets")


def _apply_sync_toggle(ctx: click.Context, name: str, *, enable: bool) -> None:
    """Resolve NAME in the DB and enable/disable its sync per the pure planner."""
    cfg = load_config(ctx.obj["config_path"])
    with psycopg.connect(cfg.database.dsn) as conn:
        account = get_account_by_name(conn, name)
        if account is None:
            raise click.ClickException(f"no such account: {name!r}")
        plan = plan_sync_toggle(
            name=name, auth_method=account.auth_method,
            currently_enabled=account.sync_enabled, enable=enable,
        )
        if plan.action == "reject":
            raise click.ClickException(plan.message)
        if plan.action == "apply":
            update_account(conn, account.id, sync_enabled=enable)
            conn.commit()
        click.echo(plan.message)


@main.command("enable-account")
@click.argument("name")
@click.pass_context
def enable_account(ctx: click.Context, name: str) -> None:
    """Resume syncing an account (set sync_enabled = TRUE)."""
    _apply_sync_toggle(ctx, name, enable=True)


@main.command("disable-account")
@click.argument("name")
@click.pass_context
def disable_account(ctx: click.Context, name: str) -> None:
    """Pause syncing an account (set sync_enabled = FALSE)."""
    _apply_sync_toggle(ctx, name, enable=False)


@main.command("oauth-login")
@click.argument("name")
@click.pass_context
def oauth_login(ctx: click.Context, name: str) -> None:
    """Run the Gmail OAuth2 consent flow and store the refresh token.

    Resolves NAME against the DB (seeding from config.toml if absent). The
    account must be auth_method='oauth2' with oauth_provider='gmail', and
    [gmail_oauth] client_secrets_file must be set.
    """
    cfg = load_config(ctx.obj["config_path"])
    with psycopg.connect(cfg.database.dsn) as conn:
        account = _resolve_account_row(conn, cfg, name)
        # Validate before commit: a mismatched command must not leave a
        # newly-seeded row behind. Raising here rolls back the seed.
        if account.auth_method != "oauth2":
            raise click.ClickException(
                f"account {name!r} uses auth_method={account.auth_method!r}; "
                f"oauth-login only applies to OAuth2 accounts"
            )
        if account.oauth_provider != "gmail":
            raise click.ClickException(
                f"unsupported oauth_provider: {account.oauth_provider!r}"
            )
        conn.commit()
    if cfg.gmail_oauth is None:
        raise click.ClickException(
            "config.toml is missing [gmail_oauth] client_secrets_file"
        )
    click.echo("opening browser for Google consent ...")
    creds = run_consent_flow(cfg.gmail_oauth.client_secrets_file)
    secrets.set_refresh_token(name, creds.refresh_token)
    with psycopg.connect(cfg.database.dsn) as conn:
        touch_account_updated_at(conn, account.id)
        conn.commit()
    click.echo(f"stored OAuth refresh token for {name} in keyring")


@main.command("sync")
@click.option("--account", "account_name", default=None,
              help="Sync only this account (default: all syncable DB accounts).")
@click.option("--no-ssl", is_flag=True, default=False,
              help="Disable TLS — for testing against a local IMAP server only.")
@click.option("--limit-per-folder", "limit_per_folder", type=int, default=None,
              help="Fetch at most N new UIDs per folder in this run. "
                   "The next run resumes from the checkpoint.")
@click.pass_context
def sync_cmd(ctx: click.Context, account_name: str | None,
             no_ssl: bool, limit_per_folder: int | None) -> None:
    """One-shot incremental sync over the DB accounts. For cron + manual testing."""
    cfg = load_config(ctx.obj["config_path"])
    gmail_secrets = cfg.gmail_oauth.client_secrets_file if cfg.gmail_oauth else None
    with psycopg.connect(cfg.database.dsn, autocommit=False) as conn:
        if account_name:
            row = get_account_by_name(conn, account_name)
            if row is None:
                raise click.ClickException(f"no such account: {account_name!r}")
            if row.auth_method == "archive":
                raise click.ClickException(
                    f"account {account_name!r} is an archive account; not synced"
                )
            rows = [row]
        else:
            rows = list_syncable_accounts(conn)
        if not rows:
            raise click.ClickException("no syncable accounts")

        for row in rows:
            account = account_config_from_row(row)
            click.echo(f"--- syncing {account.name} ---")
            with open_connection(
                account, ssl=not no_ssl, gmail_client_secrets=gmail_secrets,
                timeout=cfg.daemon.imap_timeout_s,
            ) as imap:
                results = sync_account(
                    conn, imap, account=account, account_id=row.id,
                    attachments_root=cfg.attachments.root,
                    max_messages=limit_per_folder, progress=click.echo,
                    max_body_fetch_retries=cfg.daemon.max_body_fetch_retries,
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


@main.command("import")
@click.argument("source_path", type=click.Path(exists=True))
@click.option("--account", "account_name", required=True, help="Target archive account name.")
@click.option("--kind", type=click.Choice(["mbox", "maildir"]), required=True)
@click.pass_context
def import_cmd(
    ctx: click.Context, source_path: str, account_name: str, kind: str,
) -> None:
    """Import an mbox file or maildir directory into an archive account.

    Runs synchronously and prints the final counts. Re-running is idempotent
    (per-account dedup), so a re-import skips already-imported messages.
    """
    from localmail.api.admin import accounts as accounts_svc
    from localmail.api.admin import imports as imports_svc
    from localmail.importer import runner

    cfg = load_config(ctx.obj["config_path"])
    with psycopg.connect(cfg.database.dsn, autocommit=False) as conn:
        account = accounts_svc.get_account_by_name(conn, account_name)
        if account is None:
            raise click.ClickException(f"no such account: {account_name!r}")
        if account.auth_method != "archive":
            raise click.ClickException(f"{account_name!r} is not an archive account")
        try:
            jid = imports_svc.create_job(
                conn, account_id=account.id, source_kind=kind, source_path=source_path)
        except imports_svc.ImportBusyError as e:
            raise click.ClickException(str(e))
        conn.commit()

    runner.run_import(
        lambda: psycopg.connect(cfg.database.dsn, autocommit=False), jid,
        attachments_root=cfg.attachments.root,
        checkpoint_every=cfg.imports.checkpoint_every,
        checkpoint_seconds=cfg.imports.checkpoint_seconds)

    with psycopg.connect(cfg.database.dsn) as conn:
        job = imports_svc.get_job(conn, jid)
    click.echo(
        f"status={job.status} processed={job.processed} inserted={job.inserted} "
        f"skipped_dup={job.skipped_dup} failed={job.failed}")
    if job.error_msg:
        click.echo(f"error: {job.error_msg}", err=True)


@main.command("backfill-internal-date")
@click.option("--account", "account_name", default=None,
              help="Restrict to one account (default: all).")
@click.option("--no-ssl", is_flag=True, default=False,
              help="Disable TLS — for local-test IMAP servers only.")
@click.pass_context
def backfill_internal_date_cmd(
    ctx: click.Context, account_name: str | None, no_ssl: bool,
) -> None:
    """Populate `messages.internal_date` for rows where it is NULL by
    re-fetching INTERNALDATE from the IMAP server.

    Pre-migration-0018 syncs didn't store INTERNALDATE; every row was
    inserted with `internal_date` left implicitly NULL (or the legacy
    `date_received` column held sync time, which migration 0018 already
    decoupled). This pass walks every mailbox and runs a single
    `FETCH UID INTERNALDATE` per UID — no body bytes are refetched, so
    it's fast and bandwidth-cheap. Idempotent; safe to re-run.
    """
    cfg = load_config(ctx.obj["config_path"])
    accounts = (
        [_account_or_die(cfg, account_name)] if account_name else cfg.accounts
    )
    if not accounts:
        raise click.ClickException("no accounts configured")
    gmail_secrets = cfg.gmail_oauth.client_secrets_file if cfg.gmail_oauth else None
    total_scanned = 0
    total_updated = 0
    with psycopg.connect(cfg.database.dsn, autocommit=False) as conn:
        for account in accounts:
            click.echo(f"--- backfilling {account.name} ---")
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM accounts WHERE name = %s", (account.name,))
                row = cur.fetchone()
                if not row:
                    click.echo(f"  {account.name}: not in DB; skipping")
                    continue
                account_id = int(row[0])
            with open_connection(
                account, ssl=not no_ssl, gmail_client_secrets=gmail_secrets,
                timeout=cfg.daemon.imap_timeout_s,
            ) as imap:
                scanned, updated = backfill_internal_date(
                    conn, imap,
                    account_id=account_id,
                    progress=click.echo,
                )
            total_scanned += scanned
            total_updated += updated
            click.echo(f"  {account.name}: {updated}/{scanned} updated")
    click.echo(f"done: {total_updated} rows updated across {total_scanned} candidates")


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
        if page.has_more_in_pool or page.can_grow_pool:
            click.echo(
                "hint: pagination/grow is in-process only — for follow-up pages, "
                "use the Python API (localmail.search.create_searcher) or the "
                "MCP server (Phase 3). To widen this query in one shot, re-run "
                "with --candidates-per-arm 200 --rerank-pool 200."
            )


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
@click.pass_context
def search(ctx, query, accounts, folders, after, before, from_substr, to_substr,
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

    searcher = create_searcher(load_config(ctx.obj["config_path"]))
    try:
        page = searcher.search(
            text_q, page_size=page_size, candidates_per_arm=candidates_per_arm,
            rerank_pool_size=rerank_pool, use_cache=not no_cache, smart=smart,
            disable_rerank=no_rerank,
        )
    except RuntimeError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)

    # The CLI drives the Searcher directly (single page; hard-errors above when
    # no rewriter is configured), so FAILED is the only "rewrite didn't take"
    # status reachable here — unavailable/not_attempted are run_search-only.
    from localmail.search.rewrite_status import FAILED

    if page.rewrite_status == FAILED:
        detail = page.rewrite_note or "ran the original query"
        click.echo(f"note: --smart {detail}", err=True)

    if verbose:
        click.echo(f"timing(ms): {page.timing_ms}", err=True)
    if fmt == "json":
        click.echo(_json.dumps(_page_to_dict(page), default=str))
    else:
        _print_text_page(page)


def _dsn() -> str:
    """Resolve DSN from the existing localmail config."""
    return load_config().database.dsn


def _make_backend(cfg):
    """Build the configured EmbeddingBackend. Override via monkeypatch in tests."""
    from localmail.search.embeddings import FastEmbedBackend
    return FastEmbedBackend(cfg.search)


@main.command("extract-backfill")
@click.option("--no-progress", is_flag=True)
def extract_backfill(no_progress: bool) -> None:
    """Drain the attachment-extraction queue in the foreground; exit when empty.

    Account-agnostic — extracts text from all eligible blobs whose MIME type or
    file extension matches the configured allowlists.
    """
    from localmail.db import open_pool
    from localmail.search.extract_worker import run_extract_worker_once
    cfg = load_config()
    pool = open_pool(_dsn())
    try:
        total = 0
        while True:
            with pool.connection() as conn:
                touched = run_extract_worker_once(conn, cfg.search)
            if touched == 0:
                break
            total += touched
            if not no_progress:
                click.echo(
                    f"extracted {touched} blobs (total {total})",
                    err=True,
                )
    finally:
        pool.close()
    click.echo(f"done: {total} blobs processed")


@main.command("embed-backfill")
@click.option("--no-progress", is_flag=True)
def embed_backfill(no_progress):
    """Drain the embedding queue in the foreground; exit when empty.

    Account-agnostic — fills embeddings for all accounts. Each embedding
    sweep also processes one `body_lang_detect_batch_size` slice of
    language-detection work; after the embedding queue drains, any
    remaining NULL `body_lang` rows are flushed in a tight loop so the
    command's exit means both queues are empty.
    """
    from localmail.db import open_pool
    from localmail.search.embed_worker import run_embed_worker_once
    from localmail.search.lang_detect import make_detector, run_lang_detect_pass
    cfg = load_config()
    backend = _make_backend(cfg)
    lang_detector = make_detector(cfg.search)
    pool = open_pool(_dsn())
    try:
        total = 0
        while True:
            with pool.connection() as conn:
                wrote = run_embed_worker_once(
                    conn, cfg.search, backend, lang_detector=lang_detector,
                )
            if wrote == 0:
                break
            total += wrote
            if not no_progress:
                click.echo(f"embedded {wrote} chunks (total {total})", err=True)
        lang_total = 0
        if lang_detector is not None:
            while True:
                with pool.connection() as conn:
                    processed = run_lang_detect_pass(conn, cfg.search, lang_detector)
                if processed == 0:
                    break
                lang_total += processed
                if not no_progress:
                    click.echo(
                        f"detected lang for {processed} messages (total {lang_total})",
                        err=True,
                    )
    finally:
        pool.close()
    click.echo(
        f"done: {total} chunks embedded, {lang_total} messages lang-detected"
    )


@main.command("lang-backfill")
@click.option("--no-progress", is_flag=True)
def lang_backfill(no_progress: bool) -> None:
    """Populate `messages.body_lang` for every message with NULL body_lang.

    Account-agnostic. Runs the same per-message detector the embed worker
    uses, in a tight foreground loop until no rows remain. Pre-existing
    body_lang values are never overwritten; messages with NULL body_text
    are skipped (no body to detect).
    """
    from localmail.db import open_pool
    from localmail.search.lang_detect import make_detector, run_lang_detect_pass
    cfg = load_config()
    detector = make_detector(cfg.search)
    if detector is None:
        click.echo("body_lang detection is disabled in config; nothing to do", err=True)
        return
    pool = open_pool(_dsn())
    try:
        total = 0
        while True:
            with pool.connection() as conn:
                processed = run_lang_detect_pass(conn, cfg.search, detector)
            if processed == 0:
                break
            total += processed
            if not no_progress:
                click.echo(
                    f"detected lang for {processed} messages (total {total})",
                    err=True,
                )
    finally:
        pool.close()
    click.echo(f"done: {total} messages processed")


@main.command("search-status")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def search_status(fmt):
    """Show progress: how many chunks remain to be embedded, failures, etc.

    Reports message embedding status (Phase 1) and attachment extraction /
    embedding status (Phase 2), plus failure counts for both subsystems.
    """
    from localmail.db import open_pool
    cfg = load_config()
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
            cur.execute(
                "SELECT count(*) FROM attachment_blobs b "
                "WHERE b.mime_type = ANY(%s) "
                "   OR lower(substring(b.path FROM '\\.[^.]+$')) = ANY(%s)",
                (
                    cfg.search.extractor_mime_allowlist,
                    cfg.search.extractor_extension_allowlist,
                ),
            )
            row = cur.fetchone()
            assert row is not None
            blobs_eligible = row[0]
            cur.execute(
                "SELECT count(*) FROM attachment_text "
                "WHERE extracted_text <> ''"
            )
            row = cur.fetchone()
            assert row is not None
            blobs_extracted = row[0]
            blobs_pending = max(0, blobs_eligible - blobs_extracted)
            cur.execute("SELECT count(*) FROM attachment_chunks")
            row = cur.fetchone()
            assert row is not None
            attachment_chunks_total = row[0]
            cur.execute(
                "SELECT count(*) FROM attachment_chunks "
                "WHERE embedding_v1 IS NOT NULL"
            )
            row = cur.fetchone()
            assert row is not None
            attachment_chunks_embedded = row[0]
            cur.execute("SELECT count(*) FROM failed_extractions")
            row = cur.fetchone()
            assert row is not None
            failed_extractions_count = row[0]
            cur.execute("SELECT count(*) FROM messages WHERE body_lang IS NOT NULL")
            row = cur.fetchone()
            assert row is not None
            body_lang_populated = row[0]
            cur.execute(
                "SELECT count(*) FROM messages"
                " WHERE body_lang IS NULL AND body_text IS NOT NULL"
            )
            row = cur.fetchone()
            assert row is not None
            body_lang_pending = row[0]
    finally:
        pool.close()
    payload = {
        "messages_total": messages_total,
        "chunks_total": chunks_total,
        "chunks_embedded": chunks_embedded,
        "chunks_pending": chunks_total - chunks_embedded,
        "failed_embeddings": failed,
        "blobs_eligible": blobs_eligible,
        "blobs_extracted": blobs_extracted,
        "blobs_pending": blobs_pending,
        "attachment_chunks_total": attachment_chunks_total,
        "attachment_chunks_embedded": attachment_chunks_embedded,
        "failed_extractions": failed_extractions_count,
        "body_lang_populated": body_lang_populated,
        "body_lang_pending": body_lang_pending,
    }
    if fmt == "json":
        click.echo(_json.dumps(payload))
    else:
        for k, v in payload.items():
            click.echo(f"{k:24s} {v}")


def _applied_revisions(conn: psycopg.Connection) -> set[str]:
    """Return revisions from schema_migrations as a set.

    Returns the empty set if schema_migrations doesn't exist yet
    (treats everything as pending — same convention as
    db.pending_migrations).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'schema_migrations'"
        )
        if cur.fetchone() is None:
            return set()
        cur.execute("SELECT revision FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def _format_estimate_text(results: list[EstimateResult]) -> str:
    """Render EstimateResult list as a human-readable table."""
    lines = []
    for r in results:
        lines.append(f"revision: {r.revision}")
        lines.append(f"  status: {r.status}")
        if r.current_bytes:
            for k, v in r.current_bytes.items():
                lines.append(f"  {k}: {v:>15,} bytes ({v / (1024*1024):.1f} MiB)")
        if r.projected_bytes:
            for k, v in r.projected_bytes.items():
                lines.append(f"  {k} (projected): {v:>15,} bytes ({v / (1024*1024):.1f} MiB)")
        if r.projected_duration_s > 0:
            mins, secs = divmod(int(r.projected_duration_s), 60)
            lines.append(f"  projected lock duration: ~{mins}m {secs}s")
        for w in r.warnings:
            lines.append(f"  WARNING: {w}")
        lines.append("")  # blank line between revisions
    return "\n".join(lines).rstrip()


def _estimate_to_json(r: EstimateResult) -> dict:
    """Project an EstimateResult to a JSON-serialisable dict."""
    return {
        "revision": r.revision,
        "status": r.status,
        "current_bytes": r.current_bytes,
        "projected_bytes": r.projected_bytes,
        "projected_duration_s": r.projected_duration_s,
        "warnings": r.warnings,
    }


@main.command("estimate-upgrade")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format. text (default) is human-readable; json emits a list.",
)
def estimate_upgrade(fmt: str) -> None:
    """Pre-flight estimator for lock-heavy schema migrations.

    Reports projected (or actual) size + duration for migrations that
    hold long locks against a populated `messages` table. Read-only;
    safe to run against a live archive. See
    docs/operations/upgrade-runbook.md for the full procedure.
    """
    dsn = _dsn()
    try:
        with psycopg.connect(dsn) as conn:
            applied = _applied_revisions(conn)
            cfg = load_config().upgrade
            results = [
                fn(conn, cfg, rev in applied)
                for rev, fn in ESTIMATORS.items()
            ]
    except psycopg.Error as exc:
        # Only DB-level errors are user-facing here (unreachable host,
        # auth failure, missing schema_migrations on a never-init'd DB
        # — though that last case is masked by _applied_revisions
        # returning empty). Programming bugs still raise with their
        # original traceback.
        raise click.ClickException(str(exc)) from exc

    if fmt == "json":
        click.echo(_json.dumps([_estimate_to_json(r) for r in results]))
    else:
        click.echo(_format_estimate_text(results))


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


def _dsn_from_ctx(ctx: click.Context) -> str:
    """DSN resolver: env override wins over config (useful for tests)."""
    override = os.environ.get("LOCALMAIL_DSN_OVERRIDE")
    if override:
        return override
    cfg = load_config(ctx.obj["config_path"])
    return cfg.database.dsn


@main.command("add-api-user")
@click.argument("username")
@click.option("--password", "password_opt", default=None,
              help="If omitted, prompt on TTY or read from stdin via --password-stdin.")
@click.option("--password-stdin", "password_stdin", is_flag=True, default=False,
              help="Read the password from stdin (no echo, no prompt). For "
                   "scripts and CI; refuses to run on a TTY to avoid silent "
                   "hangs.")
@click.option("--admin", "is_admin", is_flag=True, default=False,
              help="Create the user with is_admin=TRUE (admin-UI bootstrap).")
@click.pass_context
def add_api_user(
    ctx: click.Context,
    username: str,
    password_opt: str | None,
    password_stdin: bool,
    is_admin: bool,
) -> None:
    """Create a new API user. Password is hashed with argon2id.

    Newly-created users have no account grants — their `/v1/*` calls will
    return empty lists and 404s until ``localmail grant-account`` is used.
    """
    from localmail.api.auth import create_user
    if password_stdin and password_opt is not None:
        raise click.ClickException("--password and --password-stdin are mutually exclusive")
    if password_stdin:
        if sys.stdin.isatty():
            raise click.ClickException(
                "--password-stdin requires piped input; refusing to read from a TTY"
            )
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            raise click.ClickException("--password-stdin: empty password")
    elif password_opt is not None:
        password = password_opt
    elif not sys.stdin.isatty():
        raise click.ClickException(
            "stdin is not a TTY; pass --password or --password-stdin"
        )
    else:
        password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
    with psycopg.connect(_dsn_from_ctx(ctx)) as conn:
        try:
            uid = create_user(conn, username, password)
            conn.commit()
        except psycopg.errors.UniqueViolation:
            raise click.ClickException(f"user {username!r} already exists")
        if is_admin:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE api_users SET is_admin = TRUE WHERE username = %s",
                    (username,),
                )
            conn.commit()
    click.echo(f"created user {username!r} (id={uid})")
    click.echo(
        f"note: no account grants yet. Use "
        f"`localmail grant-account {username} <account-name>` to give this "
        f"user read access to mail.",
        err=True,
    )


@main.command("remove-api-user")
@click.argument("username")
@click.pass_context
def remove_api_user(ctx: click.Context, username: str) -> None:
    """Delete an API user and all its tokens."""
    with psycopg.connect(_dsn_from_ctx(ctx)) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM api_users WHERE username = %s", (username,))
        if cur.rowcount == 0:
            raise click.ClickException(f"no such user: {username!r}")
        conn.commit()
    click.echo(f"removed user {username!r}")


@main.command("list-api-users")
@click.option("--with-grants", is_flag=True, default=False,
              help="Show each user's account grants below their name.")
@click.pass_context
def list_api_users(ctx: click.Context, with_grants: bool) -> None:
    """List configured API users (and whether each is disabled)."""
    from localmail.api.acl import grants_for_user
    with psycopg.connect(_dsn_from_ctx(ctx)) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, username, disabled_at FROM api_users ORDER BY username")
        rows = cur.fetchall()
        if not rows:
            click.echo("(no users)")
            return
        for uid, username, disabled_at in rows:
            marker = " [disabled]" if disabled_at else ""
            click.echo(f"{username}{marker}")
            if with_grants:
                grants = grants_for_user(conn, uid)
                if not grants:
                    click.echo("  (no grants)")
                    continue
                for _aid, name, granted_at in grants:
                    click.echo(f"  {name} (granted {granted_at.date()})")


@main.command("grant-account")
@click.argument("username")
@click.argument("account_name")
@click.pass_context
def grant_account_cmd(ctx: click.Context, username: str, account_name: str) -> None:
    """Grant USERNAME read access to ACCOUNT_NAME. Idempotent."""
    from localmail.api.acl import (
        grant_account,
        resolve_account_id_by_name,
        resolve_user_id_by_username,
        user_has_account,
    )
    with psycopg.connect(_dsn_from_ctx(ctx)) as conn:
        uid = resolve_user_id_by_username(conn, username)
        if uid is None:
            raise click.ClickException(f"no such user: {username!r}")
        aid = resolve_account_id_by_name(conn, account_name)
        if aid is None:
            raise click.ClickException(f"no such account: {account_name!r}")
        already = user_has_account(conn, uid, aid)
        grant_account(conn, uid, aid)
        conn.commit()
    if already:
        click.echo(
            f"user {username!r} already had access to account {account_name!r} "
            f"(id={aid}); no change"
        )
    else:
        click.echo(
            f"granted user {username!r} access to account {account_name!r} (id={aid})"
        )


@main.command("revoke-account")
@click.argument("username")
@click.argument("account_name")
@click.pass_context
def revoke_account_cmd(ctx: click.Context, username: str, account_name: str) -> None:
    """Revoke USERNAME's access to ACCOUNT_NAME."""
    from localmail.api.acl import (
        resolve_account_id_by_name,
        resolve_user_id_by_username,
        revoke_account,
    )
    with psycopg.connect(_dsn_from_ctx(ctx)) as conn:
        uid = resolve_user_id_by_username(conn, username)
        if uid is None:
            raise click.ClickException(f"no such user: {username!r}")
        aid = resolve_account_id_by_name(conn, account_name)
        if aid is None:
            raise click.ClickException(f"no such account: {account_name!r}")
        affected = revoke_account(conn, uid, aid)
        conn.commit()
    if affected == 0:
        click.echo(
            f"user {username!r} did not have access to account {account_name!r} "
            f"(id={aid}); no change"
        )
    else:
        click.echo(
            f"revoked user {username!r} access to account {account_name!r} (id={aid})"
        )


@main.command("rotate-tls")
@click.option("--cert", "cert_path", required=True, type=click.Path(path_type=Path))
@click.option("--key", "key_path", required=True, type=click.Path(path_type=Path))
@click.option("--hostname", default="localhost", show_default=True)
@click.option("--force", is_flag=True, default=False,
              help="Overwrite existing cert/key without prompting.")
def rotate_tls(cert_path: Path, key_path: Path, hostname: str, force: bool) -> None:
    """Generate (or regenerate with --force) a self-signed TLS cert + key."""
    from localmail.serve.tls import (
        cert_fingerprint_sha256_hex,
        ensure_self_signed_cert,
        rotate_self_signed_cert,
    )
    if force and (cert_path.exists() or key_path.exists()):
        rotate_self_signed_cert(
            cert_path=cert_path, key_path=key_path, hostname=hostname,
        )
    else:
        ensure_self_signed_cert(
            cert_path=cert_path, key_path=key_path, hostname=hostname,
        )
    fp = cert_fingerprint_sha256_hex(cert_path=cert_path)
    click.echo(f"cert: {cert_path}")
    click.echo(f"key:  {key_path}")
    click.echo(f"sha256 fingerprint: {fp}")


@main.command("serve")
@click.option("--bind", default="127.0.0.1", show_default=True,
              help="Interface to bind. Use 0.0.0.0 to expose to the network.")
@click.option("--port", default=8443, type=int, show_default=True)
@click.option("--tls-cert", "tls_cert", default=None, type=click.Path(path_type=Path))
@click.option("--tls-key", "tls_key", default=None, type=click.Path(path_type=Path))
@click.option("--no-tls", is_flag=True, default=False,
              help="Disable TLS. Only valid when --bind is 127.0.0.1.")
@click.pass_context
def serve_cmd(
    ctx: click.Context,
    bind: str,
    port: int,
    tls_cert: Path | None,
    tls_key: Path | None,
    no_tls: bool,
) -> None:
    """Run the HTTPS API server."""
    import uvicorn
    from localmail.db import pending_migrations
    from localmail.serve.app import create_app
    from localmail.serve.tls import ensure_self_signed_cert

    if no_tls and not _is_loopback_bind(bind):
        raise click.ClickException(
            "--no-tls is only valid when --bind resolves to a loopback address"
        )

    from localmail.config import AuthConfig, DaemonConfig, McpConfig, ServeConfig
    override = os.environ.get("LOCALMAIL_DSN_OVERRIDE")
    if override:
        cfg = None
        dsn = override
        serve_cfg = ServeConfig()
        auth_cfg = AuthConfig()
        daemon_cfg = DaemonConfig()
        mcp_cfg = McpConfig()
        gmail_secrets = None
        imports_cfg = None
        attachments_root = None
    else:
        cfg = load_config(ctx.obj["config_path"])
        dsn = cfg.database.dsn
        serve_cfg = cfg.serve
        auth_cfg = cfg.auth
        daemon_cfg = cfg.daemon
        mcp_cfg = cfg.mcp
        gmail_secrets = (
            cfg.gmail_oauth.client_secrets_file if cfg.gmail_oauth else None
        )
        imports_cfg = cfg.imports
        attachments_root = cfg.attachments.root

    try:
        pending = pending_migrations(dsn)
    except Exception as exc:
        raise click.ClickException(
            f"could not check schema: {exc}. Is Postgres reachable?"
        ) from exc
    if pending:
        raise click.ClickException(
            "database is missing migrations: "
            + ", ".join(pending)
            + ". Run `localmail init-db` first."
        )

    try:
        from localmail.search import create_searcher
        # Pin the searcher to the same DSN the rest of serve uses. In the
        # LOCALMAIL_DSN_OVERRIDE branch `cfg` is None (create_searcher loads the
        # default config for search tunables) but `dsn` is the override, so the
        # searcher can never query a different database than serve itself.
        searcher = create_searcher(cfg, dsn=dsn)
    except Exception as exc:
        click.echo(f"warning: search disabled ({exc})", err=True)
        searcher = None

    if searcher is not None and searcher._reranker is not None:
        _warm_reranker_in_background(searcher._reranker)

    app = create_app(
        db_dsn=dsn,
        searcher=searcher,
        serve_config=serve_cfg,
        auth_config=auth_cfg,
        gmail_client_secrets_file=gmail_secrets,
        daemon_config=daemon_cfg,
        daemon_config_path=ctx.obj["config_path"],
        enable_control_socket=serve_cfg.supervise_daemon,
        imports_config=imports_cfg,
        attachments_root=attachments_root,
        enable_mcp=mcp_cfg.enabled,
        mcp_config=mcp_cfg,
    )

    if no_tls:
        click.echo(f"serving HTTP on {bind}:{port}", err=True)
        uvicorn.run(app, host=bind, port=port, log_level="info")
        return

    cert_path = tls_cert or Path.home() / ".config" / "localmail" / "tls" / "cert.pem"
    key_path = tls_key or Path.home() / ".config" / "localmail" / "tls" / "key.pem"
    if tls_cert is None and not _is_loopback_bind(bind):
        click.echo(
            f"warning: --bind {bind} accepts non-loopback traffic but no --tls-cert "
            "was given; using a self-signed cert. Clients must pin its fingerprint "
            "(see `rotate-tls --force` and the printed sha256).",
            err=True,
        )
    ensure_self_signed_cert(
        cert_path=cert_path, key_path=key_path,
        hostname=bind if bind != "0.0.0.0" else "localhost",
    )
    click.echo(f"serving HTTPS on {bind}:{port}", err=True)
    click.echo(f"cert: {cert_path}", err=True)
    uvicorn.run(
        app, host=bind, port=port, log_level="info",
        ssl_certfile=str(cert_path), ssl_keyfile=str(key_path),
    )


@main.command("list-failed-extractions")
@click.option("--limit", type=int, default=50)
@click.option(
    "--format", "fmt",
    type=click.Choice(["text", "json"]), default="text",
)
def list_failed_extractions(limit: int, fmt: str) -> None:
    """Show recent failed_extractions rows.

    Each row represents a blob for which text extraction failed.  Use
    ``retry-failed-extractions`` to clear rows so the worker re-attempts them.
    """
    from localmail.db import open_pool
    pool = open_pool(_dsn())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT encode(sha256,'hex'), extractor, error_class, "
                "error_message, retry_count, failed_at, last_retry_at "
                "FROM failed_extractions "
                "ORDER BY failed_at DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
    finally:
        pool.close()
    cols = ["sha256_hex", "extractor", "error_class", "error_message",
            "retry_count", "failed_at", "last_retry_at"]
    payload = [dict(zip(cols, r, strict=True)) for r in rows]
    if fmt == "json":
        click.echo(_json.dumps(payload, default=str))
    else:
        for p in payload:
            click.echo(
                f"{p['sha256_hex'][:12]}  {p['extractor']}  "
                f"{p['error_class']}  retries={p['retry_count']}  "
                f"{p['failed_at']}"
            )
            click.echo(f"    {p['error_message']}")


@main.command("retry-failed-extractions")
@click.option(
    "--sha256", "sha256_hex", default=None,
    help="Restrict to one blob (full hex sha256); clears all rows when omitted.",
)
def retry_failed_extractions(sha256_hex: str | None) -> None:
    """Clear failed-extraction state so the extract worker re-attempts blobs.

    Removes both ``failed_extractions`` (poison-pill) rows and
    ``transient_extractions`` (stuck-transient, #153) counter rows so a blob
    that exhausted either budget becomes eligible again — e.g. after fixing a
    parser bug or a misconfigured HF token. Without ``--sha256`` every row in
    both tables is removed; with ``--sha256 HEX`` only the single matching blob.
    """
    from localmail.db import open_pool
    pool = open_pool(_dsn())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            if sha256_hex:
                cur.execute(
                    "DELETE FROM failed_extractions "
                    "WHERE sha256 = decode(%s,'hex')",
                    (sha256_hex,),
                )
                n = cur.rowcount
                cur.execute(
                    "DELETE FROM transient_extractions "
                    "WHERE sha256 = decode(%s,'hex')",
                    (sha256_hex,),
                )
                n_transient = cur.rowcount
            else:
                cur.execute("DELETE FROM failed_extractions")
                n = cur.rowcount
                cur.execute("DELETE FROM transient_extractions")
                n_transient = cur.rowcount
        conn.commit()
    finally:
        pool.close()
    click.echo(
        f"cleared {n} failed_extractions rows, "
        f"{n_transient} transient_extractions rows"
    )


@main.command("grant-admin")
@click.argument("username")
@click.pass_context
def grant_admin_cmd(ctx: click.Context, username: str) -> None:
    """Grant admin privileges to USERNAME (shell-only bootstrap path)."""
    from localmail.api.admin.auth import UserNotFound, grant_admin

    with psycopg.connect(_dsn_from_ctx(ctx)) as conn:
        try:
            grant_admin(conn, username=username)
        except UserNotFound as exc:
            raise click.ClickException(str(exc))
    click.echo(f"granted admin to {username!r}")


@main.command("revoke-admin")
@click.argument("username")
@click.pass_context
def revoke_admin_cmd(ctx: click.Context, username: str) -> None:
    """Revoke admin privileges from USERNAME."""
    from localmail.api.admin.auth import UserNotFound, revoke_admin

    with psycopg.connect(_dsn_from_ctx(ctx)) as conn:
        try:
            revoke_admin(conn, username=username)
        except UserNotFound as exc:
            raise click.ClickException(str(exc))
    click.echo(f"revoked admin from {username!r}")


@main.command("revoke-admin-sessions")
@click.argument("username")
@click.pass_context
def revoke_admin_sessions_cmd(ctx: click.Context, username: str) -> None:
    """Invalidate every outstanding credential for USERNAME.

    Bumps `api_users.sessions_invalidated_at` to now(), so any credential
    minted before this moment stops working: admin cookies (redirected to
    /admin/login), bearer tokens on every /v1/* endpoint and /mcp — which
    signs USERNAME out of the desktop GUI — and OAuth refresh tokens, so an
    agent cannot mint a replacement access token. USERNAME simply logs in
    again to get working credentials.

    Admin privileges themselves are unaffected — use `revoke-admin` for that.
    """
    from localmail.api.admin.auth import UserNotFound, revoke_admin_sessions

    with psycopg.connect(_dsn_from_ctx(ctx)) as conn:
        try:
            revoke_admin_sessions(conn, username=username)
        except UserNotFound as exc:
            raise click.ClickException(str(exc))
    click.echo(f"revoked outstanding sessions for {username!r}")


if __name__ == "__main__":
    main()
