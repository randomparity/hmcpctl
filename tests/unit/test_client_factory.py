"""Recorded connection selection preserves profile and environment routing."""

from unittest.mock import Mock, sentinel

import pytest

from hmcpctl.client import client_factory


@pytest.mark.parametrize(
    ("connection", "profile"),
    [("<default>", None), ("profile-a", "profile-a"), ("", "")],
)
def test_recorded_connection_translates_only_the_default_token(
    monkeypatch, connection, profile
):
    make = Mock(return_value=sentinel.client)
    monkeypatch.setattr(client_factory, "client_from_env", make)
    assert client_factory.open_client(connection) is sentinel.client
    make.assert_called_once_with(profile)
