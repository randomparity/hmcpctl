"""Fixed creation-memory admission and classic profile reads; no native writes."""

from __future__ import annotations

import re
import shlex
from dataclasses import fields
from decimal import Decimal, InvalidOperation

from pydantic import TypeAdapter

from ..config import HMCConfig
from ..documents.lpar_memory_layout import LparMemoryLayout
from .commands import build_filter, parse_hmc_result_rows
from .transport import run_hmc_command

_RATIO_FIELDS = ("hpt_ratio", "ppt_ratio")
_LAYOUT_FIELDS = tuple(item.name for item in fields(LparMemoryLayout))
_PROFILE_FIELDS = ("lpar_name", "name", *_LAYOUT_FIELDS)
_UNKNOWN = {"", "null", "none", "unknown"}


async def admit_memory_layout(
    config: HMCConfig, system_name: str, layout: LparMemoryLayout
) -> None:
    """Require requested ratios in one selected-system advertisement."""
    requested = [(name, getattr(layout, name)) for name in _RATIO_FIELDS]
    if not any(value is not None for _, value in requested):
        return
    columns = tuple(f"possible_{name}s" for name in _RATIO_FIELDS)
    command = (
        f"lshwres -r mem -m {shlex.quote(system_name)} --level sys "
        f"-F {','.join(columns)} --header"
    )
    rows = parse_hmc_result_rows(
        await run_hmc_command(config, command), columns, "memory ratio admission"
    )
    if len(rows) != 1:
        raise ValueError(
            "Memory ratio admission requires exactly one advertisement row"
        )
    for name, value in requested:
        if value is None:
            continue
        tokens = rows[0][f"possible_{name}s"].split(",")
        if len(set(tokens)) != len(tokens) or any(
            token in _UNKNOWN for token in tokens
        ):
            raise ValueError(f"{name} advertisement is missing, unknown or ambiguous")
        for token in tokens:
            TypeAdapter(LparMemoryLayout).validate_python({name: token})
        if value not in tokens:
            raise ValueError(f"{name} is not advertised by the selected system")


async def get_lpar_memory_layout(
    config: HMCConfig, system_name: str, name: str
) -> LparMemoryLayout:
    """Read the exact default profile; callers must establish classic mode first."""
    filters = build_filter([("lpar_names", name), ("profile_names", "default_profile")])
    command = (
        f"lssyscfg -r prof -m {shlex.quote(system_name)} "
        f"--filter {shlex.quote(filters)} -F {','.join(_PROFILE_FIELDS)} --header"
    )
    rows = parse_hmc_result_rows(
        await run_hmc_command(config, command),
        _PROFILE_FIELDS,
        "memory layout readback",
    )
    if len(rows) != 1:
        raise ValueError("Memory layout readback requires exactly one profile row")
    row = rows[0]
    if row["lpar_name"] != name or row["name"] != "default_profile":
        raise ValueError(
            "Memory layout readback profile identity does not match request"
        )
    values: dict[str, object] = {}
    for field in _LAYOUT_FIELDS:
        token = row[field]
        if token == "null":
            continue
        if token in _UNKNOWN:
            raise ValueError(f"Memory layout readback has unknown {field}")
        if field.endswith("_num_huge_pages"):
            if re.fullmatch(r"[0-9]+", token) is None:
                raise ValueError(
                    f"Memory layout readback requires ASCII integer {field}"
                )
            values[field] = int(token)
        elif field == "kvm_reserved_mem_percent":
            try:
                values[field] = Decimal(token)
            except InvalidOperation as error:
                raise ValueError(
                    "Memory layout readback has malformed reservation"
                ) from error
        else:
            values[field] = token
    return TypeAdapter(LparMemoryLayout).validate_python(values)
