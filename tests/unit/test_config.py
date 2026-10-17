"""Tests for the platform-native TOML profile loader (issue #124).

All tests use tmp_path and monkeypatch — no test touches the real user home.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import warnings
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from hmcpctl import config as config_module
from hmcpctl.config import (
    ConfigError,
    ConfigFileNotFoundError,
    HMCConfig,
    build_config,
    config_dir,
    config_inventory,
    env_var_value,
    list_nicknames,
    list_profiles,
    list_profiles_and_nicknames,
    list_profiles_with_default,
    load_profile,
    resolve_config_path,
)

# ---------------------------------------------------------------------------
# ISO upload response timeout (#1055)
# ---------------------------------------------------------------------------


def test_upload_timeout_default():
    assert HMCConfig.from_mapping({}).upload_timeout == 600.0


def test_timeout_default_allows_a_slow_feed_read():
    """A ManagedSystem feed read on a large HMC has taken over 60 s (#1202)."""
    config = HMCConfig.from_mapping({})
    assert config.timeout == 180.0
    assert config.upload_timeout > config.timeout


def test_upload_timeout_reads_the_environment(monkeypatch):
    monkeypatch.setenv("HMC_UPLOAD_TIMEOUT", "900")
    assert HMCConfig().upload_timeout == 900.0


@pytest.mark.parametrize("value", [0, -1])
def test_upload_timeout_refuses_a_non_positive_value(value):
    with pytest.raises(ValueError, match="upload_timeout"):
        HMCConfig.from_mapping({"upload_timeout": value})


# ---------------------------------------------------------------------------
# Response ceiling
# ---------------------------------------------------------------------------


def test_response_bytes_default():
    assert HMCConfig().max_response_bytes == 33554432
    assert HMCConfig.from_mapping({}).max_response_bytes == 33554432


@pytest.mark.parametrize("value", [1, 8, 67108864])
def test_response_bytes_explicit_override(value):
    assert HMCConfig(max_response_bytes=value).max_response_bytes == value
    assert (
        HMCConfig.from_mapping({"max_response_bytes": value}).max_response_bytes
        == value
    )


@pytest.mark.parametrize("value", [0, -1, 1.5, "bad", None])
@pytest.mark.parametrize("source", ["constructor", "mapping"])
def test_response_bytes_invalid_value(value, source):
    values = {"max_response_bytes": value}
    with pytest.raises(ValueError, match="max_response_bytes"):
        if source == "mapping":
            HMCConfig.from_mapping(values)
        else:
            HMCConfig(**values)


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "bad", ""])
def test_response_bytes_invalid_environment(monkeypatch, value):
    monkeypatch.setenv("HMC_MAX_RESPONSE_BYTES", value)
    with pytest.raises(ValueError, match="max_response_bytes"):
        HMCConfig()


def test_response_bytes_constructor_beats_environment(monkeypatch):
    monkeypatch.setenv("HMC_MAX_RESPONSE_BYTES", "67108864")
    assert HMCConfig().max_response_bytes == 67108864
    assert HMCConfig(max_response_bytes=8).max_response_bytes == 8


@pytest.mark.parametrize("value", ["67108864", "bad"])
def test_response_bytes_mapping_ignores_environment(monkeypatch, value):
    monkeypatch.setenv("HMC_MAX_RESPONSE_BYTES", value)
    config = HMCConfig.from_mapping({})
    assert config.max_response_bytes == 33554432
    assert "max_response_bytes" not in config.model_fields_set
    explicit = HMCConfig.from_mapping({"max_response_bytes": 8})
    assert explicit.max_response_bytes == 8
    assert "max_response_bytes" in explicit.model_fields_set


@pytest.mark.parametrize(
    "name",
    [
        "HMC_MAX_RESPONSE_BYTES",
        "hmc_max_response_bytes",
        "Hmc_Max_Response_Bytes",
    ],
)
def test_response_bytes_environment_beats_toml(tmp_path, monkeypatch, name):
    path = _write_toml(
        tmp_path / "config.toml", MINIMAL_TOML + "max_response_bytes = 8\n"
    )
    assert load_profile("dev", config_path=path).max_response_bytes == 8
    monkeypatch.setenv(name, "67108864")
    assert load_profile("dev", config_path=path).max_response_bytes == 67108864


@pytest.mark.parametrize("value", ["0", "-1", "1.5", '"bad"'])
def test_response_bytes_invalid_toml(tmp_path, value):
    path = _write_toml(
        tmp_path / "config.toml",
        MINIMAL_TOML + f"max_response_bytes = {value}\n",
    )
    with pytest.raises(ValueError, match="max_response_bytes"):
        load_profile("dev", config_path=path)


def test_validation_error_hides_rejected_password():
    with pytest.raises(ValidationError) as caught:
        HMCConfig.from_mapping({"host": "h", "user": "u", "password": 99887766})
    text = str(caught.value)
    assert "99887766" not in text
    assert "input_value" not in text
    assert "password" in text


def test_load_profile_error_hides_unquoted_numeric_password(tmp_path):
    path = _write_toml(
        tmp_path / "config.toml",
        '[profiles.dev]\nhost = "h"\nuser = "u"\npassword = 99887766\n',
    )
    with pytest.raises(ValidationError) as caught:
        load_profile("dev", config_path=path)
    text = str(caught.value)
    assert "99887766" not in text
    assert "password" in text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_TOML = """\
[profiles.dev]
host = "hmc-dev.example.com"
user = "admin"
password = "devpass"  # pragma: allowlist secret
"""

TWO_PROFILE_TOML = """\
default_profile = "prod"

[profiles.prod]
host = "hmc.example.com"
user = "admin"
password = "prodpass"  # pragma: allowlist secret

[profiles.dev]
host = "hmc-dev.example.com"
user = "devadmin"
password = "devpass"  # pragma: allowlist secret
"""


def _write_toml(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# resolve_config_path — platform path derivation
# ---------------------------------------------------------------------------


def test_resolve_linux_xdg(tmp_path, monkeypatch):
    """XDG_CONFIG_HOME set → uses it."""
    xdg = tmp_path / "xdg"
    cfg = xdg / "hmcpctl" / "config.toml"
    _write_toml(cfg, MINIMAL_TOML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    with patch.object(sys, "platform", "linux"):
        result = resolve_config_path()
    assert result == cfg


def test_resolve_linux_fallback(tmp_path, monkeypatch):
    """XDG_CONFIG_HOME unset on Linux → ~/.config/hmcpctl/config.toml."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    fake_home = tmp_path / "home"
    cfg = fake_home / ".config" / "hmcpctl" / "config.toml"
    _write_toml(cfg, MINIMAL_TOML)
    with (
        patch.object(sys, "platform", "linux"),
        patch("pathlib.Path.home", return_value=fake_home),
    ):
        result = resolve_config_path()
    assert result == cfg


def test_resolve_macos(tmp_path, monkeypatch):
    """sys.platform=darwin → ~/Library/Application Support/hmcpctl/config.toml."""
    fake_home = tmp_path / "home"
    cfg = fake_home / "Library" / "Application Support" / "hmcpctl" / "config.toml"
    _write_toml(cfg, MINIMAL_TOML)
    with (
        patch.object(sys, "platform", "darwin"),
        patch("pathlib.Path.home", return_value=fake_home),
    ):
        result = resolve_config_path()
    assert result == cfg


def test_resolve_windows(tmp_path, monkeypatch):
    """sys.platform=win32, APPDATA set → %APPDATA%/hmcpctl/config.toml."""
    appdata = tmp_path / "appdata"
    cfg = appdata / "hmcpctl" / "config.toml"
    _write_toml(cfg, MINIMAL_TOML)
    monkeypatch.setenv("APPDATA", str(appdata))
    with patch.object(sys, "platform", "win32"):
        result = resolve_config_path()
    assert result == cfg


def test_resolve_returns_none_when_absent(tmp_path, monkeypatch):
    """Returns None when file does not exist."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    with patch.object(sys, "platform", "linux"):
        result = resolve_config_path()
    assert result is None


def test_resolve_ignores_old_only_directory(tmp_path, monkeypatch):
    old_config = tmp_path / "hmc-mcp" / "config.toml"
    _write_toml(old_config, MINIMAL_TOML)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with patch.object(sys, "platform", "linux"):
        result = resolve_config_path()
    assert result is None


# ---------------------------------------------------------------------------
# load_profile — selection and precedence
# ---------------------------------------------------------------------------


def test_omitted_port_defaults_to_443_without_explicit_provenance(monkeypatch):
    monkeypatch.delenv("HMC_PORT", raising=False)

    config = HMCConfig()

    assert config.port == 443
    assert "port" not in config.model_fields_set


def test_constructor_port_is_explicit_even_when_it_matches_default(monkeypatch):
    monkeypatch.delenv("HMC_PORT", raising=False)

    config = HMCConfig(port=443)

    assert config.port == 443
    assert "port" in config.model_fields_set


def test_environment_port_is_explicit(monkeypatch):
    monkeypatch.setenv("HMC_PORT", "12443")

    config = HMCConfig()

    assert config.port == 12443
    assert "port" in config.model_fields_set


def test_toml_port_is_explicit(tmp_path, monkeypatch):
    monkeypatch.delenv("HMC_PORT", raising=False)
    cfg = _write_toml(
        tmp_path / "config.toml",
        MINIMAL_TOML + "port = 12443\n",
    )

    config = load_profile("dev", config_path=cfg)

    assert config.port == 12443
    assert "port" in config.model_fields_set


def test_load_profile_explicit(tmp_path, monkeypatch):
    """Explicit profile arg selects the correct profile."""
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    monkeypatch.delenv("HMC_HOST", raising=False)
    monkeypatch.delenv("HMC_USER", raising=False)
    monkeypatch.delenv("HMC_PASSWORD", raising=False)
    cfg = _write_toml(tmp_path / "config.toml", TWO_PROFILE_TOML)
    result = load_profile("dev", config_path=cfg)
    assert result.host == "hmc-dev.example.com"
    assert result.user == "devadmin"
    assert result.password == "devpass"  # pragma: allowlist secret


def test_load_profile_env_var(tmp_path, monkeypatch):
    """HMC_PROFILE env var selects profile when no explicit arg given."""
    monkeypatch.setenv("HMC_PROFILE", "dev")
    monkeypatch.delenv("HMC_HOST", raising=False)
    monkeypatch.delenv("HMC_USER", raising=False)
    monkeypatch.delenv("HMC_PASSWORD", raising=False)
    cfg = _write_toml(tmp_path / "config.toml", TWO_PROFILE_TOML)
    result = load_profile(config_path=cfg)
    assert result.host == "hmc-dev.example.com"


def test_load_profile_default(tmp_path, monkeypatch):
    """default_profile in TOML used when no arg and no HMC_PROFILE."""
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    monkeypatch.delenv("HMC_HOST", raising=False)
    monkeypatch.delenv("HMC_USER", raising=False)
    monkeypatch.delenv("HMC_PASSWORD", raising=False)
    cfg = _write_toml(tmp_path / "config.toml", TWO_PROFILE_TOML)
    result = load_profile(config_path=cfg)
    assert result.host == "hmc.example.com"  # prod is default


def test_load_profile_env_overrides_toml(tmp_path, monkeypatch):
    """HMC_HOST env var beats TOML host."""
    monkeypatch.setenv("HMC_HOST", "override-host.example.com")
    monkeypatch.delenv("HMC_USER", raising=False)
    monkeypatch.delenv("HMC_PASSWORD", raising=False)
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    cfg = _write_toml(tmp_path / "config.toml", TWO_PROFILE_TOML)
    result = load_profile("prod", config_path=cfg)
    assert result.host == "override-host.example.com"


def test_load_profile_password_env(tmp_path, monkeypatch):
    """password_env is resolved from the environment."""
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    monkeypatch.delenv("HMC_HOST", raising=False)
    monkeypatch.delenv("HMC_USER", raising=False)
    monkeypatch.delenv("HMC_PASSWORD", raising=False)
    monkeypatch.setenv("MY_PW", "supersecret")
    toml = """\
[profiles.prod]
host = "hmc.example.com"
user = "admin"
password_env = "MY_PW"  # pragma: allowlist secret
"""
    cfg = _write_toml(tmp_path / "config.toml", toml)
    result = load_profile("prod", config_path=cfg)
    assert result.password == "supersecret"  # pragma: allowlist secret


def test_load_profile_both_passwords_error(tmp_path, monkeypatch):
    """Both password and password_env in TOML → ConfigError."""
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    monkeypatch.setenv("MY_PW", "x")
    toml = """\
[profiles.bad]
host = "hmc.example.com"
user = "admin"
password = "plain"  # pragma: allowlist secret
password_env = "MY_PW"  # pragma: allowlist secret
"""
    cfg = _write_toml(tmp_path / "config.toml", toml)
    with pytest.raises(ConfigError, match="password or password_env"):
        load_profile("bad", config_path=cfg)


def test_load_profile_missing_password_env(tmp_path, monkeypatch):
    """password_env references missing env var → ConfigError (no secret in message)."""
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    monkeypatch.delenv("MISSING_VAR", raising=False)
    toml = """\
[profiles.prod]
host = "hmc.example.com"
user = "admin"
password_env = "MISSING_VAR"  # pragma: allowlist secret
"""
    cfg = _write_toml(tmp_path / "config.toml", toml)
    with pytest.raises(ConfigError, match="MISSING_VAR") as exc_info:
        load_profile("prod", config_path=cfg)
    # Variable name is in the message, but no secret value should be
    assert "MISSING_VAR" in str(exc_info.value)


def test_load_profile_no_default_no_arg(tmp_path, monkeypatch):
    """No selection path → ConfigError."""
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    toml = """\
[profiles.dev]
host = "h"
user = "u"
password = "p"
"""
    cfg = _write_toml(tmp_path / "config.toml", toml)
    with pytest.raises(ConfigError, match="no default_profile"):
        load_profile(config_path=cfg)


def test_load_profile_explicit_path_missing(tmp_path, monkeypatch):
    """An explicit config_path that does not exist names the path, not a profile problem (#915)."""
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    missing = tmp_path / "absent.toml"
    with pytest.raises(
        ConfigFileNotFoundError, match=r"absent\.toml: config file not found"
    ):
        load_profile(config_path=missing)


def test_load_profile_explicit_path_missing_even_with_profile_requested(
    tmp_path, monkeypatch
):
    """An explicit missing path is reported regardless of a requested profile (#915)."""
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    missing = tmp_path / "absent.toml"
    with pytest.raises(
        ConfigFileNotFoundError, match=r"absent\.toml: config file not found"
    ):
        load_profile("prod", config_path=missing)


def test_load_profile_unknown_profile(tmp_path, monkeypatch):
    """Named profile not in TOML → ConfigError listing available profiles."""
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    cfg = _write_toml(tmp_path / "config.toml", MINIMAL_TOML)
    with pytest.raises(ConfigError, match="not found"):
        load_profile("nonexistent", config_path=cfg)


def test_load_profile_toml_parse_error(tmp_path, monkeypatch):
    """Bad TOML → ConfigError with path."""
    cfg = tmp_path / "config.toml"
    cfg.write_text("this is [not valid toml ][[[", encoding="utf-8")
    with pytest.raises(ConfigError, match="TOML parse error"):
        load_profile("any", config_path=cfg)


# ---------------------------------------------------------------------------
# list_profiles
# ---------------------------------------------------------------------------


def test_list_profiles_normal(tmp_path, monkeypatch):
    """Returns profile names from config.toml."""
    cfg = _write_toml(tmp_path / "config.toml", TWO_PROFILE_TOML)
    result = list_profiles(config_path=cfg)
    assert set(result) == {"prod", "dev"}


def test_list_profiles_absent(tmp_path):
    """File absent → empty list."""
    result = list_profiles(config_path=tmp_path / "nonexistent.toml")
    assert result == []


# ---------------------------------------------------------------------------
# HMCConfig — no .env loading
# ---------------------------------------------------------------------------


def test_hmcconfig_no_env_file():
    """HMCConfig model_config must not load a .env file by default."""
    cfg_dict = HMCConfig.model_config
    # env_file should be absent, None, or an empty value — never ".env"
    env_file = cfg_dict.get("env_file")
    assert env_file != ".env", (
        "HMCConfig.model_config still has env_file='.env'; "
        "the TOML loader requires this to be removed."
    )


def test_direct_construction_still_works(monkeypatch):
    """HMCConfig(host=...) direct construction is still supported."""
    monkeypatch.delenv("HMC_HOST", raising=False)
    monkeypatch.delenv("HMC_USER", raising=False)
    monkeypatch.delenv("HMC_PASSWORD", raising=False)
    cfg = HMCConfig(
        host="myhost",
        user="myuser",
        password="mypass",  # pragma: allowlist secret
    )
    assert cfg.host == "myhost"
    assert cfg.user == "myuser"
    assert cfg.password == "mypass"  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# config_dir — unconditional platform path (no existence check)
# ---------------------------------------------------------------------------


def test_config_dir_linux_xdg(monkeypatch):
    """config_dir() returns XDG-based path without checking existence."""
    xdg = Path("/tmp/fake_xdg")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    with patch.object(sys, "platform", "linux"):
        result = config_dir()
    assert result == xdg / "hmcpctl"


def test_config_dir_macos(monkeypatch):
    """config_dir() returns ~/Library/Application Support/hmcpctl on macOS."""
    fake_home = Path("/tmp/fake_home")
    with (
        patch.object(sys, "platform", "darwin"),
        patch("pathlib.Path.home", return_value=fake_home),
    ):
        result = config_dir()
    assert result == fake_home / "Library" / "Application Support" / "hmcpctl"


def test_config_dir_returns_path_even_when_absent(tmp_path, monkeypatch):
    """config_dir() does not require the directory to exist."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nonexistent"))
    with patch.object(sys, "platform", "linux"):
        result = config_dir()
    assert not result.exists()
    assert result.name == "hmcpctl"


# ---------------------------------------------------------------------------
# list_profiles_with_default
# ---------------------------------------------------------------------------


def test_list_profiles_with_default_normal(tmp_path, monkeypatch):
    """Returns (names, default) from TOML."""
    cfg = _write_toml(tmp_path / "config.toml", TWO_PROFILE_TOML)
    names, default = list_profiles_with_default(config_path=cfg)
    assert set(names) == {"prod", "dev"}
    assert default == "prod"


def test_list_profiles_with_default_no_default(tmp_path, monkeypatch):
    """Returns (names, None) when no default_profile key."""
    cfg = _write_toml(tmp_path / "config.toml", MINIMAL_TOML)
    names, default = list_profiles_with_default(config_path=cfg)
    assert "dev" in names
    assert default is None


@pytest.mark.parametrize("value", ["42", '["prod"]', "true"])
def test_list_profiles_with_default_rejects_non_string_default(tmp_path, value):
    cfg = _write_toml(
        tmp_path / "config.toml",
        f"default_profile = {value}\n\n[profiles.prod]\nhost = 'h'\n",
    )

    with pytest.raises(
        ConfigError, match="'default_profile' must be a profile-name string"
    ):
        list_profiles_with_default(config_path=cfg)


def test_list_profiles_with_default_absent(tmp_path):
    """Returns ([], None) when file absent."""
    names, default = list_profiles_with_default(
        config_path=tmp_path / "nonexistent.toml"
    )
    assert names == []
    assert default is None


# ---------------------------------------------------------------------------
# HMC_AGENT_ID and effective_audit_memento (issue #132)
# ---------------------------------------------------------------------------


def test_agent_id_unset_uses_audit_memento_default():
    cfg = HMCConfig.from_mapping({})
    assert cfg.agent_id is None
    assert cfg.effective_audit_memento == "hmcpctl"


def test_agent_id_set_prefixes_audit_memento():
    cfg = HMCConfig.from_mapping({"agent_id": "alice"})
    assert cfg.effective_audit_memento == "hmcpctl:alice"


def test_agent_id_overrides_audit_memento_field():
    # When agent_id is set, effective_audit_memento uses hmcpctl:<agent_id>
    # regardless of the audit_memento field.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        cfg = HMCConfig.from_mapping({"agent_id": "bob", "audit_memento": "custom"})
    assert cfg.effective_audit_memento == "hmcpctl:bob"


def test_agent_id_no_warning_when_audit_memento_is_default():
    # When audit_memento is default ('hmcpctl'), no warning is emitted even
    # when agent_id is set, because there is no custom value being silently discarded.
    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("error", UserWarning)
        cfg = HMCConfig.from_mapping({"agent_id": "alice"})
    assert cfg.effective_audit_memento == "hmcpctl:alice"


def test_audit_memento_without_agent_id():
    cfg = HMCConfig.from_mapping({"audit_memento": "my-tool"})
    assert cfg.effective_audit_memento == "my-tool"


def test_agent_id_invalid_raises_at_construction():
    with pytest.raises(ValueError, match="comma"):
        HMCConfig(agent_id="bad,id")


def test_agent_id_from_env(monkeypatch):
    monkeypatch.setenv("HMC_AGENT_ID", "env-agent")
    cfg = HMCConfig()
    assert cfg.agent_id == "env-agent"
    assert cfg.effective_audit_memento == "hmcpctl:env-agent"


# ---------------------------------------------------------------------------
# Printable-ASCII header configuration (issue #839, ADR 0153)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["\x00", "\r", "\n", "\t", "\x1f", "\x7f", "V1_\u00e9"])
@pytest.mark.parametrize("field", ["schema_version", "audit_memento"])
@pytest.mark.parametrize("source", ["constructor", "mapping"])
def test_header_config_rejects_non_printable_ascii(field, bad, source):
    """Controls, DEL and non-ASCII are refused at construction.

    The constructor path starts from the isolated defaults of
    ``from_mapping({})`` so no ambient ``HMC_*`` variable supplies any other
    field; the mapping path is isolated by construction (ADR 0096).
    """
    base = HMCConfig.from_mapping({}).model_dump()
    with pytest.raises(ValidationError) as caught:
        if source == "mapping":
            HMCConfig.from_mapping({field: bad})
        else:
            HMCConfig(**{**base, field: bad})
    errors = caught.value.errors()
    assert [error["loc"] for error in errors] == [(field,)]
    assert "printable ASCII" in errors[0]["msg"]


def test_header_config_accepts_printable_ascii_and_empty():
    accepted = "".join(chr(codepoint) for codepoint in range(0x20, 0x7F))
    for field in ("schema_version", "audit_memento"):
        assert HMCConfig.from_mapping({field: accepted}) is not None
        assert HMCConfig.from_mapping({field: ""}) is not None


def test_header_config_rejects_overridden_audit_memento():
    # agent_id overrides the effective header value, but the stored field is
    # still validated at construction: the defect is refused where introduced.
    with pytest.raises(ValidationError) as caught:
        HMCConfig.from_mapping({"agent_id": "alice", "audit_memento": "bad\rvalue"})
    assert [error["loc"] for error in caught.value.errors()] == [("audit_memento",)]


# ---------------------------------------------------------------------------
# The override diagnostic is logged once per state through the bounded served
# sink and never emitted through Python's warnings channel (issue #546).
# ---------------------------------------------------------------------------


def test_audit_memento_override_does_not_use_python_warnings():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for _ in range(5):
            HMCConfig.from_mapping({"agent_id": "loop-agent", "audit_memento": "mine"})

    assert caught == []


def test_audit_memento_override_logs_once_per_process(caplog):
    """The bounded log record is emitted once for an unchanged state."""
    with caplog.at_level(logging.WARNING, logger="hmcpctl.config"):
        for _ in range(5):
            HMCConfig.from_mapping({"agent_id": "log-agent", "audit_memento": "mine"})

    records = [record for record in caplog.records if record.name == "hmcpctl.config"]
    assert len(records) == 1
    assert "HMC_AGENT_ID is set" in records[0].getMessage()


def test_audit_memento_override_logs_once_across_changed_values(caplog):
    """The process-level diagnostic does not grow with caller-controlled values."""
    with caplog.at_level(logging.WARNING, logger="hmcpctl.config"):
        HMCConfig.from_mapping({"agent_id": "agent-one", "audit_memento": "mine"})
        HMCConfig.from_mapping({"agent_id": "agent-one", "audit_memento": "mine"})
        HMCConfig.from_mapping({"agent_id": "agent-two", "audit_memento": "mine"})
        HMCConfig.from_mapping({"agent_id": "agent-two", "audit_memento": "yours"})

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "'agent-one'" in messages[0] and "'mine'" in messages[0]


class _SlowFalse:
    """A false flag whose truth test yields before answering.

    Threads racing the real record never interleave here: nothing between the
    membership test and the insert releases the GIL, so the first thread through
    completes both before another is scheduled and the assertion holds whether or
    not the code synchronises. The sleep opens that window deliberately — and it
    has to come after ``super().__contains__``, not before. Sleeping first would
    hand the first thread time to finish its insert, so every later thread would
    then see ``True`` and the window would close rather than open, which is the
    shape that silently made this test unable to fail.
    """

    def __bool__(self) -> bool:
        time.sleep(0.02)
        return False


def test_audit_memento_override_warns_once_across_concurrent_threads(
    caplog, monkeypatch
):
    """ "At most once per state" must hold off the event-loop thread too.

    ``server_permissions`` resolves guards under ``asyncio.to_thread`` and builds
    one config per granted connection, so concurrent ``hmc_effective_permissions``
    calls reach the validator from several worker threads at once. An
    unsynchronised check-then-act lets each of them miss the membership test and
    emit, turning "once per state" into O(concurrency).
    """
    monkeypatch.setattr(config_module, "_reported_memento_override", _SlowFalse())
    workers = 8
    ready = threading.Barrier(workers)

    def build() -> None:
        ready.wait()
        HMCConfig.from_mapping({"agent_id": "race-agent", "audit_memento": "mine"})

    with caplog.at_level(logging.WARNING, logger="hmcpctl.config"):
        threads = [threading.Thread(target=build) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    records = [record for record in caplog.records if record.name == "hmcpctl.config"]
    assert len(records) == 1


def test_audit_memento_override_repeat_is_recoverable_at_debug(caplog):
    """The suppressed repeat stays reachable by raising the log level.

    Dropping it at every level leaves an operator no local way to confirm the
    override is still discarding their configured memento — ``hmcpctl config
    show`` prints the raw field, not the effective one, so the only other
    evidence is the HMC's own audit log across the wire. ``_log_unresolved`` in
    ``server_permissions`` demotes its repeat for the same reason.
    """
    with caplog.at_level(logging.DEBUG, logger="hmcpctl.config"):
        for _ in range(3):
            HMCConfig.from_mapping({"agent_id": "debug-agent", "audit_memento": "mine"})

    records = [record for record in caplog.records if record.name == "hmcpctl.config"]
    assert [record.levelno for record in records] == [
        logging.WARNING,
        logging.DEBUG,
        logging.DEBUG,
    ]


def test_audit_memento_override_throttles_across_every_profile_in_the_file(
    profile_home, monkeypatch, caplog
):
    """The throttle must survive the served path, not just direct construction.

    One ``hmc_effective_permissions`` call resolves a guard per granted
    connection, and a connection is a profile key, so a single call can build one
    config per profile in the operator's ``config.toml`` — each carrying that
    profile's own ``audit_memento``. That is the path #546 names, and it is the
    one where many distinct values are constructed in one request. The single
    record proves the process-level throttle survives that served path.
    """
    profiles = 96
    body = "".join(
        f'\n[profiles.p{index}]\nhost = "hmc{index}.example.com"\n'
        f'audit_memento = "memento-{index}"\n'
        for index in range(profiles)
    )
    _write_toml(config_dir() / "config.toml", f'default_profile = "p0"\n{body}')
    monkeypatch.setenv("HMC_AGENT_ID", "fleet-agent")

    with caplog.at_level(logging.WARNING, logger="hmcpctl.config"):
        for _ in range(3):
            for index in range(profiles):
                build_config(profile=f"p{index}")

    records = [record for record in caplog.records if record.name == "hmcpctl.config"]
    assert len(records) == 1


# ---------------------------------------------------------------------------
# Nickname resolution (issue #226)
# ---------------------------------------------------------------------------

NICKNAME_TOML = """\
default_profile = "prod"

[profiles.prod]
host = "prod-hmc.example.com"
user = "admin"
password = "prodpass"    # pragma: allowlist secret

[profiles.stg]
host = "stg-hmc.example.com"
user = "admin"
password = "stgpass"    # pragma: allowlist secret

[nicknames]
big-iron = "prod"
staging = "stg"
"""


def test_nickname_resolves_via_explicit_profile_arg(tmp_path, monkeypatch):
    """A --profile-style explicit arg that is a nickname resolves to its target."""
    cfg = _write_toml(tmp_path / "config.toml", NICKNAME_TOML)
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    monkeypatch.delenv("HMC_HOST", raising=False)
    result = load_profile(profile="big-iron", config_path=cfg)
    assert result.host == "prod-hmc.example.com"


def test_nickname_resolves_via_hmc_profile_env(tmp_path, monkeypatch):
    """HMC_PROFILE carrying a nickname resolves to its target profile."""
    cfg = _write_toml(tmp_path / "config.toml", NICKNAME_TOML)
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    monkeypatch.delenv("HMC_HOST", raising=False)
    monkeypatch.setenv("HMC_PROFILE", "staging")
    result = load_profile(profile=None, config_path=cfg)
    assert result.host == "stg-hmc.example.com"


def test_nickname_resolves_via_default_profile(tmp_path, monkeypatch):
    """A default_profile that is a nickname resolves to its target profile."""
    toml = NICKNAME_TOML.replace(
        'default_profile = "prod"', 'default_profile = "big-iron"'
    )
    cfg = _write_toml(tmp_path / "config.toml", toml)
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    monkeypatch.delenv("HMC_HOST", raising=False)
    result = load_profile(profile=None, config_path=cfg)
    assert result.host == "prod-hmc.example.com"


def test_profile_key_wins_over_nickname_collision(tmp_path, monkeypatch):
    """A name that is both a profile key and a nickname key uses the profile."""
    toml = """\
default_profile = "prod"

[profiles.prod]
host = "real-prod.example.com"
user = "admin"
password = "p"    # pragma: allowlist secret

[profiles.dev]
host = "dev.example.com"
user = "admin"
password = "d"    # pragma: allowlist secret

[nicknames]
prod = "dev"
"""
    cfg = _write_toml(tmp_path / "config.toml", toml)
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    monkeypatch.delenv("HMC_HOST", raising=False)
    result = load_profile(profile="prod", config_path=cfg)
    assert result.host == "real-prod.example.com"


def test_nickname_missing_target_raises_config_error(tmp_path, monkeypatch):
    """A nickname whose target is not a profile raises ConfigError naming names."""
    toml = """\
default_profile = "prod"

[profiles.prod]
host = "h.example.com"
user = "admin"
password = "p"    # pragma: allowlist secret

[nicknames]
ghost = "does-not-exist"
"""
    cfg = _write_toml(tmp_path / "config.toml", toml)
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    with pytest.raises(ConfigError) as exc:
        load_profile(profile="ghost", config_path=cfg)
    msg = str(exc.value)
    assert "ghost" in msg
    assert "does-not-exist" in msg
    assert "prod" in msg  # available profiles named


def test_unknown_name_config_error_names_nicknames(tmp_path, monkeypatch):
    """An unknown name (not a profile, not a nickname) names profiles + nicknames."""
    cfg = _write_toml(tmp_path / "config.toml", NICKNAME_TOML)
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    with pytest.raises(ConfigError) as exc:
        load_profile(profile="nope", config_path=cfg)
    msg = str(exc.value)
    assert "big-iron" in msg  # a nickname is named
    assert "prod" in msg  # a profile is named


def test_nickname_resolution_is_case_sensitive(tmp_path, monkeypatch):
    """Nickname matching is case-sensitive: BIG-IRON does not match big-iron."""
    cfg = _write_toml(tmp_path / "config.toml", NICKNAME_TOML)
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    with pytest.raises(ConfigError):
        load_profile(profile="BIG-IRON", config_path=cfg)


def test_nickname_one_level_no_chaining(tmp_path, monkeypatch):
    """A nickname whose target is another nickname is NOT resolved (one level)."""
    toml = """\
default_profile = "prod"

[profiles.prod]
host = "h.example.com"
user = "admin"
password = "p"    # pragma: allowlist secret

[nicknames]
a = "b"
b = "prod"
"""
    cfg = _write_toml(tmp_path / "config.toml", toml)
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    # "a" -> "b", but "b" is not a profile key, so resolution fails.
    with pytest.raises(ConfigError, match="b"):
        load_profile(profile="a", config_path=cfg)


def test_malformed_nicknames_table_raises_config_error(tmp_path, monkeypatch):
    """A nicknames table with a non-string value raises ConfigError."""
    toml = """\
default_profile = "prod"

[profiles.prod]
host = "h.example.com"
user = "admin"
password = "p"    # pragma: allowlist secret

[nicknames]
big-iron = 42
"""
    cfg = _write_toml(tmp_path / "config.toml", toml)
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    with pytest.raises(ConfigError):
        load_profile(profile="big-iron", config_path=cfg)


def test_malformed_nicknames_non_table_raises(tmp_path, monkeypatch):
    """A top-level nicknames key that is not a table raises ConfigError."""
    toml = """\
default_profile = "prod"
nicknames = "not-a-table"

[profiles.prod]
host = "h.example.com"
user = "admin"
password = "p"      # pragma: allowlist secret
"""
    cfg = _write_toml(tmp_path / "config.toml", toml)
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    with pytest.raises(ConfigError):
        load_profile(profile="big-iron", config_path=cfg)


def test_malformed_nicknames_table_fails_regardless_of_profile(tmp_path, monkeypatch):
    """A malformed nicknames table raises even when a valid profile is selected.

    Per ADR 0030, load_profile validates the nicknames structure whenever the
    key is present, not only when a nickname is resolved.
    """
    toml = """\
default_profile = "prod"

[profiles.prod]
host = "prod-hmc.example.com"
user = "admin"
password = "prodpass"     # pragma: allowlist secret

[nicknames]
big-iron = 42
"""
    cfg = _write_toml(tmp_path / "config.toml", toml)
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    with pytest.raises(ConfigError):
        load_profile(profile="prod", config_path=cfg)


def test_well_formed_nicknames_do_not_block_plain_profile(tmp_path, monkeypatch):
    """A well-formed nicknames table does not block selecting a plain profile."""
    cfg = _write_toml(tmp_path / "config.toml", NICKNAME_TOML)
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    monkeypatch.delenv("HMC_HOST", raising=False)
    result = load_profile(profile="prod", config_path=cfg)
    assert result.host == "prod-hmc.example.com"


def test_list_nicknames_present(tmp_path):
    """list_nicknames returns the nicknames table as dict[str, str]."""
    cfg = _write_toml(tmp_path / "config.toml", NICKNAME_TOML)
    assert list_nicknames(config_path=cfg) == {"big-iron": "prod", "staging": "stg"}


def test_list_nicknames_absent(tmp_path):
    """list_nicknames returns {} when no nicknames table or no file."""
    cfg = _write_toml(tmp_path / "config.toml", TWO_PROFILE_TOML)
    assert list_nicknames(config_path=cfg) == {}
    assert list_nicknames(config_path=tmp_path / "nonexistent.toml") == {}


# ---------------------------------------------------------------------------
# list_profiles_and_nicknames (issue #222)
# ---------------------------------------------------------------------------


def test_list_profiles_and_nicknames_returns_both_tables(tmp_path):
    """Both selection tables come back from one call, so they cannot disagree."""

    cfg = _write_toml(tmp_path / "config.toml", NICKNAME_TOML)
    profiles, nicknames = list_profiles_and_nicknames(config_path=cfg)
    assert set(profiles) == {"prod", "stg"}
    assert nicknames == {"big-iron": "prod", "staging": "stg"}


def test_list_profiles_and_nicknames_absent_file(tmp_path):
    """An absent file is an empty configuration, not an error."""

    assert list_profiles_and_nicknames(config_path=tmp_path / "nope.toml") == ([], {})


def test_list_profiles_and_nicknames_rejects_malformed_nicknames(tmp_path):
    """A malformed nicknames table raises, as it does for list_nicknames."""

    cfg = _write_toml(
        tmp_path / "config.toml",
        NICKNAME_TOML.replace('big-iron = "prod"', "big-iron = 7"),
    )
    with pytest.raises(ConfigError, match="must map to a profile-key string"):
        list_profiles_and_nicknames(config_path=cfg)


def _inventory_toml(tmp_path, body):
    return _write_toml(
        tmp_path / "config.toml",
        f'[profiles.good]\nhost = "h"\n\n[profiles.bad]\nhost = "h"\n{body}\n',
    )


@pytest.mark.parametrize("value", ['"x443"', "[1]"])
def test_config_inventory_names_profile_and_field_for_bad_port(tmp_path, value):
    cfg = _inventory_toml(tmp_path, f"port = {value}")
    with pytest.raises(
        ConfigError, match=r"profile 'bad': port must be an integer, got"
    ):
        config_inventory(config_path=cfg)


def test_config_inventory_names_profile_and_field_for_bad_verify_ssl(tmp_path):
    cfg = _inventory_toml(tmp_path, 'verify_ssl = "maybe"')
    with pytest.raises(
        ConfigError, match=r"profile 'bad': verify_ssl must be a boolean"
    ):
        config_inventory(config_path=cfg)


def test_config_inventory_coerces_like_hmcconfig(tmp_path):
    cfg = _inventory_toml(tmp_path, 'port = "8443"\nverify_ssl = "false"')
    bad = next(
        p for p in config_inventory(config_path=cfg)["profiles"] if p["name"] == "bad"
    )
    assert bad["port"] == 8443
    assert bad["verify_ssl"] is False


# ---------------------------------------------------------------------------
# The read-and-parse failure contract shared by every reader (issue #257)
#
# Each reader documents ConfigError as its failure type, so a
# ``try/except ConfigError`` around any of them must actually catch. These cases
# are parametrized over every reader rather than written per reader: before #257
# only list_profiles_and_nicknames converted them, and the other four leaked a
# PermissionError, an IsADirectoryError, a UnicodeDecodeError, a RecursionError,
# or an AttributeError naming the absolute config path.
# ---------------------------------------------------------------------------

_READERS = {
    "list_profiles_with_default": lambda p: list_profiles_with_default(config_path=p),
    "list_profiles": lambda p: list_profiles(config_path=p),
    "list_nicknames": lambda p: list_nicknames(config_path=p),
    "list_profiles_and_nicknames": lambda p: list_profiles_and_nicknames(config_path=p),
    "config_inventory": lambda p: config_inventory(config_path=p),
    "load_profile": lambda p: load_profile("prod", config_path=p),
}

# Every reader that consults the ``profiles`` table. list_nicknames is absent
# because it returns the other half of the document and never touches this one.
_PROFILE_READERS = {
    name: call for name, call in _READERS.items() if name != "list_nicknames"
}


@pytest.fixture(params=list(_READERS), ids=list(_READERS))
def reader(request):
    """Each config.toml reader in turn, called with an explicit config path."""
    return _READERS[request.param]


@pytest.fixture(params=list(_PROFILE_READERS), ids=list(_PROFILE_READERS))
def profile_reader(request):
    """Each reader that consults the ``profiles`` table."""
    return _PROFILE_READERS[request.param]


@pytest.fixture
def unreadable_config(tmp_path):
    """A well-formed config.toml with no read permission, restored on teardown."""
    cfg = _write_toml(tmp_path / "config.toml", TWO_PROFILE_TOML)
    cfg.chmod(0o000)
    yield cfg
    cfg.chmod(0o600)


def test_reader_rejects_an_unreadable_file(reader, unreadable_config):
    """A PermissionError must arrive as a ConfigError, not as a raw OSError."""

    with pytest.raises(ConfigError, match="cannot be read"):
        reader(unreadable_config)


def test_reader_rejects_a_directory(reader, tmp_path):
    """A directory at the config path is an OSError on read, not an absent file."""

    directory = tmp_path / "config.toml"
    directory.mkdir()
    with pytest.raises(ConfigError, match="cannot be read"):
        reader(directory)


def test_reader_rejects_a_null_byte_in_the_path(reader, tmp_path):
    """read_text raises ValueError for an unusable path before it reaches the fs."""

    cfg = _write_toml(tmp_path / "config.toml", TWO_PROFILE_TOML)
    with pytest.raises(ConfigError, match="cannot be read"):
        reader(Path(f"{cfg}\x00suffix"))


def test_reader_rejects_non_utf8(reader, tmp_path):
    """A latin-1 config file is a ConfigError, not a UnicodeDecodeError."""

    cfg = tmp_path / "config.toml"
    cfg.write_bytes(b'[profiles.prod]\nhost = "caf\xe9"\n')
    with pytest.raises(ConfigError, match="is not valid UTF-8"):
        reader(cfg)


def test_reader_rejects_invalid_toml(reader, tmp_path):
    """A parse error is a ConfigError naming the path."""

    cfg = tmp_path / "config.toml"
    cfg.write_text("this is [not valid toml ][[[", encoding="utf-8")
    with pytest.raises(ConfigError, match="TOML parse error"):
        reader(cfg)


def test_reader_rejects_a_deeply_nested_document(reader, tmp_path):
    """tomllib recurses on nested arrays; the stack runs out before the parser does."""

    cfg = tmp_path / "config.toml"
    cfg.write_text("deep = " + "[" * 3000 + "]" * 3000, encoding="utf-8")
    with pytest.raises(ConfigError, match="document nesting is too deep"):
        reader(cfg)


def test_reader_reports_an_unresolvable_home(reader, monkeypatch):
    """Path.home() raises under a uid with no passwd entry and no HOME."""

    def _no_home():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr("pathlib.Path.home", _no_home)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    with pytest.raises(ConfigError, match="cannot resolve the config path"):
        reader(None)


@pytest.mark.parametrize(
    ("name", "empty"),
    [
        ("list_profiles_with_default", ([], None)),
        ("list_profiles", []),
        ("list_nicknames", {}),
        ("list_profiles_and_nicknames", ([], {})),
        ("config_inventory", {"profiles": [], "config_file": None}),
    ],
)
def test_listing_reader_with_no_platform_config_file(
    name, empty, tmp_path, monkeypatch
):
    """No config.toml anywhere is an empty configuration, not a failure."""

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    with patch.object(sys, "platform", "linux"):
        assert _READERS[name](None) == empty


def test_load_profile_with_no_platform_config_file(tmp_path, monkeypatch):
    """load_profile has nothing to select from, so it raises rather than returns."""

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    with (
        patch.object(sys, "platform", "linux"),
        pytest.raises(ConfigError, match="not found"),
    ):
        _READERS["load_profile"](None)


def test_load_profile_platform_file_missing_no_profile_requested(tmp_path, monkeypatch):
    """No platform file and nothing requesting a profile names the path it looked in (#915)."""

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    with (
        patch.object(sys, "platform", "linux"),
        pytest.raises(
            ConfigFileNotFoundError, match=r"config\.toml: config file not found"
        ),
    ):
        load_profile(config_path=None)


def test_load_profile_platform_file_missing_hmc_profile_requested(
    tmp_path, monkeypatch
):
    """A missing platform file with HMC_PROFILE set still reports the profile problem,
    not the file (unaffected by #915: something did ask for a profile)."""

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.setenv("HMC_PROFILE", "prod")
    with (
        patch.object(sys, "platform", "linux"),
        pytest.raises(ConfigError, match="not found") as exc_info,
    ):
        load_profile(config_path=None)
    assert not isinstance(exc_info.value, ConfigFileNotFoundError)


def test_load_profile_platform_file_vanishes_before_read(tmp_path, monkeypatch):
    """A platform file resolve_config_path() saw but that is gone by read time is
    reported as missing, not as no default_profile (#915 gauntlet finding)."""

    monkeypatch.delenv("HMC_PROFILE", raising=False)
    # resolve_config_path() would have reported this path as present; it never
    # existed on disk here, standing in for the race window between that
    # existence check and load_profile's later read.
    vanished = tmp_path / "config.toml"
    monkeypatch.setattr(config_module, "resolve_config_path", lambda: vanished)
    with pytest.raises(
        ConfigFileNotFoundError, match="config file not found"
    ) as exc_info:
        load_profile(config_path=None)
    assert str(vanished) in str(exc_info.value)


def test_profile_reader_rejects_a_non_table_profiles_key(profile_reader, tmp_path):
    """`profiles = "x"` used to reach `.keys()` on a str, or a substring test."""

    cfg = _write_toml(tmp_path / "config.toml", "profiles = 'not-a-table'\n")
    with pytest.raises(ConfigError, match="'profiles' must be a table"):
        profile_reader(cfg)


def test_load_profile_rejects_a_non_table_profile_entry(tmp_path, monkeypatch):
    """A selected profile that is not a table is a ConfigError, not dict()'s."""

    monkeypatch.delenv("HMC_PROFILE", raising=False)
    cfg = _write_toml(tmp_path / "config.toml", "[profiles]\nprod = 'not-a-table'\n")
    with pytest.raises(ConfigError, match="profile 'prod' must be a table"):
        load_profile("prod", config_path=cfg)


# ---------------------------------------------------------------------------
# Environment-isolated construction: HMCConfig.from_mapping (issue #368, ADR 0096)
# ---------------------------------------------------------------------------

#: The three vars that silently redirect a backend: a stray HMC_HOST points it at
#: the wrong HMC, a stray HMC_SSH_KEY_FILE offers the wrong private key, and a
#: stray HMC_AGENT_ID corrupts ADR 0011 ownership attribution on every LPAR the
#: process stamps.
LEAKY_ENVIRONMENT = {
    "HMC_HOST": "leaked-host.example.com",
    "HMC_AGENT_ID": "leaked-agent",
    "HMC_SSH_KEY_FILE": "/leaked/id_rsa",
}


def _non_default_env_value(field_name: str, field_info) -> str:
    """A valid, non-default environment string for *field_name*.

    Derived from the declared annotation rather than listed per field, so a new
    setting is polluted by the tests below without editing them.
    """
    annotation = field_info.annotation
    if annotation is bool:
        return "true"
    if annotation is int:
        return "9999"
    if annotation is float:
        return "9.5"
    return f"leak-{field_name}"


def test_from_mapping_ignores_ambient_environment(monkeypatch):
    """Issue #368's shape: one config per HMC built from a row, in a polluted process."""
    for name, value in LEAKY_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)

    cfg = HMCConfig.from_mapping({"host": "row-host.example.com", "user": "rowuser"})

    assert cfg.host == "row-host.example.com"
    assert cfg.agent_id is None
    assert cfg.ssh_key_file is None


def test_plain_constructor_still_reads_the_environment(monkeypatch):
    """The operator path is deliberately unchanged — from_mapping is additive.

    Pinned so that a future attempt to make ``HMCConfig()`` itself isolated
    cannot land silently; that would break the CLI and the MCP server.
    """
    for name, value in LEAKY_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)

    cfg = HMCConfig(host="row-host.example.com")

    assert cfg.host == "row-host.example.com"
    assert cfg.agent_id == "leaked-agent"
    assert cfg.ssh_key_file == "/leaked/id_rsa"


def test_from_mapping_leaves_no_field_to_the_environment(monkeypatch):
    """Every field, not just the three above, takes its declared default.

    Enumerated from ``model_fields``, so a new setting that ``from_mapping``
    fails to pass explicitly is caught here rather than leaking in production.
    """
    prefix = HMCConfig.model_config["env_prefix"]
    env_names = {name: f"{prefix}{name.upper()}" for name in HMCConfig.model_fields}
    for env_name in env_names.values():
        monkeypatch.delenv(env_name, raising=False)
    pristine = HMCConfig().model_dump()

    for field_name, env_name in env_names.items():
        monkeypatch.setenv(
            env_name,
            _non_default_env_value(field_name, HMCConfig.model_fields[field_name]),
        )

    with warnings.catch_warnings():
        # A polluted env sets agent_id and audit_memento together, which the
        # model validator warns about; that warning is this test's setup, not
        # its subject.
        warnings.simplefilter("ignore", UserWarning)
        polluted = HMCConfig().model_dump()

    # The pollution has to be visible somewhere, or the assertion below is vacuous.
    assert polluted != pristine
    assert HMCConfig.from_mapping({}).model_dump() == pristine


def test_from_mapping_ignores_a_dotenv_file(monkeypatch, tmp_path):
    """A .env in the working directory cannot reach an isolated construction."""
    (tmp_path / ".env").write_text(
        "HMC_HOST=dotenv-host.example.com\nHMC_USER=dotenvuser\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HMC_HOST", raising=False)
    monkeypatch.delenv("HMC_USER", raising=False)

    cfg = HMCConfig.from_mapping({"host": "row-host.example.com"})

    assert cfg.host == "row-host.example.com"
    assert cfg.user == ""


def test_from_mapping_applies_every_supplied_key():
    values = {
        "host": "row-host.example.com",
        "port": 12443,
        "user": "rowuser",
        "password": "rowpass",  # pragma: allowlist secret
        "ssh_key_file": "/keys/row",
        "ssh_verify_host_key": False,
        "verify_ssl": True,
        "ca_bundle": "/ca/bundle.pem",
        "trust_system_store": False,
        "timeout": 15.0,
        "upload_timeout": 450.0,
        "max_response_bytes": 67108864,
        "ssh_timeout": 30.0,
        "audit_memento": "hmcpctl",
        "schema_version": "V1_0",
        "agent_id": "row-agent",
        "authorize_power_operations": True,
        "iso_url_allowlist": "iso.example.internal",
    }
    assert set(values) == set(HMCConfig.model_fields)

    cfg = HMCConfig.from_mapping(values)

    assert {name: getattr(cfg, name) for name in values} == values


def test_from_mapping_ignores_keys_that_name_no_field():
    """Matches the ``extra="ignore"`` HMCConfig already declares.

    A database row carries columns that are not settings; ``from_mapping``
    differs from ``HMCConfig(...)`` in environment isolation only.
    """
    cfg = HMCConfig.from_mapping(
        {"host": "row-host.example.com", "id": 7, "nickname": "prod"}
    )

    assert cfg.host == "row-host.example.com"


def test_from_mapping_runs_field_validators():
    """Isolation does not buy an escape from validation."""
    with pytest.raises(ValueError, match="comma"):
        HMCConfig.from_mapping({"agent_id": "bad,id"})


def test_from_mapping_runs_model_validators_once(monkeypatch):
    """The audit-memento override diagnostic still logs exactly once."""
    monkeypatch.delenv("HMC_AGENT_ID", raising=False)
    with patch.object(config_module._logger, "warning") as warning:
        cfg = HMCConfig.from_mapping({"agent_id": "row-agent", "audit_memento": "mine"})

    warning.assert_called_once()
    assert cfg.effective_audit_memento == "hmcpctl:row-agent"


def test_from_mapping_reports_only_the_supplied_keys_as_set():
    """model_fields_set must mean "the caller set this", not "from_mapping did".

    Passing every field explicitly is what closes the env leak, but it would
    also mark every field as caller-set. That is consumer-visible through
    ``model_dump(exclude_unset=True)`` and, more sharply, through
    ``client._verify_ssl_source``, which reads ``model_fields_set`` to name
    where ``verify_ssl`` came from in the #379 TLS audit record.
    """
    cfg = HMCConfig.from_mapping(
        {"host": "row-host.example.com", "user": "rowuser", "id": 7}
    )

    assert cfg.model_fields_set == {"host", "user"}
    assert cfg.model_dump(exclude_unset=True) == {
        "host": "row-host.example.com",
        "user": "rowuser",
    }


def test_from_mapping_keeps_the_tls_audit_provenance_accurate(monkeypatch):
    """#379's `source` names the knob to turn; from_mapping must not lie about it.

    Without the ``model_fields_set`` restoration this reports
    ``explicit-argument`` for a value nobody supplied, pointing an operator at
    an argument that does not exist.
    """
    from hmcpctl.client.core import _verify_ssl_source

    monkeypatch.delenv("HMC_VERIFY_SSL", raising=False)

    assert _verify_ssl_source(HMCConfig.from_mapping({"host": "h"})) == "field-default"
    assert (
        _verify_ssl_source(HMCConfig.from_mapping({"host": "h", "verify_ssl": False}))
        == "explicit-argument"
    )


def test_from_mapping_applies_a_none_value_rather_than_defaulting():
    """A NULL column is a value, not an omission — it must not silently default."""
    assert HMCConfig.from_mapping({"ssh_key_file": None}).ssh_key_file is None
    with pytest.raises(ValueError, match="host"):
        HMCConfig.from_mapping({"host": None})


def test_from_mapping_returns_a_plain_hmcconfig():
    """Not a private subclass: ``type()`` and pydantic equality both have to hold."""
    cfg = HMCConfig.from_mapping({"host": "row-host.example.com"})

    assert type(cfg) is HMCConfig
    assert cfg == HMCConfig.from_mapping({"host": "row-host.example.com"})


def test_from_mapping_accepts_any_mapping():
    """The parameter is a Mapping, so a read-only row proxy works."""
    cfg = HMCConfig.from_mapping(MappingProxyType({"host": "row-host.example.com"}))

    assert cfg.host == "row-host.example.com"


def test_from_mapping_names_a_required_field_the_mapping_omits(monkeypatch):
    """A required field must produce an error that names it.

    HMCConfig has no required field today. Without the guard the omission
    still does not leak — the field is passed explicitly carrying
    PydanticUndefined, so the init source still wins — but pydantic reports a
    type error about a value the caller never wrote. On a method whose contract
    is "the mapping is the only input", that is the wrong error, so the match
    below is deliberately the guard's own wording: pydantic's ValidationError
    is a ValueError whose text also contains "tenant", and a looser match would
    pass with the guard deleted.
    """

    class RequiredFieldConfig(HMCConfig):
        tenant: str

    monkeypatch.setenv("HMC_TENANT", "leaked-tenant")

    with pytest.raises(ValueError, match=r"missing required settings: tenant"):
        RequiredFieldConfig.from_mapping({"host": "row-host.example.com"})

    supplied = RequiredFieldConfig.from_mapping(
        {"host": "row-host.example.com", "tenant": "row-tenant"}
    )
    assert supplied.tenant == "row-tenant"


def test_env_file_none_does_not_suppress_environment_variables(monkeypatch):
    """The trap #368 names: ``_env_file=None`` is not isolation.

    Pinned so docs/environment-variables.md and AGENTS.md cannot drift back to
    presenting it as one. HMCConfig declares no ``env_file`` at all, so the
    argument is inert here — it suppresses a dotenv source that was never
    configured, and never touched the environment in the first place.
    """
    monkeypatch.setenv("HMC_HOST", "leaked-host.example.com")

    assert HMCConfig(_env_file=None).host == "leaked-host.example.com"


# ---------------------------------------------------------------------------
# Case-variant HMC_* exports beat a profile's TOML key (issue #531)
# ---------------------------------------------------------------------------

CASE_VARIANT_TOML = """\
default_profile = "guarded"

[profiles.guarded]
host = "toml-hmc.example.com"
user = "tomluser"
password = "tomlpass"  # pragma: allowlist secret
verify_ssl = true
authorize_power_operations = true
"""


def _hmc_env_names() -> dict[str, str]:
    """``{field_name: HMC_FIELD_NAME}`` for every ``HMCConfig`` field."""
    prefix = HMCConfig.model_config["env_prefix"]
    return {name: f"{prefix}{name.upper()}" for name in HMCConfig.model_fields}


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    """An isolated home holding ``CASE_VARIANT_TOML`` at the platform-native path.

    ``build_config`` resolves its own config path, so the profile has to be
    reachable through ``resolve_config_path`` rather than passed in — which is
    the point: these tests drive the whole ``build_config`` branch, not
    ``load_profile`` alone.

    Every ``HMC_*`` name is cleared *in every casing* — including the
    ``HMC_VERIFY_SSL`` the autouse TLS fixture sets — so a variant a test
    exports is the only environment value in play. Clearing a fixed list of
    canonical spellings would carry the exact-case assumption this whole
    section exists to remove: a stray ``hmc_host`` in the developer's or
    runner's environment reaches ``build_config`` through the code path under
    test, and a stray variant of any other field silently supplies a value the
    test believes came from the profile.
    """
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    prefix = HMCConfig.model_config["env_prefix"].upper()
    for key in list(os.environ):
        if key.upper().startswith(prefix):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return _write_toml(config_dir() / "config.toml", CASE_VARIANT_TOML)


@pytest.mark.parametrize(
    "env_name",
    ["hmc_authorize_power_operations", "Hmc_Authorize_Power_Operations"],
)
def test_case_variant_export_beats_a_profile_boolean(
    profile_home, monkeypatch, env_name
):
    """The ADR 0092 §4 guard an operator turns off for an incident actually goes off.

    The profile says ``true``; the export says ``false``. pydantic-settings
    matches ``HMC_*`` case-insensitively, so the export reaches the field — and
    the profile's value must not be handed to the constructor ahead of it.
    """
    monkeypatch.setenv(env_name, "false")

    assert build_config(profile="guarded").authorize_power_operations is False


def test_build_config_uses_a_supplied_document_without_resolving_a_path(monkeypatch):
    """An invocation snapshot changes only the source of the parsed mapping."""
    document = config_module.ConfigDocument(
        Path("snapshot-config.toml"),
        {
            "profiles": {
                "guarded": {
                    "host": "toml-hmc.example.com",
                    "user": "toml-user",
                    "authorize_power_operations": True,
                }
            }
        },
    )

    def unexpected_resolution():
        pytest.fail("a supplied document resolved the filesystem path")

    monkeypatch.setattr(config_module, "resolve_config_path", unexpected_resolution)
    monkeypatch.setenv("HMC_USER", "env-user")

    config = build_config(profile="guarded", document=document)

    assert config.host == "toml-hmc.example.com"
    assert config.user == "env-user"
    assert config.authorize_power_operations is True


@pytest.mark.parametrize("env_name", ["hmc_user", "Hmc_User"])
def test_case_variant_export_beats_a_profile_string(
    profile_home, monkeypatch, env_name
):
    monkeypatch.setenv(env_name, "envuser")

    config = build_config(profile="guarded")

    assert config.user == "envuser"
    # The profile is still the source of everything the export did not name.
    assert config.host == "toml-hmc.example.com"


def test_every_field_takes_a_case_variant_export_over_the_profile(
    profile_home, monkeypatch
):
    """The hole was never specific to two fields, so neither is the guard.

    Enumerated from ``model_fields``, so a setting added later is covered here
    without editing this test. ``host`` is deliberately left to the profile: it
    is the field ``build_config`` gates its whole TOML branch on, so exporting
    it would skip the profile entirely and make every other assertion vacuous.
    Its TOML value arriving intact is what proves the profile was consulted.
    """
    env_names = _hmc_env_names()
    fields = {
        name: HMCConfig.model_fields[name] for name in env_names if name != "host"
    }
    profile = "\n".join(
        f"{name} = {_toml_literal(name, info)}" for name, info in fields.items()
    )
    _write_toml(
        profile_home,
        f'default_profile = "guarded"\n\n[profiles.guarded]\n'
        f'host = "toml-hmc.example.com"\n{profile}\n',
    )
    for name, info in fields.items():
        monkeypatch.setenv(env_names[name].lower(), _non_default_env_value(name, info))

    with warnings.catch_warnings():
        # A fully-populated environment sets agent_id and audit_memento
        # together, which the model validator warns about; that is this test's
        # setup, not its subject.
        warnings.simplefilter("ignore", UserWarning)
        config = build_config(profile="guarded")

    assert config.host == "toml-hmc.example.com"
    assert {name: getattr(config, name) for name in fields} == {
        name: _expected_env_value(name, info) for name, info in fields.items()
    }


def _toml_literal(field_name: str, field_info) -> str:
    """A TOML literal for *field_name* that differs from its env-side value."""
    annotation = field_info.annotation
    if annotation is bool:
        return "false"  # _non_default_env_value yields "true"
    if annotation is int:
        return "1234"
    if annotation is float:
        return "1.5"
    return f'"toml-{field_name}"'


def _expected_env_value(field_name: str, field_info):
    """``_non_default_env_value`` as the model parses it."""
    annotation = field_info.annotation
    if annotation is bool:
        return True
    if annotation is int:
        return 9999
    if annotation is float:
        return 9.5
    return f"leak-{field_name}"


@pytest.mark.parametrize("env_name", ["HMC_HOST", "hmc_host", "Hmc_Host"])
def test_a_host_export_skips_the_profile_whatever_its_case(
    profile_home, monkeypatch, env_name
):
    """``build_config``'s TOML branch is gated on ``HMC_HOST``; the gate is case-blind.

    Spelling the variable differently must not choose a different resolution
    path, or ``connection_scope``'s mirror of this gate names a profile the
    connection is not actually using.
    """
    monkeypatch.setenv(env_name, "env-hmc.example.com")

    config = build_config(profile="guarded")

    assert config.host == "env-hmc.example.com"
    assert config.user == ""
    assert config.authorize_power_operations is False


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows folds every environment variable name to upper case, so "
        "hmc_profile IS HMC_PROFILE there and does select a profile — the "
        "qualification docs/environment-variables.md carries."
    ),
)
def test_hmc_profile_is_matched_exactly(profile_home, monkeypatch):
    """The one documented exception, pinned so the doc cannot drift away from it.

    ``HMC_PROFILE`` names no ``HMCConfig`` field. Both readers of it —
    ``build_config``'s branch gate and ``load_profile``'s selection — read it
    directly, so there is no case-insensitive settings loader for them to
    disagree with, and nothing to reconcile.
    """
    _write_toml(
        profile_home,
        CASE_VARIANT_TOML + '\n[profiles.other]\nhost = "other-hmc.example.com"\n',
    )
    monkeypatch.setenv("hmc_profile", "other")

    assert build_config().host == "toml-hmc.example.com"


def test_env_var_value_resolves_multiple_casings_the_way_hmcconfig_does(monkeypatch):
    """The helper's whole purpose is agreement, so agreement is what gets pinned.

    Asserted against ``HMCConfig`` itself rather than against a reading of
    pydantic-settings' source: if a future release changes how it folds the
    environment, this reddens instead of the two silently drifting apart again.
    Both orderings are driven, because the rule is positional — the last
    matching key in ``os.environ`` order wins, and the exact spelling gets no
    precedence.
    """
    for key in list(os.environ):
        if key.upper().startswith("HMC_"):
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("HMC_HOST", "exact-first.example.com")
    monkeypatch.setenv("hmc_host", "variant-last.example.com")
    assert env_var_value("HMC_HOST") == HMCConfig(_env_file=None).host

    monkeypatch.delenv("HMC_HOST")
    monkeypatch.setenv("HMC_HOST", "exact-last.example.com")
    assert env_var_value("HMC_HOST") == HMCConfig(_env_file=None).host


@pytest.mark.parametrize(
    ("env_name", "field_name"),
    [
        # U+017F LATIN SMALL LETTER LONG S: .upper() is exactly "HMC_HOST", so
        # an upper-folding helper would claim the environment supplies a host
        # the loader never sees — and build_config's gate would skip the profile
        # for it while the config resolved to nothing.
        ("hmc_hoſt", "host"),
        # U+212A KELVIN SIGN: .lower() is exactly "hmc_ssh_key_file", so the
        # loader reads it while an upper-folding helper would not — the profile's
        # key would stay an init kwarg and outrank it.
        ("hmc_ssh_Key_file", "ssh_key_file"),
    ],
)
def test_env_var_value_folds_the_way_the_loader_folds(
    monkeypatch, env_name, field_name
):
    """``str.upper()`` and ``str.lower()`` are different relations over Unicode.

    pydantic-settings folds down (``_get_env_var_key`` is ``key.lower()``), so
    the helper does too. Either direction of divergence breaks the agreement
    this function exists to provide, and neither is visible in ASCII.
    """
    for key in list(os.environ):
        if key.lower().startswith("hmc_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(env_name, "/folded/value")

    supplied = env_var_value(f"HMC_{field_name.upper()}") is not None
    reached = getattr(HMCConfig(_env_file=None), field_name)

    assert supplied == bool(reached)


def test_an_empty_exact_case_host_does_not_hide_a_non_empty_variant(
    profile_home, monkeypatch
):
    """The fail-open a tie-break preferring the exact spelling would have left open.

    ``HMC_HOST=""`` beside a non-empty ``hmc_host`` — a Kubernetes ``value: ""``
    or a bare ``export HMC_HOST=`` next to a variant — resolves to the variant in
    ``HMCConfig``. If the gates read the empty exact spelling instead, they would
    treat the host as unset, ``selected_connection`` would resolve the caller's
    token to a profile key, and a grant naming that profile would authorize a
    call issued against the exported host (ADR 0038 rule 1).
    """
    monkeypatch.setenv("HMC_HOST", "")
    monkeypatch.setenv("hmc_host", "env-hmc.example.com")

    config = build_config(profile="guarded")

    assert config.host == "env-hmc.example.com"
    assert config.user == ""  # the profile was skipped, as an HMC_HOST export does


def test_an_empty_case_variant_blanks_the_profiles_value(profile_home, monkeypatch):
    """Presence, not truthiness: pydantic-settings supplies "" for an empty export.

    The profile's key must not be handed to the constructor when the environment
    supplies the field at all, whatever the value — an init kwarg would outrank
    the (empty) environment and reinstate the TOML value. Chosen, not inherited:
    the canonical ``HMC_HOST=""`` already blanked the field, and the resolution
    fails closed, with ``validate_credentials`` naming the missing host.

    ``build_config``'s branch gate deliberately reads the same variable for
    *truthiness* — an empty host is not a connection to collapse to — so an empty
    export loads the profile and then blanks the one field it named.
    """
    monkeypatch.setenv("hmc_user", "")

    config = build_config(profile="guarded")

    assert config.user == ""
    assert config.host == "toml-hmc.example.com"


def test_env_var_value_survives_a_concurrent_environment_mutation():
    """It replaced atomic ``os.environ.get`` calls, so it must not raise either.

    ``os.environ.items()`` re-indexes every key after ``__iter__`` snapshotted
    them, so a key deleted in between raises ``KeyError`` — out of
    ``selected_connection`` and ``connection_denial``, which sit on the ADR 0038
    dispatch-time authorization path and would surface it as a bare ``KeyError``
    past the machinery that exists to explain a refused call. ``hmcpctl`` is a
    supported reusable API (ADR 0029), so an embedding host mutating the
    environment from another thread is reachable.
    """
    stop = threading.Event()

    def churn() -> None:
        index = 0
        while not stop.is_set():
            os.environ[f"HMCTEST_CHURN_{index % 32}"] = "x"
            os.environ.pop(f"HMCTEST_CHURN_{(index + 7) % 32}", None)
            index += 1

    worker = threading.Thread(target=churn, daemon=True)
    worker.start()
    try:
        for _ in range(20_000):
            env_var_value("HMC_HOST")
    finally:
        stop.set()
        worker.join(timeout=5)
        for key in list(os.environ):
            if key.startswith("HMCTEST_CHURN_"):
                del os.environ[key]
