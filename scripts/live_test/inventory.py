"""Read-only inventory scenarios for the live HMC test harness."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, cast

from fastmcp import Client

from hmcpctl.config import HMCConfig
from hmcpctl.ssh.commands import build_filter

from .observation import plain_data
from .results import entries
from .results import resource as get_resource

if TYPE_CHECKING:
    from live_test_runner import RunState


async def _capture_lpar_properties(client: Client, state: RunState) -> None:
    config = state.config
    artifacts = state.artifacts
    # 1. Basic LPAR info
    st, data = await state.call(
        client, "hmc_get_lpar", lpar_name_or_uuid=config.lp3_name
    )
    state.record(0, "hmc_get_lpar (baseline)", st, data)
    if st == "PASS" and isinstance(data, dict):
        artifacts.lp3_uuid = data.get("uuid") or data.get("UUID")
        artifacts.lp3_baseline["lpars"] = data

    # 2. Composite summary
    st, data = await state.call(
        client, "hmc_lpar_summary", lpar_name_or_uuid=config.lp3_name
    )
    state.record(0, "hmc_lpar_summary (baseline)", st, data)
    if st == "PASS":
        artifacts.lp3_baseline["summary"] = data

    # 3. Description
    st, data = await state.call(
        client,
        "hmc_get_lpar_description",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
    )
    state.record(0, "hmc_get_lpar_description (baseline)", st, data)
    if st == "PASS":
        # Store the plain string value, not the raw result dict, so ST10/ST15
        # restore guards don't have to unwrap a nested dict at restore time.
        desc_val = data
        if isinstance(desc_val, dict):
            desc_val = desc_val.get("description") or desc_val.get("value") or ""
        # The CLI read ends in a line terminator that is not part of the value;
        # kept, it makes the restore refuse the description as non-printable.
        text = str(desc_val) if desc_val else ""
        artifacts.lp3_baseline["description"] = text.removesuffix("\n").removesuffix(
            "\r"
        )

    # 4. MSP flag
    st, data = await state.call(
        client,
        "hmc_get_lpar_msp",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
    )
    state.record(0, "hmc_get_lpar_msp (baseline)", st, data)
    if st == "PASS":
        artifacts.lp3_baseline["msp"] = data

    # 5. Proc compat
    st, data = await state.call(
        client,
        "hmc_get_lpar_proc_compat",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
    )
    state.record(0, "hmc_get_lpar_proc_compat (baseline)", st, data)
    if st == "PASS":
        artifacts.lp3_baseline["proc_compat"] = plain_data(data)


async def _capture_adapter_topology(client: Client, state: RunState) -> None:
    config = state.config
    artifacts = state.artifacts
    # 6. CNA adapters — capture PVID and vswitch ID for ST14
    st, data = await state.call(
        client,
        "hmc_list_adapters",
        lpar_name_or_uuid=config.lp3_name,
        adapter_type="ClientNetworkAdapter",
    )
    state.record(0, "hmc_list_adapters CNA (baseline)", st, data)
    if st == "PASS":
        artifacts.lp3_baseline["cna_adapters"] = data
        _capture_cna_identifiers(data, artifacts.lp3_baseline)

    # 7. vSCSI adapters — baseline topology
    st, data = await state.call(
        client,
        "hmc_list_adapters",
        lpar_name_or_uuid=config.lp3_name,
        adapter_type="VirtualSCSIClientAdapter",
    )
    state.record(0, "hmc_list_adapters vSCSI (baseline)", st, data)
    if st == "PASS":
        artifacts.lp3_baseline["vscsi_adapters"] = data


def _capture_cna_identifiers(data: object, baseline: dict[str, object]) -> None:
    """Store the first CNA's PVID and virtual-switch identity, when present."""
    for entry in entries(data):
        resource = get_resource(entry)
        pvid = resource.get("PortVLANID") or resource.get("port_vlan_id")
        if pvid:
            baseline["pvid"] = int(cast(str | int, pvid))
            baseline["vswitch_id"] = int(
                cast(
                    str | int,
                    resource.get("VirtualSwitchID")
                    or resource.get("virtual_switch_id")
                    or 0,
                )
            )
            return


async def _capture_vios_identity(client: Client, state: RunState) -> None:
    config = state.config
    artifacts = state.artifacts
    # 8. VIOS — capture UUID and numeric PartitionID scoped to our managed system
    st, data = await state.call(
        client, "hmc_list_vios", system_name_or_uuid=config.system_name
    )
    state.record(0, "hmc_list_vios (baseline)", st, data)
    if st == "PASS":
        for e in entries(data):
            resource = get_resource(e)
            uuid = e.get("UUID") or e.get("uuid")
            pid = resource.get("PartitionID") or resource.get("partition_id")
            if uuid:
                artifacts.vios_uuid = cast(str, uuid)
                artifacts.vios_partition_id = (
                    int(cast(str | int, pid)) if pid is not None else None
                )
                break


async def _capture_lpar_cli_dump(client: Client, state: RunState) -> None:
    config = state.config
    artifacts = state.artifacts
    # 9. Full CLI dump
    st, data = await state.call(
        client,
        "hmc_run_command",
        cmd=f"lssyscfg -r lpar -m {shlex.quote(config.system_name)}"
        f" --filter {shlex.quote(build_filter([('lpar_names', config.lp3_name)]))}",
    )
    state.record(0, "hmc_run_command lssyscfg (baseline)", st, data)
    if st == "PASS":
        artifacts.lp3_baseline["lssyscfg"] = data


async def read_sync_state(
    client: Client, state: RunState, subtask: int
) -> tuple[str, str] | None:
    """Read the test partition's ``sync_curr_profile`` value and partition state.

    Returns ``None`` when the read fails or does not print exactly two fields.
    """
    config = state.config
    st, data = await state.call(
        client,
        "hmc_run_command",
        cmd=f"lssyscfg -r lpar -m {shlex.quote(config.system_name)}"
        f" --filter {shlex.quote(build_filter([('lpar_names', config.lp3_name)]))}"
        " -F sync_curr_profile,state",
    )
    state.record(subtask, "hmc_run_command lssyscfg sync_curr_profile", st, data)
    if st != "PASS" or not isinstance(data, str):
        return None
    parts = data.strip().split(",")
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


async def _capture_sync_state(client: Client, state: RunState) -> None:
    # 10. Profile-sync setting and partition state, restored by ST10 (ADR 0201)
    sync_state = await read_sync_state(client, state, 0)
    if sync_state is not None:
        baseline = state.artifacts.lp3_baseline
        baseline["sync_curr_profile"], baseline["state"] = sync_state


def _print_baseline_summary(state: RunState) -> None:
    artifacts = state.artifacts
    print(f"  lp3 UUID: {artifacts.lp3_uuid}")
    print(
        f"  VIOS UUID: {artifacts.vios_uuid}  PartitionID: {artifacts.vios_partition_id}"
    )
    print(f"  lp3 PVID: {artifacts.lp3_baseline.get('pvid')}")
    print(f"  Baseline keys: {list(artifacts.lp3_baseline.keys())}")


async def capture_lpar_baseline(client: Client, state: RunState) -> None:
    print("\n=== ST0: Capture baseline LPAR ===")
    await _capture_lpar_properties(client, state)
    await _capture_adapter_topology(client, state)
    await _capture_vios_identity(client, state)
    await _capture_lpar_cli_dump(client, state)
    await _capture_sync_state(client, state)
    if state.group == "round2":
        # Import locally: the runner also imports this scenario module.
        import live_test_baseline as baseline

        captured = state.artifacts.lp3_baseline
        vios_id = state.artifacts.vios_partition_id
        if (
            type(vios_id) is not int
            or vios_id <= 0
            or not isinstance(captured.get("description"), str)
            or not isinstance(captured.get("proc_compat"), dict)
            or any(
                row["status"] == "FAIL" for row in state.results if row["subtask"] == 0
            )
        ):
            raise ValueError("round2 baseline incomplete; refusing mutations")
        captured["round2_snapshot"] = await baseline.capture(
            state.hmc_config or HMCConfig(), state.config.system_name, [vios_id]
        )
    _print_baseline_summary(state)
