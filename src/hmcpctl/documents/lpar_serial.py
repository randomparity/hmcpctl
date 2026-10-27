"""Finite seven-component serial creation input; no capability inference."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from pydantic import ConfigDict, StrictBool, StrictInt, StrictStr

SERIAL_INPUT_BYTES = 65536


def serial_name(value: object) -> str:
    """Keep a partition name within the nested native grammar and input bound."""
    if type(value) is not str or not value:
        raise ValueError("serial partition name must be a nonempty string")
    if (
        len(value) > SERIAL_INPUT_BYTES
        or len(value.encode("utf-8")) > SERIAL_INPUT_BYTES
    ):
        raise ValueError("serial partition name exceeds 64 KiB")
    if any(ord(c) < 32 or ord(c) == 127 or c in '/,="\\' for c in value):
        raise ValueError("serial partition name contains a native record delimiter")
    return value


def serial_integer(value: object, label: str, minimum: int = 0) -> int:
    """Require a strict integer; bound expansion without inventing a hardware cap."""
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    # Four bits per decimal character overestimates the bounded encoded input.
    if value.bit_length() > SERIAL_INPUT_BYTES * 4:
        raise ValueError(f"{label} cannot fit the bounded serial input encoding")
    return value


@dataclass(frozen=True)
class VirtualSerialAdapter:
    """One creation record; optional server selectors remain independently optional."""

    __pydantic_config__ = ConfigDict(extra="forbid")
    local_slot: StrictInt = field(
        metadata={
            "description": "Local virtual slot, integer >=2 and below explicit max_virtual_slots; unique in this request."
        }
    )
    role: Literal["client", "server"] = field(
        metadata={
            "description": "Native client or server role; client eligibility is decided by HMC in all documented environments."
        }
    )
    is_required: StrictBool = field(
        metadata={
            "description": "Explicit boolean required flag; false is retained as native 0."
        }
    )
    supports_hmc: StrictInt | None = field(
        default=None,
        metadata={
            "description": "Server-only optional HMC flag; the only explicit value is integer 0. Omit for clients."
        },
    )
    remote_lpar_id: StrictInt | None = field(
        default=None,
        metadata={
            "description": "Optional native partition ID, distinct from REST UUID. Client requires exactly one ID or exact name; dual server selectors must agree."
        },
    )
    remote_lpar_name: StrictStr | None = field(
        default=None,
        metadata={
            "description": "Optional exact contained partition name; numeric-looking names remain names. Native delimiters and controls are refused."
        },
    )
    remote_slot: StrictInt | None = field(
        default=None,
        metadata={
            "description": "Remote virtual slot; required for clients, optional for servers, below selected peer maximum. No reciprocal adapter is created."
        },
    )

    def __post_init__(self) -> None:
        serial_integer(self.local_slot, "local_slot", 2)
        if type(self.role) is not str or self.role not in {"client", "server"}:
            raise ValueError("serial role must be client or server")
        if type(self.is_required) is not bool:
            raise ValueError("is_required must be a boolean")
        if self.supports_hmc is not None and (
            type(self.supports_hmc) is not int or self.supports_hmc != 0
        ):
            raise ValueError("explicit supports_hmc must be integer 0")
        if self.remote_lpar_id is not None:
            serial_integer(self.remote_lpar_id, "remote_lpar_id")
        if self.remote_lpar_name is not None:
            serial_name(self.remote_lpar_name)
        if self.remote_slot is not None:
            serial_integer(self.remote_slot, "remote_slot")
        if self.role == "client" and (
            self.supports_hmc is not None
            or (self.remote_lpar_id is None) == (self.remote_lpar_name is None)
            or self.remote_slot is None
        ):
            raise ValueError(
                "serial client requires exactly one remote ID/name, a remote slot and no HMC flag"
            )

    def native(self) -> str:
        """Preserve all seven positions, including explicit zero and false."""
        values = (
            self.local_slot,
            self.role,
            self.supports_hmc,
            self.remote_lpar_id,
            self.remote_lpar_name,
            self.remote_slot,
            int(self.is_required),
        )
        fields = []
        for value in values:
            if value is None:
                fields.append("")
            elif isinstance(value, int):
                # Avoid the interpreter's configurable int-to-string digit limit.
                fields.append(format(Decimal(value), "f"))
            else:
                fields.append(value)
        return "/".join(fields)


def validate_serial_input(
    adapters: tuple[VirtualSerialAdapter, ...], maximum: int | None
) -> str:
    """Validate request-local slots and return only the finite native list value."""
    if type(adapters) is not tuple:
        raise TypeError("virtual_serial_adapters must be a tuple")
    if not adapters:
        return ""
    maximum = serial_integer(maximum, "explicit max_virtual_slots", 3)
    slots: set[int] = set()
    records = []
    size = 0
    for adapter in adapters:
        if not isinstance(adapter, VirtualSerialAdapter):
            raise TypeError("virtual_serial_adapters contains a non-adapter")
        text = adapter.native()
        if adapter.local_slot >= maximum or adapter.local_slot in slots:
            raise ValueError(
                "serial local slots must be unique and below max_virtual_slots"
            )
        slots.add(adapter.local_slot)
        size += len(text.encode("utf-8")) + bool(records)
        if size > SERIAL_INPUT_BYTES:
            raise ValueError("serial collection exceeds 64 KiB")
        records.append(text)
    return ",".join(records)
