import psycopg
import pytest
from conftest import EXAMPLE_SOURCES, ROLE_PASSWORDS
from psycopg.conninfo import make_conninfo
from typer.testing import CliRunner

from ingest_api.admin_cli import app
from ingest_api.tokens import hash_token, lookup_token

runner = CliRunner()


@pytest.fixture
def cli_env(test_dsn, monkeypatch):
    monkeypatch.setenv("STORE_ADMIN_DSN", test_dsn)
    monkeypatch.setenv("STORE_INGEST_DSN", test_dsn)
    monkeypatch.setenv("SOURCES_FILE", str(EXAMPLE_SOURCES))
    for key, value in ROLE_PASSWORDS.items():
        monkeypatch.setenv(key, value)
    return test_dsn


@pytest.mark.integration
def test_token_create_prints_only_token(cli_env):
    assert runner.invoke(app, ["bootstrap"]).exit_code == 0
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
    runner.invoke(app, ["bootstrap"])
    created = runner.invoke(
        app, ["token", "create", "--device", "pc1", "--source", "usage", "--note", "laptop"]
    )
    token = created.stdout.strip()
    listing = runner.invoke(app, ["token", "list"])
    assert listing.exit_code == 0
    assert "pc1" in listing.stdout
    assert "laptop" in listing.stdout
    assert token not in listing.stdout
    assert hash_token(token) not in listing.stdout
    assert runner.invoke(app, ["token", "revoke", "1"]).exit_code == 0
    assert runner.invoke(app, ["token", "revoke", "1"]).exit_code == 1
    with psycopg.connect(cli_env) as conn:
        assert lookup_token(conn, token) is None


@pytest.mark.integration
def test_token_create_rejects_unknown_source(cli_env):
    runner.invoke(app, ["bootstrap"])
    result = runner.invoke(app, ["token", "create", "--device", "pc1", "--source", "nope"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "nope" in result.stderr
    with psycopg.connect(cli_env) as conn:
        assert conn.execute("SELECT count(*) FROM store.device_tokens").fetchone()[0] == 0


@pytest.mark.integration
def test_bootstrap_command_creates_roles_and_schema(cli_env):
    result = runner.invoke(app, ["bootstrap"])
    assert result.exit_code == 0, result.output
    assert "0001_store" in result.stderr
    with psycopg.connect(cli_env) as conn:
        roles = {r[0] for r in conn.execute("SELECT rolname FROM pg_roles")}
        assert {"ingest", "usage_owner", "bank_reader", "sms_owner"} <= roles
        assert conn.execute("SELECT count(*) FROM store.sources").fetchone()[0] == 4
    again = runner.invoke(app, ["bootstrap"])
    assert again.exit_code == 0
    assert "none pending" in again.stderr


@pytest.mark.integration
def test_bootstrap_fails_without_password(cli_env, monkeypatch):
    monkeypatch.delenv("SMS_READER_PASSWORD")
    result = runner.invoke(app, ["bootstrap"])
    assert result.exit_code == 1
    assert "SMS_READER_PASSWORD" in result.stderr


def test_bootstrap_requires_admin_dsn(monkeypatch):
    monkeypatch.delenv("STORE_ADMIN_DSN", raising=False)
    assert runner.invoke(app, ["bootstrap"]).exit_code == 2


@pytest.mark.integration
def test_token_cli_works_with_ingest_dsn_only(cli_env, monkeypatch):
    assert runner.invoke(app, ["bootstrap"]).exit_code == 0
    ingest_dsn = make_conninfo(cli_env, user="ingest", password="pw-ingest")
    monkeypatch.delenv("STORE_ADMIN_DSN")
    monkeypatch.setenv("STORE_INGEST_DSN", ingest_dsn)
    created = runner.invoke(app, ["token", "create", "--device", "pc1", "--source", "usage"])
    assert created.exit_code == 0, created.output
    token = created.stdout.strip()
    listing = runner.invoke(app, ["token", "list"])
    assert listing.exit_code == 0 and "pc1" in listing.stdout
    assert runner.invoke(app, ["token", "revoke", "1"]).exit_code == 0
    with psycopg.connect(ingest_dsn) as conn:
        assert lookup_token(conn, token) is None
