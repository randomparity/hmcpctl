"""Declarative PCIe assignment validation and orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.operations.virtualization.pcie import (
    InventorySelector,
    PcieAssignmentPartialError,
    PcieAssignmentUnavailableError,
    SriovLogicalPortCapabilityError,
    SriovLogicalPortPartialError,
    assign_dedicated_pcie_slot,
    assign_sriov_logical_port,
    list_sriov_adapters,
    list_sriov_logical_ports,
    list_sriov_physical_ports,
    require_admitted_environment,
    require_capacity_granularity,
    require_dedicated_pcie_environment,
    require_drc_index,
)
from hmcpctl.operations.virtualization.validation import (
    require_command_safe_text,
    validate_capacity_percent,
)
from hmcpctl.operations.virtualization.vnic import (
    VnicBackingSelector,
    VnicCapabilityError,
    VnicPartialError,
    add_vnic,
)
from hmcpctl.ssh.selectors import resolve_ssh_names
from hmcpctl.ssh.sriov import list_sriov_configured_logical_port_rows
from hmcpctl.ssh.vnic import (
    list_vnic_backing_rows,
    read_vios_identity,
)

from .workflow_contract import WorkflowStep


@dataclass(frozen=True)
class DedicatedPcieAssignment:
    """Dedicated-slot profile assignment request."""

    profile_name: str = field(metadata={"description": "Target HMC profile name."})
    drc_index: str = field(metadata={"description": "Dedicated-slot DRC index."})


@dataclass(frozen=True)
class SriovLogicalPortAssignment:
    """Direct SR-IOV logical-port assignment request."""

    profile_name: str = field(metadata={"description": "Target HMC profile name."})
    adapter_id: str = field(metadata={"description": "SR-IOV adapter identifier."})
    physical_port_id: str = field(metadata={"description": "Physical-port identifier."})
    logical_port_id: str = field(metadata={"description": "Logical-port identifier."})
    capacity_percent: Decimal = field(
        metadata={"description": "Requested decimal capacity percentage."}
    )


@dataclass(frozen=True)
class VnicAssignment:
    """vNIC assignment request using the ADR 0057 backing selector."""

    backing: VnicBackingSelector = field(
        metadata={"description": "Evidence-bounded VIOS SR-IOV backing selector."}
    )
    port_vlan_id: int = field(metadata={"description": "Port VLAN ID from 0 to 4094."})


@dataclass(frozen=True)
class LparPcieAssignments:
    """Shared declarative assignment vocabulary for LPAR workflows."""

    dedicated: tuple[DedicatedPcieAssignment, ...] = field(
        default_factory=tuple,
        metadata={"description": "Ordered dedicated-slot assignments."},
    )
    sriov: tuple[SriovLogicalPortAssignment, ...] = field(
        default_factory=tuple,
        metadata={"description": "Ordered direct SR-IOV logical-port assignments."},
    )
    vnics: tuple[VnicAssignment, ...] = field(
        default_factory=tuple,
        metadata={"description": "Ordered vNIC assignments."},
    )


@dataclass(frozen=True)
class AssignmentResult:
    """Outcome of applying a declarative assignment collection."""

    workflow_completed: bool
    dry_run: bool
    steps: tuple[WorkflowStep, ...]


@dataclass(frozen=True)
class LparPcieWorkflowResult:
    """Create or modify outcome with recoverable ordered state."""

    resource_created: bool
    workflow_completed: bool
    lpar: dict[str, Any] | None
    ownership_stamped: bool | None
    steps: tuple[WorkflowStep, ...]
    warnings: tuple[str, ...]

    def __getitem__(self, key: str) -> Any:
        """Preserve dictionary-style access to the returned LPAR resource."""
        if self.lpar is None:
            raise KeyError(key)
        return self.lpar[key]


def assignment_step_names(assignments: LparPcieAssignments) -> list[str]:
    """Return stable workflow step names for an assignment collection."""
    return [
        *(f"dedicated[{index}]" for index, _ in enumerate(assignments.dedicated)),
        *(f"sriov[{index}]" for index, _ in enumerate(assignments.sriov)),
        *(f"vnic[{index}]" for index, _ in enumerate(assignments.vnics)),
    ]


async def _existing_capacity(
    hmc: HMCClient, system_name: str, adapter: str, physical: str
) -> Decimal:
    """Reconcile direct and vNIC projections by complete logical-port identity."""
    observations: dict[tuple[str, str, str], tuple[str, Decimal]] = {}
    direct = await list_sriov_configured_logical_port_rows(
        hmc.config, system_name, adapter
    )
    backing = await list_vnic_backing_rows(hmc.config, system_name)
    for row, capacity_field, physical_field in (
        *((row, "capacity", "phys_port_id") for row in direct),
        *((row, "desired_capacity", "physical_port_id") for row in backing),
    ):
        row_adapter = row["adapter_id"]
        key = system_name, row_adapter, row["logical_port_id"]
        observation = row[physical_field], Decimal(row[capacity_field])
        if key in observations and observations[key] != observation:
            raise ValueError("conflicting cross-projection logical-port capacity")
        observations[key] = observation
    return sum(
        (
            capacity
            for (_, item_adapter, _), (item_physical, capacity) in observations.items()
            if item_adapter == adapter and item_physical == physical
        ),
        Decimal(),
    )


def _validate_dedicated_requests(items: tuple[DedicatedPcieAssignment, ...]) -> None:
    """Validate dedicated selectors and refuse a repeated profile/slot pair."""
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (
            require_command_safe_text(item.profile_name, "profile_name"),
            require_drc_index(item.drc_index),
        )
        if key in seen:
            raise ValueError("duplicate dedicated slot assignment")
        seen.add(key)


def _analyze_assignment_requests(
    assignments: LparPcieAssignments,
) -> tuple[dict[tuple[str, str], tuple[Decimal, ...]], set[tuple[str, str]]]:
    """Validate request structure and return its inventory requirements.

    Capacities are kept per requested item, in request order, because the port's
    granularity binds each logical port or backing, not their per-port sum.
    """
    _validate_dedicated_requests(assignments.dedicated)

    identities: dict[tuple[str, str], tuple[str, Decimal]] = {}
    requested_capacity: dict[tuple[str, str], tuple[Decimal, ...]] = {}
    for item in assignments.sriov:
        require_command_safe_text(item.profile_name, "profile_name")
        adapter = require_command_safe_text(item.adapter_id, "adapter_id")
        physical = require_command_safe_text(item.physical_port_id, "physical_port_id")
        logical = require_command_safe_text(item.logical_port_id, "logical_port_id")
        capacity = validate_capacity_percent(item.capacity_percent)
        key = adapter, logical
        observation = physical, capacity
        if key in identities:
            if identities[key] != observation:
                raise ValueError("conflicting duplicate logical-port assignment")
            raise ValueError("duplicate logical-port assignment")
        identities[key] = observation
        requested_capacity[adapter, physical] = (
            *requested_capacity.get((adapter, physical), ()),
            capacity,
        )

    vnic_requests: set[tuple[str, str, str, str, Decimal, int]] = set()
    vios_identities: set[tuple[str, str]] = set()
    for item in assignments.vnics:
        backing = item.backing
        adapter = require_command_safe_text(backing.adapter_id, "adapter_id")
        physical = require_command_safe_text(
            backing.physical_port_id, "physical_port_id"
        )
        require_command_safe_text(backing.vios_name, "vios_name")
        require_command_safe_text(backing.vios_lpar_id, "vios_lpar_id")
        capacity = validate_capacity_percent(backing.capacity_percent)
        if type(item.port_vlan_id) is not int or not 0 <= item.port_vlan_id <= 4094:
            raise ValueError("port_vlan_id must be an integer between 0 and 4094")
        identity = (
            backing.vios_name,
            backing.vios_lpar_id,
            adapter,
            physical,
            capacity,
            item.port_vlan_id,
        )
        if identity in vnic_requests:
            raise ValueError("duplicate vNIC assignment")
        vnic_requests.add(identity)
        vios_identities.add((backing.vios_name, backing.vios_lpar_id))
        requested_capacity[adapter, physical] = (
            *requested_capacity.get((adapter, physical), ()),
            capacity,
        )
    return requested_capacity, vios_identities


async def _validate_sriov_inventory(
    hmc: HMCClient,
    system: str,
    requested_capacity: dict[tuple[str, str], tuple[Decimal, ...]],
) -> None:
    """Validate adapter, port, logical-port, and capacity inventory."""

    for (adapter, physical), requested in requested_capacity.items():
        adapters = await list_sriov_adapters(hmc, system, adapter)
        if adapters.capability != "available" or len(adapters.items) != 1:
            raise ValueError(f"SR-IOV adapter {adapter!r} is unavailable")
        if adapters.items[0].mode != "sriov" or adapters.items[0].availability != "1":
            raise ValueError(f"SR-IOV adapter {adapter!r} is not healthy")
        ports = await list_sriov_physical_ports(hmc, system, adapter, physical)
        if ports.capability != "available" or len(ports.items) != 1:
            raise ValueError(
                f"SR-IOV physical port {adapter}/{physical} is unavailable"
            )
        if ports.items[0].availability != "up":
            raise ValueError(
                f"SR-IOV physical port {adapter}/{physical} is not healthy"
            )
        for capacity in requested:
            require_capacity_granularity(
                capacity, ports.items[0].minimum_capacity_granularity_percent
            )
        logical = await list_sriov_logical_ports(hmc, system, adapter, physical)
        if logical.capability != "available":
            raise ValueError(
                logical.unavailable_reason or "logical-port inventory unavailable"
            )
        used = await _existing_capacity(hmc, adapters.system, adapter, physical)
        total = sum(requested, Decimal())
        if used + total > 100:
            raise ValueError(
                f"capacity exhausted on {adapter}/{physical}: {used}% used and "
                f"{total}% requested"
            )


async def _validate_vios_inventory(
    hmc: HMCClient, system: str, identities: set[tuple[str, str]]
) -> None:
    """Validate each unique VIOS name and partition-ID pair."""
    for identity in identities:
        system_name = (await list_sriov_adapters(hmc, system)).system
        observed = await read_vios_identity(hmc.config, system_name, identity[0])
        if observed != {
            "name": identity[0],
            "lpar_id": identity[1],
            "lpar_env": "vioserver",
        }:
            raise ValueError(
                "selected VIOS name, ID, or partition type does not match inventory"
            )


async def prevalidate_lpar_pcie_assignments(
    hmc: HMCClient,
    system_name_or_uuid: str,
    assignments: LparPcieAssignments,
) -> None:
    """Validate the complete collection without reserving or mutating resources."""
    requested_capacity, vios_identities = _analyze_assignment_requests(assignments)
    if assignments.dedicated or requested_capacity:
        system_name, _ = await resolve_ssh_names(hmc, system_name_or_uuid, None)
    if assignments.dedicated:
        await require_dedicated_pcie_environment(hmc.config, system_name)
    if requested_capacity:
        # ADR 0183 widens SR-IOV reads only; refuse here, before any LPAR exists,
        # an assignment the mutation envelope would refuse after creation.
        await require_admitted_environment(hmc.config, system_name)
    await _validate_sriov_inventory(hmc, system_name_or_uuid, requested_capacity)
    await _validate_vios_inventory(hmc, system_name_or_uuid, vios_identities)


async def apply_lpar_pcie_assignments(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    assignments: LparPcieAssignments,
    *,
    dry_run: bool = False,
    ownership_override: bool = False,
) -> AssignmentResult:
    """Apply requests in stable order and expose partial state without rollback."""
    await prevalidate_lpar_pcie_assignments(hmc, system_name_or_uuid, assignments)
    return await apply_validated_lpar_pcie_assignments(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        assignments,
        dry_run=dry_run,
        ownership_override=ownership_override,
    )


async def apply_validated_lpar_pcie_assignments(
    hmc: HMCClient,
    system: str,
    lpar: str,
    assignments: LparPcieAssignments,
    *,
    dry_run: bool = False,
    ownership_override: bool = False,
) -> AssignmentResult:
    """Execute a collection validated by the enclosing atomic workflow."""
    names = assignment_step_names(assignments)
    if dry_run:
        return AssignmentResult(
            False, True, tuple(WorkflowStep(n, "dry_run") for n in names)
        )

    operations: list[tuple[str, Any]] = []
    operations.extend(
        (
            f"dedicated[{index}]",
            lambda item=item: assign_dedicated_pcie_slot(
                hmc,
                system,
                lpar,
                item.profile_name,
                item.drc_index,
                ownership_override=ownership_override,
            ),
        )
        for index, item in enumerate(assignments.dedicated)
    )
    operations.extend(
        (
            f"sriov[{index}]",
            lambda item=item: assign_sriov_logical_port(
                hmc,
                system,
                lpar,
                InventorySelector(
                    item.adapter_id, item.physical_port_id, item.logical_port_id
                ),
                item.capacity_percent,
                profile_name=item.profile_name,
                ownership_override=ownership_override,
            ),
        )
        for index, item in enumerate(assignments.sriov)
    )
    operations.extend(
        (
            f"vnic[{index}]",
            lambda item=item: add_vnic(
                hmc,
                system,
                lpar,
                item.backing,
                item.port_vlan_id,
                ownership_override=ownership_override,
            ),
        )
        for index, item in enumerate(assignments.vnics)
    )
    steps: list[WorkflowStep] = []
    for index, (name, operation) in enumerate(operations):
        try:
            steps.append(WorkflowStep(name, "ok", await operation()))
        except (
            ValueError,
            PermissionError,
            HMCError,
            PcieAssignmentUnavailableError,
            PcieAssignmentPartialError,
            SriovLogicalPortCapabilityError,
            SriovLogicalPortPartialError,
            VnicCapabilityError,
            VnicPartialError,
        ) as error:
            result = getattr(error, "result", str(error))
            steps.append(WorkflowStep(name, "error", result))
            steps.extend(
                WorkflowStep(rest, "skipped") for rest, _ in operations[index + 1 :]
            )
            return AssignmentResult(False, False, tuple(steps))
    return AssignmentResult(True, False, tuple(steps))
