"""Presentation-neutral, verified SSH network workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Literal

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.operations.lpar.ownership import resolve_and_authorize_lpar_names
from hmcpctl.operations.virtualization.pcie import (
    eth_capacity_granularity,
    require_admitted_environment,
    require_capacity_granularity,
)
from hmcpctl.operations.virtualization.validation import (
    require_command_safe_text,
    validate_capacity_percent,
)
from hmcpctl.ssh.io_inventory import (
    list_fc_ports as _list_fc_ports,
)
from hmcpctl.ssh.io_inventory import (
    list_sea_adapters as _list_sea_adapters,
)
from hmcpctl.ssh.selectors import resolve_ssh_names
from hmcpctl.ssh.sriov import (
    list_sriov_adapter_rows,
    list_sriov_configured_logical_port_rows,
    list_sriov_physical_port_rows,
)
from hmcpctl.ssh.vnic import (
    add_vnic_backing,
    list_vnic_backing_rows,
    list_vnic_rows,
    read_vios_identity,
    remove_vnic_slot,
)
from hmcpctl.ssh.vnic import (
    list_vnics as _list_vnics,
)


@dataclass(frozen=True)
class VnicBackingSelector:
    vios_name: str = field(metadata={"description": "VIOS partition name."})
    vios_lpar_id: str = field(metadata={"description": "VIOS partition ID."})
    adapter_id: str = field(metadata={"description": "SR-IOV adapter identifier."})
    physical_port_id: str = field(metadata={"description": "Physical-port identifier."})
    capacity_percent: Decimal = field(
        metadata={"description": "Requested decimal capacity percentage."}
    )


@dataclass(frozen=True)
class VnicBackingSnapshot:
    vios_name: str
    vios_lpar_id: str
    adapter_id: str
    physical_port_id: str
    logical_port_id: str
    capacity_percent: Decimal
    desired_capacity_percent: Decimal
    maximum_capacity_percent: Decimal
    desired_maximum_capacity_percent: Decimal
    failover_priority: str
    is_active: bool
    status: str


@dataclass(frozen=True)
class VnicSnapshot:
    lpar_name: str
    lpar_id: str
    slot_num: str
    port_vlan_id: int
    backing_devices: tuple[VnicBackingSnapshot, ...]


@dataclass(frozen=True)
class VnicChangeResult:
    operation: Literal["add", "remove"]
    mutation_dispatched: bool
    changed: bool | None
    selector: VnicBackingSelector | None
    slot_num: str | None
    vnic_before: tuple[VnicSnapshot, ...]
    backing_before: tuple[VnicBackingSnapshot, ...]
    vnic_after: tuple[VnicSnapshot, ...]
    backing_after: tuple[VnicBackingSnapshot, ...]
    vnic_after_read_succeeded: bool
    backing_after_read_succeeded: bool
    output: str
    errors: tuple[str, ...]


@dataclass(frozen=True)
class _VnicPreflightContext:
    system_name: str
    lpar_name: str
    vnics: tuple[VnicSnapshot, ...]
    backings: tuple[VnicBackingSnapshot, ...]
    used_capacity: Decimal
    granularity: Decimal | None


@dataclass(frozen=True)
class _VnicReadback:
    vnics: tuple[VnicSnapshot, ...]
    backings: tuple[VnicBackingSnapshot, ...]
    vnic_succeeded: bool
    backing_succeeded: bool
    errors: tuple[str, ...]
    cause: Exception | None


class VnicCapabilityError(RuntimeError):
    """Raised when evidence does not admit a requested mutation."""


class VnicPartialError(RuntimeError):
    """Raised when a dispatched mutation cannot be fully reconciled."""

    def __init__(self, message: str, result: VnicChangeResult):
        super().__init__(message)
        self.result = result


async def list_fc_ports(
    hmc: HMCClient, lpar_name_or_uuid: str | None = None, *, system_name_or_uuid: str
) -> list[dict[str, str]]:
    """List Fibre Channel ports, optionally scoped to one LPAR."""
    config = hmc.config
    system_name, lpar_name = await resolve_ssh_names(
        hmc, system_name_or_uuid, lpar_name_or_uuid
    )
    return await _list_fc_ports(config, system_name, lpar_name)


async def list_sea_adapters(
    hmc: HMCClient, lpar_name_or_uuid: str | None = None, *, system_name_or_uuid: str
) -> list[dict[str, str]]:
    """List shared Ethernet adapters, optionally scoped to one LPAR."""
    config = hmc.config
    system_name, lpar_name = await resolve_ssh_names(
        hmc, system_name_or_uuid, lpar_name_or_uuid
    )
    return await _list_sea_adapters(config, system_name, lpar_name)


async def list_vnics(
    hmc: HMCClient, lpar_name_or_uuid: str, *, system_name_or_uuid: str
) -> list[dict[str, object]]:
    """List vNICs on an LPAR."""
    config = hmc.config
    system_name, lpar_name = await resolve_ssh_names(
        hmc, system_name_or_uuid, lpar_name_or_uuid
    )
    return await _list_vnics(config, system_name, lpar_name)


def _decimal(value: str, field: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{field} must be a decimal percentage") from error
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal percentage")
    return result


def _validate_vnic_backing_selector(
    selector: VnicBackingSelector,
) -> VnicBackingSelector:
    return VnicBackingSelector(
        require_command_safe_text(selector.vios_name, "vios_name"),
        require_command_safe_text(selector.vios_lpar_id, "vios_lpar_id"),
        require_command_safe_text(selector.adapter_id, "adapter_id"),
        require_command_safe_text(selector.physical_port_id, "physical_port_id"),
        validate_capacity_percent(selector.capacity_percent),
    )


def _parse_backing_snapshot(row: dict[str, str]) -> VnicBackingSnapshot:
    if row["type"] != "sriov":
        raise ValueError("vNIC backing row type must be sriov")
    return VnicBackingSnapshot(
        row["lpar_name"],
        row["lpar_id"],
        row["adapter_id"],
        row["physical_port_id"],
        row["logical_port_id"],
        _decimal(row["capacity"], "capacity"),
        _decimal(row["desired_capacity"], "desired_capacity"),
        _decimal(row["max_capacity"], "max_capacity"),
        _decimal(row["desired_max_capacity"], "desired_max_capacity"),
        row["failover_priority"],
        row["is_active"] == "1",
        row["status"],
    )


def _parse_backing_states(value: str) -> dict[str, tuple[bool, str]]:
    """Map logical port ID to (is_active, status) from ``sriov/<port>/<0|1>/<status>``."""
    states: dict[str, tuple[bool, str]] = {}
    for record in value.split(","):
        parts = record.split("/")
        if len(parts) != 4 or parts[0] != "sriov" or parts[2] not in ("0", "1"):
            raise ValueError("vNIC backing_device_states row has an unsupported shape")
        if parts[1] in states:
            raise ValueError("vNIC backing_device_states repeats a logical port")
        states[parts[1]] = (parts[2] == "1", parts[3])
    return states


def _parse_embedded_backings(
    value: str, states_value: str
) -> tuple[VnicBackingSnapshot, ...]:
    if not value.strip() or value == "none":
        return ()
    states = _parse_backing_states(states_value)
    result: list[VnicBackingSnapshot] = []
    for record in value.split(","):
        parts = record.split("/")
        if len(parts) != 11 or parts[0] != "sriov":
            raise ValueError("vNIC backing_devices row has an unsupported shape")
        if parts[5] not in states:
            raise ValueError(
                "vNIC backing_device_states has no record for a backing_devices port"
            )
        is_active, status = states[parts[5]]
        result.append(
            VnicBackingSnapshot(
                parts[1],
                parts[2],
                parts[3],
                parts[4],
                parts[5],
                _decimal(parts[6], "capacity"),
                _decimal(parts[7], "desired_capacity"),
                _decimal(parts[9], "max_capacity"),
                _decimal(parts[10], "desired_max_capacity"),
                parts[8],
                is_active,
                status,
            )
        )
    if len(states) != len(result):
        raise ValueError(
            "vNIC backing_device_states has a record for no backing_devices port"
        )
    return tuple(result)


def _parse_vnic_snapshots(rows: list[dict[str, str]]) -> tuple[VnicSnapshot, ...]:
    result = tuple(
        VnicSnapshot(
            row["lpar_name"],
            row["lpar_id"],
            row["slot_num"],
            int(row["port_vlan_id"]),
            _parse_embedded_backings(
                row["backing_devices"], row["backing_device_states"]
            ),
        )
        for row in rows
    )
    slots = [item.slot_num for item in result]
    if len(slots) != len(set(slots)):
        raise ValueError("duplicate vNIC slot inventory rows")
    return result


def _backing_matches_selector(
    item: VnicBackingSnapshot, selector: VnicBackingSelector
) -> bool:
    return (
        item.vios_name,
        item.vios_lpar_id,
        item.adapter_id,
        item.physical_port_id,
        item.desired_capacity_percent,
    ) == (
        selector.vios_name,
        selector.vios_lpar_id,
        selector.adapter_id,
        selector.physical_port_id,
        selector.capacity_percent,
    )


def _vnic_contains_backing_identity(
    vnic: VnicSnapshot, backing: VnicBackingSnapshot
) -> bool:
    return any(_same_backing_identity(item, backing) for item in vnic.backing_devices)


def _same_backing_identity(
    left: VnicBackingSnapshot, right: VnicBackingSnapshot
) -> bool:
    return (
        left.vios_name,
        left.vios_lpar_id,
        left.adapter_id,
        left.physical_port_id,
        left.logical_port_id,
        left.capacity_percent,
        left.desired_capacity_percent,
    ) == (
        right.vios_name,
        right.vios_lpar_id,
        right.adapter_id,
        right.physical_port_id,
        right.logical_port_id,
        right.capacity_percent,
        right.desired_capacity_percent,
    )


def _active_matching_backing_pairs(
    vnics: tuple[VnicSnapshot, ...],
    backings: tuple[VnicBackingSnapshot, ...],
    selector: VnicBackingSelector,
    vlan: int,
) -> list[tuple[VnicSnapshot, VnicBackingSnapshot]]:
    return [
        (vnic, backing)
        for vnic in vnics
        if vnic.port_vlan_id == vlan
        for backing in backings
        if _backing_matches_selector(backing, selector)
        and _vnic_contains_backing_identity(vnic, backing)
        and backing.is_active
        and backing.status == "Operational"
    ]


def _matching_vnics(
    vnics: tuple[VnicSnapshot, ...], selector: VnicBackingSelector, vlan: int
) -> tuple[VnicSnapshot, ...]:
    return tuple(
        vnic
        for vnic in vnics
        if vnic.port_vlan_id == vlan
        and any(
            _backing_matches_selector(item, selector) for item in vnic.backing_devices
        )
    )


def _correlated_matching_backings(
    vnics: tuple[VnicSnapshot, ...],
    backings: tuple[VnicBackingSnapshot, ...],
    selector: VnicBackingSelector,
) -> tuple[VnicBackingSnapshot, ...]:
    return tuple(
        item
        for item in backings
        if _backing_matches_selector(item, selector)
        and any(_vnic_contains_backing_identity(vnic, item) for vnic in vnics)
    )


def _reconciled_logical_ports(
    direct: list[dict[str, str]],
    backings: tuple[VnicBackingSnapshot, ...],
) -> dict[tuple[str, str], tuple[str, Decimal]]:
    direct_observations: dict[tuple[str, str], tuple[str, Decimal]] = {}
    for row in direct:
        key = row["adapter_id"], row["logical_port_id"]
        observation = row["phys_port_id"], _decimal(row["capacity"], "capacity")
        if key in direct_observations:
            raise ValueError("duplicate direct logical-port inventory")
        direct_observations[key] = observation
    backing_observations: dict[tuple[str, str], tuple[str, Decimal]] = {}
    for item in backings:
        key = item.adapter_id, item.logical_port_id
        observation = item.physical_port_id, item.desired_capacity_percent
        if key in backing_observations:
            raise ValueError("duplicate backing logical-port inventory")
        backing_observations[key] = observation
    for key in direct_observations.keys() & backing_observations.keys():
        if direct_observations[key] != backing_observations[key]:
            raise ValueError("conflicting cross-projection logical-port capacity")
    return direct_observations | backing_observations


def _used_port_capacity(
    observations: dict[tuple[str, str], tuple[str, Decimal]],
    selector: VnicBackingSelector,
) -> Decimal:
    return sum(
        (
            capacity
            for (adapter, _), (port, capacity) in observations.items()
            if adapter == selector.adapter_id and port == selector.physical_port_id
        ),
        Decimal(),
    )


async def _preflight_add(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    selector: VnicBackingSelector,
    ownership_override: bool,
) -> _VnicPreflightContext:
    system_name, lpar_name = await resolve_and_authorize_lpar_names(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    config = hmc.config
    await require_admitted_environment(config, system_name)
    identity = await read_vios_identity(config, system_name, selector.vios_name)
    if (
        identity["name"] != selector.vios_name
        or identity["lpar_id"] != selector.vios_lpar_id
        or identity["lpar_env"] != "vioserver"
    ):
        raise VnicCapabilityError(
            "selected VIOS name, ID, or partition type does not match inventory"
        )
    adapters = [
        row
        for row in await list_sriov_adapter_rows(config, system_name)
        if row["adapter_id"] == selector.adapter_id
    ]
    if (
        len(adapters) != 1
        or adapters[0]["config_state"] != "sriov"
        or adapters[0]["functional_state"] != "1"
    ):
        raise VnicCapabilityError("adapter is not in healthy SR-IOV mode")
    ports = [
        row
        for row in await list_sriov_physical_port_rows(
            config, system_name, selector.adapter_id
        )
        if row["phys_port_id"] == selector.physical_port_id
    ]
    if len(ports) != 1 or ports[0]["state"] != "1":
        raise VnicCapabilityError("physical port is unavailable or mismatched")
    granularity = eth_capacity_granularity(ports[0])
    direct = await list_sriov_configured_logical_port_rows(
        config, system_name, selector.adapter_id
    )
    before = _parse_vnic_snapshots(await list_vnic_rows(config, system_name, lpar_name))
    backings = tuple(
        _parse_backing_snapshot(row)
        for row in await list_vnic_backing_rows(config, system_name)
    )
    observations = _reconciled_logical_ports(direct, backings)
    return _VnicPreflightContext(
        system_name,
        lpar_name,
        before,
        backings,
        _used_port_capacity(observations, selector),
        granularity,
    )


async def _read_vnic_state_after_mutation(
    config: HMCConfig, system: str, lpar: str
) -> _VnicReadback:
    vnics: tuple[VnicSnapshot, ...] = ()
    backings: tuple[VnicBackingSnapshot, ...] = ()
    v_ok = b_ok = False
    errors: list[str] = []
    cause: Exception | None = None
    try:
        vnics = _parse_vnic_snapshots(await list_vnic_rows(config, system, lpar))
        v_ok = True
    except Exception as error:  # noqa: BLE001 - captured into the readback result and reconciled by the caller
        cause = error
        errors.append(f"vNIC reconciliation read failed: {error}")
    try:
        backings = tuple(
            _parse_backing_snapshot(row)
            for row in await list_vnic_backing_rows(config, system)
        )
        b_ok = True
    except Exception as error:  # noqa: BLE001 - captured into the readback result and reconciled by the caller
        cause = cause or error
        errors.append(f"backing reconciliation read failed: {error}")
    return _VnicReadback(vnics, backings, v_ok, b_ok, tuple(errors), cause)


def _add_payload(selector: VnicBackingSelector) -> str:
    return "/".join(
        (
            "sriov",
            selector.vios_name,
            selector.vios_lpar_id,
            selector.adapter_id,
            selector.physical_port_id,
            str(selector.capacity_percent),
        )
    )


def _reconcile_add(
    context: _VnicPreflightContext,
    readback: _VnicReadback,
    selector: VnicBackingSelector,
    port_vlan_id: int,
    output: str,
    mutation_errors: list[str],
) -> VnicChangeResult:
    errors = [*mutation_errors, *readback.errors]
    candidates = _matching_vnics(context.vnics, selector, port_vlan_id)
    backing_before = _correlated_matching_backings(
        candidates, context.backings, selector
    )
    before_slots = {item.slot_num for item in context.vnics}
    matching_after = (
        _matching_vnics(readback.vnics, selector, port_vlan_id)
        if readback.vnic_succeeded
        else ()
    )
    backing_after = (
        _correlated_matching_backings(matching_after, readback.backings, selector)
        if readback.vnic_succeeded and readback.backing_succeeded
        else ()
    )
    observed_new = tuple(
        item for item in matching_after if item.slot_num not in before_slots
    )
    new_pairs = tuple(
        pair
        for pair in _active_matching_backing_pairs(
            readback.vnics, readback.backings, selector, port_vlan_id
        )
        if pair[0].slot_num not in before_slots
    )
    final = (
        readback.vnic_succeeded
        and readback.backing_succeeded
        and len(observed_new) == 1
        and len(new_pairs) == 1
        and len(backing_after) == 1
    )
    unchanged = (
        readback.vnic_succeeded
        and readback.backing_succeeded
        and matching_after == candidates
        and backing_after == backing_before
    )
    if not final:
        errors.append(
            "add reconciliation did not prove exactly one new active Operational backing"
        )
    return VnicChangeResult(
        operation="add",
        mutation_dispatched=True,
        changed=True if final else False if unchanged else None,
        selector=selector,
        slot_num=observed_new[0].slot_num if len(observed_new) == 1 else None,
        vnic_before=candidates,
        backing_before=backing_before,
        vnic_after=matching_after,
        backing_after=backing_after,
        vnic_after_read_succeeded=readback.vnic_succeeded,
        backing_after_read_succeeded=readback.backing_succeeded,
        output=output,
        errors=tuple(errors),
    )


def _reconcile_remove(
    selected: tuple[VnicSnapshot, ...],
    correlated: tuple[VnicBackingSnapshot, ...],
    selector: VnicBackingSelector,
    slot_num: str,
    readback: _VnicReadback,
    output: str,
    mutation_errors: list[str],
) -> VnicChangeResult:
    captured = correlated[0]
    errors = [*mutation_errors, *readback.errors]
    matching_after = (
        tuple(item for item in readback.vnics if item.slot_num == slot_num)
        if readback.vnic_succeeded
        else ()
    )
    backing_after = (
        tuple(
            item for item in readback.backings if _same_backing_identity(item, captured)
        )
        if readback.backing_succeeded
        else ()
    )
    final = (
        readback.vnic_succeeded
        and readback.backing_succeeded
        and not matching_after
        and not backing_after
    )
    unchanged = (
        readback.vnic_succeeded
        and readback.backing_succeeded
        and selected[0] in readback.vnics
        and bool(backing_after)
    )
    if not final:
        errors.append(
            "remove reconciliation did not prove the slot and captured backing absent"
        )
    return VnicChangeResult(
        operation="remove",
        mutation_dispatched=True,
        changed=True if final else False if unchanged else None,
        selector=selector,
        slot_num=slot_num,
        vnic_before=selected,
        backing_before=correlated,
        vnic_after=matching_after,
        backing_after=backing_after,
        vnic_after_read_succeeded=readback.vnic_succeeded,
        backing_after_read_succeeded=readback.backing_succeeded,
        output=output,
        errors=tuple(errors),
    )


async def add_vnic(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    selector: VnicBackingSelector,
    port_vlan_id: int,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> VnicChangeResult:
    """Add a vNIC backing and reconcile the resulting state.

    Raises:
        ValueError: If selectors, VLAN, capacity granularity, or available capacity
            are invalid.
        VnicCapabilityError: If current inventory forbids the mutation.
        VnicPartialError: If a dispatched mutation cannot be reconciled.
    """
    selector = _validate_vnic_backing_selector(selector)
    if type(port_vlan_id) is not int or not 0 <= port_vlan_id <= 4094:
        raise ValueError("port_vlan_id must be an integer between 0 and 4094")
    context = await _preflight_add(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        selector,
        ownership_override,
    )
    candidates = _matching_vnics(context.vnics, selector, port_vlan_id)
    matching_backing_before = _correlated_matching_backings(
        candidates, context.backings, selector
    )
    pairs = _active_matching_backing_pairs(
        candidates, matching_backing_before, selector, port_vlan_id
    )
    if len(pairs) == 1 and len(candidates) == 1 and len(matching_backing_before) == 1:
        return VnicChangeResult(
            operation="add",
            mutation_dispatched=False,
            changed=False,
            selector=selector,
            slot_num=pairs[0][0].slot_num,
            vnic_before=candidates,
            backing_before=matching_backing_before,
            vnic_after=candidates,
            backing_after=matching_backing_before,
            vnic_after_read_succeeded=True,
            backing_after_read_succeeded=True,
            output="",
            errors=(),
        )
    if pairs or candidates or matching_backing_before:
        raise VnicCapabilityError(
            "existing matching vNIC inventory is ambiguous or degraded"
        )
    require_capacity_granularity(selector.capacity_percent, context.granularity)
    if context.used_capacity + selector.capacity_percent > 100:
        raise ValueError(f"capacity exhausted: {context.used_capacity}% used of 100%")
    payload = _add_payload(selector)
    output = ""
    errors: list[str] = []
    mutation_error: Exception | None = None
    try:
        output = await add_vnic_backing(
            hmc.config,
            context.system_name,
            context.lpar_name,
            payload,
            port_vlan_id,
        )
    except Exception as error:  # noqa: BLE001 - captured into the readback result and reconciled by the caller
        mutation_error = error
        errors.append(f"mutation failed: {error}")
    readback = await _read_vnic_state_after_mutation(
        hmc.config, context.system_name, context.lpar_name
    )
    result = _reconcile_add(context, readback, selector, port_vlan_id, output, errors)
    if result.errors:
        partial = VnicPartialError("vNIC add could not be fully verified", result)
        cause = mutation_error or readback.cause
        if cause is not None:
            raise partial from cause
        raise partial
    return result


async def remove_vnic(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    slot_num: str,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> VnicChangeResult:
    """Remove a vNIC slot and reconcile the resulting state.

    Raises:
        ValueError: If ``slot_num`` is unsafe.
        VnicCapabilityError: If current inventory forbids the mutation.
        VnicPartialError: If a dispatched mutation cannot be reconciled.
    """
    slot_num = require_command_safe_text(slot_num, "slot_num")
    system_name, lpar_name = await resolve_and_authorize_lpar_names(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    await require_admitted_environment(hmc.config, system_name)
    all_vnics = _parse_vnic_snapshots(
        await list_vnic_rows(hmc.config, system_name, lpar_name)
    )
    all_backings = tuple(
        _parse_backing_snapshot(row)
        for row in await list_vnic_backing_rows(hmc.config, system_name)
    )
    selected = tuple(item for item in all_vnics if item.slot_num == slot_num)
    if not selected:
        return VnicChangeResult(
            operation="remove",
            mutation_dispatched=False,
            changed=False,
            selector=None,
            slot_num=slot_num,
            vnic_before=(),
            backing_before=(),
            vnic_after=(),
            backing_after=(),
            vnic_after_read_succeeded=True,
            backing_after_read_succeeded=True,
            output="",
            errors=(),
        )
    if len(selected[0].backing_devices) != 1:
        raise VnicCapabilityError(
            "selected vNIC does not have exactly one embedded backing"
        )
    correlated = tuple(
        item
        for item in all_backings
        if _vnic_contains_backing_identity(selected[0], item)
    )
    if (
        len(correlated) != 1
        or not correlated[0].is_active
        or correlated[0].status != "Operational"
    ):
        raise VnicCapabilityError(
            "selected vNIC does not have exactly one active Operational backing"
        )
    captured = correlated[0]
    selector = VnicBackingSelector(
        captured.vios_name,
        captured.vios_lpar_id,
        captured.adapter_id,
        captured.physical_port_id,
        captured.desired_capacity_percent,
    )
    output = ""
    errors: list[str] = []
    mutation_error: Exception | None = None
    try:
        output = await remove_vnic_slot(hmc.config, system_name, lpar_name, slot_num)
    except Exception as error:  # noqa: BLE001 - captured into the readback result and reconciled by the caller
        mutation_error = error
        errors.append(f"mutation failed: {error}")
    readback = await _read_vnic_state_after_mutation(hmc.config, system_name, lpar_name)
    result = _reconcile_remove(
        selected, correlated, selector, slot_num, readback, output, errors
    )
    if result.errors:
        partial = VnicPartialError("vNIC remove could not be fully verified", result)
        cause = mutation_error or readback.cause
        if cause is not None:
            raise partial from cause
        raise partial
    return result
