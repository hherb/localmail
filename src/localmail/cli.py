"""localmail CLI entry point."""

from __future__ import annotations

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


if __name__ == "__main__":
    main()
