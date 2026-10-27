"""Fixed non-pool processor commands; target admission belongs to the operation."""

from __future__ import annotations

import csv
import io
import shlex

from ..config import HMCConfig
from ..documents.lpar_processors import (
    LparProcessorChange,
    LparProcessorMove,
    bounded_processor_number,
)
from .transport import HMCCLIError, run_hmc_command

_FIELDS = [
    "lpar_id",
    "lpar_name",
    "curr_proc_mode",
    "curr_min_procs",
    "curr_procs",
    "curr_max_procs",
    "pend_proc_mode",
    "pend_min_procs",
    "pend_procs",
    "pend_max_procs",
    "run_procs",
    "curr_min_proc_units",
    "curr_proc_units",
    "curr_max_proc_units",
    "pend_min_proc_units",
    "pend_proc_units",
    "pend_max_proc_units",
    "run_proc_units",
]


def _require_id(partition_id: int) -> None:
    if type(partition_id) is not int or partition_id < 1:
        raise ValueError("partition_id must be a positive integer; nothing was sent")
    bounded_processor_number(partition_id, whole=True)


async def read_lpar_processor_quantities(
    config: HMCConfig, system_name: str, partition_id: int
) -> dict[str, str]:
    """Read exactly one projected CPU record with a bounded SSH response."""
    _require_id(partition_id)
    output = await run_hmc_command(
        config,
        f"lshwres -r proc -m {shlex.quote(system_name)} --level lpar "
        f"--filter lpar_ids={partition_id} -F {','.join(_FIELDS)}",
        max_output_bytes=65536,
    )
    try:
        rows = list(csv.reader(io.StringIO(output), strict=True))
    except csv.Error as error:
        raise HMCCLIError(
            "Malformed processor CSV; cannot establish quantities"
        ) from error
    if len(rows) != 1 or len(rows[0]) != len(_FIELDS):
        raise HMCCLIError("Expected one eighteen-field processor record")
    row = dict(zip(_FIELDS, rows[0], strict=True))
    if row["lpar_id"] != str(partition_id) or not row["lpar_name"]:
        raise HMCCLIError(
            "Processor record identity does not match the selected partition"
        )
    return row


async def change_lpar_processor_quantity(
    config: HMCConfig, system_name: str, partition_id: int, change: LparProcessorChange
) -> str:
    """Submit once; errors may follow effects and require independent readback."""
    _require_id(partition_id)
    if not isinstance(change, LparProcessorChange):
        raise TypeError("change must be a typed LparProcessorChange; nothing was sent")
    operation = {"add": "a", "remove": "r", "set": "s"}[change.operation]
    parts = [
        "chhwres",
        "-r",
        "proc",
        "-m",
        shlex.quote(system_name),
        "-o",
        operation,
        "--id",
        str(partition_id),
    ]
    if change.processors is not None:
        parts.extend(
            ("--procs", str(bounded_processor_number(change.processors, whole=True)))
        )
    if change.processing_units is not None:
        parts.extend(
            ("--procunits", f"{bounded_processor_number(change.processing_units):.2f}")
        )
    if change.force:
        parts.append("--force")
    return await run_hmc_command(
        config, " ".join(parts), include_stderr=True, max_output_bytes=65536
    )


async def move_lpar_processor_quantity(
    config: HMCConfig,
    system_name: str,
    source_id: int,
    target_id: int,
    move: LparProcessorMove,
) -> str:
    """Submit one fixed native move; admission and postconditions belong upstream."""
    _require_id(source_id)
    _require_id(target_id)
    if source_id == target_id:
        raise ValueError("Processor move requires distinct partition IDs")
    if not isinstance(move, LparProcessorMove):
        raise TypeError("move must be a typed LparProcessorMove; nothing was sent")
    parts = [
        "chhwres",
        "-r",
        "proc",
        "-m",
        shlex.quote(system_name),
        "-o",
        "m",
        "--id",
        str(source_id),
        "--tid",
        str(target_id),
    ]
    if move.processors is not None:
        parts.extend(
            ("--procs", str(bounded_processor_number(move.processors, whole=True)))
        )
    if move.processing_units is not None:
        parts.extend(
            ("--procunits", f"{bounded_processor_number(move.processing_units):.2f}")
        )
    if move.force:
        parts.append("--force")
    return await run_hmc_command(
        config, " ".join(parts), include_stderr=True, max_output_bytes=65536
    )
