"""CLI boundary helpers for loading LPAR assignment input."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from ...documents.lpar_io import LparPhysicalIO
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
