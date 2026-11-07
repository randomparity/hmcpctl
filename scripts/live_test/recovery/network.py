"""Independent network baseline and residue witnesses."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from live_test import network

from .common import Finding, StateUnreadable


@dataclass(frozen=True)
class NetworkInputs:
    """What the network arm (ST9) read before changing anything."""

    system_name: str
    lpar_name: str
    #: The run's test VLAN, when its VLAN round trip reached the create.
    vlan: int | None
    #: Adapter type -> its baseline listing on the test partition.
    adapters: dict[str, Any]
    #: The serving VIOS's FC-port label rows, when that round trip read them.
    fc_labels: list[Any] | None
    #: Whether the vFC group-label round trip read its baseline.
    groups_read: bool = False


def _baseline_row(rows: list[Any], tool: str) -> Any:
    """The data of the ST9 row recording *tool* (``<tool> (<label>)``), or None."""
    for row in rows:
        if (
            isinstance(row, dict)
            and row.get("subtask") == network.SUBTASK
            and row.get("tool") == tool
            and row.get("status") == "PASS"
        ):
            return row.get("data")
    return None


def network_inputs_from_document(
    document: Any, subtasks: list[int]
) -> NetworkInputs | None:
    """ST9's baselines, or `None` when it changed nothing (it SKIPped or never ran)."""
    if network.SUBTASK not in subtasks or not isinstance(document, dict):
        return None
    config, artifacts = document.get("config"), document.get("artifacts")
    rows = document.get("results")
    if not (
        isinstance(config, dict)
        and isinstance(artifacts, dict)
        and isinstance(rows, list)
    ):
        return None
    adapters = {
        kind: data
        for kind in network.ROUND_TRIP_ADAPTER_TYPES
        if (data := _baseline_row(rows, f"hmc_list_adapters ({kind}, baseline)"))
        is not None
    }
    # Any create row, whatever its status: a refused create can still have applied.
    created = any(
        isinstance(row, dict)
        and row.get("subtask") == network.SUBTASK
        and row.get("tool") == "hmc_create_virtual_network (create)"
        for row in rows
    )
    fc_labels = _baseline_row(
        rows, "hmc_list_vios_fc_port_labels (FC-port labels, baseline)"
    )
    vlan = artifacts.get("test_vlan_id") if created else None
    groups_read = (
        _baseline_row(
            rows, "hmc_list_vios_vfc_group_labels (vFC group labels, baseline)"
        )
        is not None
    )
    if not adapters and vlan is None and fc_labels is None and not groups_read:
        return None
    return NetworkInputs(
        str(config.get("system_name")),
        str(config.get("lp3_name")),
        vlan if isinstance(vlan, int) else None,
        adapters,
        fc_labels if isinstance(fc_labels, list) else None,
        groups_read,
    )


async def check_network(call, inputs: NetworkInputs) -> list[Finding]:
    """A network on the test VLAN, adapters and labels off ST9's baselines."""
    findings: list[Finding] = []
    system = inputs.system_name
    status, data = await call("hmc_list_virtual_networks", system_name_or_uuid=system)
    if status != "PASS" or not isinstance(data, list):
        raise StateUnreadable(f"could not list the virtual networks ({status})")
    if inputs.vlan is not None:
        for entry in data:
            resource = entry.get("Resource", entry) if isinstance(entry, dict) else {}
            if network.as_int(resource.get("NetworkVLANID")) == inputs.vlan:
                findings.append(
                    Finding(
                        "network left on the test VLAN",
                        f"{resource.get('NetworkName')!r} ({entry.get('UUID')}) is on "
                        f"VLAN {inputs.vlan}, which was unused before the run",
                        "hmc_delete_virtual_network with that network_uuid, after "
                        "confirming it is the run's (its name starts "
                        f"{network.NAME_PREFIX})",
                    )
                )
    for kind, baseline in inputs.adapters.items():
        before = network.adapter_placements(baseline)
        status, data = await call(
            "hmc_list_adapters",
            lpar_name_or_uuid=inputs.lpar_name,
            adapter_type=kind,
            system_name_or_uuid=system,
        )
        now = network.adapter_placements(data) if status == "PASS" else None
        if before is None or now is None:
            raise StateUnreadable(
                f"could not compare {inputs.lpar_name}'s {kind} adapters", findings
            )
        if now != before:
            findings.append(
                Finding(
                    f"{kind} off its baseline",
                    f"{inputs.lpar_name}'s {kind} adapters differ from the run's baseline",
                    network.placement_drift(before, now),
                )
            )
    findings += await _network_labels(call, inputs, findings)
    return findings


async def _network_labels(
    call, inputs: NetworkInputs, found: list[Finding]
) -> list[Finding]:
    findings: list[Finding] = []
    status, groups = await call(
        "hmc_list_vios_vfc_group_labels", system_name_or_uuid=inputs.system_name
    )
    if status == "PASS" and isinstance(groups, list):
        left = sorted(
            str(network.group_name(row))
            for row in groups
            if isinstance(row, dict)
            and str(network.group_name(row)).startswith(network.LABEL_PREFIX)
        )
        if left:
            findings.append(
                Finding(
                    "vFC group label left",
                    f"{left} carry the run's prefix",
                    f"labelvios -m {shlex.quote(inputs.system_name)} -o r -l <label>",
                )
            )
    elif inputs.groups_read:
        raise StateUnreadable("could not list the vFC group labels", found + findings)
    if inputs.fc_labels is None:
        return findings
    before = {
        (str(row.get("name")), str(row.get("port_name"))): str(
            row.get("port_label") or ""
        )
        for row in inputs.fc_labels
        if isinstance(row, dict)
    }
    for vios in sorted({name for name, _ in before}):
        status, rows = await call(
            "hmc_list_vios_fc_port_labels",
            system_name_or_uuid=inputs.system_name,
            vios_name=vios,
        )
        if status != "PASS" or not isinstance(rows, list):
            raise StateUnreadable(
                f"could not list {vios}'s FC-port labels", found + findings
            )
        now = {
            (str(row.get("name")), str(row.get("port_name"))): str(
                row.get("port_label") or ""
            )
            for row in rows
            if isinstance(row, dict)
        }
        for key, label in before.items():
            if key[0] == vios and now.get(key) != label:
                findings.append(
                    Finding(
                        "FC-port label off its original",
                        f"{vios} {key[1]} reads {now.get(key)!r}, was {label!r}",
                        "hmc_set_vios_fc_port_label with the original label"
                        if label
                        else "hmc_remove_vios_fc_port_label",
                    )
                )
    return findings
