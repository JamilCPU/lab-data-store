import psycopg
import pytest
from typer.testing import CliRunner

from ingest_api.admin_cli import app
from ingest_api.tokens import lookup_token

runner = CliRunner()


@pytest.fixture
def cli_env(test_dsn, monkeypatch):
    monkeypatch.setenv("STORE_ADMIN_DSN", test_dsn)
    monkeypatch.setenv("STORE_INGEST_DSN", test_dsn)
    return test_dsn


@pytest.mark.integration
def test_token_create_prints_only_token(cli_env):
    assert runner.invoke(app, ["migrate"]).exit_code == 0
    result = runner.invoke(
        app, ["token", "create", "--device", "pc1", "--source", "usage", "--source", "email"]
    )
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("lds_")
    assert "created token" in result.stderr
    with psycopg.connect(cli_env) as conn:
        info = lookup_token(conn, lines[0])
    assert info is not None
    assert (info.device, info.sources) == ("pc1", ("usage", "email"))


@pytest.mark.integration
def test_token_list_and_revoke(cli_env):
    runner.invoke(app, ["migrate"])
    created = runner.invoke(
        app, ["token", "create", "--device", "pc1", "--source", "usage", "--note", "laptop"]
    )
    token = created.stdout.strip()
    listing = runner.invoke(app, ["token", "list"])
    assert listing.exit_code == 0
    assert "pc1" in listing.stdout
    assert "laptop" in listing.stdout
    assert token not in listing.stdout
    assert runner.invoke(app, ["token", "revoke", "1"]).exit_code == 0
    assert runner.invoke(app, ["token", "revoke", "1"]).exit_code == 1
    with psycopg.connect(cli_env) as conn:
        assert lookup_token(conn, token) is None
