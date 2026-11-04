"""Public user-tool documentation contracts."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.users import core as server_users


def _client_context(client: MagicMock) -> MagicMock:
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


def test_nullable_user_mutations_document_empty_responses() -> None:
    nullable_mutations = (
        server_users.hmc_create_user,
        server_users.hmc_modify_user,
        server_users.hmc_configure_remote_access,
    )

    for handler in nullable_mutations:
        assert handler.__doc__ is not None
        normalized_doc = " ".join(handler.__doc__.split())
        assert "None" in normalized_doc or "partial" in normalized_doc


def test_create_user_tool_forwards_identifiers_and_optional_fields() -> None:
    client = MagicMock()
    context = _client_context(client)
    operation = AsyncMock(return_value={"Resource": {"UserID": "alice"}})
    resource_roles = ["/roles/operators", "/roles/storage"]

    with (
        patch("hmcpctl._app.client_from_env", return_value=context) as factory,
        patch.object(server_users, "create_user", operation),
    ):
        result = server_users.hmc_create_user(
            "console-1",
            "alice",
            "secret",
            "LDAP",
            description="database operator",
            associated_task_role="/roles/task-operator",
            associated_resource_roles=resource_roles,
            password_expiry=30,
            session_timeout=60,
            verify_session_timeout=15,
            idle_session_timeout=15,
            user_inactivity=90,
            minimum_password_age=2,
            allow_web_remote_access=True,
            allow_ssh_remote_access=False,
            remote_user_id="directory-alice",
            profile="lab",
        )

    factory.assert_called_once_with("lab")
    operation.assert_awaited_once_with(
        client,
        "console-1",
        server_users.CreateUserRequest(
            user_id="alice",
            password="secret",  # pragma: allowlist secret -- synthetic fixture
            authentication_type="LDAP",
            description="database operator",
            associated_task_role="/roles/task-operator",
            associated_resource_roles=resource_roles,
            password_expiry=30,
            session_timeout=60,
            verify_session_timeout=15,
            idle_session_timeout=15,
            user_inactivity=90,
            minimum_password_age=2,
            allow_web_remote_access=True,
            allow_ssh_remote_access=False,
            remote_user_id="directory-alice",
        ),
    )
    context.__aexit__.assert_awaited_once()
    assert result == {"Resource": {"UserID": "alice"}}


def test_modify_user_tool_preserves_explicit_clear_values() -> None:
    client = MagicMock()
    context = _client_context(client)
    operation = AsyncMock(return_value=None)

    with (
        patch("hmcpctl._app.client_from_env", return_value=context),
        patch.object(server_users, "modify_user", operation),
    ):
        result = server_users.hmc_modify_user(
            "console-1",
            "profile-1",
            password="replacement",  # pragma: allowlist secret -- synthetic fixture
            description="",
            authentication_type="Kerberos",
            associated_task_role="",
            associated_resource_roles=[],
            password_expiry=0,
            session_timeout=0,
            verify_session_timeout=0,
            idle_session_timeout=0,
            user_inactivity=0,
            minimum_password_age=0,
            allow_web_remote_access=False,
            allow_ssh_remote_access=False,
            remote_user_id="",
        )

    operation.assert_awaited_once_with(
        client,
        "console-1",
        "profile-1",
        server_users.ModifyUserPatch(
            authentication_type="Kerberos",
            password="replacement",  # pragma: allowlist secret -- synthetic fixture
            description="",
            associated_task_role="",
            associated_resource_roles=[],
            password_expiry=0,
            session_timeout=0,
            verify_session_timeout=0,
            idle_session_timeout=0,
            user_inactivity=0,
            minimum_password_age=0,
            allow_web_remote_access=False,
            allow_ssh_remote_access=False,
            remote_user_id="",
        ),
    )
    context.__aexit__.assert_awaited_once()
    assert result is None


def test_delete_user_tool_returns_identified_confirmation() -> None:
    client = MagicMock()
    client.delete_hmc_user = AsyncMock(return_value=None)
    context = _client_context(client)

    with patch("hmcpctl._app.client_from_env", return_value=context):
        result = server_users.hmc_delete_user("console-1", "profile-1", profile="lab")

    client.delete_hmc_user.assert_awaited_once_with("console-1", "profile-1")
    context.__aexit__.assert_awaited_once()
    assert result == "Deleted HMC user profile profile-1"


@pytest.mark.parametrize(
    ("values", "clear_fields"),
    [
        ({"LdapEnabled": True, "LdapServer": "ldap.example.test"}, None),
        (None, ["LdapServer", "KerberosRealm"]),
        ({"LdapEnabled": False}, ["LdapServer"]),
    ],
)
def test_remote_access_tool_preserves_value_and_clear_semantics(
    values: dict[str, str | int | bool] | None,
    clear_fields: list[str] | None,
) -> None:
    client = MagicMock()
    client.configure_remote_access = AsyncMock(
        return_value={"Resource": {"LdapEnabled": False}}
    )
    context = _client_context(client)

    with patch("hmcpctl._app.client_from_env", return_value=context) as factory:
        result = server_users.hmc_configure_remote_access(
            "console-1", values, clear_fields, profile="security"
        )

    factory.assert_called_once_with("security")
    client.configure_remote_access.assert_awaited_once_with(
        "console-1", values, clear_fields
    )
    context.__aexit__.assert_awaited_once()
    assert result == {"Resource": {"LdapEnabled": False}}


@pytest.mark.parametrize(
    "tool_name, expected",
    [
        ("hmc_list_users", ("console", "console_uuid")),
        ("hmc_create_user", ("user", "user_id")),
        ("hmc_get_user", ("user", "user_profile_uuid")),
        ("hmc_modify_user", ("user", "user_profile_uuid")),
        ("hmc_delete_user", ("user", "user_profile_uuid")),
    ],
)
def test_user_tools_name_their_targets(tool_name, expected) -> None:
    """Each user tool's ADR 0039 grant target is the identity it acts on (#632)."""
    from hmcpctl.server import TOOL_SECURITY

    built = {
        (target.kind, target.argument) for target in TOOL_SECURITY[tool_name].targets
    }
    assert expected in built


_TIMEOUT_CALLS = {
    "hmc_create_user": {"console_uuid": "c", "user_id": "u", "password": "p"},
    "hmc_modify_user": {"console_uuid": "c", "user_profile_uuid": "u"},
}


@pytest.mark.parametrize("tool", sorted(_TIMEOUT_CALLS))
@pytest.mark.parametrize("value", [True, False, -1])
def test_verify_session_timeout_is_refused_at_the_mcp_boundary(tool, value) -> None:
    """FastMCP validates in lax mode, so JSON ``true`` would reach the HMC as 1
    minute; a boolean or negative value is refused before a client opens (#1381)."""
    policy = compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,))

    async def call() -> dict:
        async with Client(create_mcp(policy, catalog="registered")) as client:
            tools = {item.name: item for item in await client.list_tools()}
            with pytest.raises(ToolError, match="verify_session_timeout"):
                await client.call_tool(
                    tool, {**_TIMEOUT_CALLS[tool], "verify_session_timeout": value}
                )
            return tools[tool].input_schema["properties"]["verify_session_timeout"]

    with patch(
        "hmcpctl._app.client_from_env", side_effect=AssertionError("client opened")
    ):
        schema = asyncio.run(call())

    assert {"type": "integer", "minimum": 0} in schema["anyOf"]
