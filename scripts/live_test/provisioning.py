"""Provisioning scenarios for the live HMC test harness."""

from __future__ import annotations

import json
import secrets
import shlex
from typing import TYPE_CHECKING, cast

from fastmcp import Client

from hmcpctl.operations.lpar.ownership import parse_lpar_ownership_caller_token
from hmcpctl.ssh.commands import parse_hmc_result_rows
from hmcpctl.ssh.transport import HMCCLIError

from .network import listed_vlans
from .observation import judge_create_result, plain_data
from .pcie import partition_not_found
from .results import resource as get_resource
from .storage import configured_vg_uuid
from .storage_lifecycle import LISTING_NAME, volume_listing, volume_names

if TYPE_CHECKING:
    from live_test_runner import RunState

# ---------------------------------------------------------------------------
# ST13 — Provision Dry Run
# ---------------------------------------------------------------------------


async def validate_provisioning_dry_run(client: Client, state: RunState) -> None:
    config = state.config
    artifacts = state.artifacts
    print("\n=== ST13: Provision Dry Run ===")

    vios_uuid = artifacts.vios_uuid

    if not vios_uuid:
        state.skip(13, "hmc_provision_lpar (dry_run)", "no VIOS UUID")
        return

    st, data = await state.call(
        client,
        "hmc_provision_lpar",
        dry_run=True,
        system_name_or_uuid=config.system_name,
        name=config.dry_run_lpar_name,
        adapters={"port_vlan_id": config.provision_vlan_id},
        storage={
            "vios_uuid": vios_uuid,
            "storage_name": config.dry_run_storage_name,
        },
        resources={"desired_memory": config.dry_run_memory_mib},
    )
    state.record(13, "hmc_provision_lpar (dry_run)", st, data)
    data = plain_data(data)
    if st == "PASS" and isinstance(data, dict):
        steps = data.get("steps") or []
        all_dry = all(s.get("status") == "dry_run" for s in steps)
        print(f"  dry_run steps: {[s.get('step') for s in steps]}")
        print(f"  all status=dry_run: {all_dry}")


# ---------------------------------------------------------------------------
# ST14 — Storage Lifecycle + Full Live Provision
# ---------------------------------------------------------------------------


async def _test_disk_names(client: Client, state: RunState) -> frozenset[str] | None:
    config = state.config
    vios_id = state.artifacts.vios_partition_id
    if type(vios_id) is not int or vios_id <= 0:
        state.record(
            14, "test disk inventory", "FAIL", "positive VIOS partition ID required"
        )
        return None
    status, data = await state.call(
        client,
        "hmc_run_command",
        cmd=volume_listing(config.system_name, vios_id, config.vdisk_volume_group_name),
    )
    lines = (
        [line.split() for line in data.splitlines() if line.strip()]
        if isinstance(data, str)
        else []
    )
    valid = (
        len(lines) >= 2
        and lines[0] == [f"{config.vdisk_volume_group_name}:"]
        and lines[1]
        == ["LV", "NAME", "TYPE", "LPs", "PPs", "PVs", "LV", "STATE", "MOUNT", "POINT"]
        and all(
            len(row) == 7
            and LISTING_NAME.fullmatch(row[0])
            and all(value.isdecimal() for value in row[2:5])
            for row in lines[2:]
        )
    )
    names = volume_names(data) if status == "PASS" and valid else None
    state.record(
        14, "test disk inventory", "PASS" if names is not None else "FAIL", data
    )
    return names


def _baseline_provision_resources(state: RunState) -> dict[str, int]:
    """Translate the captured REST resource keys into provision request fields."""
    config = state.config
    artifacts = state.artifacts
    baseline_lpar = artifacts.lp3_baseline.get("lpars") or {}
    resource = get_resource(baseline_lpar) if isinstance(baseline_lpar, dict) else {}
    values = (
        (
            "min_memory",
            "MinimumMemory",
            "minimum_memory",
            config.provision_min_memory_mib,
        ),
        (
            "desired_memory",
            "DesiredMemory",
            "desired_memory",
            config.provision_desired_memory_mib,
        ),
        (
            "max_memory",
            "MaximumMemory",
            "maximum_memory",
            config.provision_max_memory_mib,
        ),
        (
            "desired_vcpus",
            "DesiredVirtualProcessors",
            "desired_virtual_processors",
            config.provision_desired_vcpus,
        ),
        (
            "max_vcpus",
            "MaximumVirtualProcessors",
            "maximum_virtual_processors",
            config.provision_max_vcpus,
        ),
    )
    return {
        name: int(
            cast(str | int, resource.get(upper) or resource.get(lower) or default)
        )
        for name, upper, lower, default in values
    }


async def _provision_vlan_refusal(client: Client, state: RunState) -> str | None:
    """Say why the configured VLAN cannot be provisioned on, or None when it can.

    The VLAN is operator-configured rather than the test partition's own, and
    ST14 checks it before creating fixtures; provision also validates it.
    """
    vlan_id = state.config.provision_vlan_id
    status, data = await state.call(
        client,
        "hmc_list_virtual_networks",
        system_name_or_uuid=state.config.system_name,
    )
    state.record(14, "hmc_list_virtual_networks (provision VLAN)", status, data)
    fix = "set LIVE_TEST_PROVISION_VLAN_ID to a VLAN with a virtual network"
    if status != "PASS":
        return f"hmc_list_virtual_networks returned {status}; cannot confirm VLAN {vlan_id}"
    vlans, malformed = listed_vlans(data)
    if vlan_id in vlans:
        return None
    note = (
        f" (unparsable VLAN identifiers: {', '.join(repr(v) for v in malformed)})"
        if malformed
        else ""
    )
    return f"no virtual network on VLAN {vlan_id}{note}; {fix}"


async def _fixture_partition(client: Client, state: RunState, name: str) -> bool | None:
    status, data = await state.call(client, "hmc_get_lpar", lpar_name_or_uuid=name)
    if (
        status == "PASS"
        and isinstance(data, dict)
        and (data.get("UUID") or data.get("uuid"))
    ):
        return True
    if status == "FAIL" and partition_not_found(status, data):
        return False
    state.record(14, "fixture identity", "FAIL", "partition presence is unreadable")
    return None


async def _fixture_disk_unmapped(
    client: Client, state: RunState, fixture: dict[str, str]
) -> bool:
    import live_test_baseline as baseline

    status, detail = await state.call(
        client,
        "hmc_get_vios_storage_detail",
        vios_name_or_uuid=fixture["vios_uuid"],
        system_name_or_uuid=state.config.system_name,
    )
    if status != "PASS" or not isinstance(detail, dict):
        return False
    resource = detail.get("Resource")
    if not isinstance(resource, dict):
        return False
    try:
        # The complete validator distinguishes bare adapters and empty optical
        # targets from malformed backings, unlike the lossy list projection.
        rows = baseline.mapping_rows(resource, str(state.artifacts.vios_partition_id))
    except (ValueError, TypeError, KeyError):
        return False
    return not any(
        json.loads(row["backing"])["storage"].get("VirtualDisk", {}).get("DiskName")
        == fixture["disk"]
        for row in rows
    )


async def _cleanup_fixture(
    client: Client, state: RunState, fixture: dict[str, str]
) -> None:
    name, disk = fixture["name"], fixture["disk"]
    present = await _fixture_partition(client, state, name)
    if present is None:
        return
    if present:
        status, description = await state.call(
            client,
            "hmc_get_lpar_description",
            system_name_or_uuid=state.config.system_name,
            lpar_name_or_uuid=name,
        )
        if (
            status != "PASS"
            or not isinstance(description, str)
            or parse_lpar_ownership_caller_token(description) != fixture["token"]
        ):
            state.record(
                14,
                "fixture cleanup ownership",
                "FAIL",
                "MANUAL RECOVERY REQUIRED: fixture ownership not confirmed",
            )
            return
        status, data = await state.call(
            client,
            "hmc_decommission_lpar",
            system_name_or_uuid=state.config.system_name,
            lpar_name_or_uuid=name,
            dry_run=False,
            immediate=True,
        )
        state.record(14, "hmc_decommission_lpar (fixture)", status, data)
        if await _fixture_partition(client, state, name) is not False:
            state.record(
                14,
                "fixture cleanup",
                "FAIL",
                "MANUAL RECOVERY REQUIRED: partition remains",
            )
            return
    if not await _fixture_disk_unmapped(client, state, fixture):
        state.record(
            14,
            "fixture disk cleanup",
            "FAIL",
            "MANUAL RECOVERY REQUIRED: disk mapping absence not confirmed",
        )
        return
    names = await _test_disk_names(client, state)
    if names is None:
        return
    if disk in names:
        status, data = await state.call(
            client,
            "hmc_delete_virtual_disk",
            vios_name_or_uuid=fixture["vios_uuid"],
            system_name_or_uuid=state.config.system_name,
            vg_uuid=fixture["vg_uuid"],
            disk_name=disk,
        )
        state.record(14, "hmc_delete_virtual_disk (fixture)", status, data)
        names = await _test_disk_names(client, state)
    if names is None or disk in names:
        state.record(
            14,
            "fixture disk removal",
            "FAIL",
            "MANUAL RECOVERY REQUIRED: disk absence not confirmed",
        )


async def _provision_from_baseline(
    client: Client, state: RunState, fixture: dict[str, str]
) -> None:
    config = state.config
    vios_uuid, vg_uuid = fixture["vios_uuid"], fixture["vg_uuid"]
    status, data = await state.call(
        client,
        "hmc_provision_lpar",
        system_name_or_uuid=config.system_name,
        name=fixture["name"],
        caller_token=fixture["token"],
        adapters={"port_vlan_id": config.provision_vlan_id},
        storage={
            "vios_uuid": str(vios_uuid),
            "storage_name": fixture["disk"],
            "kind": "VirtualDisk",
            "vg_uuid": str(vg_uuid),
        },
        resources=_baseline_provision_resources(state),
        partition_type="AIX/Linux",
        power_on=True,
        dry_run=False,
    )
    result, reason = judge_create_result(status, data)
    state.record(14, "hmc_provision_lpar (live)", result, data, reason)
    data = plain_data(data)
    if isinstance(data, dict):
        for step in data.get("steps") or []:
            print(f"provision step [{step.get('step')}]: {step.get('status')}")


async def exercise_storage_provisioning(client: Client, state: RunState) -> None:
    """Provision and remove fresh owned objects, preserving the configured originals."""
    config, artifacts = state.config, state.artifacts
    vios_uuid, vg_uuid = artifacts.vios_uuid, configured_vg_uuid(state)
    if (
        not vios_uuid
        or not vg_uuid
        or type(artifacts.vios_partition_id) is not int
        or artifacts.vios_partition_id <= 0
    ):
        state.record(
            14,
            "pre-flight check",
            "FAIL",
            "vios_uuid, vios_partition_id and vg_uuid baseline required",
        )
        return
    refusal = await _provision_vlan_refusal(client, state)
    if refusal:
        state.record(14, "pre-flight check", "FAIL", refusal)
        return
    tag = secrets.token_hex(4)
    fixture = {
        "name": f"hmcpctl-live-round2-{tag}",
        "disk": f"hp14{tag}",
        "token": f"round2-{tag}",
        "vios_uuid": str(vios_uuid),
        "vg_uuid": str(vg_uuid),
    }
    status, partitions = await state.call(
        client,
        "hmc_run_command",
        cmd=f"lssyscfg -r lpar -m {shlex.quote(config.system_name)} -F name --header",
    )
    try:
        partition_rows = (
            parse_hmc_result_rows(partitions, ("name",), "fixture admission")
            if status == "PASS" and isinstance(partitions, str)
            else None
        )
    except HMCCLIError:
        partition_rows = None
    names = await _test_disk_names(client, state)
    if (
        partition_rows is None
        or any(row["name"] == fixture["name"] for row in partition_rows)
        or names is None
        or fixture["disk"] in names
    ):
        state.record(
            14,
            "fixture admission",
            "FAIL",
            "fresh names are present or inventory unreadable",
        )
        return
    artifacts.lp3_baseline["round2_fixture"] = fixture
    try:
        status, data = await state.call(
            client,
            "hmc_create_virtual_disk",
            vios_name_or_uuid=str(vios_uuid),
            vg_uuid=str(vg_uuid),
            disk_name=fixture["disk"],
            capacity_mib=config.provision_disk_mib,
        )
        state.record(14, "hmc_create_virtual_disk (fixture)", status, data)
        if status != "PASS":
            return
        await _provision_from_baseline(client, state, fixture)
    finally:
        await _cleanup_fixture(client, state, fixture)
