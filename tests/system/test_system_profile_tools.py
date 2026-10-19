"""Exact system-profile MCP consumers and whole-system authorization metadata."""

import asyncio
from unittest.mock import AsyncMock

from hmcpctl.server_tools.systems import profiles


def test_profiles_tool_contract(monkeypatch):
    call = AsyncMock(return_value="outcome")
    monkeypatch.setattr(profiles, "create_system_profile", call)
    monkeypatch.setattr(
        profiles, "with_client", lambda f, profile=None: asyncio.run(f("hmc"))
    )
    pair = profiles.SystemProfilePair(partition="member", profile="contained")
    assert (
        profiles.hmc_create_system_profile(
            "system", "name", [pair], exclusive_writer_window=True
        )
        == "outcome"
    )
    assert call.await_args.args == ("hmc", "system", "name", [pair])
    assert call.await_args.kwargs["exclusive_writer_window"] is True


def test_profiles_all_targets():
    from hmcpctl.server_tools.catalog import TOOL_MODULES

    assert profiles in TOOL_MODULES
    metadata = profiles.tool_security()
    assert len(metadata) == 5
    assert metadata["hmc_list_system_profiles"].effect == "read"
    for name, value in metadata.items():
        if name != "hmc_list_system_profiles":
            assert value.exhaustive_targets is False


def test_profiles_target_table_cannot_grant_mutations():
    import pytest

    from hmcpctl.authorization.access_policy import (
        AccessPolicyError,
        compile_access_policy,
    )
    from hmcpctl.server_tools.catalog import TOOL_SECURITY

    for tool in (
        "hmc_create_system_profile",
        "hmc_update_system_profile",
        "hmc_delete_system_profile",
        "hmc_activate_system_profile",
    ):
        with pytest.raises(AccessPolicyError, match="all-targets"):
            compile_access_policy(
                {
                    "policies": {
                        "test": {
                            "grants": [
                                {
                                    "tools": [tool],
                                    "connections": ["connection"],
                                    "targets": {"managed_system": ["system"]},
                                }
                            ]
                        }
                    }
                },
                "test",
                TOOL_SECURITY,
                "test.toml",
            )
