"""Typed native partition-create physical and IBM i tagged I/O (ADR 0274)."""

from dataclasses import dataclass, field, fields

from pydantic import ConfigDict, StrictInt

from ..ssh.profiles import DRC_INDEX_PATTERN, ProfileIoSlot


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
            "description": "IBM i load-source physical or declared direct SR-IOV DRC; virtual-slot forms await #638."
        },
    )
    alt_restart_device_slot: str | None = field(
        default=None,
        metadata={
            "description": "IBM i alternate restart physical or declared direct SR-IOV DRC."
        },
    )
    console_slot: str | None = field(
        default=None,
        metadata={
            "description": "IBM i physical/direct SR-IOV console DRC or explicit hmc; no default is inserted."
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
            if isinstance(value, str) and value.isdecimal() and len(value) < 8:
                raise ValueError(
                    f"{name}: positive virtual-slot forms require creation-time adapter linkage; see #638"
                )
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

    @property
    def requested(self) -> bool:
        return bool(
            self.io_slots
            or self.lpar_io_pool_ids
            or self.tagged()
            or self.virtual_opti_pool_id is not None
        )
