"""Fixed memory-resource commands over the SSH transport."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from typing import Any

from ..config import HMCConfig
from ..documents.lpar_memory import LparMemoryChange
from .commands import _parse_lshwres_output, hmc_error_code
from .transport import HMCCLIError, run_hmc_command

# The managed system does not support Active Memory Sharing (V11R2, POWER11).
_AMS_UNSUPPORTED = "HSCLA4A0"


async def list_memory_pools(
    config: HMCConfig,
    system_name: str,
) -> list[dict[str, Any]]:
    """List the shared memory pool on *system_name* via SSH.

    Runs ``lshwres -r mempool -m <system_name>`` and returns one dict per row
    parsed from the key=value output. A system has at most one pool
    (``max_mem_pools=1``) and the pool has no name. The row's field names are
    uncaptured: no swept system had a pool (#1245), so callers must not key
    on any of them. A system with no pool answers ``No results were found.``
    with exit 0, and a system without Active Memory Sharing refuses with
    ``HSCLA4A0``; both return an empty list.
    """
    try:
        output = await run_hmc_command(
            config, f"lshwres -r mempool -m {shlex.quote(system_name)}"
        )
    except HMCCLIError as error:
        if hmc_error_code(error) == _AMS_UNSUPPORTED:
            return []
        raise
    return _parse_lshwres_output(output)


def _not_dedicated(
    rows: list[dict[str, Any]], label: Callable[[dict[str, Any]], str]
) -> list[str]:
    """Name each row whose ``mem_mode`` is not the captured ``ded`` value."""
    return [
        f"{label(row)} (mem_mode={row.get('mem_mode', '<absent>')})"
        for row in rows
        if row.get("mem_mode") != "ded"
    ]


async def remove_memory_pool(config: HMCConfig, system_name: str) -> str:
    """Remove the shared memory pool from *system_name* via SSH.

    Refuses without issuing ``chhwres`` when the system has no pool, or when
    any partition or profile is not confirmed to use dedicated memory: every
    ``lshwres -r mem --level lpar`` row and every ``lssyscfg -r prof`` row
    must carry ``mem_mode=ded``. A ``shared`` partition is paged from the
    pool, a ``shared`` profile could no longer be activated, and a row
    without the field cannot be confirmed either way, so all of them refuse.
    Otherwise runs the documented ``chhwres -r mempool -m <system_name> -o r``,
    which takes no pool name because the system has at most one pool.

    Returns the HMC CLI output (immediate delete — no job to poll).

    Raises:
        HMCCLIError: If *system_name* has no shared memory pool, or if any
            partition or profile is not confirmed in dedicated memory mode.
    """
    system = shlex.quote(system_name)
    if not await list_memory_pools(config, system_name):
        raise HMCCLIError(
            f"Cannot remove the shared memory pool on '{system_name}' — the "
            "system has no shared memory pool. Use hmc_list_memory_pools to "
            "check before retrying."
        )

    partitions = _parse_lshwres_output(
        await run_hmc_command(config, f"lshwres -r mem --level lpar -m {system}")
    )
    profiles = _parse_lshwres_output(
        await run_hmc_command(config, f"lssyscfg -r prof -m {system}")
    )
    unconfirmed = _not_dedicated(
        partitions, lambda row: f"partition {row.get('lpar_name', '<unnamed>')}"
    ) + _not_dedicated(
        profiles,
        lambda row: (
            f"profile {row.get('name', '<unnamed>')} of "
            f"{row.get('lpar_name', '<unnamed>')}"
        ),
    )
    if unconfirmed:
        raise HMCCLIError(
            f"Cannot remove the shared memory pool on '{system_name}' — not "
            f"confirmed in dedicated memory mode: {', '.join(unconfirmed)}. "
            "Change them to mem_mode=ded before retrying."
        )

    return await run_hmc_command(config, f"chhwres -r mempool -m {system} -o r")


async def change_lpar_memory_quantity(
    config: HMCConfig,
    system_name: str,
    partition_id: int,
    change: LparMemoryChange,
) -> str:
    """Submit one fixed native quantity command; admission belongs to the operation.

    This function never retries. A transport failure may follow partial effects;
    its caller independently reads the contained partition before reporting an outcome.
    """
    if type(partition_id) is not int or partition_id < 1:
        raise ValueError("partition_id must be a positive integer; nothing was sent")
    if not isinstance(change, LparMemoryChange):
        raise TypeError("change must be a typed LparMemoryChange; nothing was sent")
    operation = {"add": "a", "remove": "r", "set": "s"}[change.operation]
    parts = [
        "chhwres",
        "-r",
        "mem",
        "-m",
        shlex.quote(system_name),
        "-o",
        operation,
        "--id",
        str(partition_id),
    ]
    if change.quantity_mib is not None:
        parts.extend(("-q", str(change.quantity_mib)))
    if change.entitled_mib is not None:
        parts.extend(("--entitled", str(change.entitled_mib)))
    if change.force:
        parts.append("--force")
    return await run_hmc_command(config, " ".join(parts), include_stderr=True)
