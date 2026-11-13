"""Finite six-component vSCSI client creation input; never a server record (ADR 0376)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import ConfigDict, StrictBool, StrictInt, StrictStr

from .lpar_serial import (
    SERIAL_INPUT_BYTES,
    VirtualSerialAdapter,
    serial_integer,
    serial_name,
)


@dataclass(frozen=True)
class VirtualScsiClientAdapter:
    """One client record naming a remote VIOS; creation never writes that VIOS."""

    __pydantic_config__ = ConfigDict(extra="forbid")
    local_slot: StrictInt = field(
        metadata={
            "description": "Local virtual slot, integer >=2 and below explicit max_virtual_slots; unique across serial and vSCSI records in this request."
        }
    )
    remote_slot: StrictInt = field(
        metadata={
            "description": "Server slot on the remote VIOS, integer >=2 and below that VIOS's maximum. No server adapter or mapping is created; pairing is unproven."
        }
    )
    is_required: StrictBool = field(
        metadata={
            "description": "Explicit boolean required flag; false is retained as native 0."
        }
    )
    remote_lpar_id: StrictInt | None = field(
        default=None,
        metadata={
            "description": "Native partition ID of the remote VIOS on the same system; supply exactly one of ID or name."
        },
    )
    remote_lpar_name: StrictStr | None = field(
        default=None,
        metadata={
            "description": "Exact name of the remote VIOS on the same system; supply exactly one of ID or name. Native delimiters and controls are refused."
        },
    )

    def __post_init__(self) -> None:
        serial_integer(self.local_slot, "local_slot", 2)
        serial_integer(self.remote_slot, "remote_slot", 2)
        if type(self.is_required) is not bool:
            raise ValueError("is_required must be a boolean")
        if (self.remote_lpar_id is None) == (self.remote_lpar_name is None):
            raise ValueError(
                "vSCSI client requires exactly one of remote_lpar_id or remote_lpar_name"
            )
        if self.remote_lpar_id is not None:
            serial_integer(self.remote_lpar_id, "remote_lpar_id", 1)
        if self.remote_lpar_name is not None:
            serial_name(self.remote_lpar_name)

    def native(self) -> str:
        """Render all six positions; the absent selector stays empty."""
        values = (
            self.local_slot,
            "client",
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


def validate_vscsi_input(
    adapters: tuple[VirtualScsiClientAdapter, ...],
    maximum: int | None,
    serial: tuple[VirtualSerialAdapter, ...] = (),
) -> str:
    """Validate request-local slots against vSCSI and serial; return the native value."""
    if type(adapters) is not tuple:
        raise TypeError("virtual_scsi_adapters must be a tuple")
    if not adapters:
        return ""
    maximum = serial_integer(maximum, "explicit max_virtual_slots", 3)
    slots = {adapter.local_slot for adapter in serial}
    records = []
    size = 0
    for adapter in adapters:
        if not isinstance(adapter, VirtualScsiClientAdapter):
            raise TypeError("virtual_scsi_adapters contains a non-adapter")
        if adapter.local_slot >= maximum or adapter.local_slot in slots:
            raise ValueError(
                "vSCSI local slots must be unique across serial and vSCSI records "
                "and below max_virtual_slots"
            )
        slots.add(adapter.local_slot)
        text = adapter.native()
        size += len(text.encode("utf-8")) + bool(records)
        if size > SERIAL_INPUT_BYTES:
            raise ValueError("vSCSI collection exceeds 64 KiB")
        records.append(text)
    return ",".join(records)
