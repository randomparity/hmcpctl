"""Recorded connection selection preserves profile and environment routing."""

from types import SimpleNamespace
from unittest.mock import Mock, sentinel

import pytest

from hmcpctl.client import client_factory
from hmcpctl.config import build_config


@pytest.mark.parametrize(
    ("connection", "profile"),
    [("<default>", None), ("profile-a", "profile-a"), ("", "")],
)
def test_recorded_connection_translates_only_the_default_token(
    monkeypatch, connection, profile
):
    config = SimpleNamespace(host="hmc.test")
    resolve = Mock(return_value=config)
    make = Mock(return_value=sentinel.client)
    monkeypatch.setattr(client_factory, "build_config", resolve)
    monkeypatch.setattr(client_factory, "HMCClient", make)
    assert client_factory.open_client(connection, "hmc.test") is sentinel.client
    resolve.assert_called_once_with(profile=profile)
    make.assert_called_once_with(config)


@pytest.mark.parametrize("connection", ["recorded", "nickname", "<default>"])
def test_edited_profile_is_refused_before_constructing_a_client(
    tmp_path, monkeypatch, connection
):
    path = tmp_path / "config.toml"

    def profile(host):
        path.write_text(
            "default_profile = 'recorded'\n"
            "[profiles.recorded]\n"
            f"host = '{host}'\n"
            "[nicknames]\n"
            "nickname = 'recorded'\n"
        )

    monkeypatch.setattr("hmcpctl.config.resolve_config_path", lambda: path)
    profile("admitted.test")
    admitted = build_config(profile=None if connection == "<default>" else connection)
    profile("changed.test")
    make = Mock()
    monkeypatch.setattr(client_factory, "HMCClient", make)
    with pytest.raises(ValueError, match="admitted host") as error:
        client_factory.open_client(connection, admitted.host)
    make.assert_not_called()
    assert "admitted.test" not in str(error.value)
    assert "changed.test" not in str(error.value)
