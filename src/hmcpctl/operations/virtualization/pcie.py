"""Presentation-neutral normalized PCIe inventory contracts."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Generic, Literal, NoReturn, TypeVar

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.operations.lpar.ownership import resolve_and_authorize_lpar_names
from hmcpctl.operations.virtualization.validation import (
    require_command_safe_text,
    validate_capacity_percent,
)
from hmcpctl.ssh.io_inventory import list_dedicated_pcie_slot_rows
from hmcpctl.ssh.profiles import (
    DRC_INDEX_PATTERN,
    ProfileIoSlot,
    assign_profile_io_slot,
    parse_profile_io_slots,
    read_profile_io_slot_rows,
    unassign_profile_io_slot,
)
from hmcpctl.ssh.selectors import resolve_ssh_names
from hmcpctl.ssh.sriov import (
    SriovMode,
    assign_sriov_logical_port_dynamic,
    list_sriov_adapter_rows,
    list_sriov_configured_logical_port_rows,
    list_sriov_physical_port_rows,
    list_sriov_unconfigured_logical_port_rows,
    read_sriov_environment,
    read_sriov_lpar_state,
    read_sriov_profile_ports,
    unassign_sriov_logical_port_profile,
    validate_adapter_id,
    validate_sriov_mode,
)
from hmcpctl.ssh.transport import HMCCLIError

CapabilityState = Literal["available", "capability-unavailable"]
ResourceKind = Literal[
    "dedicated_slot",
    "sriov_adapter",
    "sriov_physical_port",
    "sriov_logical_port",
]
SRIOV_UNAVAILABLE_REASON = "ADR 0053 admits selectors but no SR-IOV read projection"
_ADMITTED_SYSTEM_MODEL = "8375-42A"
PCIE_ASSIGNMENT_UNAVAILABLE_REASON = (
    "dedicated PCIe profile assignment is admitted only for HMC V10R3 M1060 "
    "with managed-system model 8375-42A (ADR 0165)"
)

_T = TypeVar("_T")


@dataclass(frozen=True)
class InventorySelector:
    """Optional selector scope copied from the caller without inference."""

    adapter_id: str | None = None
    physical_port_id: str | None = None
    logical_port_id: str | None = None


@dataclass(frozen=True)
class InventoryResult(Generic[_T]):
    """Stable collection envelope separating availability from capability."""

    resource_kind: ResourceKind
    capability: CapabilityState
    system: str
    selector: InventorySelector
    items: list[_T]
    unavailable_reason: str | None


@dataclass(frozen=True)
class DedicatedSlot:
    system: str
    drc_index: str
    description: str | None
    owner_lpar: str | None
    availability: str | None


@dataclass(frozen=True)
class SriovAdapter:
    system: str
    # None for a dedicated-mode adapter, which the HMC lists with adapter_id `null`.
    adapter_id: str | None
    mode: str | None
    availability: str | None
    location_code: str | None
    owner_lpar: str | None
    logical_ports_in_use: int | None
    logical_ports_available: int | None


@dataclass(frozen=True)
class SriovPhysicalPort:
    system: str
    adapter_id: str
    physical_port_id: str
    availability: str | None
    location_code: str | None
    owner_lpar: str | None
    minimum_capacity_granularity_percent: Decimal | None
    logical_ports_in_use: int | None
    logical_ports_available: int | None


@dataclass(frozen=True)
class SriovLogicalPort:
    system: str
    adapter_id: str
    physical_port_id: str | None
    logical_port_id: str
    availability: str | None
    owner_lpar: str | None
    owner_lpar_id: str | None
    capacity_percent: Decimal | None
    maximum_capacity_percent: Decimal | None
    compatibility: str | None


@dataclass(frozen=True)
class SriovLogicalPortSnapshot:
    adapter_id: str
    physical_port_id: str
    logical_port_id: str
    owner_lpar: str
    owner_lpar_id: str
    capacity_percent: Decimal
    functional_state: str


@dataclass(frozen=True)
class SriovLogicalPortChangeResult:
    operation: Literal["assign", "unassign"]
    path: Literal["dynamic", "profile"]
    changed: bool
    selector: InventorySelector
    effective_before: SriovLogicalPortSnapshot | None
    effective_after: SriovLogicalPortSnapshot | None
    profile_before: str | None
    profile_after: str | None
    output: str


@dataclass(frozen=True)
class _SriovAssignmentReadback:
    effective: SriovLogicalPortSnapshot | None
    profile: str | None
    effective_error: Exception | None
    profile_error: Exception | None

    @property
    def error(self) -> Exception | None:
        """Return the first read failure for exception chaining compatibility."""
        return self.effective_error or self.profile_error


@dataclass(frozen=True)
class _SriovAssignmentPreflight:
    config: HMCConfig
    system_name: str
    lpar_name: str
    selector: InventorySelector
    capacity: Decimal
    effective_before: SriovLogicalPortSnapshot | None
    profile_before: str
    idempotent_result: SriovLogicalPortChangeResult | None = None


class SriovLogicalPortCapabilityError(RuntimeError):
    """Raised for an uncharacterized state/mutation matrix cell."""


class SriovLogicalPortPartialError(RuntimeError):
    """Raised when a dispatched mutation cannot be fully reconciled."""

    def __init__(self, message: str, result: SriovLogicalPortChangeResult):
        super().__init__(message)
        self.result = result


class PcieAssignmentUnavailableError(RuntimeError):
    """Raised outside the ADR 0165 envelope for dedicated profile assignment."""


class PcieAssignmentPartialError(RuntimeError):
    """Raised when a dispatched dedicated-slot change cannot be verified by readback."""


@dataclass(frozen=True)
class _DedicatedProfileTarget:
    config: HMCConfig
    system_name: str
    lpar_name: str
    profile_name: str
    drc_index: str
    io_slots: str
    profile_rows: list[dict[str, str]]


def require_drc_index(value: str) -> str:
    """Require a DRC index in the form every admitted `io_slots` index takes."""
    if not DRC_INDEX_PATTERN.fullmatch(value):
        raise ValueError(
            "drc_index must be exactly eight uppercase hexadecimal digits, "
            f"got {value!r}"
        )
    return value


async def assign_dedicated_pcie_slot(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    profile_name: str,
    drc_index: str,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> None:
    """Add a dedicated slot to an LPAR profile and verify it by exact readback.

    Raises:
        ValueError: If a selector is invalid, the profile is missing, or the profile
            lists the slot in a form other than the one this operation writes.
        PermissionError: If ADR 0011 ownership authorization refuses the LPAR.
        PcieAssignmentUnavailableError: Outside the ADR 0165 envelope.
        PcieAssignmentPartialError: If a dispatched change cannot be verified.
    """
    target = await _authorize_pcie_profile_request(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        profile_name,
        drc_index,
        ownership_override=ownership_override,
    )
    await _change_dedicated_slot(target, add=True)


async def unassign_dedicated_pcie_slot(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    profile_name: str,
    drc_index: str,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> None:
    """Remove a dedicated slot from an LPAR profile and verify it by exact readback.

    Raises the same errors as :func:`assign_dedicated_pcie_slot`.
    """
    target = await _authorize_pcie_profile_request(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        profile_name,
        drc_index,
        ownership_override=ownership_override,
    )
    await _change_dedicated_slot(target, add=False)


async def _authorize_pcie_profile_request(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile_name: str,
    drc_index: str,
    *,
    ownership_override: bool,
) -> _DedicatedProfileTarget:
    """Validate, authorize, confine to the envelope and LPAR state, then read the profile."""
    require_command_safe_text(profile_name, "profile_name")
    require_drc_index(drc_index)
    system_name, lpar_name = await resolve_and_authorize_lpar_names(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    config = hmc.config
    await require_dedicated_pcie_environment(config, system_name)
    # The state matrix admits profile-only mutation for a Not Activated LPAR alone;
    # on a running one the change would report success and not take effect.
    state = (await read_sriov_lpar_state(config, system_name, lpar_name))["state"]
    if state != "Not Activated":
        raise ValueError(
            "dedicated PCIe profile assignment requires a Not Activated LPAR; "
            f"{lpar_name!r} is {state!r}"
        )
    profile_rows = await read_profile_io_slot_rows(config, system_name)
    io_slots = _select_profile_io_slots(profile_rows, lpar_name, profile_name)
    return _DedicatedProfileTarget(
        config, system_name, lpar_name, profile_name, drc_index, io_slots, profile_rows
    )


def _select_profile_io_slots(
    profile_rows: list[dict[str, str]], lpar_name: str, profile_name: str
) -> str:
    rows = [
        row
        for row in profile_rows
        if row["lpar_name"] == lpar_name and row["name"] == profile_name
    ]
    if not rows:
        raise ValueError(f"profile {profile_name!r} not found on LPAR {lpar_name!r}")
    if len(rows) > 1:
        raise HMCCLIError(
            "profile io_slots readback returned more than one matching row"
        )
    return rows[0]["io_slots"]


def _slots_by_drc(io_slots: str) -> dict[str, ProfileIoSlot]:
    return {slot.drc_index: slot for slot in parse_profile_io_slots(io_slots)}


async def _change_dedicated_slot(target: _DedicatedProfileTarget, *, add: bool) -> None:
    """Apply the one triple form this operation owns and verify it by readback (ADR 0166)."""
    drc_index = target.drc_index
    written = ProfileIoSlot(drc_index, None, False)
    before = _slots_by_drc(target.io_slots)
    present = before.get(drc_index)
    if present is not None and present != written:
        raise ValueError(
            f"profile lists slot {drc_index} as {present}; only {drc_index}/none/0, "
            "the form this operation writes, is changed"
        )
    if add:
        _refuse_slot_listed_by_another_lpar(target)
    if (present is not None) == add:
        return
    expected = dict(before)
    if add:
        expected[drc_index] = written
    else:
        del expected[drc_index]
    builder = assign_profile_io_slot if add else unassign_profile_io_slot
    error: Exception | None = None
    try:
        await builder(
            target.config,
            target.system_name,
            target.lpar_name,
            target.profile_name,
            drc_index,
        )
    except Exception as caught:  # noqa: BLE001 - classified by the readback below
        error = caught
    await _verify_dedicated_change(target, before, expected, error, add=add)


def _refuse_slot_listed_by_another_lpar(target: _DedicatedProfileTarget) -> None:
    """Refuse to list a slot another LPAR's profile already lists (ADR 0166).

    Two partitions whose profiles both list a slot contend for it at activation, a
    state no evidence characterizes. Only rows that mention the DRC are parsed, so an
    unrelated row cannot block the operation.
    """
    holders = _other_holders(target, target.profile_rows)
    if holders:
        raise ValueError(
            f"slot {target.drc_index} is already listed by a profile of LPAR "
            f"{', '.join(holders)}; remove it there first"
        )


def _other_holders(
    target: _DedicatedProfileTarget, profile_rows: list[dict[str, str]]
) -> list[str]:
    return sorted(
        {
            row["lpar_name"]
            for row in profile_rows
            if row["lpar_name"] != target.lpar_name
            and target.drc_index in row["io_slots"]
            and target.drc_index in _slots_by_drc(row["io_slots"])
        }
    )


async def _verify_dedicated_change(
    target: _DedicatedProfileTarget,
    before: dict[str, ProfileIoSlot],
    expected: dict[str, ProfileIoSlot],
    error: Exception | None,
    *,
    add: bool,
) -> None:
    """Classify a dispatched change by what the profile reads back as.

    An assign is also re-checked for another LPAR listing the slot, since a
    concurrent assign elsewhere passes the pre-write holder check too. An
    unverified unassign looks holders up for its advice only (#905).
    """
    after_text: str | None = None
    after: dict[str, ProfileIoSlot] | None = None
    read_error: Exception | None = None
    holders: list[str] = []
    holder_error: HMCCLIError | None = None
    try:
        rows = await read_profile_io_slot_rows(target.config, target.system_name)
        after_text = _select_profile_io_slots(
            rows, target.lpar_name, target.profile_name
        )
        after = _slots_by_drc(after_text)
    except Exception as caught:  # noqa: BLE001 - reported through the partial error
        read_error = caught
    else:
        if add:
            holders, holder_error = _holders_or_error(target, rows)
        if after == expected and not holders and holder_error is None:
            return
        if error is not None and after == before and holder_error is None:
            raise error
        if not add:
            holders, holder_error = _holders_or_error(target, rows)
    contended = add and (bool(holders) or holder_error is not None)
    cause = error or read_error or (holder_error if add else None)
    reasons = [str(cause)] if cause is not None else []
    if add and holders:
        reasons.append(f"slot is also listed by a profile of LPAR {', '.join(holders)}")
    operation = "assignment" if add else "unassignment"
    raise PcieAssignmentPartialError(
        f"dedicated slot {operation} could not be verified: "
        f"{'; '.join(reasons) or 'readback mismatch'}; io_slots before={target.io_slots!r} "
        f"after={after_text!r}. The write may have run, so the profile may hold the change, "
        "none of it, or a form this operation refuses. Read it with `lssyscfg -r prof -m "
        f"{shlex.quote(target.system_name)} -F lpar_name,name,io_slots --header`. "
        f"{_recovery_advice(target, after, add=add, contended=contended)} Never write the "
        "read value back as `io_slots=` input: that rendering is not established as valid "
        f"input (ADR 0166).{_holder_advice(holders, holder_error)}"
    ) from cause


def _holders_or_error(
    target: _DedicatedProfileTarget, profile_rows: list[dict[str, str]]
) -> tuple[list[str], HMCCLIError | None]:
    """Return other holders, or the parse error of a row naming the slot, without raising."""
    try:
        return _other_holders(target, profile_rows), None
    except HMCCLIError as caught:
        return [], caught


def _recovery_advice(
    target: _DedicatedProfileTarget,
    after: dict[str, ProfileIoSlot] | None,
    *,
    add: bool,
    contended: bool,
) -> str:
    """Advise from what the readback shows, naming no command that changes the profile.

    A named reversal was wrong in some concurrent state each time one was offered
    (#882 review rounds 1 and 2), so every reversal goes through the HMC UI. A slot
    read as requested is not to be undone (#905), except after an assign that another
    LPAR's profile also lists, or may list, where the two profiles contend for it.
    """
    drc_index = target.drc_index
    where = f"slot {drc_index} of profile {target.profile_name!r} of LPAR {target.lpar_name!r}"
    if after is None:
        return (
            f"The profile holding {where} could not be read or parsed: inspect it with the "
            "read command above, and make any reversal through the HMC UI."
        )
    written = ProfileIoSlot(drc_index, None, False)
    if after.get(drc_index) == (None if add else written):
        rendering = "absent" if add else f"{drc_index}/none/0"
        return f"The readback lists {where} as before ({rendering}), so no reversal is needed."
    if after.get(drc_index) == (written if add else None) and not contended:
        rendering = f"{drc_index}/none/0" if add else "absent"
        return (
            f"The readback lists {where} as requested ({rendering}), so the difference is "
            "elsewhere in the profile: compare the read value with the before value, make any "
            f"reversal of the other slots through the HMC UI, and do not undo slot {drc_index}."
        )
    return (
        "Compare the read value with the before value, and make any reversal of "
        f"{where} through the HMC UI."
    )


def _holder_advice(holders: list[str], holder_error: HMCCLIError | None) -> str:
    """Name another LPAR listing the slot, whose profile ADR 0011 does not authorize."""
    if holder_error is not None:
        return (
            " Whether another LPAR's profile lists the slot could not be read "
            f"({holder_error}); check that in the HMC UI."
        )
    if not holders:
        return ""
    return (
        f" The profile of LPAR {', '.join(holders)} also lists the slot; do not edit that "
        "profile without its owner: this tool did not write it, and ADR 0011 authorizes "
        "only the requested LPAR."
    )


async def _system_name(hmc: HMCClient, system: str) -> str:
    system_name, _ = await resolve_ssh_names(hmc, system, None)
    return system_name


async def list_dedicated_slots(
    hmc: HMCClient,
    system_name_or_uuid: str,
) -> InventoryResult[DedicatedSlot]:
    """List dedicated PCIe slots with stable identity and explicit unknowns."""
    config = hmc.config
    system_name = await _system_name(hmc, system_name_or_uuid)
    rows = await list_dedicated_pcie_slot_rows(config, system_name)
    items: list[DedicatedSlot] = []
    for row in rows:
        drc_index = row["drc_index"]
        if not drc_index.strip():
            raise ValueError("dedicated PCIe slot row has a blank drc_index")
        items.append(
            DedicatedSlot(
                system=system_name,
                drc_index=drc_index,
                description=_optional_text(row["description"]),
                owner_lpar=_optional_text(row["lpar_name"]),
                availability=None,
            )
        )
    return InventoryResult(
        "dedicated_slot", "available", system_name, InventorySelector(), items, None
    )


def _optional_text(value: str) -> str | None:
    """Read an ``lshwres -F`` field, where the HMC prints ``null`` for an absent value.

    An unowned slot's ``lpar_name`` comes back as the literal ``null`` (V10R3, #1195).
    """
    return None if value.strip() in {"", "null"} else value


def _snapshot(row: dict[str, str]) -> SriovLogicalPortSnapshot:
    return SriovLogicalPortSnapshot(
        row["adapter_id"],
        row["phys_port_id"],
        row["logical_port_id"],
        row["lpar_name"],
        row["lpar_id"],
        Decimal(row["capacity"]),
        row["functional_state"],
    )


_ADMITTED_RELEASE_FIELDS = {"version": "10", "release": "3", "service pack": "1060"}

SriovRead = Literal["adapter", "physical_port", "logical_port"]
_ALL_SRIOV_READS: frozenset[SriovRead] = frozenset(
    ("adapter", "physical_port", "logical_port")
)
# ADR 0183: each (HMC Version, Release, Service Pack, model) pair admits the SR-IOV
# reads its read-only capture shows. Both POWER11 systems hold only dedicated-mode
# adapters, so physical- and logical-port reads stay unadmitted there.
_SRIOV_READ_ENVELOPE: dict[tuple[str, str, str, str], frozenset[SriovRead]] = {
    ("10", "3", "1060", "8375-42A"): _ALL_SRIOV_READS,
    ("11", "2", "1120", "9009-42A"): _ALL_SRIOV_READS,
    ("11", "2", "1120", "9824-42A"): frozenset(("adapter",)),
    ("11", "2", "1120", "9242-21B"): frozenset(("adapter",)),
}


def _release_fields(version: str) -> dict[str, str] | None:
    """Return `lshmc -V`'s own Version/Release/Service Pack fields, or None.

    Never a substring test: an HMC at a later service pack may still list an
    ``M1060`` fix line, and ``1060`` is a prefix of ``10600``. A repeated or missing
    field is None.
    """
    pairs = re.findall(r"\b(Version|Release|Service Pack):[ \t]*(\S+)", version)
    fields = {name.lower(): value for name, value in pairs}
    return fields if len(pairs) == len(fields) == 3 else None


def _is_exact_admitted_environment(version: str, model: str) -> bool:
    """Match the ADR 0056/0165 mutation envelope: V10R3 M1060 on 8375-42A exactly."""
    return (
        _release_fields(version) == _ADMITTED_RELEASE_FIELDS
        and model == _ADMITTED_SYSTEM_MODEL
    )


def _admitted_sriov_reads(version: str, model: str) -> frozenset[SriovRead]:
    fields = _release_fields(version)
    if fields is None:
        return frozenset()
    key = (fields["version"], fields["release"], fields["service pack"], model)
    return _SRIOV_READ_ENVELOPE.get(key, frozenset())


async def require_dedicated_pcie_environment(
    config: HMCConfig, system_name: str
) -> None:
    """Refuse dedicated profile assignment outside the ADR 0165 envelope."""
    if not _is_exact_admitted_environment(
        *await read_sriov_environment(config, system_name)
    ):
        raise PcieAssignmentUnavailableError(PCIE_ASSIGNMENT_UNAVAILABLE_REASON)


async def require_sriov_read_environment(
    config: HMCConfig, system_name: str, read: SriovRead
) -> None:
    """Refuse an SR-IOV inventory read outside the ADR 0183 read envelope."""
    version, model = await read_sriov_environment(config, system_name)
    admitted = _admitted_sriov_reads(version, model)
    if read in admitted:
        return
    subject = f"SR-IOV {read.replace('_', '-')} inventory"
    if admitted:
        raise SriovLogicalPortCapabilityError(
            f"{subject} is not admitted on model {model}: no SR-IOV-mode adapter "
            "captured on this model (ADR 0183)"
        )
    raise SriovLogicalPortCapabilityError(
        f"{subject} is admitted only for {_sriov_read_envelope_text(read)} (ADR 0183)"
    )


def _sriov_read_envelope_text(read: SriovRead) -> str:
    pairs = [
        f"HMC Version {version} Release {release} Service Pack {service_pack} "
        f"with model {model}"
        for (
            version,
            release,
            service_pack,
            model,
        ), reads in _SRIOV_READ_ENVELOPE.items()
        if read in reads
    ]
    return "; ".join(pairs)


async def require_admitted_environment(config: HMCConfig, system_name: str) -> None:
    if not _is_exact_admitted_environment(
        *await read_sriov_environment(config, system_name)
    ):
        raise SriovLogicalPortCapabilityError(
            "SR-IOV mutations are admitted only for HMC V10R3 M1060 "
            "with managed-system model 8375-42A: no SR-IOV mutation is captured on "
            "any other HMC level or model, so ADR 0183 widens only inventory reads"
        )


async def _read_assignment_state(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
    profile_name: str,
    adapter_id: str,
    logical_port_id: str,
) -> _SriovAssignmentReadback:
    effective = None
    profile = None
    effective_error: Exception | None = None
    profile_error: Exception | None = None
    try:
        rows = await list_sriov_configured_logical_port_rows(
            config, system_name, adapter_id
        )
        matching = [row for row in rows if row["logical_port_id"] == logical_port_id]
        if len(matching) > 1:
            raise ValueError("duplicate logical-port inventory rows")
        effective = _snapshot(matching[0]) if matching else None
    except Exception as caught:  # noqa: BLE001 - captured into the readback result and reconciled by the caller
        effective_error = caught
    try:
        profile = (
            await read_sriov_profile_ports(config, system_name, lpar_name, profile_name)
        )["sriov_eth_logical_ports"]
    except Exception as caught:  # noqa: BLE001 - captured into the readback result and reconciled by the caller
        profile_error = caught
    return _SriovAssignmentReadback(effective, profile, effective_error, profile_error)


async def _read_sriov_assignment_inventory(
    config: HMCConfig,
    system_name: str,
    adapter_id: str,
    physical_port_id: str,
    logical_port_id: str,
) -> tuple[dict[str, str], list[dict[str, str]], SriovLogicalPortSnapshot | None]:
    """Validate the selected adapter and physical port, then read assignment state."""
    adapters = [
        row
        for row in await list_sriov_adapter_rows(config, system_name)
        if row["adapter_id"] == adapter_id
    ]
    if (
        len(adapters) != 1
        or adapters[0]["config_state"] != "sriov"
        or adapters[0]["functional_state"] != "1"
    ):
        raise SriovLogicalPortCapabilityError("adapter is not in healthy SR-IOV mode")
    physical = [
        row
        for row in await list_sriov_physical_port_rows(config, system_name, adapter_id)
        if row["phys_port_id"] == physical_port_id
    ]
    if len(physical) != 1 or physical[0]["state"] != "1":
        raise SriovLogicalPortCapabilityError("physical port is unavailable")
    rows = await list_sriov_configured_logical_port_rows(
        config, system_name, adapter_id
    )
    matching = [row for row in rows if row["logical_port_id"] == logical_port_id]
    if len(matching) > 1:
        raise ValueError("duplicate logical-port inventory rows")
    before = _snapshot(matching[0]) if matching else None
    return physical[0], rows, before


def eth_capacity_granularity(row: dict[str, str]) -> Decimal | None:
    """Return the port's Ethernet capacity granularity, or None when it reports none."""
    value = row.get("min_eth_capacity_granularity", "")
    if value in {"", "null"}:
        return None
    try:
        granularity = Decimal(value)
    except InvalidOperation:
        granularity = Decimal("NaN")
    # A two-decimal capacity from 1 to 100 can only be a multiple of 0.01 through 100.
    if not granularity.is_finite() or not Decimal("0.01") <= granularity <= 100:
        raise HMCCLIError(f"malformed physical-port capacity granularity: {value!r}")
    return granularity


def require_capacity_granularity(
    capacity: Decimal, granularity: Decimal | None
) -> None:
    """Refuse a capacity that is not a multiple of the port's reported granularity."""
    if granularity is not None and capacity % granularity:
        raise ValueError(
            f"capacity_percent {capacity} is not a multiple of the physical port's "
            f"capacity granularity {granularity}%"
        )


async def _require_sriov_assignment_capacity_and_state(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
    adapter_id: str,
    physical_port_id: str,
    logical_port_id: str,
    physical_port: dict[str, str],
    configured_rows: list[dict[str, str]],
    capacity: Decimal,
) -> None:
    """Require an available logical port, sufficient capacity, and mutable LPAR state."""
    require_capacity_granularity(capacity, eth_capacity_granularity(physical_port))
    candidates = await list_sriov_unconfigured_logical_port_rows(config, system_name)
    port_location = physical_port["phys_port_loc"] + "-S"
    if not any(
        row.get("adapter_id") == adapter_id
        and row.get("logical_port_id") == logical_port_id
        and row.get("location_code", "").startswith(port_location)
        for row in candidates
    ):
        raise ValueError(
            "logical port is not an unconfigured member of the selected physical port"
        )
    total = Decimal()
    seen: set[str] = set()
    for row in configured_rows:
        if row["phys_port_id"] != physical_port_id:
            continue
        if row["logical_port_id"] in seen:
            raise ValueError("duplicate logical-port inventory rows")
        seen.add(row["logical_port_id"])
        total += validate_capacity_percent(Decimal(row["capacity"]))
    if total + capacity > 100:
        raise ValueError(f"capacity exhausted: {total}% used of 100%")
    state = await read_sriov_lpar_state(config, system_name, lpar_name)
    if state["state"] == "Running" and state["rmc_state"] != "active":
        raise SriovLogicalPortCapabilityError("Running assignment requires active RMC")
    if state["state"] not in {"Running", "Not Activated"}:
        raise SriovLogicalPortCapabilityError(
            f"unsupported LPAR state: {state['state']}"
        )


async def _preflight_sriov_assignment(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    selector: InventorySelector,
    capacity_percent: Decimal,
    profile_name: str,
    ownership_override: bool,
) -> _SriovAssignmentPreflight:
    selector, adapter_id, physical_port_id, logical_port_id = _required_sriov_selector(
        selector
    )
    capacity = validate_capacity_percent(capacity_percent)
    system_name, lpar_name = await resolve_and_authorize_lpar_names(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    config = hmc.config
    require_command_safe_text(profile_name, "profile_name")
    await require_admitted_environment(config, system_name)
    physical, rows, before = await _read_sriov_assignment_inventory(
        config,
        system_name,
        adapter_id,
        physical_port_id,
        logical_port_id,
    )
    profile_before = (
        await read_sriov_profile_ports(config, system_name, lpar_name, profile_name)
    )["sriov_eth_logical_ports"]
    if before:
        if before.physical_port_id != physical_port_id:
            raise ValueError("logical port is assigned on a different physical port")
        if before.owner_lpar != lpar_name:
            raise PermissionError(
                f"logical port {logical_port_id} is already assigned to {before.owner_lpar}"
            )
        if before.capacity_percent != capacity:
            raise ValueError(
                "logical port is already assigned with a different capacity"
            )
        return _SriovAssignmentPreflight(
            config,
            system_name,
            lpar_name,
            selector,
            capacity,
            before,
            profile_before,
            SriovLogicalPortChangeResult(
                operation="assign",
                path="dynamic",
                changed=False,
                selector=selector,
                effective_before=before,
                effective_after=before,
                profile_before=profile_before,
                profile_after=profile_before,
                output="",
            ),
        )
    await _require_sriov_assignment_capacity_and_state(
        config,
        system_name,
        lpar_name,
        adapter_id,
        physical_port_id,
        logical_port_id,
        physical,
        rows,
        capacity,
    )
    return _SriovAssignmentPreflight(
        config, system_name, lpar_name, selector, capacity, before, profile_before
    )


def _required_sriov_selector(
    selector: InventorySelector,
) -> tuple[InventorySelector, str, str, str]:
    """Validate and normalize the three IDs required for an SR-IOV mutation."""
    adapter_id = require_command_safe_text(selector.adapter_id or "", "adapter_id")
    physical_port_id = require_command_safe_text(
        selector.physical_port_id or "", "physical_port_id"
    )
    logical_port_id = require_command_safe_text(
        selector.logical_port_id or "", "logical_port_id"
    )
    return (
        InventorySelector(adapter_id, physical_port_id, logical_port_id),
        adapter_id,
        physical_port_id,
        logical_port_id,
    )


@dataclass(frozen=True)
class _SriovReadbackOutcome:
    """The post-dispatch readback facts a partial-error raise needs, bundled to keep
    `_raise_sriov_partial_error` under the five-positional-parameter limit."""

    cause: Exception | None
    matches_pre_mutation: bool
    unverified_detail: object


def _raise_sriov_partial_error(
    operation: Literal["assignment", "unassignment"],
    error: Exception | None,
    outcome: _SriovReadbackOutcome,
    result: SriovLogicalPortChangeResult,
) -> NoReturn:
    """Raise the structured partial error for a dispatched SR-IOV mutation.

    A dispatch failure whose immediate readback matches the pre-mutation state exactly
    is an HMC refusal: nothing changed, so the HMC's own failure text is reported
    directly. Any other failure -- a missing or mismatched readback, or a dispatch error
    where the readback also changed -- leaves the state genuinely uncertain, so the
    existing "could not be verified" wording is kept, with the same fallback detail the
    caller used before this classification existed.
    """
    if error is not None and outcome.matches_pre_mutation:
        refused = SriovLogicalPortPartialError(
            f"{operation} refused by HMC: {error}", result
        )
        raise refused from error
    detail = error or outcome.unverified_detail or "readback mismatch"
    partial = SriovLogicalPortPartialError(
        f"{operation} could not be verified: {detail}", result
    )
    if outcome.cause is not None:
        raise partial from outcome.cause
    raise partial


async def assign_sriov_logical_port(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    selector: InventorySelector,
    capacity_percent: Decimal,
    *,
    system_name_or_uuid: str,
    profile_name: str,
    ownership_override: bool = False,
) -> SriovLogicalPortChangeResult:
    """Assign an SR-IOV logical port and reconcile the resulting state.

    Raises:
        ValueError: If a selector or requested capacity is invalid.
        SriovLogicalPortCapabilityError: If current inventory forbids assignment.
        SriovLogicalPortPartialError: If a dispatched mutation cannot be reconciled.
    """
    preflight = await _preflight_sriov_assignment(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        selector,
        capacity_percent,
        profile_name,
        ownership_override,
    )
    if preflight.idempotent_result is not None:
        return preflight.idempotent_result
    config = preflight.config
    system_name = preflight.system_name
    lpar_name = preflight.lpar_name
    selector = preflight.selector
    _selector, adapter_id, physical_port_id, logical_port_id = _required_sriov_selector(
        selector
    )
    capacity = preflight.capacity
    before = preflight.effective_before
    profile_before = preflight.profile_before
    output = ""
    error: Exception | None = None
    try:
        output = await assign_sriov_logical_port_dynamic(
            config,
            system_name,
            lpar_name,
            adapter_id,
            physical_port_id,
            logical_port_id,
            str(capacity),
        )
    except Exception as caught:  # noqa: BLE001 - captured into the readback result and reconciled by the caller
        error = caught
    readback = await _read_assignment_state(
        config,
        system_name,
        lpar_name,
        profile_name,
        adapter_id,
        logical_port_id,
    )
    after = readback.effective
    profile_after = readback.profile
    result = SriovLogicalPortChangeResult(
        operation="assign",
        path="dynamic",
        changed=True,
        selector=selector,
        effective_before=before,
        effective_after=after,
        profile_before=profile_before,
        profile_after=profile_after,
        output=output,
    )
    if (
        error
        or readback.error
        or not after
        or after.physical_port_id != physical_port_id
        or after.functional_state != "1"
        or after.owner_lpar != lpar_name
        or after.capacity_percent != capacity
        or profile_after != profile_before
    ):
        readback_errors = "; ".join(
            label
            for label, failure in (
                ("effective-state read failed", readback.effective_error),
                ("profile-state read failed", readback.profile_error),
            )
            if failure is not None
        )
        matches_pre_mutation = (
            readback.error is None
            and after == before
            and profile_after == profile_before
        )
        outcome = _SriovReadbackOutcome(
            error or readback.error, matches_pre_mutation, readback_errors
        )
        _raise_sriov_partial_error("assignment", error, outcome, result)
    return result


async def unassign_sriov_logical_port(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    selector: InventorySelector,
    *,
    system_name_or_uuid: str,
    profile_name: str,
    ownership_override: bool = False,
) -> SriovLogicalPortChangeResult:
    """Unassign an SR-IOV logical port from an inactive profile.

    Raises:
        ValueError: If selectors or the profile record are invalid.
        SriovLogicalPortCapabilityError: If current state forbids unassignment.
        SriovLogicalPortPartialError: If a dispatched mutation cannot be reconciled.
    """
    selector, adapter_id, physical_port_id, logical_port_id = _required_sriov_selector(
        selector
    )
    require_command_safe_text(profile_name, "profile_name")
    system_name, lpar_name = await resolve_and_authorize_lpar_names(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    config = hmc.config
    await require_admitted_environment(config, system_name)
    state = await read_sriov_lpar_state(config, system_name, lpar_name)
    if state["state"] != "Not Activated":
        raise SriovLogicalPortCapabilityError(
            "only Not Activated profile unassign is supported"
        )
    before = (
        await read_sriov_profile_ports(config, system_name, lpar_name, profile_name)
    )["sriov_eth_logical_ports"]
    if before == "none":
        return SriovLogicalPortChangeResult(
            operation="unassign",
            path="profile",
            changed=False,
            selector=selector,
            effective_before=None,
            effective_after=None,
            profile_before=before,
            profile_after=before,
            output="",
        )
    parts = before.split(":")
    if (
        "," in before
        or len(parts) < 14
        or (parts[1], parts[2], parts[3])
        != (
            adapter_id,
            physical_port_id,
            logical_port_id,
        )
    ):
        raise ValueError("profile does not contain exactly the selected logical port")
    output = ""
    error: Exception | None = None
    try:
        output = await unassign_sriov_logical_port_profile(
            config, system_name, lpar_name, profile_name
        )
    except Exception as caught:  # noqa: BLE001 - captured into the readback result and reconciled by the caller
        error = caught
    after = None
    read_error: Exception | None = None
    try:
        after = (
            await read_sriov_profile_ports(config, system_name, lpar_name, profile_name)
        )["sriov_eth_logical_ports"]
    except Exception as caught:  # noqa: BLE001 - captured into the readback result and reconciled by the caller
        read_error = caught
    result = SriovLogicalPortChangeResult(
        operation="unassign",
        path="profile",
        changed=True,
        selector=selector,
        effective_before=None,
        effective_after=None,
        profile_before=before,
        profile_after=after,
        output=output,
    )
    if error or read_error or after != "none":
        outcome = _SriovReadbackOutcome(
            error or read_error, read_error is None and after == before, read_error
        )
        _raise_sriov_partial_error("unassignment", error, outcome, result)
    return result


async def set_sriov_adapter_mode(
    hmc: HMCClient, system_name_or_uuid: str, adapter_id: str, mode: SriovMode
) -> str:
    """Confirm an adapter already has the requested admitted mode.

    Raises:
        ValueError: If the mode or adapter selector is invalid.
        SriovLogicalPortCapabilityError: If a mode transition would be required.
    """
    config = hmc.config
    validate_sriov_mode(mode)
    validate_adapter_id(adapter_id)
    system_name = await _system_name(hmc, system_name_or_uuid)
    # Only reads: the check confirms the current mode and never changes it.
    await require_sriov_read_environment(config, system_name, "adapter")
    rows = [
        row
        for row in await list_sriov_adapter_rows(config, system_name)
        if row["adapter_id"] == adapter_id
    ]
    if len(rows) == 1 and rows[0]["config_state"] == mode:
        return f"Adapter {adapter_id} already in {mode} mode"
    raise SriovLogicalPortCapabilityError(
        "adapter mode transition is not admitted by ADR 0056"
    )


async def list_sriov_adapters(
    hmc: HMCClient,
    system_name_or_uuid: str,
    adapter_id: str | None = None,
) -> InventoryResult[SriovAdapter]:
    """Return the evidence-bounded SR-IOV adapter capability state."""
    config = hmc.config
    system_name = await _system_name(hmc, system_name_or_uuid)
    try:
        await require_sriov_read_environment(config, system_name, "adapter")
    except SriovLogicalPortCapabilityError as caught:
        return _unavailable(
            "sriov_adapter", system_name, InventorySelector(adapter_id), str(caught)
        )
    rows = await list_sriov_adapter_rows(config, system_name)
    items = [
        SriovAdapter(
            system_name,
            _optional_text(row["adapter_id"]),
            row["config_state"],
            row["functional_state"],
            row["phys_loc"],
            None,
            None,
            None,
        )
        for row in rows
        if adapter_id is None or row["adapter_id"] == adapter_id
    ]
    return InventoryResult(
        "sriov_adapter",
        "available",
        system_name,
        InventorySelector(adapter_id),
        items,
        None,
    )


async def list_sriov_physical_ports(
    hmc: HMCClient,
    system_name_or_uuid: str,
    adapter_id: str | None = None,
    physical_port_id: str | None = None,
) -> InventoryResult[SriovPhysicalPort]:
    """Return the evidence-bounded SR-IOV physical-port capability state."""
    config = hmc.config
    system_name = await _system_name(hmc, system_name_or_uuid)
    selector = InventorySelector(adapter_id, physical_port_id)
    try:
        await require_sriov_read_environment(config, system_name, "physical_port")
    except SriovLogicalPortCapabilityError as caught:
        return _unavailable("sriov_physical_port", system_name, selector, str(caught))
    if adapter_id is None:
        raise ValueError("adapter_id is required for SR-IOV physical-port inventory")
    rows = await list_sriov_physical_port_rows(config, system_name, adapter_id)
    if not rows:
        raise SriovLogicalPortCapabilityError("physical-port inventory is unavailable")
    state_availability = {"1": "up", "0": "down"}
    items: list[SriovPhysicalPort] = []
    for row in rows:
        availability = state_availability.get(row["state"])
        if availability is None:
            raise HMCCLIError(f"malformed physical-port state: {row['state']!r}")
        if physical_port_id is None or row["phys_port_id"] == physical_port_id:
            items.append(
                SriovPhysicalPort(
                    system_name,
                    row["adapter_id"],
                    row["phys_port_id"],
                    availability,
                    row["phys_port_loc"],
                    None,
                    eth_capacity_granularity(row),
                    None,
                    None,
                )
            )
    return InventoryResult(
        "sriov_physical_port", "available", system_name, selector, items, None
    )


async def list_sriov_logical_ports(
    hmc: HMCClient,
    system_name_or_uuid: str,
    adapter_id: str | None = None,
    physical_port_id: str | None = None,
    logical_port_id: str | None = None,
) -> InventoryResult[SriovLogicalPort]:
    """Return the evidence-bounded SR-IOV logical-port capability state."""
    config = hmc.config
    system_name = await _system_name(hmc, system_name_or_uuid)
    selector = InventorySelector(adapter_id, physical_port_id, logical_port_id)
    try:
        await require_sriov_read_environment(config, system_name, "logical_port")
    except SriovLogicalPortCapabilityError as caught:
        return _unavailable("sriov_logical_port", system_name, selector, str(caught))
    if adapter_id is None:
        raise ValueError("adapter_id is required for SR-IOV logical-port inventory")
    configured = await list_sriov_configured_logical_port_rows(
        config, system_name, adapter_id
    )
    items = [
        SriovLogicalPort(
            system_name,
            row["adapter_id"],
            row["phys_port_id"],
            row["logical_port_id"],
            row["functional_state"],
            _optional_text(row["lpar_name"]),
            _optional_text(row["lpar_id"]),
            Decimal(row["capacity"]),
            Decimal(row["max_capacity"]),
            None,
        )
        for row in configured
        if (physical_port_id is None or row["phys_port_id"] == physical_port_id)
        and (logical_port_id is None or row["logical_port_id"] == logical_port_id)
    ]
    unconfigured = await list_sriov_unconfigured_logical_port_rows(config, system_name)
    physical_rows = await list_sriov_physical_port_rows(config, system_name, adapter_id)

    selected_unconfigured = [
        row for row in unconfigured if row.get("adapter_id") == adapter_id
    ]
    resolved_unconfigured: list[tuple[dict[str, str], str]] = []
    for row in selected_unconfigured:
        location = row.get("location_code", "")
        matches = [
            port["phys_port_id"]
            for port in physical_rows
            if location.startswith(port["phys_port_loc"] + "-S")
        ]
        if len(matches) != 1:
            raise SriovLogicalPortCapabilityError(
                "unconfigured logical-port inventory has an ambiguous physical-port parent"
            )
        resolved_unconfigured.append((row, matches[0]))
    items.extend(
        SriovLogicalPort(
            system_name,
            row["adapter_id"],
            parent_id,
            row["logical_port_id"],
            "unconfigured",
            None,
            None,
            None,
            None,
            None,
        )
        for row, parent_id in resolved_unconfigured
        if (physical_port_id is None or parent_id == physical_port_id)
        and (logical_port_id is None or row.get("logical_port_id") == logical_port_id)
    )
    return InventoryResult(
        "sriov_logical_port", "available", system_name, selector, items, None
    )


def _unavailable(
    resource_kind: ResourceKind,
    system: str,
    selector: InventorySelector,
    reason: str = SRIOV_UNAVAILABLE_REASON,
) -> InventoryResult:
    return InventoryResult(
        resource_kind,
        "capability-unavailable",
        system,
        selector,
        [],
        reason,
    )
