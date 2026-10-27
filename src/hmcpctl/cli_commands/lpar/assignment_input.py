"""CLI boundaries for typed assignments and inline security-policy input."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from ...documents.lpar_io import LparPhysicalIO
from ...documents.lpar_security_policy import LparSecurityPolicy
from ...documents.lpar_serial import SERIAL_INPUT_BYTES, VirtualSerialAdapter
from ...operations.lpar.assignments import LparPcieAssignments
from ..output import terminal_field, usage_error


def load_pcie_assignments(path: Path | None) -> LparPcieAssignments:
    """Load and validate the shared PCIe assignment schema from JSON."""
    if path is None:
        return LparPcieAssignments()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return TypeAdapter(LparPcieAssignments).validate_python(payload)
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        usage_error(f"Cannot load --pcie-assignments {path}: {error}")


def load_physical_io(path: Path | None) -> LparPhysicalIO:
    """Read at most 64 KiB of the typed creation-only JSON input."""
    if path is None:
        return LparPhysicalIO()
    try:
        with path.open("rb") as source:
            data = source.read(65537)
        if len(data) > 65536:
            raise ValueError("physical I/O JSON exceeds 64 KiB")
        return TypeAdapter(LparPhysicalIO).validate_json(data)
    except (OSError, ValueError) as error:
        usage_error(f"Cannot load --physical-io: {terminal_field(error, err=True)}")


SECURITY_POLICY_WARNING = (
    "Secure boot may weaken; signed/Linux enables are irreversible. Store enable/increase "
    "requires inactivity; nonzero shrink requires Power11; disable requires HMC emptiness "
    "checks. PK bypass skips old-PK authentication. Linux selection declares intent only. "
    "Pending policy may require restart; no guest, key-readiness or runtime proof."
)


def load_security_policy(value: str | None) -> LparSecurityPolicy:
    """Parse inline policy only, without reflecting untrusted JSON or file paths."""
    if value is None:
        return LparSecurityPolicy()
    if len(value) > 65536 or len(value.encode("utf-8")) > 65536:
        usage_error("Invalid --security-policy: inline JSON exceeds 64 KiB")
    try:
        payload = json.loads(value)
    except (ValueError, RecursionError):
        usage_error("Invalid --security-policy: expected an inline JSON object")
    try:
        return TypeAdapter(LparSecurityPolicy).validate_python(payload)
    except ValidationError as error:
        details = ", ".join(
            ".".join(str(part) for part in item["loc"]) or item["type"]
            for item in error.errors()
        )
        usage_error(f"Invalid --security-policy: {details}")


SERIAL_INPUT_HELP = (
    "Inline JSON array (64 KiB): local_slot, role (client/server), is_required boolean; "
    "optional supports_hmc (server integer 0 only), remote_lpar_id, remote_lpar_name, "
    "remote_slot. Clients need one ID/name and remote slot. Explicit --max-virtual-slots "
    "must exceed unique local slots >=2. Selected peers require ownership; no peer is "
    "created. HMC decides client capability; saved pair verification proves no traffic."
)


def load_virtual_serial_adapters(value: str | None) -> tuple[VirtualSerialAdapter, ...]:
    """Decode bounded inline serial JSON without reflecting untrusted values."""
    if value is None:
        return ()
    if (
        len(value) > SERIAL_INPUT_BYTES
        or len(value.encode("utf-8")) > SERIAL_INPUT_BYTES
    ):
        usage_error("Invalid --virtual-serial-adapters: inline JSON exceeds 64 KiB")
    try:
        payload = json.loads(value)
    except (ValueError, RecursionError):
        usage_error("Invalid --virtual-serial-adapters: expected an inline JSON array")
    if not isinstance(payload, list):
        usage_error("Invalid --virtual-serial-adapters: expected an inline JSON array")
    try:
        return TypeAdapter(tuple[VirtualSerialAdapter, ...]).validate_python(payload)
    except ValidationError as error:
        details = ", ".join(
            ".".join(str(part) for part in item["loc"]) or item["type"]
            for item in error.errors()
        )
        usage_error(f"Invalid --virtual-serial-adapters: {details}")
