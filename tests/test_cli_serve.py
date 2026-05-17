from click.testing import CliRunner

from localmail.cli import main


def test_serve_help() -> None:
    runner = CliRunner()
    r = runner.invoke(main, ["serve", "--help"])
    assert r.exit_code == 0
    assert "--bind" in r.output
    assert "--port" in r.output
    assert "--tls-cert" in r.output
    assert "--no-tls" in r.output
