"""SSH reference-code reads for partitions on a managed system."""

from __future__ import annotations

import shlex
from typing import Any

from ..config import HMCConfig
from ..progress_codes import lookup_progress_code
from .commands import build_filter, parse_hmc_result_rows
from .install import validate_hmc_name
from .transport import HMCCLIError, run_hmc_command

MAX_REFCODE_COUNT = 100
REFCODE_FIELDS = ("lpar_name", "time_stamp", "refcode")


async def list_lpar_refcodes(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
    count: int = 1,
) -> list[dict[str, Any]]:
    """Read the *count* most recent reference codes for one partition.

    Rows preserve the HMC's three raw fields and add nullable ``definition``
    and ``definition_source`` metadata from the offline POWER9 catalog. These
    are informational summaries, not boot or readiness decisions.

    Rows come back most-recent first.  *count* is bounded because the whole
    result is returned to the caller in one message; ``count=1`` reproduces
    the HMC's own default of the current code alone.  Two guards protect two
    parsers, neither substituting for the other: :func:`build_filter` keeps
    the ``--filter`` record's ``,``, ``=`` and ``"`` structure meaningful to
    the HMC (ADR 0061), and :func:`shlex.quote` keeps each value one word for
    the remote shell, which runs first.

    Raises:
        TypeError: If *count* is not an ``int``.  ``bool`` is rejected too:
            it is an ``int`` subclass, so ``True`` would otherwise reach the
            command string as ``-n 1``.
        ValueError: If *count* is outside ``1..MAX_REFCODE_COUNT``, or either
            selector is empty, blank, starts with ``-``, or carries a
            control character.
        HMCCLIError: If the selector carries a record delimiter, the response
            header does not name the three fields, the HMC reports a partition
            other than the one asked for, or the HMC refuses the command.
    """
    if isinstance(count, bool) or not isinstance(count, int):
        raise TypeError(f"count must be an int, got {type(count).__name__}")
    if not 1 <= count <= MAX_REFCODE_COUNT:
        raise ValueError(
            f"count must be between 1 and {MAX_REFCODE_COUNT}, got {count}"
        )
    for field, value in (("system_name", system_name), ("lpar_name", lpar_name)):
        # validate_hmc_name admits whitespace-only text, and an empty or blank
        # selector composes a filter naming no partition rather than failing.
        validate_hmc_name(value, field)
        if not value.strip():
            raise ValueError(f"{field} must be non-empty printable text")
    selector = build_filter([("lpar_names", lpar_name)])
    command = (
        f"lsrefcode -r lpar -m {shlex.quote(system_name)}"
        f" --filter {shlex.quote(selector)}"
        f" -n {count} -F {','.join(REFCODE_FIELDS)} --header"
    )
    rows = parse_hmc_result_rows(
        await run_hmc_command(config, command),
        REFCODE_FIELDS,
        "lsrefcode",
        blank_is_empty=True,
    )
    # The --filter is the HMC's narrowing, not ours; check it landed, the way
    # ssh/affinity.py does for its own filtered lsmemopt read.
    for row in rows:
        if row["lpar_name"] != lpar_name:
            raise HMCCLIError(
                f"lsrefcode reported partition {row['lpar_name']!r}; "
                f"expected {lpar_name!r}"
            )
    enriched = []
    for row in rows:
        definition = lookup_progress_code(row["refcode"])
        enriched.append(
            {
                **row,
                "definition": definition.definition if definition else None,
                "definition_source": definition.source() if definition else None,
            }
        )
    return enriched
