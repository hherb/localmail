"""CLI `localmail import` test."""
from __future__ import annotations

import mailbox as _mailbox

from click.testing import CliRunner

from localmail.cli import main
from tests import _eml


def test_cli_import_mbox(db_conn, tmp_path, cli_config):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, imap_host, "
            "imap_port, config) "
            "VALUES ('arch', 'a@b.test', 'archive', NULL, NULL, '{}') RETURNING id")
        db_conn.commit()
    p = tmp_path / "a.mbox"
    box = _mailbox.mbox(str(p))
    box.lock()
    box.add(_mailbox.mboxMessage(_eml.plain()))
    box.flush()
    box.unlock()

    runner = CliRunner()
    result = runner.invoke(
        main, ["import", str(p), "--account", "arch", "--kind", "mbox"])
    assert result.exit_code == 0, result.output
    assert "inserted" in result.output.lower()

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 1
