"""Typed native partition-create physical and IBM i tagged I/O (ADR 0274)."""

import re
from dataclasses import dataclass, field, fields
from decimal import Decimal

from pydantic import ConfigDict, StrictBool, StrictInt

#: The DRC-index form every captured and documented `io_slots` index takes (ADR 0166).
DRC_INDEX_PATTERN = re.compile(r"[0-9A-F]{8}")

#: Tagged roles whose virtual-slot form names a declared vSCSI client (ADR 0382).
VIRTUAL_TAGGED = ("load_source_slot", "alt_restart_device_slot")


def virtual_form(value: object) -> bool:
    """Whether a tagged value is a virtual slot number rather than a DRC or ``hmc``."""
    return (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and len(value) < 8
    )


def virtual_tagged_value(name: str, value: object) -> bool:
    """Whether a tagged field carries a bindable virtual load-source/restart slot."""
    return name in VIRTUAL_TAGGED and virtual_form(value)


@dataclass(frozen=True)
class ProfileIoSlot:
    """One `io_slots` entry in the ADR 0165-admitted read rendering."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    drc_index: str = field(
        metadata={
            "description": "Physical slot DRC index: eight uppercase hexadecimal digits."
        }
    )
    pool_id: str | None = field(
        metadata={"description": "Decimal I/O pool ID, or null for no pool."}
    )
    is_required: StrictBool = field(
        metadata={"description": "Whether the slot is required for profile activation."}
    )


def decimal_id(value: str) -> str:
    """Require a canonical nonnegative decimal identifier."""
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ValueError("pool/port identity must be a decimal string")
    if str(int(value)) != value:
        raise ValueError("pool/port identity must use canonical decimal notation")
    return value


def drc(value: str) -> str:
    """Require the documented hexadecimal DRC form and schema index range."""
    if (
        not isinstance(value, str)
        or not DRC_INDEX_PATTERN.fullmatch(value)
        or int(value, 16) < 65536
    ):
        raise ValueError(
            "slot must be eight uppercase hexadecimal digits representing a DRC index"
        )
    return value


@dataclass(frozen=True)
class LparPhysicalIO:
    """Initial-profile fields; omitted values retain HMC defaults, never a console choice."""

    __pydantic_config__ = ConfigDict(extra="forbid")
    io_slots: tuple[ProfileIoSlot, ...] = field(
        default=(),
        metadata={
            "description": "Initial physical slots with DRC, optional pool ID and required flag."
        },
    )
    lpar_io_pool_ids: tuple[str, ...] = field(
        default=(),
        metadata={
            "description": "Initial partition I/O pool memberships as decimal IDs."
        },
    )
    load_source_slot: str | None = field(
        default=None,
        metadata={
            "description": "IBM i load source: physical or declared direct SR-IOV DRC, or the decimal local slot of a vSCSI client declared in virtual_scsi_adapters."
        },
    )
    alt_restart_device_slot: str | None = field(
        default=None,
        metadata={
            "description": "IBM i alternate restart: physical or declared direct SR-IOV DRC, or the decimal local slot of a vSCSI client declared in virtual_scsi_adapters."
        },
    )
    console_slot: str | None = field(
        default=None,
        metadata={
            "description": "IBM i physical/direct SR-IOV console DRC or explicit hmc; required for native IBM i creation, no default is inserted; virtual slots are refused."
        },
    )
    alt_console_slot: str | None = field(
        default=None,
        metadata={
            "description": "IBM i alternate-console physical DRC, declared in io_slots."
        },
    )
    op_console_slot: str | None = field(
        default=None,
        metadata={
            "description": "IBM i operations-console physical DRC, declared in io_slots."
        },
    )
    virtual_opti_pool_id: StrictInt | None = field(
        default=None,
        metadata={
            "description": "IBM i virtual OptiConnect: integer 0 disabled or 1 enabled."
        },
    )

    def __post_init__(self) -> None:
        if not isinstance(self.io_slots, tuple) or not isinstance(
            self.lpar_io_pool_ids, tuple
        ):
            raise TypeError("io_slots and lpar_io_pool_ids must be tuples")
        if len(self.io_slots) > 256 or len(self.lpar_io_pool_ids) > 256:
            raise ValueError("I/O creation lists must contain at most 256 entries")
        for slot in self.io_slots:
            if (
                not isinstance(slot, ProfileIoSlot)
                or type(slot.is_required) is not bool
            ):
                raise ValueError(
                    "io_slots must contain typed slots with boolean is_required"
                )
            drc(slot.drc_index)
            if slot.pool_id is not None:
                decimal_id(slot.pool_id)
        if len({s.drc_index for s in self.io_slots}) != len(self.io_slots):
            raise ValueError("duplicate physical slot DRC")
        for pool in self.lpar_io_pool_ids:
            decimal_id(pool)
        if len(set(self.lpar_io_pool_ids)) != len(self.lpar_io_pool_ids):
            raise ValueError("duplicate partition I/O pool")
        for name, value in self.tagged():
            if name == "console_slot" and value == "hmc":
                continue
            if virtual_form(value):
                if name not in VIRTUAL_TAGGED:
                    raise ValueError(
                        f"{name}: virtual-slot forms are refused; no cited source "
                        "establishes an eligible virtual device family for this role "
                        "(ADR 0382)"
                    )
                if str(int(value)) != value:
                    raise ValueError(
                        f"{name}: virtual slot must use canonical decimal notation"
                    )
                continue
            drc(value)
        if self.virtual_opti_pool_id is not None and (
            type(self.virtual_opti_pool_id) is not int
            or self.virtual_opti_pool_id not in (0, 1)
        ):
            raise ValueError("virtual_opti_pool_id must be integer 0 or 1")

    def tagged(self) -> list[tuple[str, str]]:
        return [
            (f.name, value)
            for f in fields(self)
            if f.name.endswith("_slot") and (value := getattr(self, f.name)) is not None
        ]

    def virtual_tagged(self) -> tuple[tuple[str, int], ...]:
        """Virtual-slot load-source and alternate-restart roles, by slot number."""
        return tuple(
            (name, int(value))
            for name, value in self.tagged()
            if virtual_tagged_value(name, value)
        )

    @property
    def requested(self) -> bool:
        return bool(
            self.io_slots
            or self.lpar_io_pool_ids
            or self.tagged()
            or self.virtual_opti_pool_id is not None
        )


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


def validate_capacity_percent(value: Decimal) -> Decimal:
    """Require a finite percentage from 1 through 100 with two decimal places."""
    if not value.is_finite() or value < 1 or value > 100:
        raise ValueError("capacity_percent must be between 1 and 100")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise ValueError("capacity_percent supports at most two decimal places")
    return value
