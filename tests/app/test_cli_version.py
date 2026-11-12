"""``hmcpctl --version`` reports the installed package version offline (#1950)."""

from __future__ import annotations

from importlib.metadata import version

import pytest
from click import unstyle
from typer.testing import CliRunner

import hmcpctl
from hmcpctl.cli import app
from hmcpctl.cli_commands import runtime

runner = CliRunner()

_HMC_VARS = (
    "HMC_HOST",
    "HMC_USER",
    "HMC_PASSWORD",
    "HMC_PROFILE",
    "HMC_VERIFY_SSL",
    "HMC_TRUST_SYSTEM_STORE",
    "HMC_CA_BUNDLE",
    "HMC_CERT_FINGERPRINT",
)


@pytest.fixture
def bare_environment(monkeypatch, tmp_path):
    for name in _HMC_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "unreadable"))

    def forbidden(*args, **kwargs):
        pytest.fail("--version attempted an HMC call")

    monkeypatch.setattr(runtime, "client", forbidden)


def test_version_prints_installed_version_to_stdout(bare_environment):
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == f"{version('hmcpctl')}\n"
    assert result.stdout.strip() == hmcpctl.__version__


def test_version_is_eager_before_a_subcommand_and_connection_options(bare_environment):
    result = runner.invoke(
        app, ["--profile", "missing", "--version", "systems", "list"]
    )
    assert result.exit_code == 0
    assert result.stdout == f"{version('hmcpctl')}\n"


def test_help_lists_version_option(bare_environment):
    result = runner.invoke(app, ["--help"], terminal_width=120)

    assert result.exit_code == 0
    assert "--version" in unstyle(result.stdout)
