"""Tests for tool capability annotations and destructive-tool precondition guards.

The MCP server tags every tool with ToolAnnotations (readOnlyHint /
destructiveHint) so clients and gateways can gate on capability instead of
treating every tool equally. The classification lives on each tool's
ToolSecurity record; tests/app/test_tool_security.py holds the exhaustive
registry contract, and these tests cover annotation-adjacent schema stability
and the precondition guards on hmc_delete_lpar / hmc_delete_vios (refuse to
delete a partition that is not powered off), the pattern established by
hmc_remove_memory_pool.
"""

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from conftest import live_fixture, live_response, mock_system_summaries

from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
from hmcpctl.authorization.dispatch_scope import dispatch_authorizer
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.errors import HMCError
from hmcpctl.operations.affinity.rest import ProvisionAffinityAssessment
from hmcpctl.server import (
    TOOL_SECURITY,
    create_mcp,
)
from hmcpctl.server_tools.lpar.lifecycle import (
    hmc_decommission_lpar,
    hmc_delete_lpar,
)
from hmcpctl.server_tools.vios.core import hmc_delete_vios

# Composed here rather than imported: ADR 0041 removed the module-level application, so
# every consumer builds its own. The legacy-equivalent policy registers exactly the
# surface the unpolicied composition used to (pinned by G2 in
# tests/app/test_fail_closed_startup.py), and the dispatch wrapper is schema-transparent,
# so every assertion below reads the same registry it always did.
_LEGACY = compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,))
mcp = create_mcp(_LEGACY)

# The escape hatch's gates come from a policy that *grants* it. Since ADR 0041
# `configure_arbitrary_command_tool` requires both gates, and the flag and the grant
# compose conjunctively (ADR 0036) — so a toggle test asserting "enabled means
# registered" has to supply the grant as well as the flag, which is what an operator
# enabling it actually does.
_HATCH = compile_legacy_policy(
    TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,), include_arbitrary_command=True
)


LPAR_UUID = "aaaa0000-0000-0000-0000-000000000001"


def _hmc_env(monkeypatch) -> None:
    """Set env vars so HMCConfig() succeeds inside the tool."""
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")
    monkeypatch.setenv("HMC_VERIFY_SSL", "true")


# ------------------------------------------------------------------ #
# Tool capability classification
# ------------------------------------------------------------------ #


def _tools_by_name():
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def _missing_parameter_descriptions(schema: dict) -> list[str]:
    """Return object-property paths whose schema description is blank."""
    missing = []

    def walk(node: object, path: str, active_refs: frozenset[str]) -> None:
        if not isinstance(node, dict):
            return
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/"):
            if reference in active_refs:
                return
            target: object = schema
            for segment in reference[2:].split("/"):
                if not isinstance(target, dict):
                    return
                target = target.get(segment.replace("~1", "/").replace("~0", "~"))
            walk(target, path, active_refs | {reference})
        for name, property_schema in node.get("properties", {}).items():
            property_path = f"{path}.{name}" if path else name
            description = property_schema.get("description")
            if not isinstance(description, str) or not description.strip():
                missing.append(property_path)
            walk(property_schema, property_path, active_refs)
        for keyword in ("anyOf", "oneOf", "allOf"):
            for child in node.get(keyword, []):
                walk(child, path, active_refs)
        walk(node.get("items"), path, active_refs)

    walk(schema, "", frozenset())
    return missing


def test_schema_description_checker_rejects_top_level_and_nested_gaps():
    schema = {
        "properties": {
            "plain": {"type": "string"},
            "nested": {"description": "Nested input.", "$ref": "#/$defs/Nested"},
            "nested_again": {
                "description": "Second nested input.",
                "$ref": "#/$defs/Nested",
            },
            "recursive": {
                "description": "Recursive input.",
                "$ref": "#/$defs/Recursive",
            },
        },
        "$defs": {
            "Nested": {
                "type": "object",
                "properties": {"blank": {"type": "integer", "description": " "}},
            },
            "Recursive": {
                "type": "object",
                "properties": {
                    "next": {
                        "description": "Next node.",
                        "$ref": "#/$defs/Recursive",
                    }
                },
            },
            "Unused": {"properties": {"ignored": {"type": "string"}}},
        },
    }
    assert _missing_parameter_descriptions(schema) == [
        "plain",
        "nested.blank",
        "nested_again.blank",
    ]


@pytest.mark.parametrize("arbitrary_command_enabled", [False, True])
def test_every_registered_parameter_has_a_description(arbitrary_command_enabled):
    """Every exposed object property must carry useful rendered guidance."""
    from hmcpctl.server_tools import command as server_command

    try:
        asyncio.run(
            server_command.configure_arbitrary_command_tool(
                arbitrary_command_enabled,
                mcp,
                permits=_HATCH.permits_tool,
                authorize=dispatch_authorizer(_HATCH),
            )
        )
        missing = {
            tool.name: paths
            for tool in asyncio.run(mcp.list_tools())
            if (paths := _missing_parameter_descriptions(tool.parameters))
        }
        assert missing == {}
    finally:
        asyncio.run(
            server_command.configure_arbitrary_command_tool(
                False,
                mcp,
                permits=_HATCH.permits_tool,
                authorize=dispatch_authorizer(_HATCH),
            )
        )


def test_arbitrary_command_tool_is_disabled_by_default():
    assert "hmc_run_command" not in _tools_by_name()


def test_attach_disk_is_state_changing_not_destructive():
    assert TOOL_SECURITY["hmc_attach_disk_to_lpar"].effect == "mutate"
    annotations = _tools_by_name()["hmc_attach_disk_to_lpar"].annotations
    assert annotations is None or (
        annotations.read_only_hint is not True
        and annotations.destructive_hint is not True
    )


def test_fleet_health_is_read_only():
    assert TOOL_SECURITY["hmc_fleet_health"].effect == "read"
    annotations = _tools_by_name()["hmc_fleet_health"].annotations
    assert annotations is not None and annotations.read_only_hint is True


def test_arbitrary_command_tool_configuration_is_symmetric_and_idempotent():
    from hmcpctl.server_tools import command as server_command

    try:
        asyncio.run(
            server_command.configure_arbitrary_command_tool(
                False,
                mcp,
                permits=_HATCH.permits_tool,
                authorize=dispatch_authorizer(_HATCH),
            )
        )
        asyncio.run(
            server_command.configure_arbitrary_command_tool(
                False,
                mcp,
                permits=_HATCH.permits_tool,
                authorize=dispatch_authorizer(_HATCH),
            )
        )
        asyncio.run(
            server_command.configure_arbitrary_command_tool(
                True,
                mcp,
                permits=_HATCH.permits_tool,
                authorize=dispatch_authorizer(_HATCH),
            )
        )
        asyncio.run(
            server_command.configure_arbitrary_command_tool(
                True,
                mcp,
                permits=_HATCH.permits_tool,
                authorize=dispatch_authorizer(_HATCH),
            )
        )

        tools = [
            tool
            for tool in asyncio.run(mcp.list_tools())
            if tool.name == "hmc_run_command"
        ]
        assert len(tools) == 1
        assert tools[0].annotations is not None
        assert tools[0].annotations.read_only_hint is False
        asyncio.run(
            server_command.configure_arbitrary_command_tool(
                False,
                mcp,
                permits=_HATCH.permits_tool,
                authorize=dispatch_authorizer(_HATCH),
            )
        )
        asyncio.run(
            server_command.configure_arbitrary_command_tool(
                False,
                mcp,
                permits=_HATCH.permits_tool,
                authorize=dispatch_authorizer(_HATCH),
            )
        )
        assert "hmc_run_command" not in _tools_by_name()
    finally:
        asyncio.run(
            server_command.configure_arbitrary_command_tool(
                False,
                mcp,
                permits=_HATCH.permits_tool,
                authorize=dispatch_authorizer(_HATCH),
            )
        )


def test_closed_vocab_enum_matches_runtime_constant():
    """The MCP enum for closed-vocab params must not drift from the runtime set.

    The tool parameters are annotated with Literal aliases that share their
    values with the runtime constants (PARTITION_TYPES / _VALID_BACKUP_TYPES);
    adding a value must be a single edit. This pins the rendered schema to the
    constant so either side changing alone is caught.
    """
    from hmcpctl.client.client_contracts import (
        ADAPTER_TYPES,
        VALID_AUTHENTICATION_FILTERS,
    )
    from hmcpctl.documents.access import AUTHENTICATION_TYPES
    from hmcpctl.documents.lpar import PARTITION_TYPES, SHARING_MODES
    from hmcpctl.documents.storage import STORAGE_KINDS
    from hmcpctl.jobs import DEVICE_TYPES, LU_TYPES
    from hmcpctl.operations.vios.core import _VALID_BACKUP_TYPES
    from hmcpctl.ssh.io_inventory import _VALID_PCI_CLASSES
    from hmcpctl.ssh.sriov import _VALID_SRIOV_MODES

    by_name = _tools_by_name()

    partition_type = by_name["hmc_create_lpar"].parameters["properties"][
        "partition_type"
    ]
    assert set(partition_type["enum"]) == set(PARTITION_TYPES)
    assert partition_type["default"] in PARTITION_TYPES

    sharing_mode = by_name["hmc_create_lpar"].parameters["properties"]["resources"][
        "properties"
    ]["sharing_mode"]
    sharing_mode_enum = next(
        option["enum"] for option in sharing_mode["anyOf"] if "enum" in option
    )
    assert set(sharing_mode_enum) == set(SHARING_MODES)

    backup_type = by_name["hmc_backup_vios"].parameters["properties"]["backup_type"]
    assert set(backup_type["enum"]) == set(_VALID_BACKUP_TYPES)
    assert backup_type["default"] in _VALID_BACKUP_TYPES

    expected_enums = {
        ("hmc_create_user", "authentication_type"): AUTHENTICATION_TYPES,
        ("hmc_list_adapters", "adapter_type"): ADAPTER_TYPES,
        ("hmc_delete_adapter", "adapter_type"): ADAPTER_TYPES,
        ("hmc_map_storage_to_lpar", "storage_kind"): STORAGE_KINDS,
        ("hmc_create_logical_unit", "lu_type"): LU_TYPES,
        ("hmc_create_logical_unit", "device_type"): DEVICE_TYPES,
        ("hmc_list_users", "authentication_type"): VALID_AUTHENTICATION_FILTERS,
        ("hmc_set_sriov_adapter_mode", "mode"): _VALID_SRIOV_MODES,
        ("hmc_list_io_slots", "pci_class"): _VALID_PCI_CLASSES,
    }
    for (tool_name, parameter_name), values in expected_enums.items():
        parameter = by_name[tool_name].parameters["properties"][parameter_name]
        assert set(parameter["enum"]) == set(values)


def test_vios_backup_and_restore_schemas_pin_the_supported_contracts():
    """The public schemas expose every HMC input required by the replacement CLIs."""
    by_name = _tools_by_name()

    backup = by_name["hmc_backup_vios"].parameters
    assert set(backup["properties"]) == {
        "system_name_or_uuid",
        "vios_name_or_uuid",
        "backup_name",
        "backup_type",
        "profile",
    }
    assert set(backup["required"]) == {
        "system_name_or_uuid",
        "vios_name_or_uuid",
        "backup_name",
    }
    assert backup["properties"]["backup_type"]["default"] == "vios"

    restore = by_name["hmc_restore_vios"].parameters
    assert set(restore["properties"]) == {
        "system_name_or_uuid",
        "vios_name_or_uuid",
        "backup_name",
        "backup_type",
        "restart_if_required",
        "profile",
    }
    assert set(restore["required"]) == {
        "system_name_or_uuid",
        "vios_name_or_uuid",
        "backup_name",
        "backup_type",
    }
    assert restore["properties"]["backup_type"]["enum"] == ["viosioconfig", "ssp"]
    assert restore["properties"]["restart_if_required"]["default"] is False


def test_parameter_normalization_contract_is_schema_pinned():
    from hmcpctl.operations.lpar.core import PROCESSOR_COMPATIBILITY_MODES
    from hmcpctl.operations.metrics.pcm import PCM_CATEGORIES
    from hmcpctl.operations.partition_state import PARTITION_STATES

    by_name = _tools_by_name()
    replacements = {
        "hmc_install_vios": {"install_source", "system_name_or_uuid"},
        "hmc_attach_disk_to_lpar": {"capacity_mib"},
        "hmc_create_virtual_disk": {"capacity_mib"},
        "hmc_create_media_repository": {"size_mib"},
        "hmc_create_optical_media": {"size_mib"},
        "hmc_create_logical_unit": {"lu_size_gib"},
        "hmc_create_virtual_network": {"virtual_switch_id"},
        "hmc_add_vnic": {
            "vios_name",
            "vios_lpar_id",
            "adapter_id",
            "physical_port_id",
            "capacity_percent",
            "port_vlan_id",
        },
    }
    displaced = {
        "timeout",
        "capacity_mb",
        "size_mb",
        "lu_size_gb",
        "vswitch_id",
        "vswitch_name",
    }
    for tool_name, expected in replacements.items():
        properties = set(by_name[tool_name].parameters["properties"])
        assert expected <= properties
        assert not (properties & displaced)
    add_vnic = set(by_name["hmc_add_vnic"].parameters["properties"])
    remove_vnic = set(by_name["hmc_remove_vnic"].parameters["properties"])
    assert not add_vnic & {"backing_devices", "virtual_switch_name", "capacity"}
    assert "slot_num" in remove_vnic
    assert "vnic_id" not in remove_vnic
    for properties in (
        by_name["hmc_add_vnic"].parameters["properties"],
        by_name["hmc_remove_vnic"].parameters["properties"],
    ):
        assert properties["ownership_override"]["default"] is False
    properties = by_name["hmc_install_vios"].parameters["properties"]
    # ADR 0070: the REST job-polling surface is gone with the dead
    # InstallLPAR/InstallVIOS endpoints; the CLI bridge exposes no wait.
    assert not set(properties) & {
        "nim_ip",
        "wait",
        "wait_timeout_seconds",
        "poll_interval",
        "hmc_timeout_minutes",
    }
    assert properties["profile_name"]["default"] == "default"
    assert properties["vlan_id"]["default"] == "0"
    assert properties["mac_address"]["default"] is None

    enum_contracts = {
        ("hmc_list_lpars", "state"): PARTITION_STATES,
        ("hmc_list_vios", "state"): PARTITION_STATES,
        ("hmc_set_lpar_proc_compat", "mode"): PROCESSOR_COMPATIBILITY_MODES,
    }
    pcm_tools = (
        "hmc_get_pcm_preferences",
        "hmc_set_pcm_preferences",
        "hmc_processed_metrics",
        "hmc_processed_metric_links",
        "hmc_aggregated_metrics",
        "hmc_aggregated_metric_links",
    )
    enum_contracts.update(
        {(tool_name, "category"): PCM_CATEGORIES for tool_name in pcm_tools}
    )
    for key, values in enum_contracts.items():
        tool_name, parameter_name = key
        parameter = by_name[tool_name].parameters["properties"][parameter_name]
        enum = next(
            (
                option["enum"]
                for option in parameter.get("anyOf", [])
                if "enum" in option
            ),
            parameter.get("enum"),
        )
        assert set(enum) == set(values)


def test_destructive_partition_tools_expose_system_scope():
    by_name = _tools_by_name()

    for tool_name in (
        "hmc_decommission_lpar",
        "hmc_power_off_lpar",
        "hmc_dump_restart_lpar",
        "hmc_delete_vios",
        "hmc_restore_vios",
        "hmc_power_off_vios",
    ):
        properties = by_name[tool_name].parameters["properties"]
        assert "system_name_or_uuid" in properties


def test_decommission_lpar_is_public_destructive_and_schema_stable():
    assert callable(hmc_decommission_lpar)

    tool = _tools_by_name()["hmc_decommission_lpar"]
    assert TOOL_SECURITY["hmc_decommission_lpar"].effect == "destructive"
    assert tool.annotations is not None and tool.annotations.destructive_hint is True
    assert tool.annotations.read_only_hint is not True

    properties = tool.parameters["properties"]
    assert set(properties) == {
        "system_name_or_uuid",
        "lpar_name_or_uuid",
        "dry_run",
        "ownership_override",
        "immediate",
        "timeout_seconds",
        "poll_interval",
        "profile",
    }
    assert properties["dry_run"]["default"] is False
    assert properties["ownership_override"]["default"] is False
    assert properties["immediate"]["default"] is False
    assert properties["timeout_seconds"]["default"] == 300
    assert properties["poll_interval"]["default"] == 5

    schema = tool.output_schema
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {
        "resource_deleted",
        "workflow_completed",
        "lpar_uuid",
        "dry_run",
        "steps",
        "warnings",
        "blast_radius",
    }
    assert set(schema["required"]) == set(schema["properties"])


def test_partition_creation_tools_share_resource_object_schema():
    by_name = _tools_by_name()
    lpar_properties = by_name["hmc_create_lpar"].parameters["properties"]
    vios_properties = by_name["hmc_create_vios"].parameters["properties"]

    assert "resources" in lpar_properties
    assert "resources" in vios_properties
    for legacy_name in (
        "min_memory",
        "desired_memory",
        "max_memory",
        "min_vcpus",
        "desired_vcpus",
        "max_vcpus",
        "min_procs",
        "desired_procs",
        "max_procs",
    ):
        assert legacy_name not in vios_properties


def test_password_policy_tools_are_absent_without_a_documented_target():
    by_name = _tools_by_name()
    for tool_name in ("hmc_create_password_policy", "hmc_modify_password_policy"):
        assert tool_name not in by_name


def test_update_source_enums_match_runtime_constants():
    """Rendered console and VIOS source enums must not drift from runtime.

    Each public tool schema is pinned to the corresponding runtime Literal.
    """
    from hmcpctl.operations.updates.models import (
        _CONSOLE_UPDATE_MEDIA_TYPES,
        _VIOS_UPDATE_RESOURCE_TYPES,
        _VIOS_UPGRADE_RESOURCE_TYPES,
    )

    by_name = _tools_by_name()

    media_type = by_name["hmc_update_console_software"].parameters["properties"][
        "repository"
    ]["properties"]["MediaType"]
    assert set(media_type["enum"]) == set(_CONSOLE_UPDATE_MEDIA_TYPES)
    repository = by_name["hmc_update_console_software"].parameters["properties"][
        "repository"
    ]
    assert repository["required"] == ["MediaType"]

    update_sources = by_name["hmc_vios_update"].parameters["properties"]["repository"][
        "anyOf"
    ]
    upgrade_sources = by_name["hmc_vios_upgrade"].parameters["properties"][
        "repository"
    ]["anyOf"]
    assert {
        source["properties"]["ResourceType"]["const"] for source in update_sources
    } == set(_VIOS_UPDATE_RESOURCE_TYPES)
    assert {
        source["properties"]["ResourceType"]["const"] for source in upgrade_sources
    } == set(_VIOS_UPGRADE_RESOURCE_TYPES)
    assert all(
        "ResourceType" in source["required"]
        for source in (*update_sources, *upgrade_sources)
    )
    assert all(
        property_schema.get("description")
        for source in (*update_sources, *upgrade_sources)
        for property_schema in source["properties"].values()
    )
    update_required = {
        source["properties"]["ResourceType"]["const"]: set(source["required"])
        for source in update_sources
    }
    upgrade_required = {
        source["properties"]["ResourceType"]["const"]: set(source["required"])
        for source in upgrade_sources
    }
    assert update_required["HMC"] == {"ResourceType", "Name"}
    assert update_required["NFS"] == {
        "ResourceType",
        "ServerHostOrIP",
        "RemoteDirectory",
    }
    assert update_required["USB"] == {"ResourceType", "USBDevice"}
    assert all("Disks" in required for required in upgrade_required.values())
    assert all("Disks" not in source["properties"] for source in update_sources)
    assert all("RestartVIOS" not in source["properties"] for source in upgrade_sources)


def test_metrics_tools_have_stable_output_schemas():
    """Metric fetch and discovery tools expose distinct stable schemas."""
    by_name = _tools_by_name()
    for tool_name in (
        "hmc_processed_metrics",
        "hmc_aggregated_metrics",
        "hmc_processed_metric_links",
        "hmc_aggregated_metric_links",
    ):
        tool = by_name[tool_name]
        assert (
            tool.annotations is not None and tool.annotations.read_only_hint is True
        ), tool_name
        assert "mode" not in tool.parameters.get("properties", {})


@pytest.mark.parametrize(
    "tool_name", ["hmc_get_pcm_preferences", "hmc_set_pcm_preferences"]
)
def test_pcm_preference_tools_describe_managed_system_only(tool_name):
    tool = _tools_by_name()[tool_name]
    category = tool.parameters["properties"]["category"]

    assert "ManagedSystem" in tool.description
    assert "ManagedSystem" in category["description"]
    assert "LogicalPartition" not in category["description"]


def test_wait_for_job_has_one_stable_output_schema():
    by_name = _tools_by_name()
    schema = by_name["hmc_wait_for_job"].output_schema

    assert schema["type"] == "object"
    assert set(schema["properties"]) == {
        "job_id",
        "status",
        "timed_out",
        "error",
        "job",
        "found",
        "job_href",
    }
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["job_id"] == {"type": "string"}
    assert schema["properties"]["timed_out"] == {"type": "boolean"}
    assert schema["properties"]["found"] == {"type": "boolean"}
    for nullable_field in ("status", "error", "job", "job_href"):
        assert {
            variant["type"] for variant in schema["properties"][nullable_field]["anyOf"]
        } == {
            "object" if nullable_field == "job" else "string",
            "null",
        }

    assert by_name["hmc_get_job"].output_schema == {
        "properties": {
            "result": {
                "anyOf": [
                    {"additionalProperties": True, "type": "object"},
                    {"type": "null"},
                ]
            }
        },
        "required": ["result"],
        "type": "object",
        "x-fastmcp-wrap-result": True,
    }


def test_lpm_recovery_tools_have_standard_wait_contract():
    by_name = _tools_by_name()

    for tool_name in (
        "hmc_migrate_abort_lpar",
        "hmc_migrate_recover_lpar",
        "hmc_remote_restart_lpar",
    ):
        tool = by_name[tool_name]
        properties = tool.parameters["properties"]
        assert properties["wait"]["default"] is False
        assert properties["timeout_seconds"]["default"] == 300
        assert properties["poll_interval"]["default"] == 5
        assert set(tool.output_schema["properties"]) == {
            "job_id",
            "status",
            "timed_out",
            "error",
            "job",
            "found",
            "job_href",
        }
        assert set(tool.output_schema["required"]) == set(
            tool.output_schema["properties"]
        )


# ------------------------------------------------------------------ #
# Delete precondition guards (hmc_delete_lpar / hmc_delete_vios)
# ------------------------------------------------------------------ #


def _mock_state_and_delete(router, state: str, status: int = 200):
    router.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/quick/PartitionState").mock(
        return_value=httpx.Response(status, text=f'"{state}"')
    )
    return router.delete(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(204)
    )


def test_delete_lpar_refuses_when_active(monkeypatch, mock_hmc):
    """Deleting a running LPAR is refused before any DELETE is issued."""
    _hmc_env(monkeypatch)
    delete_route = _mock_state_and_delete(mock_hmc, "running")

    with (
        patch(
            "hmcpctl.operations.lpar.core.resolve_and_authorize_lpar_mutation",
            new=AsyncMock(return_value=LPAR_UUID),
        ) as guard,
        pytest.raises(HMCError) as exc_info,
    ):
        hmc_delete_lpar(SYSTEM_UUID, LPAR_UUID, ownership_override=True)

    assert exc_info.value.status_code == 409
    assert "not activated" in str(exc_info.value)
    assert not delete_route.called
    assert guard.await_args.kwargs == {"ownership_override": True}


def test_delete_lpar_succeeds_when_powered_off(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    _mock_state_and_delete(mock_hmc, "not activated")

    with patch(
        "hmcpctl.operations.lpar.core.resolve_and_authorize_lpar_mutation",
        new=AsyncMock(return_value=LPAR_UUID),
    ) as guard:
        result = hmc_delete_lpar(SYSTEM_UUID, LPAR_UUID, ownership_override=True)

    assert result == f"Deleted LPAR {LPAR_UUID}"
    assert guard.await_args.kwargs == {"ownership_override": True}


VIOS_UUID = "00000005-ABCD-4EF0-8ABC-000000000005"


def _mock_vios_state_and_delete(router, state: str):
    """V10R3 answers a VIOS only under VirtualIOServer (#1202).

    The LogicalPartition entry and quick paths 404 for a VIOS UUID; the
    VirtualIOServer quick read answers 200 with the state JSON-quoted.
    """
    for name in ("rest-lpar-path-vios", "rest-lpar-quick-vios"):
        path, response = live_response(name)
        router.get(path).mock(return_value=response)
    quick = live_fixture("rest-vios-quick-state")
    router.get(quick["path"]).mock(
        return_value=httpx.Response(
            quick["status"],
            text=f'"{state}"',
            headers={"Content-Type": quick["content_type"]},
        )
    )
    return router.delete(f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}").mock(
        return_value=httpx.Response(204)
    )


def test_delete_vios_refuses_when_active(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    delete_route = _mock_vios_state_and_delete(mock_hmc, "running")

    with pytest.raises(HMCError) as exc_info:
        hmc_delete_vios(VIOS_UUID)

    assert exc_info.value.status_code == 409
    assert "'running'" in str(exc_info.value)
    assert not delete_route.called


def test_delete_vios_succeeds_when_powered_off(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    delete_route = _mock_vios_state_and_delete(mock_hmc, "not activated")

    result = hmc_delete_vios(VIOS_UUID)

    assert result == VIOS_UUID
    assert delete_route.call_count == 1


# ------------------------------------------------------------------ #
# hmc_power_on_lpar precondition guard
# ------------------------------------------------------------------ #

POWER_ON_JOB_ENTRY = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:job-uuid-power-on</id>
  <title>Job</title>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <Job xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <JobID>job-uuid-power-on</JobID>
      <Status>RUNNING</Status>
    </Job>
  </content>
</entry>
"""


def _mock_power_on_guard(router, state: str):
    router.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/quick/PartitionState").mock(
        return_value=httpx.Response(200, text=f'"{state}"')
    )
    return router.put(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/do/PowerOn").mock(
        return_value=httpx.Response(202, text=POWER_ON_JOB_ENTRY)
    )


def _affinity_request() -> ProvisionAffinityAssessment:
    return ProvisionAffinityAssessment(
        system_name_or_uuid=SYSTEM_UUID,
        lpar_name=LPAR_UUID,
        captured_score=80,
        captured_policy_state="absent",
        captured_minimum=None,
        captured_at=datetime.now(UTC),
        stale_after_seconds=300,
        response="warn",
        regression_threshold=5,
        optimization_threshold=5,
    )


def test_power_on_lpar_already_running_returns_message(monkeypatch, mock_hmc):
    """The already-running path returns the stable PowerOn outcome."""
    from hmcpctl.server_tools.lpar.lifecycle import (
        hmc_power_on_lpar,
    )

    _hmc_env(monkeypatch)
    power_on_route = _mock_power_on_guard(mock_hmc, "running")

    result = hmc_power_on_lpar(LPAR_UUID)

    assert not power_on_route.called
    assert set(asdict(result)) == {
        "already_running",
        "job",
        "message",
        "affinity_assessment",
        "warnings",
    }
    assert result.already_running is True
    assert result.job is None
    assert isinstance(result.message, str)
    assert LPAR_UUID in result.message
    assert result.affinity_assessment.status == "skipped"
    assert result.affinity_assessment.measured is False


def test_power_on_lpar_not_activated_submits_job(monkeypatch, mock_hmc):
    """hmc_power_on_lpar submits the PowerOn job when partition is not activated."""
    from hmcpctl.server_tools.lpar.lifecycle import (
        hmc_power_on_lpar,
    )

    _hmc_env(monkeypatch)
    power_on_route = _mock_power_on_guard(mock_hmc, "not activated")

    result = hmc_power_on_lpar(LPAR_UUID)

    assert power_on_route.called
    assert set(asdict(result)) == {
        "already_running",
        "job",
        "message",
        "affinity_assessment",
        "warnings",
    }
    assert result.already_running is False
    assert result.job["Resource"]["JobID"] == "job-uuid-power-on"
    assert result.message is None
    assert result.affinity_assessment.status == "skipped"


def test_power_on_lpar_has_one_stable_output_schema():
    schema = _tools_by_name()["hmc_power_on_lpar"].output_schema

    assert schema["type"] == "object"
    assert set(schema["properties"]) == {
        "already_running",
        "job",
        "message",
        "affinity_assessment",
        "warnings",
    }
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["already_running"] == {"type": "boolean"}
    assert {variant["type"] for variant in schema["properties"]["job"]["anyOf"]} == {
        "object",
        "null",
    }
    assert {
        variant["type"] for variant in schema["properties"]["message"]["anyOf"]
    } == {"string", "null"}
    assert schema["properties"]["warnings"]["type"] == "array"


def test_power_on_lpar_force_skips_guard(monkeypatch, mock_hmc):
    """hmc_power_on_lpar(force=True) submits the job even when running."""
    from hmcpctl.server_tools.lpar.lifecycle import (
        hmc_power_on_lpar,
    )

    _hmc_env(monkeypatch)
    # When force=True the state check endpoint is not called; only the job PUT matters.
    power_on_route = mock_hmc.put(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/do/PowerOn"
    ).mock(return_value=httpx.Response(202, text=POWER_ON_JOB_ENTRY))

    result = hmc_power_on_lpar(LPAR_UUID, force=True)

    assert power_on_route.called
    assert result.already_running is False
    assert result.job["Resource"]["JobID"] == "job-uuid-power-on"
    assert result.message is None


def test_power_on_lpar_non_waiting_assessment_does_not_measure(monkeypatch, mock_hmc):
    """Opt-in assessment preserves non-waiting submission semantics."""
    from hmcpctl.server_tools.lpar.lifecycle import (
        hmc_power_on_lpar,
    )

    _hmc_env(monkeypatch)
    _mock_power_on_guard(mock_hmc, "not activated")
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(200, text=EXISTING_LPAR_FEED)
    )

    result = hmc_power_on_lpar(
        LPAR_UUID,
        system_name_or_uuid=SYSTEM_UUID,
        affinity_assessment=_affinity_request(),
    )

    assert result.job["Resource"]["JobID"] == "job-uuid-power-on"
    assert result.affinity_assessment.measured is False
    assert result.affinity_assessment.status == "skipped"
    assert "wait=true" in result.affinity_assessment.reason


def test_power_on_lpar_already_running_assessment_does_not_measure(
    monkeypatch, mock_hmc
):
    """Already running is not an activation observed by this call."""
    from hmcpctl.server_tools.lpar.lifecycle import (
        hmc_power_on_lpar,
    )

    _hmc_env(monkeypatch)
    _mock_power_on_guard(mock_hmc, "running")
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(200, text=EXISTING_LPAR_FEED)
    )

    result = hmc_power_on_lpar(
        LPAR_UUID,
        wait=True,
        system_name_or_uuid=SYSTEM_UUID,
        affinity_assessment=_affinity_request(),
    )

    assert result.already_running is True
    assert result.affinity_assessment.measured is False
    assert result.affinity_assessment.status == "skipped"
    assert "already activated" in result.affinity_assessment.reason


# ------------------------------------------------------------------ #
# hmc_create_lpar name-collision guard
# ------------------------------------------------------------------ #

EXISTING_LPAR_FEED = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:{LPAR_UUID}</id>
    <title>LogicalPartition</title>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <PartitionName>existing-lpar</PartitionName>
        <PartitionState>not activated</PartitionState>
      </LogicalPartition>
    </content>
  </entry>
</feed>
"""

EMPTY_FEED = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom"/>
"""

SYSTEM_UUID = "00000000-0000-0000-0000-000000000001"

SYSTEM_ENTRY = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:{SYSTEM_UUID}</id>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <ManagedSystem xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <SystemName>sys1</SystemName>
      <State>operating</State>
    </ManagedSystem>
  </content>
</entry>
"""

NEW_LPAR_FEED = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:new-lpar-uuid-0001</id>
    <title>LogicalPartition</title>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <PartitionName>new-lpar</PartitionName>
      </LogicalPartition>
    </content>
  </entry>
</feed>
"""


def test_create_lpar_refuses_name_collision(monkeypatch, mock_hmc):
    """hmc_create_lpar raises ValueError when a partition with the same name exists."""
    from hmcpctl.server_tools.lpar.lifecycle_create import hmc_create_lpar

    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(200, text=EXISTING_LPAR_FEED)
    )
    create_route = mock_hmc.put(
        f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition"
    )

    with pytest.raises(ValueError, match="existing-lpar"):
        hmc_create_lpar(SYSTEM_UUID, name="existing-lpar")

    assert not create_route.called


def test_create_lpar_proceeds_when_no_collision(monkeypatch, mock_hmc):
    """hmc_create_lpar creates the partition when no LPAR with the same name exists."""
    from unittest.mock import AsyncMock, patch

    from hmcpctl.server_tools.lpar.lifecycle_create import hmc_create_lpar

    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(200, text=EMPTY_FEED)
    )
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}").mock(
        return_value=httpx.Response(200, text=SYSTEM_ENTRY)
    )
    mock_system_summaries(mock_hmc, (SYSTEM_UUID, "sys1", "operating"))
    create_route = mock_hmc.put(
        f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition"
    ).mock(return_value=httpx.Response(201, text=NEW_LPAR_FEED))

    with (
        patch(
            "hmcpctl.operations.lpar.ownership.stamp_lpar_ownership",
            new=AsyncMock(return_value="tok"),
        ),
        patch(
            "hmcpctl.operations.lpar.ownership._resolve_system_name",
            new=AsyncMock(return_value="sys1"),
        ),
    ):
        result = hmc_create_lpar(SYSTEM_UUID, name="new-lpar")

    assert create_route.called
    # result is now wrapped: {"lpar": <entry>, "ownership_stamped": ..., "warnings": []}
    assert result.lpar["Resource"]["PartitionName"] == "new-lpar"
