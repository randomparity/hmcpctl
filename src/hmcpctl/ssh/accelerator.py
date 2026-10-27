"""Fixed native GZIP accelerator inventory and QoS submission commands."""

import shlex
from typing import Any

from ..config import HMCConfig
from ..documents.lpar_accelerator import LparAcceleratorQos
from .commands import _parse_lshwres_output, build_filter
from .transport import run_hmc_command


async def read_accelerator_qos(
    config: HMCConfig, system: str, partition_id: int | None
) -> list[dict[str, Any]]:
    """Read system inventory or one resolved partition's native QoS fields."""
    if not isinstance(system, str) or not system.strip():
        raise ValueError("system must be nonblank text; nothing was sent")
    if partition_id is not None and (type(partition_id) is not int or partition_id < 1):
        raise ValueError("partition_id must be a positive integer; nothing was sent")
    if partition_id is None:
        command = (
            f"lshwres -r accel -m {shlex.quote(system)} --rsubtype gzip --level sys"
        )
    else:
        selection = build_filter([("lpar_ids", partition_id)])
        command = (
            f"lshwres -r accel -m {shlex.quote(system)} --rsubtype gzip --level lpar "
            f"--filter {shlex.quote(selection)}"
        )
    output = await run_hmc_command(config, command, max_output_bytes=65536)
    return _parse_lshwres_output(output)


async def set_accelerator_qos(
    config: HMCConfig,
    system: str,
    partition_id: int,
    change: LparAcceleratorQos,
) -> str:
    """Submit one explicit quantity without retries or inferred no-op behavior."""
    if not isinstance(system, str) or not system.strip():
        raise ValueError("system must be nonblank text; nothing was sent")
    if type(partition_id) is not int or partition_id < 1:
        raise ValueError("partition_id must be a positive integer; nothing was sent")
    if not isinstance(change, LparAcceleratorQos):
        raise TypeError("change must be a typed LparAcceleratorQos; nothing was sent")
    command = (
        f"chhwres -r accel -m {shlex.quote(system)} --rsubtype gzip -o s "
        f"--id {partition_id} -q {change.qos}"
    )
    if change.wait_minutes is not None:
        command += f" -w {change.wait_minutes}"
    if change.detail_level is not None:
        command += f" -d {change.detail_level}"
    if change.force:
        command += " --force"
    return await run_hmc_command(
        config, command, include_stderr=True, max_output_bytes=65536
    )
