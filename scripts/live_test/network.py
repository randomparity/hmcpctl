"""Virtual-network, client-adapter and VIOS-label scenarios for the live harness (#629).

ST2 reads the virtual-network inventory, the test partition's client adapters and
the VIOS Fibre Channel labels, and records each non-empty read through
`record_verified`. ST9 runs only in the `network` arm. On the test partition, while
it is Not Activated, it round-trips a test VLAN with a client network adapter, a
vSCSI and a vFC client adapter paired to the VIOS serving the partition, and that
VIOS's FC-port and vFC group labels.

Every ST9 scenario reads its own baseline and re-reads after each mutation.
The client-network scenario deletes only its independently verified fixture UUID;
storage scenarios reverse their baseline difference. Uncertain verification or
cleanup records a manual-recovery row and stops ST9: no later mutation runs.
"""

from __future__ import annotations

import csv
import io
import secrets
import shlex
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

from fastmcp import Client

from hmcpctl.errors import HMCError
from hmcpctl.resource_identity import is_uuid
from hmcpctl.ssh.commands import build_attribute_record, build_filter
from hmcpctl.xmlutil import leaf_text

from .observation import Assertion
from .results import entries
from .results import resource as get_resource

if TYPE_CHECKING:
    from live_test.runner import RunState

INVENTORY_SUBTASK = 2
SUBTASK = 9
GROUP = "network"
INVENTORY_SCENARIO = "st2-network-inventory"
NAME_PREFIX = "hmcpctl-live-"
#: The prefix of the run's VIOS labels. The HMC refuses a group label longer than
#: 16 characters (HSCLC3A4, V10R3 live run 2026-10-06), and the renamed label adds
#: "-r" to the prefix and 8 hex characters.
LABEL_PREFIX = "hmcl-"
#: A port name no VIOS has, for the label negative.
ABSENT_PORT = "fcs9999"
_READ_FAILED = Assertion("read-failed", False)
#: The client adapter types ST9 adds and removes on the test partition.
ROUND_TRIP_ADAPTER_TYPES = (
    "ClientNetworkAdapter",
    "VirtualSCSIClientAdapter",
    "VirtualFibreChannelClientAdapter",
)
_ADAPTER_TYPES = (
    "ClientNetworkAdapter",
    "VirtualSCSIClientAdapter",
    "VirtualFibreChannelClientAdapter",
    "VirtualNICDedicated",
)
_SCSI_FIELDS = "lpar_name,lpar_id,slot_num,adapter_type,remote_lpar_id,remote_slot_num"


def as_int(value: object) -> int | None:
    """An HMC integer field's value, or None when it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def listed_vlans(data: Any) -> tuple[set[int], list[object]]:
    """Return the VLAN IDs a virtual-network listing names, and any it cannot parse."""
    vlans: set[int] = set()
    malformed: list[object] = []
    for entry in entries(data):
        resource = get_resource(entry)
        vlan = (
            resource.get("NetworkVLANID")
            or resource.get("VLANId")
            or resource.get("vlan_id")
        )
        if vlan is not None:
            parsed = as_int(vlan)
            if parsed is None:
                malformed.append(vlan)
            else:
                vlans.add(parsed)
    return vlans, malformed


def unused_vlan(used: set[int], start: int, end: int) -> int | None:
    """The first VLAN id in the configured range that no network uses."""
    return next((vlan for vlan in range(start, end + 1) if vlan not in used), None)


def _all(rows: list[Mapping[str, object]], check: Callable[[Mapping], bool]) -> bool:
    return bool(rows) and all(check(row) for row in rows)


def _vlan_in_range(entry: Mapping[str, object]) -> bool:
    vlan = as_int(get_resource(entry).get("NetworkVLANID"))
    return vlan is not None and 1 <= vlan <= 4094


def group_name(row: Mapping[str, object]) -> object:
    """A vFC group label row's name: its `name` column, else its first column."""
    if "name" in row:
        return row["name"]
    return next(iter(row.values()), None)


def _switch_id(entry: Mapping[str, object]) -> bool:
    return as_int(get_resource(entry).get("SwitchID")) is not None


def _identified(entry: Mapping[str, object]) -> bool:
    return bool(entry.get("UUID") or entry.get("uuid"))


def _eth_row(row: Mapping[str, object]) -> bool:
    return bool(row.get("lpar_name")) and as_int(row.get("port_vlan_id")) is not None


def _fc_row(row: Mapping[str, object]) -> bool:
    return all(row.get(key) for key in ("lpar_name", "adapter_type", "slot_num"))


def _fc_port_label_row(row: Mapping[str, object]) -> bool:
    return bool(row.get("name")) and bool(row.get("port_name"))


# ---------------------------------------------------------------------------
# ST2 — Network inventory
# ---------------------------------------------------------------------------


def _empty(state: RunState, tool: str, status: str, data: Any) -> bool:
    """Record an empty listing as non-promoting: it proves no row shape."""
    if status == "PASS" and isinstance(data, list) and not data:
        state.record(INVENTORY_SUBTASK, f"{tool} (empty)", status, data)
        return True
    return False


async def _inventory_network_resources(client: Client, state: RunState) -> None:
    system = state.config.system_name
    st, data = await state.call(
        client, "hmc_list_virtual_switches", system_name_or_uuid=system
    )
    if not _empty(state, "hmc_list_virtual_switches", st, data):
        state.record_verified(
            INVENTORY_SUBTASK,
            "hmc_list_virtual_switches",
            operation="network.list_switches",
            scenario=INVENTORY_SCENARIO,
            assertions=[
                Assertion("switch-ids-integral", _all(entries(data), _switch_id))
            ]
            if st == "PASS"
            else [_READ_FAILED],
            cleanup="not-required",
            data=data,
        )
    st, data = await state.call(
        client, "hmc_list_virtual_networks", system_name_or_uuid=system
    )
    if not _empty(state, "hmc_list_virtual_networks", st, data):
        state.record_verified(
            INVENTORY_SUBTASK,
            "hmc_list_virtual_networks",
            operation="network.list_networks",
            scenario=INVENTORY_SCENARIO,
            assertions=[
                Assertion("vlan-ids-in-range", _all(entries(data), _vlan_in_range))
            ]
            if st == "PASS"
            else [_READ_FAILED],
            cleanup="not-required",
            data=data,
        )
    st, data = await state.call(
        client, "hmc_list_network_bridges", system_name_or_uuid=system
    )
    if not _empty(state, "hmc_list_network_bridges", st, data):
        state.record_verified(
            INVENTORY_SUBTASK,
            "hmc_list_network_bridges",
            operation="network.list_bridges",
            scenario=INVENTORY_SCENARIO,
            assertions=[
                Assertion("bridge-entries-identified", _all(entries(data), _identified))
            ]
            if st == "PASS"
            else [_READ_FAILED],
            cleanup="not-required",
            data=data,
        )


async def _inventory_virtual_io(client: Client, state: RunState) -> None:
    system = state.config.system_name
    st, data = await state.call(
        client, "hmc_list_sea_adapters", system_name_or_uuid=system
    )
    if not _empty(state, "hmc_list_sea_adapters", st, data):
        state.record_verified(
            INVENTORY_SUBTASK,
            "hmc_list_sea_adapters",
            operation="network.list_sea",
            scenario=INVENTORY_SCENARIO,
            assertions=[Assertion("eth-rows-parsed", _all(entries(data), _eth_row))]
            if st == "PASS"
            else [_READ_FAILED],
            cleanup="not-required",
            data=data,
        )
    st, data = await state.call(client, "hmc_list_fc_ports", system_name_or_uuid=system)
    if not _empty(state, "hmc_list_fc_ports", st, data):
        state.record_verified(
            INVENTORY_SUBTASK,
            "hmc_list_fc_ports",
            operation="network.list_fc_ports",
            scenario=INVENTORY_SCENARIO,
            assertions=[Assertion("fc-rows-parsed", _all(entries(data), _fc_row))]
            if st == "PASS"
            else [_READ_FAILED],
            cleanup="not-required",
            data=data,
        )


async def _inventory_labels(client: Client, state: RunState) -> None:
    system = state.config.system_name
    st, data = await state.call(
        client, "hmc_list_vios_fc_port_labels", system_name_or_uuid=system
    )
    if not _empty(state, "hmc_list_vios_fc_port_labels", st, data):
        state.record_verified(
            INVENTORY_SUBTASK,
            "hmc_list_vios_fc_port_labels",
            operation="vios_label.list_fc_ports",
            scenario=INVENTORY_SCENARIO,
            assertions=[
                Assertion(
                    "fc-port-rows-parsed", _all(entries(data), _fc_port_label_row)
                )
            ]
            if st == "PASS"
            else [_READ_FAILED],
            cleanup="not-required",
            data=data,
        )
    st, data = await state.call(
        client, "hmc_list_vios_vfc_group_labels", system_name_or_uuid=system
    )
    if not _empty(state, "hmc_list_vios_vfc_group_labels", st, data):
        state.record_verified(
            INVENTORY_SUBTASK,
            "hmc_list_vios_vfc_group_labels",
            operation="vios_label.list_vfc_groups",
            scenario=INVENTORY_SCENARIO,
            assertions=[
                Assertion(
                    "group-rows-parsed",
                    _all(entries(data), lambda row: bool(group_name(row))),
                )
            ]
            if st == "PASS"
            else [_READ_FAILED],
            cleanup="not-required",
            data=data,
        )


async def _inventory_adapters(client: Client, state: RunState) -> None:
    listed: list[Mapping[str, object]] = []
    readable = True
    for adapter_type in _ADAPTER_TYPES:
        st, data = await state.call(
            client,
            "hmc_list_adapters",
            lpar_name_or_uuid=state.config.lp3_name,
            adapter_type=adapter_type,
            system_name_or_uuid=state.config.system_name,
        )
        state.record(INVENTORY_SUBTASK, f"hmc_list_adapters ({adapter_type})", st, data)
        readable = readable and st == "PASS" and isinstance(data, list)
        listed.extend(entries(data))
    tool = "hmc_list_adapters (test partition, every type)"
    if readable and not listed:
        state.record(INVENTORY_SUBTASK, f"{tool} (empty)", "PASS", listed)
        return
    state.record_verified(
        INVENTORY_SUBTASK,
        tool,
        operation="adapter.list",
        scenario=INVENTORY_SCENARIO,
        assertions=[Assertion("adapter-entries-identified", _all(listed, _identified))]
        if readable
        else [_READ_FAILED],
        cleanup="not-required",
        data=listed,
    )


async def inventory_network(client: Client, state: RunState) -> None:
    print("\n=== ST2: Network Inventory ===")
    await _inventory_network_resources(client, state)
    await _inventory_virtual_io(client, state)
    await _inventory_labels(client, state)
    await _inventory_adapters(client, state)


# ---------------------------------------------------------------------------
# ST9 — Round trips on the test partition and the VIOS serving it
# ---------------------------------------------------------------------------


class _Stop(Exception):
    """A reversal was not confirmed: no further ST9 mutation may run."""


@dataclass(frozen=True)
class _Boundary:
    """The test partition's id and the one VIOS serving it ("" when not one)."""

    lpar_id: str
    vios: str
    vios_id: str
    #: A virtual slot the test partition's own vSCSI client already uses ("" if none).
    used_slot: str = ""


#: The client adapter fields naming the VIOS partition and server slot it pairs with.
_REMOTE_FIELDS = {
    "VirtualSCSIClientAdapter": ("RemoteLogicalPartitionID", "RemoteSlotNumber"),
    "VirtualFibreChannelClientAdapter": (
        "ConnectingPartitionID",
        "ConnectingVirtualSlotNumber",
    ),
}

Listing = dict[str, Mapping[str, object]]
Networks = dict[str, tuple[int, str]]


def _uuid(entry: Mapping[str, object]) -> str | None:
    value = entry.get("UUID") or entry.get("uuid")
    return value if isinstance(value, str) and value else None


def _by_uuid(data: Any) -> Listing | None:
    """A listing keyed by UUID; None when it is not a list of identified entries."""
    if not isinstance(data, list):
        return None
    keyed = {_uuid(entry): get_resource(entry) for entry in entries(data)}
    if None in keyed or len(keyed) != len(data):
        return None
    return {key: value for key, value in keyed.items() if key is not None}


def _csv_rows(text: str, fields: str) -> list[dict[str, str]] | None:
    """`-F` output as rows; None when a line does not have the requested columns."""
    names = fields.split(",")
    if text.strip() == "No results were found.":
        return []
    rows = [row for row in csv.reader(io.StringIO(text)) if row]
    if any(len(row) != len(names) for row in rows):
        return None
    return [dict(zip(names, row, strict=True)) for row in rows]


def _plain_label(value: str) -> bool:
    """Whether the label tools accept `value` back, so it can be restored exactly."""
    return bool(value.strip()) and not any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    )


def _run_networks(current: Networks | None, vlan: int, name: str) -> Networks | None:
    """The networks this run created: on its VLAN, under its own name.

    The VLAN was unused at baseline and the name carries the run's tag, so a
    network matching both is the run's own whatever call created it. Anything
    else on the VLAN is never deleted: the baseline compare reports it.
    """
    if current is None:
        return None
    return {
        key: value
        for key, value in current.items()
        if value[0] == vlan and value[1].startswith(name)
    }


def _server_slot(servers: Iterable[tuple[str, ...]], lpar_id: str) -> str | None:
    """The lowest server slot assigned to the test partition, or None.

    A slot open to any partition could be serving another client, so it is never
    used (orchestrator ruling, 2026-10-06).
    """
    index = {name: i for i, name in enumerate(_SCSI_FIELDS.split(","))}
    slots = sorted(
        (
            row[index["slot_num"]]
            for row in servers
            if row[index["remote_lpar_id"]] == lpar_id
        ),
        key=lambda slot: as_int(slot) or 0,
    )
    return slots[0] if slots else None


#: The fields that place a client adapter: its slot, VLAN and server pairing.
_PLACEMENT_FIELDS = (
    "VirtualSlotNumber",
    "PortVLANID",
    "RemoteLogicalPartitionID",
    "RemoteSlotNumber",
    "ConnectingPartitionID",
    "ConnectingVirtualSlotNumber",
)

#: A partition's adapters of one type: UUID -> placement.
Adapters = dict[str, tuple[str, ...]]


def _placements(listed: Listing) -> Adapters:
    return {
        key: tuple(str(resource.get(field, "")) for field in _PLACEMENT_FIELDS)
        for key, resource in listed.items()
    }


def placement_drift(baseline: Adapters, after: Adapters | None) -> str:
    """What differs from the baseline, so a manual recovery names only the run's own."""
    if after is None:
        return "the adapters could not be read; compare by hand with the baseline"
    fields = ", ".join(_PLACEMENT_FIELDS)
    new = sorted(k for k in after if k not in baseline)
    off = sorted(k for k in baseline if after.get(k) != baseline[k])
    return (
        f"remove only the new UUIDs {new}; restore each of {off} to its baseline "
        f"placement ({fields}) {[baseline[k] for k in off]}"
    )


def adapter_placements(data: Any) -> Adapters | None:
    """A `hmc_list_adapters` result as UUID -> placement; None when not a listing."""
    listed = _by_uuid(data)
    return None if listed is None else _placements(listed)


def _new(
    after: Listing | None, baseline: Adapters
) -> list[Mapping[str, object]] | None:
    return None if after is None else [after[k] for k in after if k not in baseline]


_NETWORK_CREATION_SETTINGS: dict[str, Any] = {
    "addl_vlan_ids": [],
    "allowed_os_mac_addrs": "all",
    "qos_priority": "none",
    "vsi_manager_id": 0,
    "vsi_type_id": 0,
    "vsi_type_version": 0,
}


def _network_keys(snapshot: Mapping[str, object] | None) -> dict[str, str] | None:
    if snapshot is None:
        return None
    keys: dict[str, str] = {}
    for value in snapshot:
        if not isinstance(value, str) or not is_uuid(value) or value.casefold() in keys:
            return None
        keys[value.casefold()] = value
    return keys


def _network_text(value: object) -> str | None:
    if isinstance(value, dict) and (
        set(value) != {"@attrs", "text"} or not isinstance(value.get("@attrs"), dict)
    ):
        return None
    text = leaf_text(value)
    return text.strip() if isinstance(text, str) else None


def _network_fields_match(resource: Mapping[str, object]) -> bool:
    vlans = _network_text(resource.get("TaggedVLANIDs"))
    return (
        isinstance(vlans, str)
        and not vlans.strip()
        and _network_text(resource.get("AllowedOperatingSystemMACAddresses")) == "ALL"
        and _network_text(resource.get("QualityOfServicePriorityEnabled"))
        in ("false", "0")
        and all(
            as_int(_network_text(resource.get(name))) == 0
            for name in (
                "VirtualStationInterfaceManagerID",
                "VirtualStationInterfaceTypeID",
                "VirtualStationInterfaceTypeVersion",
            )
        )
    )


def _network_metadata_verified(data: Any) -> bool:
    metadata = data.get("creation_verification") if isinstance(data, Mapping) else None
    return (
        isinstance(metadata, dict)
        and set(metadata)
        == {"status", "checked_fields", "mismatched_fields", "unavailable_fields"}
        and metadata["status"] == "verified"
        and metadata["checked_fields"] == list(_NETWORK_CREATION_SETTINGS)
        and metadata["mismatched_fields"] == []
        and metadata["unavailable_fields"] == []
    )


def _network_fixture(
    listed: Listing | None,
    baseline: Adapters,
    identity: object,
    vlan: int,
    expected_placement: tuple[str, ...] | None = None,
) -> str | None:
    keys, original = _network_keys(listed), _network_keys(baseline)
    if (
        keys is None
        or original is None
        or not isinstance(identity, str)
        or not is_uuid(identity)
    ):
        return None
    wanted = identity.casefold()
    if wanted in original or set(keys) != set(original) | {wanted} or listed is None:
        return None
    if not all(isinstance(resource, Mapping) for resource in listed.values()):
        return None
    placed = _placements(listed)
    if any(placed[keys[key]] != baseline[original[key]] for key in original):
        return None
    resource, placement = listed[keys[wanted]], placed[keys[wanted]]
    if as_int(
        _network_text(resource.get("PortVLANID"))
    ) != vlan or not _network_fields_match(resource):
        return None
    if expected_placement is not None and placement != expected_placement:
        return None
    return keys[wanted]


def _client_assertions(
    added: str,
    new: list[Mapping[str, object]] | None,
    fields: tuple[str, str],
    pairing: tuple[str, str],
    collision_refused: bool | None,
    restored: bool,
    vios_side: bool,
) -> list[Assertion]:
    one = new is not None and len(new) == 1
    paired = (
        one
        and new is not None
        and tuple(str(as_int(new[0].get(field))) for field in fields) == pairing
    )
    collision = (
        []
        if collision_refused is None
        else [Assertion("slot-collision-refused", collision_refused)]
    )
    return [
        Assertion("adapter-added", one and added == "PASS"),
        Assertion("pairing-matches", paired),
        *collision,
        Assertion("adapters-equal-baseline", restored),
        Assertion("vios-side-unchanged", vios_side),
    ]


class _Arm:
    """One ST9 run: its calls, and the baselines it read for the final compare."""

    def __init__(self, client: Client, state: RunState) -> None:
        self.client = client
        self.state = state
        self.system = state.config.system_name
        self.lpar = state.config.lp3_name
        self.tag = secrets.token_hex(4)
        self.baselines: dict[str, tuple[Callable[[], Any], object]] = {}

    def skip(self, tool: str, reason: str) -> None:
        self.state.skip(SUBTASK, tool, reason)

    def note(self, label: str, tool: str, result: tuple[str, Any]) -> str:
        """Record one call's row and return its status."""
        status, data = result
        self.state.record(SUBTASK, f"{tool} ({label})", status, data)
        return status

    def data(self, label: str, tool: str, result: tuple[str, Any]) -> Any:
        """Record one read's row and return its data, or None when it failed."""
        return result[1] if self.note(label, tool, result) == "PASS" else None

    def manual(self, what: str, command: str) -> NoReturn:
        self.state.record(
            SUBTASK,
            "network round trip",
            "FAIL",
            f"MANUAL RECOVERY REQUIRED: {what} ({command})",
        )
        raise _Stop(what)

    def baseline(self, label: str, reread: Callable[[], Any], value: object) -> None:
        self.baselines[label] = (reread, value)

    async def compare_baselines(self) -> None:
        for label, (reread, value) in self.baselines.items():
            now = await reread()
            same = now == value
            self.state.record(
                SUBTASK,
                f"network baseline compare ({label})",
                "PASS" if same else "FAIL",
                None if same else {"baseline": repr(value), "now": repr(now)},
            )

    # -- reads ---------------------------------------------------------------

    async def command(self, label: str, cmd: str) -> str | None:
        result = await self.state.call(self.client, "hmc_run_command", cmd=cmd)
        text = self.data(label, "hmc_run_command", result)
        return text if isinstance(text, str) else None

    async def networks(self, label: str) -> Networks | None:
        """The system's virtual networks as UUID -> (VLAN, name); None if unreadable."""
        result = await self.state.call(
            self.client, "hmc_list_virtual_networks", system_name_or_uuid=self.system
        )
        keyed = _by_uuid(self.data(label, "hmc_list_virtual_networks", result))
        if keyed is None:
            return None
        networks = {}
        for key, resource in keyed.items():
            vlan = as_int(resource.get("NetworkVLANID"))
            if vlan is None:
                return None
            networks[key] = (vlan, str(resource.get("NetworkName", "")))
        return networks

    async def adapters(self, adapter_type: str, label: str) -> Listing | None:
        result = await self.state.call(
            self.client,
            "hmc_list_adapters",
            lpar_name_or_uuid=self.lpar,
            adapter_type=adapter_type,
            system_name_or_uuid=self.system,
        )
        return _by_uuid(
            self.data(f"{adapter_type}, {label}", "hmc_list_adapters", result)
        )

    async def placements(self, adapter_type: str, label: str) -> Adapters | None:
        """The partition's adapters of a type, each with its slot and pairing."""
        listed = await self.adapters(adapter_type, label)
        return None if listed is None else _placements(listed)

    async def scsi_rows(self, label: str) -> list[dict[str, str]] | None:
        text = await self.command(
            f"vSCSI adapters, {label}",
            f"lshwres -r virtualio --rsubtype scsi --level lpar "
            f"-m {shlex.quote(self.system)} -F {_SCSI_FIELDS}",
        )
        return None if text is None else _csv_rows(text, _SCSI_FIELDS)

    async def server_rows(
        self, adapter_type: str, vios_id: str, label: str
    ) -> frozenset[tuple[str, ...]] | None:
        """The VIOS's server adapters of the client type's kind, as comparable rows."""
        rows: Any
        if adapter_type == "VirtualSCSIClientAdapter":
            rows = await self.scsi_rows(label)
        else:
            result = await self.state.call(
                self.client, "hmc_list_fc_ports", system_name_or_uuid=self.system
            )
            rows = self.data(f"vFC adapters, {label}", "hmc_list_fc_ports", result)
        if not isinstance(rows, list):
            return None
        return frozenset(
            tuple(str(row.get(key, "")) for key in _SCSI_FIELDS.split(","))
            for row in rows
            if str(row.get("lpar_id")) == vios_id
            and row.get("adapter_type") == "server"
        )

    async def mappings(self, boundary: _Boundary, label: str) -> frozenset[str] | None:
        """The partition's mappings on the VIOS, compared without their order."""
        result = await self.state.call(
            self.client,
            "hmc_list_storage_mappings",
            vios_name_or_uuid=boundary.vios,
            lpar_name_or_uuid=self.lpar,
            system_name_or_uuid=self.system,
        )
        data = self.data(
            f"{self.lpar} mappings, {label}", "hmc_list_storage_mappings", result
        )
        if not isinstance(data, list):
            return None
        return frozenset(
            repr(sorted(m.items())) if isinstance(m, Mapping) else repr(m) for m in data
        )

    async def fc_labels(self, boundary: _Boundary, label: str) -> dict[str, str] | None:
        result = await self.state.call(
            self.client,
            "hmc_list_vios_fc_port_labels",
            system_name_or_uuid=self.system,
            vios_name=boundary.vios,
        )
        data = self.data(
            f"FC-port labels, {label}", "hmc_list_vios_fc_port_labels", result
        )
        if not isinstance(data, list):
            return None
        return {
            str(row.get("port_name")): str(row.get("port_label") or "")
            for row in data
            if isinstance(row, Mapping)
        }

    async def groups(self, label: str) -> frozenset[str] | None:
        result = await self.state.call(
            self.client,
            "hmc_list_vios_vfc_group_labels",
            system_name_or_uuid=self.system,
        )
        data = self.data(
            f"vFC group labels, {label}", "hmc_list_vios_vfc_group_labels", result
        )
        if not isinstance(data, list):
            return None
        return frozenset(
            str(group_name(row)) for row in data if isinstance(row, Mapping)
        )

    # -- preconditions -------------------------------------------------------

    async def find_boundary(self) -> _Boundary | None:
        """The test partition's id and its one serving VIOS; None unless Not Activated."""
        listing = await self.command(
            "test partition",
            f"lssyscfg -r lpar -m {shlex.quote(self.system)} "
            f"--filter {shlex.quote(build_filter([('lpar_names', self.lpar)]))} "
            "-F lpar_id,state",
        )
        fields = (listing or "").strip().split(",")
        if len(fields) != 2 or fields[1] != "Not Activated":
            self.skip("network preconditions", f"{self.lpar} is not 'Not Activated'")
            return None
        lpar_id = fields[0]
        rows = await self.scsi_rows("serving VIOS")
        servers = {
            (row["lpar_name"], row["lpar_id"])
            for row in rows or []
            if row["adapter_type"] == "server" and row["remote_lpar_id"] == lpar_id
        }
        if len(servers) != 1:
            self.skip(
                "network preconditions",
                f"{self.lpar} needs exactly one VIOS with a vSCSI server adapter "
                "toward it; only the VLAN round trip runs",
            )
            return _Boundary(lpar_id, "", "")
        ((vios, vios_id),) = servers
        used = sorted(
            (
                row["slot_num"]
                for row in rows or []
                if row["adapter_type"] == "client" and row["lpar_id"] == lpar_id
            ),
            key=lambda slot: as_int(slot) or 0,
        )
        return _Boundary(lpar_id, vios, vios_id, used[0] if used else "")

    # -- (a) VLAN and client network adapter --------------------------------

    async def vlan_baseline(self) -> tuple[Networks, int, int, Adapters] | None:
        """The network set, the test VLAN and switch, and the partition's CNAs."""
        baseline = await self.networks("baseline")
        result = await self.state.call(
            self.client, "hmc_list_virtual_switches", system_name_or_uuid=self.system
        )
        switches = self.data("baseline", "hmc_list_virtual_switches", result)
        cna = await self.placements("ClientNetworkAdapter", "baseline")
        if baseline is None or switches is None or cna is None:
            self.skip("hmc_create_virtual_network", "the baseline could not be read")
            return None
        config = self.state.config
        vlan = unused_vlan(
            {vlan for vlan, _ in baseline.values()},
            config.vlan_range_start,
            config.vlan_range_end,
        )
        if vlan is None:
            self.skip("hmc_create_virtual_network", "no unused VLAN in the range")
            return None
        ids = (as_int(get_resource(e).get("SwitchID")) for e in entries(switches))
        switch = next((sid for sid in ids if sid is not None), 0)
        self.state.artifacts.test_vlan_id = vlan
        self.state.artifacts.test_vswitch_id = switch
        self.baseline("networks", lambda: self.networks("final"), baseline)
        self.baseline(
            f"{self.lpar} ClientNetworkAdapter",
            lambda: self.placements("ClientNetworkAdapter", "final"),
            cna,
        )
        return baseline, vlan, switch, cna

    async def create_network(
        self, label: str, name: str, vlan: int, switch: int
    ) -> str:
        result = await self.state.call(
            self.client,
            "hmc_create_virtual_network",
            system_name_or_uuid=self.system,
            name=name,
            vlan_id=vlan,
            virtual_switch_id=switch,
            tagged=False,
        )
        return self.note(label, "hmc_create_virtual_network", result)

    async def vlan_round_trip(self) -> None:
        found = await self.vlan_baseline()
        if found is None:
            return
        baseline, vlan, switch, cna = found
        name = f"{NAME_PREFIX}vlan{vlan}-{self.tag}"
        created = await self.create_network("create", name, vlan, switch)
        ours = _run_networks(await self.networks("after create"), vlan, name)
        listed = ours is not None and [n for _, n in ours.values()] == [name]
        duplicate_refused = False
        if listed and ours is not None:
            self.state.artifacts.test_network_uuid = next(iter(ours))
            duplicate = await self.create_network(
                "duplicate VLAN", f"{name}-dup", vlan, switch
            )
            again = _run_networks(await self.networks("after duplicate"), vlan, name)
            duplicate_refused = duplicate == "FAIL" and again == ours
            try:
                await self.client_network_adapter(vlan, switch, cna)
            except _Stop:
                self.manual(
                    f"networks named {name}* were not deleted: the adapter on "
                    f"VLAN {vlan} was not removed first",
                    f"remove the adapter, then delete networks {name}* on "
                    f"{self.system}",
                )
        deleted, restored = await self.delete_run_networks(baseline, vlan, name)
        self.state.record_verified(
            SUBTASK,
            "hmc_create_virtual_network",
            operation="network.create_network",
            scenario="st9-virtual-network-round-trip",
            assertions=[
                Assertion("create-accepted", created == "PASS"),
                Assertion("network-listed", listed),
                Assertion("duplicate-vlan-refused", duplicate_refused),
            ],
            cleanup="passed" if restored else "failed",
            data=None,
        )
        if deleted is None:
            self.skip(
                "hmc_delete_virtual_network", "the create left no network to delete"
            )
        else:
            self.state.record_verified(
                SUBTASK,
                "hmc_delete_virtual_network",
                operation="network.delete_network",
                scenario="st9-virtual-network-round-trip",
                assertions=[
                    Assertion("delete-accepted", deleted),
                    Assertion("networks-equal-baseline", restored),
                ],
                cleanup="passed" if restored else "failed",
                data=None,
            )
        if not restored:
            self.manual(
                f"virtual networks on test VLAN {vlan} remain",
                f"delete every network named {name}* on {self.system}",
            )

    async def delete_run_networks(
        self, baseline: Networks, vlan: int, name: str
    ) -> tuple[bool | None, bool]:
        """Delete every network the run left on its VLAN.

        Returns whether every delete was accepted (None when there was nothing to
        delete) and whether the set read back equals the baseline.
        """
        ours = _run_networks(await self.networks("before delete"), vlan, name)
        if ours is None:
            return False, False
        accepted: bool | None = None
        for network_uuid in ours:
            result = await self.state.call(
                self.client,
                "hmc_delete_virtual_network",
                system_name_or_uuid=self.system,
                network_uuid=network_uuid,
            )
            status = self.note("delete", "hmc_delete_virtual_network", result)
            accepted = accepted is not False and status == "PASS"
        restored = await self.networks("after delete") == baseline
        if restored:
            self.state.artifacts.test_network_uuid = None
        return accepted, restored

    async def client_network_adapter(
        self, vlan: int, switch: int, baseline: Adapters
    ) -> None:
        kind = "ClientNetworkAdapter"
        if _network_keys(baseline) is None:
            self.manual(
                "client adapter baseline identity is unavailable",
                "inspect the baseline; no adapter was created",
            )
        result = await self.state.call(
            self.client,
            "hmc_add_network_adapter",
            lpar_name_or_uuid=self.lpar,
            port_vlan_id=vlan,
            virtual_switch_id=switch,
            system_name_or_uuid=self.system,
            addl_vlan_ids=[],
            allowed_os_mac_addrs="all",
            qos_priority="none",
            vsi_manager_id=0,
            vsi_type_id=0,
            vsi_type_version=0,
        )
        added = self.note("test VLAN", "hmc_add_network_adapter", result)
        after = await self.adapters(kind, "after add")
        identity = result[1].get("UUID") if isinstance(result[1], Mapping) else None
        fixture = _network_fixture(after, baseline, identity, vlan)
        qualified = (
            added == "PASS"
            and _network_metadata_verified(result[1])
            and fixture is not None
        )
        if not qualified or after is None or fixture is None:
            self.manual(
                "N1 creation identity or selected fields are unverified",
                "inspect adapter residue; no automatic cleanup or retry",
            )
        placement = _placements(after)[fixture]
        unknown = str(uuid.uuid4())
        if unknown.casefold() in {key.casefold() for key in after}:
            self.manual(
                "negative delete target is not absent",
                "inspect the fixture; no negative delete attempted",
            )
        result = await self.state.call(
            self.client,
            "hmc_delete_adapter",
            lpar_name_or_uuid=self.lpar,
            adapter_type=kind,
            adapter_uuid=unknown,
            system_name_or_uuid=self.system,
        )
        refused = self.note("unknown UUID", "hmc_delete_adapter", result)
        unchanged = await self.adapters(kind, "after unknown delete")
        unknown_refused = (
            refused == "FAIL"
            and _network_fixture(unchanged, baseline, fixture, vlan, placement)
            is not None
        )
        if not unknown_refused:
            self.manual(
                "unknown delete changed or obscured the adapter baseline",
                "inspect residue; no automatic cleanup or retry",
            )
        deleted, restored = await self.delete_network_fixture(
            baseline, fixture, vlan, placement
        )
        self.state.record_verified(
            SUBTASK,
            "hmc_add_network_adapter",
            operation="adapter.add_network",
            scenario="st9-client-network-adapter",
            assertions=[
                Assertion("adapter-added", added == "PASS"),
                Assertion("pvid-matches", fixture is not None),
                Assertion("creation-fields-verified", qualified),
            ],
            cleanup="passed" if restored else "failed",
            data=None,
        )
        self.state.record_verified(
            SUBTASK,
            "hmc_delete_adapter",
            operation="adapter.delete",
            scenario="st9-client-network-adapter",
            assertions=[
                Assertion("delete-accepted", deleted),
                Assertion("unknown-uuid-refused", unknown_refused),
                Assertion("adapters-equal-baseline", restored),
            ],
            cleanup="passed" if restored else "failed",
            data=None,
        )

    async def delete_network_fixture(
        self, baseline: Adapters, fixture: str, vlan: int, placement: tuple[str, ...]
    ) -> tuple[bool, bool]:
        """Delete only the independently proven network fixture, never a delta."""
        current = await self.adapters(
            "ClientNetworkAdapter", "before verified fixture delete"
        )
        target = _network_fixture(current, baseline, fixture, vlan, placement)
        if target is None:
            self.manual(
                "adapter drift before fixture cleanup",
                "inspect residue; no automatic deletion",
            )
        result = await self.state.call(
            self.client,
            "hmc_delete_adapter",
            lpar_name_or_uuid=self.lpar,
            adapter_type="ClientNetworkAdapter",
            adapter_uuid=target,
            system_name_or_uuid=self.system,
        )
        accepted = (
            self.note("verified fixture delete", "hmc_delete_adapter", result) == "PASS"
        )
        after = await self.placements(
            "ClientNetworkAdapter", "after verified fixture delete"
        )
        restored = (
            after is not None
            and _network_keys(after) is not None
            and {key.casefold(): value for key, value in after.items()}
            == {key.casefold(): value for key, value in baseline.items()}
        )
        if not accepted or not restored:
            self.manual(
                "fixture deletion or restoration is unconfirmed",
                "inspect residue; no retry or later mutation",
            )
        return accepted, restored

    async def remove_new_adapters(
        self, adapter_type: str, baseline: Adapters
    ) -> tuple[bool | None, bool, str]:
        """Delete every adapter of `adapter_type` the run added; confirm by re-reading.

        A new UUID placed exactly where a vanished baseline adapter was is that
        adapter re-identified, not the run's: it is left alone, and the set then
        reads as off the baseline.
        """
        current = await self.placements(adapter_type, "before delete")
        if current is None:
            return False, False, placement_drift(baseline, None)
        vanished = {baseline[k] for k in baseline if k not in current}
        accepted: bool | None = None
        for adapter_uuid in sorted(k for k in current if k not in baseline):
            if current[adapter_uuid] in vanished:
                accepted = False
                continue
            result = await self.state.call(
                self.client,
                "hmc_delete_adapter",
                lpar_name_or_uuid=self.lpar,
                adapter_type=adapter_type,
                adapter_uuid=adapter_uuid,
                system_name_or_uuid=self.system,
            )
            status = self.note("delete", "hmc_delete_adapter", result)
            accepted = accepted is not False and status == "PASS"
        after = await self.placements(adapter_type, "after delete")
        return accepted, after == baseline, placement_drift(baseline, after)

    # -- (b, c) vSCSI and vFC clients ----------------------------------------

    async def client_baseline(
        self, adapter_type: str, boundary: _Boundary
    ) -> tuple[frozenset[tuple[str, ...]], Adapters, frozenset[str], str] | None:
        """The VIOS server side, the partition's clients, its mappings and the slot."""
        servers = await self.server_rows(adapter_type, boundary.vios_id, "baseline")
        clients = await self.placements(adapter_type, "baseline")
        mappings = await self.mappings(boundary, "baseline")
        if servers is None or clients is None or mappings is None:
            self.skip(f"{adapter_type} round trip", "the baseline could not be read")
            return None
        slot = _server_slot(servers, boundary.lpar_id)
        if slot is None:
            self.skip(
                f"{adapter_type} round trip",
                f"{boundary.vios} has no server slot of this kind assigned to "
                f"{self.lpar} (gap: slots open to any partition are not used)",
            )
            return None
        self.baseline(
            f"{boundary.vios} {adapter_type} servers",
            lambda: self.server_rows(adapter_type, boundary.vios_id, "final"),
            servers,
        )
        self.baseline(
            f"{self.lpar} {adapter_type}",
            lambda: self.placements(adapter_type, "final"),
            clients,
        )
        return servers, clients, mappings, slot

    async def add_client(
        self,
        label: str,
        adapter_type: str,
        boundary: _Boundary,
        slot: str,
        slot_number: int | None = None,
    ) -> str:
        if adapter_type == "VirtualSCSIClientAdapter":
            result = await self.state.call(
                self.client,
                "hmc_add_vscsi_adapter",
                lpar_name_or_uuid=self.lpar,
                vios_partition_id=int(boundary.vios_id),
                vios_slot=int(slot),
                slot_number=slot_number,
                system_name_or_uuid=self.system,
            )
            return self.note(label, "hmc_add_vscsi_adapter", result)
        result = await self.state.call(
            self.client,
            "hmc_add_vfc_adapter",
            lpar_name_or_uuid=self.lpar,
            vios_partition_id=int(boundary.vios_id),
            vios_slot=int(slot),
            slot_number=slot_number,
            system_name_or_uuid=self.system,
        )
        return self.note(label, "hmc_add_vfc_adapter", result)

    async def slot_collision(
        self, adapter_type: str, boundary: _Boundary, slot: str, clients: Adapters
    ) -> bool | None:
        """Whether an add on a client slot already in use is refused and changes nothing.

        None when the test partition has no client slot to collide with. Anything
        an accepted collision added is removed by the round trip's reversal.
        """
        if not boundary.used_slot:
            return None
        status = await self.add_client(
            "slot collision", adapter_type, boundary, slot, int(boundary.used_slot)
        )
        after = await self.placements(adapter_type, "after slot collision")
        return status == "FAIL" and after == clients

    async def client_round_trip(self, adapter_type: str, boundary: _Boundary) -> None:
        found = await self.client_baseline(adapter_type, boundary)
        if found is None:
            return
        servers, clients, mappings, slot = found
        collision_refused = await self.slot_collision(
            adapter_type, boundary, slot, clients
        )
        added = await self.add_client("paired", adapter_type, boundary, slot)
        new = _new(await self.adapters(adapter_type, "after add"), clients)
        _, restored, drift = await self.remove_new_adapters(adapter_type, clients)
        servers_after = await self.server_rows(
            adapter_type, boundary.vios_id, "after delete"
        )
        vios_side = (
            servers_after == servers
            and await self.mappings(boundary, "after delete") == mappings
        )
        cleanup = "passed" if restored and vios_side else "failed"
        fields = _REMOTE_FIELDS[adapter_type]
        pairing = (boundary.vios_id, slot)
        if adapter_type == "VirtualSCSIClientAdapter":
            self.state.record_verified(
                SUBTASK,
                "hmc_add_vscsi_adapter",
                operation="adapter.add_vscsi",
                scenario="st9-vscsi-client-adapter",
                assertions=_client_assertions(
                    added, new, fields, pairing, collision_refused, restored, vios_side
                ),
                cleanup=cleanup,
                data=None,
            )
        else:
            self.state.record_verified(
                SUBTASK,
                "hmc_add_vfc_adapter",
                operation="adapter.add_vfc",
                scenario="st9-vfc-client-adapter",
                assertions=_client_assertions(
                    added, new, fields, pairing, collision_refused, restored, vios_side
                ),
                cleanup=cleanup,
                data=None,
            )
        if not restored:
            self.manual(f"{self.lpar}'s {adapter_type} set is off its baseline", drift)
        if not vios_side:
            self.manual(
                f"{boundary.vios}'s server adapters or {self.lpar}'s mappings changed",
                f"compare lshwres -r virtualio --level lpar on {boundary.vios} "
                "with the baseline rows in the results document",
            )

    # -- (d) FC-port label ---------------------------------------------------

    async def set_port_label(
        self, label: str, boundary: _Boundary, value: str, port: str
    ) -> str:
        result = await self.state.call(
            self.client,
            "hmc_set_vios_fc_port_label",
            system_name_or_uuid=self.system,
            label=value,
            port_name=port,
            vios_name=boundary.vios,
        )
        return self.note(label, "hmc_set_vios_fc_port_label", result)

    async def remove_port_label(
        self, label: str, boundary: _Boundary, port: str
    ) -> str:
        result = await self.state.call(
            self.client,
            "hmc_remove_vios_fc_port_label",
            system_name_or_uuid=self.system,
            port_name=port,
            vios_name=boundary.vios,
        )
        return self.note(label, "hmc_remove_vios_fc_port_label", result)

    async def port_label_baseline(
        self, boundary: _Boundary
    ) -> tuple[dict[str, str], str, str] | None:
        """The VIOS's FC-port labels, and the port and label the round trip uses."""
        baseline = await self.fc_labels(boundary, "baseline")
        if not baseline:
            self.skip(
                "hmc_set_vios_fc_port_label",
                f"{boundary.vios}'s FC-port labels could not be read or list no port",
            )
            return None
        port, original = next(iter(baseline.items()))
        if original and not _plain_label(original):
            self.skip(
                "hmc_set_vios_fc_port_label",
                f"{port}'s current label cannot be restored exactly",
            )
            return None
        self.baseline(
            f"{boundary.vios} FC-port labels",
            lambda: self.fc_labels(boundary, "final"),
            baseline,
        )
        return baseline, port, original

    async def fc_port_label_round_trip(self, boundary: _Boundary) -> None:
        found = await self.port_label_baseline(boundary)
        if found is None:
            return
        baseline, port, original = found
        test = f"{LABEL_PREFIX}{self.tag}"
        await self.set_port_label("test label", boundary, test, port)
        after_set = await self.fc_labels(boundary, "after set")
        label_set = after_set is not None and after_set.get(port) == test
        negative = await self.set_port_label("absent port", boundary, test, ABSENT_PORT)
        if negative == "PASS":
            await self.remove_port_label("absent port", boundary, ABSENT_PORT)
        after_negative = await self.fc_labels(boundary, "after absent port")
        refused = negative == "FAIL" and after_negative == after_set
        removed = await self.remove_port_label("remove", boundary, port)
        after_remove = await self.fc_labels(boundary, "after remove")
        label_removed = (
            removed == "PASS"
            and after_remove is not None
            and not after_remove.get(port)
        )
        if original and (after_remove or {}).get(port) != original:
            await self.set_port_label("restore", boundary, original, port)
        restored = await self.fc_labels(boundary, "after restore") == baseline
        cleanup = "passed" if restored else "failed"
        self.state.record_verified(
            SUBTASK,
            "hmc_set_vios_fc_port_label",
            operation="vios_label.set_fc_port",
            scenario="st9-fc-port-label",
            assertions=[
                Assertion("label-set", label_set),
                Assertion("unknown-port-refused", refused),
                Assertion("labels-equal-baseline", restored),
            ],
            cleanup=cleanup,
            data=None,
        )
        self.state.record_verified(
            SUBTASK,
            "hmc_remove_vios_fc_port_label",
            operation="vios_label.remove_fc_port",
            scenario="st9-fc-port-label",
            assertions=[
                Assertion("label-removed", label_removed),
                Assertion("labels-equal-baseline", restored),
            ],
            cleanup=cleanup,
            data=None,
        )
        if not restored:
            operation = f"-o s -l {shlex.quote(original)}" if original else "-o r"
            try:
                record = build_attribute_record(
                    [
                        ("resource", "fcport"),
                        ("port_name", port),
                        ("vios_names", boundary.vios),
                    ]
                )
                command = (
                    f"labelvios -m {shlex.quote(self.system)} {operation} "
                    f"-i {shlex.quote(record)}"
                )
            except (HMCError, ValueError):
                command = (
                    f"labelvios {operation} for port {port!r} on {boundary.vios!r}"
                )
            self.manual(f"{boundary.vios} {port} label is not its original", command)

    # -- (e) vFC group label -------------------------------------------------

    async def create_group(self, label: str, boundary: _Boundary, name: str) -> str:
        result = await self.state.call(
            self.client,
            "hmc_create_vios_vfc_group_label",
            system_name_or_uuid=self.system,
            label=name,
            vios_names=[boundary.vios],
        )
        return self.note(label, "hmc_create_vios_vfc_group_label", result)

    async def rename_group(self, name: str, renamed: str) -> str:
        result = await self.state.call(
            self.client,
            "hmc_update_vios_vfc_group_label",
            system_name_or_uuid=self.system,
            label=name,
            action="rename",
            new_name=renamed,
        )
        return self.note("rename", "hmc_update_vios_vfc_group_label", result)

    async def remove_run_groups(
        self, baseline: frozenset[str], names: frozenset[str]
    ) -> bool | None:
        """Remove every group the run left; None when there was none to remove."""
        current = await self.groups("before remove")
        removed: bool | None = None
        for label in sorted(names if current is None else (current - baseline) & names):
            result = await self.state.call(
                self.client,
                "hmc_remove_vios_vfc_group_label",
                system_name_or_uuid=self.system,
                label=label,
            )
            status = self.note("remove", "hmc_remove_vios_vfc_group_label", result)
            removed = removed is not False and status == "PASS"
        return removed

    async def group_label_round_trip(self, boundary: _Boundary) -> None:
        baseline = await self.groups("baseline")
        name = f"{LABEL_PREFIX}{self.tag}"
        renamed = f"{name}-r"
        if baseline is None or {name, renamed} & baseline:
            self.skip(
                "hmc_create_vios_vfc_group_label", "the group labels could not be read"
            )
            return
        self.baseline("vFC group labels", lambda: self.groups("final"), baseline)
        created = await self.create_group("create", boundary, name)
        after_create = await self.groups("after create")
        listed = created == "PASS" and after_create == baseline | {name}
        duplicate = await self.create_group("duplicate", boundary, name)
        after_duplicate = await self.groups("after duplicate")
        duplicate_refused = duplicate == "FAIL" and after_duplicate == after_create
        renamed_ok = False
        if listed:
            await self.rename_group(name, renamed)
            renamed_ok = await self.groups("after rename") == baseline | {renamed}
        removed = await self.remove_run_groups(baseline, frozenset({name, renamed}))
        restored = await self.groups("after remove") == baseline
        cleanup = "passed" if restored else "failed"
        self.state.record_verified(
            SUBTASK,
            "hmc_create_vios_vfc_group_label",
            operation="vios_label.create_vfc_group",
            scenario="st9-vfc-group-label",
            assertions=[
                Assertion("group-created", listed),
                Assertion("duplicate-refused", duplicate_refused),
            ],
            cleanup=cleanup,
            data=None,
        )
        if listed:
            self.state.record_verified(
                SUBTASK,
                "hmc_update_vios_vfc_group_label",
                operation="vios_label.update_vfc_group",
                scenario="st9-vfc-group-label",
                assertions=[Assertion("group-renamed", renamed_ok)],
                cleanup=cleanup,
                data=None,
            )
        if removed is not None:
            self.state.record_verified(
                SUBTASK,
                "hmc_remove_vios_vfc_group_label",
                operation="vios_label.remove_vfc_group",
                scenario="st9-vfc-group-label",
                assertions=[
                    Assertion("group-removed", removed),
                    Assertion("groups-equal-baseline", restored),
                ],
                cleanup=cleanup,
                data=None,
            )
        if not restored:
            self.manual(
                f"vFC group labels {name}* remain",
                f"labelvios -m {shlex.quote(self.system)} -o r -l <label>",
            )


async def mutate_virtual_networking(client: Client, state: RunState) -> None:
    print("\n=== ST9: Virtual network and adapter round trips ===")
    if state.group != GROUP:
        state.skip(SUBTASK, "network arm", "runs only in the network arm")
        return
    arm = _Arm(client, state)
    boundary = await arm.find_boundary()
    if boundary is None:
        return
    try:
        await arm.vlan_round_trip()
        if boundary.vios:
            await arm.client_round_trip("VirtualSCSIClientAdapter", boundary)
            await arm.client_round_trip("VirtualFibreChannelClientAdapter", boundary)
            await arm.fc_port_label_round_trip(boundary)
            await arm.group_label_round_trip(boundary)
    except _Stop:
        arm.skip("remaining round trips", "stopped after a reversal was not confirmed")
    await arm.compare_baselines()
