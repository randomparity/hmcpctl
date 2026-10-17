"""Tests for hmcpctl config init/list/show/init-access-policy commands.

The first three are issue #125. `init-access-policy` is issue #225; it covers
docs/workflow/specs/2026-08-19-fail-closed-startup-design.md.

Spec item -> node id:
  R11  test_init_access_policy_writes_a_loadable_policy_at_0600
  R11  test_init_access_policy_refuses_to_overwrite_and_names_the_remedy
  R11  test_init_access_policy_reports_an_unresolvable_config_home
  R11a test_init_access_policy_output_redirects_the_write
  R9b  test_init_access_policy_refuses_a_key_that_cannot_be_a_connection
  R11  test_a_write_failure_after_the_create_leaves_no_partial_file
  287  test_init_access_policy_output_collision_names_a_different_remedy
  287  test_init_access_policy_output_at_the_default_path_uses_output_case

`config_dir()` is steered by patching `sys.platform` to "linux" and setting
XDG_CONFIG_HOME, which is this module's established idiom: on darwin `config_dir()`
reads `Path.home()` with no environment override, so without it these tests would
write into the developer's own config directory.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from rendered_output import collapse_whitespace
from typer.testing import CliRunner

from hmcpctl import cli

RUNNER = CliRunner()

TWO_PROFILE_TOML = """\
default_profile = "prod"

[profiles.prod]
host = "hmc.example.com"
user = "admin"
password = "prodpass"  # pragma: allowlist secret

[profiles.dev]
host = "hmc-dev.example.com"
user = "devadmin"
password_env = "HMC_DEV_PW"  # pragma: allowlist secret
"""

NO_DEFAULT_TOML = """\
[profiles.alpha]
host = "hmc-alpha.example.com"
user = "admin"
password = "alphapw"  # pragma: allowlist secret

[profiles.beta]
host = "hmc-beta.example.com"
user = "admin"
password = "betapw"  # pragma: allowlist secret
"""


def _write_toml(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# config init
# ---------------------------------------------------------------------------


def test_init_creates_file(tmp_path, monkeypatch):
    """init creates the config file and prints the path when it does not exist."""
    target = tmp_path / "hmcpctl" / "config.toml"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["config", "init"])
    assert result.exit_code == 0, result.output
    assert target.exists()
    # Rich may wrap long paths across lines — compare against the filename
    assert "config.toml" in result.output
    assert "hmcpctl" in result.output


def test_init_refuses_existing_file(tmp_path, monkeypatch):
    """init exits 1 with an error message when the file already exists."""
    target = tmp_path / "hmcpctl" / "config.toml"
    _write_toml(
        target,
        "[profiles.x]\nhost='h'\nuser='u'\npassword='p'  # pragma: allowlist secret\n",
    )
    original_content = target.read_text()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["config", "init"])
    assert result.exit_code == 1
    assert "already exists" in result.output or "already exists" in (
        result.stderr or ""
    )
    # File must be unchanged
    assert target.read_text() == original_content


@pytest.mark.skipif(sys.platform == "win32", reason="chmod not meaningful on Windows")
def test_init_permissions(tmp_path, monkeypatch):
    """init creates the file with mode 0o600 on POSIX."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["config", "init"])
    assert result.exit_code == 0, result.output
    target = tmp_path / "hmcpctl" / "config.toml"
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"


# ---------------------------------------------------------------------------
# config list
# ---------------------------------------------------------------------------


def test_list_shows_profiles_and_default(tmp_path, monkeypatch):
    """list shows both profile names; the default is marked."""
    _write_toml(tmp_path / "hmcpctl" / "config.toml", TWO_PROFILE_TOML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["config", "list"])
    assert result.exit_code == 0, result.output
    assert "prod" in result.output
    assert "dev" in result.output
    assert "(default)" in result.output
    # Only prod should be marked default
    lines = result.output.splitlines()
    default_lines = [ln for ln in lines if "(default)" in ln]
    assert len(default_lines) == 1
    assert "prod" in default_lines[0]


def test_list_no_default_key(tmp_path, monkeypatch):
    """list shows names without any (default) marker when default_profile absent."""
    _write_toml(tmp_path / "hmcpctl" / "config.toml", NO_DEFAULT_TOML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["config", "list"])
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "beta" in result.output
    assert "(default)" not in result.output


def test_list_absent_file(tmp_path, monkeypatch):
    """list exits 0 with a helpful message when no config file exists."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["config", "list"])
    assert result.exit_code == 0
    output_lower = result.output.lower()
    assert "no config file" in output_lower or "config file" in output_lower


# ---------------------------------------------------------------------------
# config show
# ---------------------------------------------------------------------------


def test_show_password_redacted(tmp_path, monkeypatch):
    """show emits password_configured:True but never the literal password."""
    _write_toml(tmp_path / "hmcpctl" / "config.toml", TWO_PROFILE_TOML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["--profile", "prod", "config", "show"])
    assert result.exit_code == 0, result.output
    assert "prodpass" not in result.output
    assert "password_configured" in result.output
    assert "True" in result.output or "true" in result.output


def test_show_password_env_not_resolved(tmp_path, monkeypatch):
    """show reports password_configured:True for password_env without resolving the var."""
    _write_toml(tmp_path / "hmcpctl" / "config.toml", TWO_PROFILE_TOML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    # Deliberately DO NOT set HMC_DEV_PW — show must not resolve it.
    # But load_profile() WILL try to resolve it for the HMCConfig fields.
    # So we set a dummy value: this tests that show reports the boolean,
    # not that it avoids calling load_profile entirely.
    monkeypatch.setenv("HMC_DEV_PW", "dummy-value-for-test")  # pragma: allowlist secret
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["--profile", "dev", "config", "show"])
    assert result.exit_code == 0, result.output
    assert "password_configured" in result.output
    assert "True" in result.output or "true" in result.output
    # Must not show the actual env var value or the password_env var name as a password
    assert "dummy-value-for-test" not in result.output  # pragma: allowlist secret


def test_show_json_flag(tmp_path, monkeypatch):
    """show --json emits valid JSON with no password key."""
    _write_toml(tmp_path / "hmcpctl" / "config.toml", TWO_PROFILE_TOML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(
            cli.app, ["--profile", "prod", "config", "show", "--json"]
        )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "password" not in data
    assert "password_configured" in data
    assert data["password_configured"] is True
    assert data["host"] == "hmc.example.com"


def test_show_reports_the_power_ownership_guard(tmp_path, monkeypatch):
    """A fail-open authorization control must have an observable value (#371).

    A mistyped profile key or environment variable is dropped silently, and the
    result is indistinguishable from a correct ``false``, so ``config show`` is
    the only way an operator can confirm the guard is actually on.
    """
    _write_toml(tmp_path / "hmcpctl" / "config.toml", TWO_PROFILE_TOML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    monkeypatch.delenv("HMC_AUTHORIZE_POWER_OPERATIONS", raising=False)

    with patch.object(sys, "platform", "linux"):
        default = RUNNER.invoke(
            cli.app, ["--profile", "prod", "config", "show", "--json"]
        )
    assert default.exit_code == 0, default.output
    assert json.loads(default.output)["authorize_power_operations"] is False

    monkeypatch.setenv("HMC_AUTHORIZE_POWER_OPERATIONS", "true")
    with patch.object(sys, "platform", "linux"):
        enabled = RUNNER.invoke(
            cli.app, ["--profile", "prod", "config", "show", "--json"]
        )
    assert enabled.exit_code == 0, enabled.output
    assert json.loads(enabled.output)["authorize_power_operations"] is True


def test_show_unknown_profile_error(tmp_path, monkeypatch):
    """show exits 1 with a message containing the unknown profile name."""
    _write_toml(tmp_path / "hmcpctl" / "config.toml", TWO_PROFILE_TOML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["--profile", "nonexistent", "config", "show"])
    assert result.exit_code == 1
    assert "nonexistent" in result.output


def test_show_absent_config_file_error(tmp_path, monkeypatch):
    """show exits 1 with a helpful message when no config file exists."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["--profile", "prod", "config", "show"])
    assert result.exit_code == 1


def test_show_unreadable_config_file_error(tmp_path, monkeypatch):
    """An unreadable config.toml exits 1 with a message, not a traceback (#257)."""
    cfg = _write_toml(tmp_path / "hmcpctl" / "config.toml", TWO_PROFILE_TOML)
    cfg.chmod(0o000)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    try:
        with patch.object(sys, "platform", "linux"):
            result = RUNNER.invoke(cli.app, ["--profile", "prod", "config", "show"])
    finally:
        cfg.chmod(0o600)
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "cannot be read" in collapse_whitespace(result.output)


def test_show_non_table_profiles_key_error(tmp_path, monkeypatch):
    """`profiles = "x"` exits 1 instead of an AttributeError traceback (#257)."""
    _write_toml(tmp_path / "hmcpctl" / "config.toml", "profiles = 'not-a-table'\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["--profile", "prod", "config", "show"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "'profiles' must be a" in collapse_whitespace(result.output)


def test_list_unreadable_config_file_error(tmp_path, monkeypatch):
    """`config list` reports an unreadable file rather than raising (#257)."""
    cfg = _write_toml(tmp_path / "hmcpctl" / "config.toml", TWO_PROFILE_TOML)
    cfg.chmod(0o000)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    try:
        with patch.object(sys, "platform", "linux"):
            result = RUNNER.invoke(cli.app, ["config", "list"])
    finally:
        cfg.chmod(0o600)
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "cannot be read" in collapse_whitespace(result.output)


def test_list_non_table_profiles_key_error(tmp_path, monkeypatch):
    """`profiles = "x"` exits 1 instead of an AttributeError traceback (#257, #300).

    `config list` now derives `names` from `_coerce_profiles` directly rather
    than through `list_profiles_with_default` (#300); this pins that the
    error handling PR #294 unified survives that change.
    """
    _write_toml(tmp_path / "hmcpctl" / "config.toml", "profiles = 'not-a-table'\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["config", "list"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "'profiles' must be a" in collapse_whitespace(result.output)


def test_list_non_table_nicknames_key_error(tmp_path, monkeypatch):
    """`nicknames = "x"` exits 1 instead of an AttributeError traceback (#300).

    `config list` now derives `nicknames` from `_coerce_nicknames` directly
    rather than through `list_nicknames` (#300); this pins that the error
    handling PR #294 unified survives that change.
    """
    _write_toml(
        tmp_path / "hmcpctl" / "config.toml",
        "nicknames = 'not-a-table'\n\n[profiles.prod]\nhost='h'\nuser='u'\n"
        "password='p'  # pragma: allowlist secret\n",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["config", "list"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "'nicknames' must be a" in collapse_whitespace(result.output)


def test_show_no_profile_no_default_error(tmp_path, monkeypatch):
    """show exits 1 when no --profile, no HMC_PROFILE, no default_profile in TOML."""
    _write_toml(tmp_path / "hmcpctl" / "config.toml", NO_DEFAULT_TOML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    monkeypatch.delenv("HMC_HOST", raising=False)
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["config", "show"])
    assert result.exit_code == 1


def test_show_local_profile_flag_takes_precedence(tmp_path, monkeypatch):
    """show --profile (subcommand arg) selects the profile, not global --profile."""
    _write_toml(tmp_path / "hmcpctl" / "config.toml", TWO_PROFILE_TOML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    # dev profile uses password_env — set a dummy value so load_profile succeeds
    monkeypatch.setenv("HMC_DEV_PW", "dummy-for-test")  # pragma: allowlist secret
    with patch.object(sys, "platform", "linux"):
        # Global --profile=prod but subcommand --profile=dev should win
        result = RUNNER.invoke(
            cli.app, ["--profile", "prod", "config", "show", "--profile", "dev"]
        )
    assert result.exit_code == 0, result.output
    assert "hmc-dev.example.com" in result.output


def test_error_message_on_failure(tmp_path, monkeypatch):
    """Config command errors produce exit code 1 and an error message."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["--profile", "x", "config", "show"])
    assert result.exit_code == 1
    # Error message should mention Error and the profile or missing file
    assert "Error" in result.output or "error" in result.output.lower()


# ---------------------------------------------------------------------------
# Nickname surfacing (issue #226)
# ---------------------------------------------------------------------------

NICKNAME_TOML = """\
default_profile = "prod"

[profiles.prod]
host = "prod-hmc.example.com"
user = "admin"
password_env = "HMC_PROD_PW"     # pragma: allowlist secret

[profiles.stg]
host = "stg-hmc.example.com"
user = "admin"
password = "stgpass"     # pragma: allowlist secret

[nicknames]
big-iron = "prod"
staging = "stg"
ghost = "does-not-exist"
"""


def test_list_surfaces_nicknames(tmp_path, monkeypatch):
    """config list prints each nickname as 'nick -> target'."""
    _write_toml(tmp_path / "hmcpctl" / "config.toml", NICKNAME_TOML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["config", "list"])
    assert result.exit_code == 0, result.output
    assert "big-iron -> prod" in result.output
    assert "staging -> stg" in result.output


def test_list_flags_dangling_nickname_target(tmp_path, monkeypatch):
    """A nickname whose target is not a profile is flagged in config list."""
    _write_toml(tmp_path / "hmcpctl" / "config.toml", NICKNAME_TOML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["config", "list"])
    assert result.exit_code == 0, result.output
    line = next(ln for ln in result.output.splitlines() if "ghost" in ln)
    assert "(no such profile)" in line


def test_show_resolves_nickname(tmp_path, monkeypatch):
    """config show <nick> resolves the nickname and reports resolved_from."""
    _write_toml(tmp_path / "hmcpctl" / "config.toml", NICKNAME_TOML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    monkeypatch.setenv(
        "HMC_PROD_PW", "dummy-value-for-test"
    )  # pragma: allowlist secret
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(
            cli.app, ["--profile", "big-iron", "config", "show", "--json"]
        )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["profile"] == "prod"
    assert data["resolved_from"] == "big-iron"
    assert data["host"] == "prod-hmc.example.com"
    assert "dummy-value-for-test" not in result.output  # pragma: allowlist secret


def test_show_profile_key_has_null_resolved_from(tmp_path, monkeypatch):
    """A direct profile key reports resolved_from as null."""
    _write_toml(tmp_path / "hmcpctl" / "config.toml", NICKNAME_TOML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    monkeypatch.setenv(
        "HMC_PROD_PW", "dummy-value-for-test"
    )  # pragma: allowlist secret
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(
            cli.app, ["--profile", "prod", "config", "show", "--json"]
        )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["profile"] == "prod"
    assert data["resolved_from"] is None


def test_init_scaffolds_commented_nicknames(tmp_path, monkeypatch):
    """config init writes a commented nicknames example to the starter file."""
    target = tmp_path / "hmcpctl" / "config.toml"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["config", "init"])
    assert result.exit_code == 0, result.output
    content = target.read_text(encoding="utf-8")
    assert "nicknames" in content
    assert "# nicknames = " in content


def test_show_env_nickname_target_differs_from_default(tmp_path, monkeypatch):
    """HMC_PROFILE nickname resolves to its target profile, not the default."""
    _write_toml(tmp_path / "hmcpctl" / "config.toml", NICKNAME_TOML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HMC_PROFILE", "staging")
    monkeypatch.delenv("HMC_PROD_PW", raising=False)
    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, ["config", "show", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["profile"] == "stg"
    assert data["resolved_from"] == "staging"
    assert data["host"] == "stg-hmc.example.com"
    assert "prod-hmc.example.com" not in result.output


# ---------------------------------------------------------------------------
# Read count (issue #295) — one parse of config.toml per invocation
# ---------------------------------------------------------------------------


def test_show_reads_config_document_exactly_once(tmp_path, monkeypatch):
    """config show parses config.toml once, not three times (#295).

    Patches the shared choke point `_read_config_document` in both the module
    that owns it (`hmcpctl.config`, where `list_nicknames`/`load_profile`
    resolve the name as a module global at call time) and
    `hmcpctl.cli_config`'s own imported name (its direct call site), so every
    read reaches the same counter regardless of which call site makes it.
    """
    from unittest.mock import MagicMock

    import hmcpctl.config as config_mod

    _write_toml(tmp_path / "hmcpctl" / "config.toml", TWO_PROFILE_TOML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("HMC_PROFILE", raising=False)

    counter = MagicMock(wraps=config_mod._read_config_document)
    with (
        patch.object(config_mod, "_read_config_document", counter),
        patch.object(sys, "platform", "linux"),
    ):
        result = RUNNER.invoke(cli.app, ["--profile", "prod", "config", "show"])

    assert result.exit_code == 0, result.output
    assert counter.call_count == 1


def test_list_reads_config_document_exactly_once(tmp_path, monkeypatch):
    """config list parses config.toml once, not twice (#300).

    Same technique as test_show_reads_config_document_exactly_once: patch the
    shared choke point `_read_config_document` in both the owning module and
    `hmcpctl.cli_config`'s own imported name, so a read from either call site
    reaches the same counter. `config list` calls no other module-level
    reader (no `load_profile`), so both reads previously came from
    `cli_config`'s own call sites — patching the module-owned name alone
    would not have observed the second one.
    """
    from unittest.mock import MagicMock

    import hmcpctl.config as config_mod

    _write_toml(tmp_path / "hmcpctl" / "config.toml", NICKNAME_TOML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("HMC_PROFILE", raising=False)

    counter = MagicMock(wraps=config_mod._read_config_document)
    with (
        patch.object(config_mod, "_read_config_document", counter),
        patch.object(sys, "platform", "linux"),
    ):
        result = RUNNER.invoke(cli.app, ["config", "list"])

    assert result.exit_code == 0, result.output
    assert counter.call_count == 1


# ---------------------------------------------------------------------------
# config init-access-policy (issue #225)
# ---------------------------------------------------------------------------

POLICY_ARGV = ["config", "init-access-policy"]


def _generate(tmp_path, monkeypatch, *extra):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with patch.object(sys, "platform", "linux"):
        return RUNNER.invoke(cli.app, [*POLICY_ARGV, *extra])


def test_init_access_policy_writes_a_loadable_policy_at_0600(tmp_path, monkeypatch):
    """R11: the file a server has to read, created the way `config init` creates one."""
    from hmcpctl.authorization.access_policy import load_access_policy
    from hmcpctl.cli_commands.legacy_policy import LEGACY_POLICY_NAME
    from hmcpctl.server import TOOL_SECURITY

    result = _generate(tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    target = tmp_path / "hmcpctl" / "access-policy.toml"
    assert target.exists()
    assert "access-policy.toml" in result.output

    # Loaded through the real loader, not re-parsed here: the claim is that a server
    # could start on this file.
    policy = load_access_policy(LEGACY_POLICY_NAME, TOOL_SECURITY, path=target)
    assert policy.tools == frozenset(set(TOOL_SECURITY) - {"hmc_run_command"})

    if sys.platform != "win32":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_init_access_policy_refuses_to_overwrite_and_names_the_remedy(
    tmp_path, monkeypatch
):
    """R11: it cannot destroy a reviewed policy, and it says what to do instead.

    R5's refusal points every operator here unconditionally — nothing at that point
    distinguishes an upgrade from a first run — so the deployment that already has an
    authored-but-unselected file arrives at this error, and a bare "already exists"
    would be a dead end.
    """
    first = _generate(tmp_path, monkeypatch)
    assert first.exit_code == 0, first.output
    target = tmp_path / "hmcpctl" / "access-policy.toml"
    before = target.read_bytes()

    second = _generate(tmp_path, monkeypatch)

    assert second.exit_code == 1
    assert target.read_bytes() == before
    assert "--output" in second.output


def test_init_access_policy_output_collision_names_a_different_remedy(
    tmp_path, monkeypatch
):
    """#287: a scratch --output collision is not the default-path failure.

    The operator already used --output once; telling them to "write to a scratch path
    with --output PATH" again is circular — it is exactly the action that just failed.
    The message must name a remedy that differs from it (delete the file, or choose a
    different --output PATH) and must not claim the colliding file is a reviewed
    policy, since it may just be a stale scratch file from an earlier attempt.
    """
    scratch = tmp_path / "scratch" / "new-policy.toml"

    first = _generate(tmp_path, monkeypatch, "--output", str(scratch))
    assert first.exit_code == 0, first.output
    before = scratch.read_bytes()

    second = _generate(tmp_path, monkeypatch, "--output", str(scratch))
    # rich hard-wraps a non-tty at 80 columns, so a long path may carry an inserted
    # newline; flatten before checking for path substrings.
    flattened = second.output.replace("\n", "")

    assert second.exit_code == 1
    assert scratch.read_bytes() == before
    assert str(scratch) in flattened
    assert "write to a scratch path with --output PATH" not in flattened
    assert "reviewed policy" not in flattened


def test_init_access_policy_output_at_the_default_path_uses_output_case(
    tmp_path, monkeypatch
):
    """#287: the discriminator is whether --output was passed, not the resolved path.

    An operator can point --output directly at the platform-native path. Comparing
    the resolved target against the platform default would misclassify this as the
    "reviewed policy" case even though the operator explicitly named the path with
    --output; the handler must key off the presence of --output itself.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    default_target = tmp_path / "hmcpctl" / "access-policy.toml"

    with patch.object(sys, "platform", "linux"):
        first = RUNNER.invoke(cli.app, [*POLICY_ARGV, "--output", str(default_target)])
        assert first.exit_code == 0, first.output
        second = RUNNER.invoke(cli.app, [*POLICY_ARGV, "--output", str(default_target)])

    flattened = second.output.replace("\n", "")
    assert second.exit_code == 1
    assert "write to a scratch path with --output PATH" not in flattened
    assert "reviewed policy" not in flattened


def test_init_access_policy_output_redirects_the_write(tmp_path, monkeypatch):
    """R11a: the only way to regenerate, since the command cannot overwrite."""
    scratch = tmp_path / "scratch" / "new-policy.toml"

    result = _generate(tmp_path, monkeypatch, "--output", str(scratch))

    assert result.exit_code == 0, result.output
    assert scratch.exists()
    assert not (tmp_path / "hmcpctl" / "access-policy.toml").exists()
    if sys.platform != "win32":
        assert stat.S_IMODE(scratch.stat().st_mode) == 0o600


def test_init_access_policy_refuses_a_key_that_cannot_be_a_connection(
    tmp_path, monkeypatch
):
    """R9b: escaping makes it parse; ADR 0036's entry rules still have to pass.

    `[profiles." prod"]` is legal TOML that `load_profile` resolves today, so this is a
    working deployment. The generation must fail before any file exists rather than
    leave one that refuses to load, and the message must reach past the policy document
    the operator never wrote to the config key they did.
    """
    config_dir = tmp_path / "hmcpctl"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        '[profiles." prod"]\nhost = "a"\n', encoding="utf-8"
    )

    result = _generate(tmp_path, monkeypatch)

    assert result.exit_code == 1
    assert not (config_dir / "access-policy.toml").exists()
    output = result.output
    assert "config.toml" in output
    assert "padded" in output


def test_init_access_policy_reports_an_unresolvable_config_home(tmp_path, monkeypatch):
    """R11: the generator resolves the same path `serve` does, under the same guard."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)

    def _no_home(cls):
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr(Path, "home", classmethod(_no_home))

    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, POLICY_ARGV)

    assert result.exit_code == 1
    assert "HOME" in result.output or "XDG_CONFIG_HOME" in result.output


@pytest.mark.skipif(sys.platform == "win32", reason="exercises the POSIX fdopen path")
def test_a_write_failure_after_the_create_leaves_no_partial_file(tmp_path, monkeypatch):
    """R11: O_EXCL alone only covers a failure at `open`.

    ENOSPC, EDQUOT or EIO after the descriptor exists would otherwise leave a truncated
    file — which exists, so the command's own no-overwrite rule refuses to regenerate
    over it, and does not compile, so `serve` refuses too. That is a deployment that can
    neither start nor recover without a manual delete.
    """
    import hmcpctl.cli_commands.config as cli_config

    def _explode(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli_config.os, "fdopen", _explode)

    result = _generate(tmp_path, monkeypatch)

    assert result.exit_code == 1
    assert not (tmp_path / "hmcpctl" / "access-policy.toml").exists()


def test_every_spec_numbered_test_named_in_the_header_still_exists():
    """A header naming a deleted test is worse than no header (see #224)."""
    import re

    source = Path(__file__).read_text(encoding="utf-8")
    header = source.split('"""', 2)[1]

    named = set(re.findall(r"^  \w+\s+(test_\w+)$", header, flags=re.MULTILINE))
    defined = set(re.findall(r"^def (test_\w+)", source, flags=re.MULTILINE))

    assert named, "the header maps no test; the guard would pass vacuously"
    assert named <= defined, f"named but not defined: {sorted(named - defined)}"


# ---------------------------------------------------------------------------
# config diff-access-policy (issue #276)
# ---------------------------------------------------------------------------

DIFF_ARGV = ["config", "diff-access-policy"]


def _generate_and_deploy(tmp_path, monkeypatch, config_toml=None):
    """Generate the platform-native policy with init-access-policy; return its path."""
    if config_toml is not None:
        config_dir = tmp_path / "hmcpctl"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text(config_toml, encoding="utf-8")
    target = tmp_path / "hmcpctl" / "access-policy.toml"
    result = _generate(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    return target


def test_diff_access_policy_is_green_when_the_deployed_policy_is_current(
    tmp_path, monkeypatch
):
    """#276: exit 0, and no diff hunks, when the deployed document matches this build."""
    deployed = _generate_and_deploy(tmp_path, monkeypatch)

    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, [*DIFF_ARGV, str(deployed)])

    assert result.exit_code == 0, result.output
    assert not [
        line for line in result.output.splitlines() if line.startswith(("+", "-"))
    ], result.output


def test_diff_access_policy_shows_a_tool_a_later_release_added(tmp_path, monkeypatch):
    """#276 drift arm 1: TOOL_SECURITY grew after generation; the diff names the tool.

    Patching `server_tools.catalog.TOOL_SECURITY` works because the command imports it inside the
    handler, at call time — the same attribute `init-access-policy` renders from.
    """
    from dataclasses import replace

    from hmcpctl.server_tools import catalog

    deployed = _generate_and_deploy(tmp_path, monkeypatch)
    drifted = dict(catalog.TOOL_SECURITY)
    drifted["hmc_hypothetical_tool"] = replace(next(iter(drifted.values())))

    with patch.object(catalog, "TOOL_SECURITY", drifted):
        result = RUNNER.invoke(cli.app, [*DIFF_ARGV, str(deployed)])

    assert result.exit_code == 1, result.output
    added = [line for line in result.output.splitlines() if line.startswith("+")]
    assert any('"hmc_hypothetical_tool"' in line for line in added)


def test_diff_access_policy_shows_a_profile_added_after_generation(
    tmp_path, monkeypatch
):
    """#276 drift arm 2: a profile key config.toml gains after the policy was written."""
    deployed = _generate_and_deploy(tmp_path, monkeypatch, TWO_PROFILE_TOML)
    config_dir = tmp_path / "hmcpctl"
    (config_dir / "config.toml").write_text(
        TWO_PROFILE_TOML
        + "\n[profiles.staging]\n"
        + 'host = "staging.example.com"\n'
        + 'user = "admin"\n'
        + 'password_env = "HMC_STAGING_PW"  # pragma: allowlist secret\n',
        encoding="utf-8",
    )

    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, [*DIFF_ARGV, str(deployed)])

    assert result.exit_code == 1, result.output
    assert '"staging"' in result.output


def test_diff_access_policy_reports_an_unreadable_deployed_file(tmp_path, monkeypatch):
    """#276 error arm: a path with no policy behind it exits distinctly, naming the path."""
    missing = tmp_path / "nowhere" / "access-policy.toml"

    result = RUNNER.invoke(cli.app, [*DIFF_ARGV, str(missing)])

    assert result.exit_code == 3
    flattened = result.output.replace("\n", "")
    assert str(missing) in flattened
    assert "init-access-policy" in flattened


def test_diff_access_policy_reports_a_non_text_deployed_file(tmp_path, monkeypatch):
    """#276 error arm: bytes that are not a TOML document are refused, not diffed."""
    deployed = tmp_path / "access-policy.bin"
    deployed.write_bytes(b"\x00\xff\xfe")

    result = RUNNER.invoke(cli.app, [*DIFF_ARGV, str(deployed)])

    assert result.exit_code == 3


def test_diff_access_policy_generation_failure_beats_the_deployed_file_check(
    tmp_path, monkeypatch
):
    """#276 error arm: generation failure exits 4 even with a readable deployed file.

    A padded profile key fails rendering (R9b); the deployed document beside it is
    fine and never gets read. Distinct from the exit-3 unreadable-deployed arm above.
    """
    deployed = _generate_and_deploy(tmp_path, monkeypatch)
    config_dir = tmp_path / "hmcpctl"
    (config_dir / "config.toml").write_text(
        '[profiles." prod"]\nhost = "a"\n', encoding="utf-8"
    )

    with patch.object(sys, "platform", "linux"):
        result = RUNNER.invoke(cli.app, [*DIFF_ARGV, str(deployed)])

    assert result.exit_code == 4
    assert "padded" in result.output
