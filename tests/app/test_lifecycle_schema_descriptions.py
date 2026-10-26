"""Rendered-schema contracts for the core lifecycle tools."""

from __future__ import annotations

import asyncio
import re

from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.server import TOOL_SECURITY, create_mcp

# Composed here rather than imported: ADR 0041 removed the module-level application, so
# every consumer builds its own. The legacy-equivalent policy registers exactly the
# surface the unpolicied composition used to (pinned by G2 in
# tests/app/test_fail_closed_startup.py), and the dispatch wrapper is schema-transparent,
# so every assertion below reads the same registry it always did.
mcp = create_mcp(compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,)))


SCOPED_TOOLS = {
    "hmc_get_console_info",
    "hmc_list_configured_hosts",
    "hmc_list_systems",
    "hmc_list_lpars",
    "hmc_get_lpar",
    "hmc_get_lpar_state",
    "hmc_list_vios",
    "hmc_get_vios_storage_detail",
    "hmc_list_resources",
    "hmc_get_system",
    "hmc_modify_system",
    "hmc_power_on_system",
    "hmc_power_off_system",
    "hmc_create_lpar",
    "hmc_modify_lpar",
    "hmc_rename_lpar",
    "hmc_dlpar_proc",
    "hmc_dlpar_mem",
    "hmc_delete_lpar",
    "hmc_power_on_lpar",
    "hmc_power_off_lpar",
    "hmc_dump_restart_lpar",
    "hmc_decommission_lpar",
    "hmc_create_vios",
    "hmc_delete_vios",
    "hmc_install_vios",
    "hmc_list_vios_backups",
    "hmc_backup_vios",
    "hmc_restore_vios",
    "hmc_power_on_vios",
    "hmc_power_off_vios",
    "hmc_get_job",
    "hmc_wait_for_job",
    "hmc_lpar_summary",
    "hmc_system_summary",
    "hmc_provision_lpar",
    "hmc_capacity_report",
    "hmc_find_placement",
    "hmc_migrate_lpar",
    "hmc_migrate_validate_lpar",
    "hmc_migrate_abort_lpar",
    "hmc_migrate_recover_lpar",
    "hmc_remote_restart_lpar",
}


def _tools_by_name():
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


def test_core_lifecycle_parameters_have_rendered_descriptions():
    tools = _tools_by_name()
    assert SCOPED_TOOLS <= tools.keys()
    missing = {
        (name, parameter)
        for name in SCOPED_TOOLS
        for parameter, schema in tools[name].parameters.get("properties", {}).items()
        if not schema.get("description", "").strip()
    }
    assert missing == set()


def test_nested_lifecycle_parameters_have_rendered_descriptions():
    tools = _tools_by_name()
    nested = {
        "resources": tools["hmc_create_lpar"].parameters["properties"]["resources"],
        "adapters": tools["hmc_provision_lpar"].parameters["properties"]["adapters"],
        "storage": tools["hmc_provision_lpar"].parameters["properties"]["storage"],
    }
    missing = {
        (group, field)
        for group, schema in nested.items()
        for field, field_schema in schema["properties"].items()
        if not field_schema.get("description", "").strip()
    }
    assert missing == set()


def test_high_risk_lifecycle_guidance_is_rendered():
    tools = _tools_by_name()
    wait_description = tools["hmc_wait_for_job"].description
    for status in (
        "CANCELED_BEFORE_START",
        "CANCELED_WHILE_RUNNING",
        "COMPLETED",
        "COMPLETED_OK",
        "COMPLETED_WITH_ERROR",
        "COMPLETED_WITH_WARNINGS",
        "FAILED_BEFORE_COMPLETION",
        "FAILED_BEFORE_COMPLETION_RETRY",
        "FAILED_TO_START",
    ):
        assert status in wait_description
    for undocumented in ("EXCEPTION", "FAILED"):
        assert re.search(rf"\b{undocumented}\b", wait_description) is None

    rename = tools["hmc_rename_lpar"]
    assert "ADR 0011" in rename.description
    assert (
        "explicit operator approval"
        in rename.parameters["properties"]["ownership_override"]["description"]
    )

    decommission = tools["hmc_decommission_lpar"]
    assert "dry_run=True" in decommission.description
    assert (
        "explicit operator approval"
        in decommission.parameters["properties"]["ownership_override"]["description"]
    )
    for field in (
        "resource_deleted",
        "workflow_completed",
        "lpar_uuid",
        "dry_run",
        "steps",
        "warnings",
        "blast_radius",
    ):
        assert decommission.output_schema["properties"][field]["description"].strip()

    provision = tools["hmc_provision_lpar"].output_schema["properties"]
    for field in (
        "resource_created",
        "workflow_completed",
        "lpar_uuid",
        "dry_run",
        "ownership_stamped",
        "steps",
        "warnings",
    ):
        assert provision[field]["description"].strip()


def test_vios_backup_lifecycle_guidance_names_supported_commands():
    """Rendered guidance describes the replacement CLI contracts and version floor."""
    tools = _tools_by_name()

    listing = tools["hmc_list_vios_backups"]
    assert "lsviosbk" in listing.description
    assert "name,type" in listing.description
    assert "HMC V10 or newer" in listing.description

    backup = tools["hmc_backup_vios"]
    assert "mkviosbk" in backup.description
    assert "HMC V10 or newer" in backup.description
    assert "system_name_or_uuid" in backup.parameters["properties"]
    assert "backup_name" in backup.parameters["properties"]
    assert set(backup.parameters["required"]) == {
        "system_name_or_uuid",
        "vios_name_or_uuid",
        "backup_name",
    }

    restore = tools["hmc_restore_vios"]
    assert "rstviosbk" in restore.description
    assert "HMC V10 or newer" in restore.description
    assert "viosioconfig" in restore.description
    assert "ssp" in restore.description
    assert "restart_if_required" in restore.parameters["properties"]
    assert set(restore.parameters["required"]) == {
        "system_name_or_uuid",
        "vios_name_or_uuid",
        "backup_name",
        "backup_type",
    }


def test_caller_token_parameter_documents_grammar():
    tools = _tools_by_name()
    for name in ("hmc_create_lpar", "hmc_provision_lpar"):
        description = tools[name].parameters["properties"]["caller_token"][
            "description"
        ]
        assert "[caller " in description
        assert "64" in description


def test_lpar_create_tools_do_not_offer_a_vios_or_an_os_type():
    """A LogicalPartition PUT typed Virtual IO Server returns 500 REST0140 on V10R3, and
    OperatingSystemType is read-only (#1179); ``hmc_create_vios`` creates a VIOS."""
    tools = _tools_by_name()
    for name in ("hmc_create_lpar", "hmc_provision_lpar"):
        properties = tools[name].parameters["properties"]
        assert "os_type" not in properties, name
        partition_type = properties["partition_type"]
        assert partition_type["enum"] == ["AIX/Linux", "OS400"], name
        assert "hmc_create_vios" in partition_type["description"], name


def test_power_on_partition_profile_states_that_activation_discards_changes():
    """#1384: a profile activation discards current-configuration changes the profile
    lacks, memory and processor included, and no warning reports those."""
    tool = _tools_by_name()["hmc_power_on_lpar"]
    description = " ".join(
        tool.parameters["properties"]["partition_profile_uuid"]["description"].split()
    )
    assert "discards" in description
    for writer in ("hmc_modify_lpar", "hmc_dlpar_mem", "hmc_dlpar_proc"):
        assert writer in description
    assert "CurrentProfileSync" in description


def test_processor_modes_keep_distinct_quantity_schemas():
    properties = _tools_by_name()["hmc_dlpar_proc"].parameters["properties"]
    native = next(
        item for item in properties["change"]["anyOf"] if item.get("type") == "object"
    )
    assert {item["type"] for item in native["properties"]["processors"]["anyOf"]} == {
        "integer",
        "null",
    }
    assert "processing_units" in native["properties"]
    assert "desired_procs" in properties["resources"]["properties"]
    assert "desired_vcpus" in properties["resources"]["properties"]
    assert "No replay" in properties["change"]["description"]
