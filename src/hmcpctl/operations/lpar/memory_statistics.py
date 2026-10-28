"""One native statistics reset and independent, bounded observations (ADR 0288)."""

from __future__ import annotations

import csv
import math
import re
from typing import Any

from ...client.core import HMCClient
from ...documents.lpar_memory import LparMemoryStatisticsReset
from ...errors import HMCError
from ...resource_identity import (
    is_uuid,
    resolve_system_uuid,
    summary_matching,
)
from ...ssh.memory import (
    read_lpar_memory_statistics,
    reset_lpar_memory_statistics_native,
)
from ...ssh.transport import HMCCLIError
from ...xmlutil import leaf_text
from ..inventory.composite import resource_container
from .memory import _contained_partition
from .ownership import authorize_lpar_mutation

_COUNTERS = (
    "min_io_entitled_mem",
    "optimal_io_entitled_mem",
    "max_io_entitled_mem_usage",
)
_CELL = r'(?:[^",]*|"(?:[^\"]|\"\")*")'
_ALLOCATION = (
    "CurrentMemory",
    "DesiredMemory",
    "RuntimeMemory",
    "CurrentMinimumMemory",
    "CurrentMaximumMemory",
)


def parse_statistics(raw: str) -> dict[str, Any]:
    """Retain text observations without assigning units or inferring a reset."""
    result: dict[str, Any] = {
        "raw": raw,
        "entries": [],
        "missing": None,
        "usable": False,
        "error": None,
    }
    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 65536:
            result["raw"] = None
            raise ValueError("Statistics row exceeds the 64 KiB text bound")
        line = (
            raw.removesuffix("\r\n") if raw.endswith("\r\n") else raw.removesuffix("\n")
        )
        if not line or not all(character.isprintable() for character in line):
            raise ValueError("Statistics require one printable, nonempty CSV row")
        if not re.fullmatch(_CELL + r"(?:," + _CELL + r")*", line):
            raise ValueError("Statistics row has malformed CSV quoting")
        cells = next(csv.reader([line], strict=True))
        if len(cells) > 256:
            raise ValueError("Statistics row exceeds 256 fields")
        entries, seen = [], set()
        for cell in cells:
            key, separator, value = cell.partition("=")
            if (
                not separator
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key)
                or key in seen
                or len(value.encode("utf-8")) > 4096
            ):
                raise ValueError(
                    "Statistics row has an invalid, duplicate or oversized field"
                )
            seen.add(key)
            kind = "opaque"
            if key in _COUNTERS:
                kind = "numeric" if re.fullmatch(r"[0-9]+", value) else "unavailable"
            entries.append({"key": key, "value": value, "kind": kind})
        result.update(
            entries=entries,
            missing=[key for key in _COUNTERS if key not in seen],
            usable=any(entry["kind"] == "numeric" for entry in entries),
        )
    except UnicodeError:
        result["raw"] = None
        result["error"] = "Statistics row is not valid UTF-8 text"
    except (ValueError, csv.Error) as error:
        result["error"] = str(error)
    return result


def _text(value: object, label: str) -> str:
    if isinstance(value, dict) and set(value) - {"text", "@attrs"}:
        raise ValueError(f"Statistics reset has malformed {label}")
    text = leaf_text(value)
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"Statistics reset requires reported {label}")
    return text


def _boolean(value: object, label: str) -> bool:
    if isinstance(value, dict) and set(value) - {"text", "@attrs"}:
        raise ValueError(f"Statistics reset has malformed {label}")
    text = leaf_text(value)
    if text is True or text in ("true", "1"):
        return True
    if text is False or text in ("false", "0"):
        return False
    raise ValueError(f"Statistics reset requires reported boolean {label}")


async def _context(hmc: HMCClient, system_uuid: str, selector: str) -> dict[str, Any]:
    kind, uuid = await _contained_partition(hmc, system_uuid, selector)
    read = hmc.get_vios if kind == "VirtualIOServer" else hmc.get_logical_partition
    entry = await read(uuid)
    if entry is None:
        raise ValueError("Statistics partition detail is unavailable")
    if entry.get("ResourceType", kind) != kind:
        raise ValueError("Statistics partition kind differs from contained resource")
    observed = entry.get("UUID")
    if (
        not isinstance(observed, str)
        or not is_uuid(observed)
        or observed.lower() != uuid.lower()
    ):
        raise ValueError("Statistics partition identity differs from contained UUID")
    resource = resource_container(entry, "Resource")
    name = _text(resource.get("PartitionName"), "PartitionName")
    if not is_uuid(selector) and name != selector:
        raise ValueError("Statistics partition name changed after containment")
    number = _text(resource.get("PartitionID"), "PartitionID")
    if not re.fullmatch(r"[0-9]{1,4096}", number) or int(number) < 1:
        raise ValueError("Statistics reset requires a positive integer PartitionID")
    system = await hmc.get_managed_system(system_uuid)
    if system is None:
        raise ValueError("Statistics managed system detail is unavailable")
    observed_system = system.get("UUID")
    if (
        not isinstance(observed_system, str)
        or observed_system.lower() != system_uuid.lower()
    ):
        raise ValueError("Statistics managed system identity differs")
    system_resource = resource_container(system, "Resource")
    summaries = await hmc.list_system_summaries()
    selected = summary_matching(summaries, system_uuid)
    system_name = selected.get("SystemName") if selected else None
    named = (
        summary_matching(summaries, system_name)
        if isinstance(system_name, str)
        else None
    )
    if (
        not system_name
        or named is None
        or named["UUID"].lower() != system_uuid.lower()
        or _text(system_resource.get("SystemName"), "SystemName") != system_name
    ):
        raise ValueError("Statistics managed system has no matching unique CLI name")
    memory = resource_container(resource, "PartitionMemoryConfiguration")
    capabilities = resource_container(system_resource, "AssociatedSystemCapabilities")
    return {
        "system_uuid": system_uuid,
        "system_name": system_name,
        "uuid": uuid,
        "name": name,
        "native_id": int(number),
        "kind": kind,
        "state": _text(resource.get("PartitionState"), "PartitionState"),
        "shared": _boolean(memory.get("SharedMemoryEnabled"), "SharedMemoryEnabled"),
        "ams": _boolean(
            capabilities.get("ActiveMemorySharingCapable"), "ActiveMemorySharingCapable"
        ),
        "partition_type": leaf_text(resource.get("PartitionType")),
        "allocation": {key: leaf_text(memory.get(key)) for key in _ALLOCATION},
    }


def _identity(context: dict[str, Any]) -> tuple[object, ...]:
    return (
        context["system_uuid"].lower(),
        context["system_name"],
        context["uuid"].lower(),
        context["name"],
        context["native_id"],
        context["kind"],
        context["state"],
        context["shared"],
        context["ams"],
    )


def _admit(context: dict[str, Any], reset: LparMemoryStatisticsReset) -> None:
    if not context["shared"] or context["state"] != "running" or not context["ams"]:
        raise ValueError(
            "Statistics reset requires a Running shared-memory partition and system AMS capability"
        )
    if (reset.wait_minutes is not None or reset.detail_level is not None) and context[
        "partition_type"
    ] != "AIX/Linux":
        raise ValueError(
            "Statistics wait/detail settings require reported PartitionType AIX/Linux"
        )


async def _statistics(hmc: HMCClient, context: dict[str, Any]) -> dict[str, Any]:
    try:
        return parse_statistics(
            await read_lpar_memory_statistics(
                hmc.config, context["system_name"], context["native_id"]
            )
        )
    except (HMCError, OSError, UnicodeError) as error:
        return {
            "raw": None,
            "entries": [],
            "missing": None,
            "usable": False,
            "error": f"Statistics read failed ({type(error).__name__}); inspect the HMC",
        }


async def reset_lpar_memory_statistics(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    reset: LparMemoryStatisticsReset,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> dict[str, Any]:
    """Admit one reset, retain native facts and independently observe the same target."""
    if not isinstance(reset, LparMemoryStatisticsReset):
        raise TypeError("reset must be a typed LparMemoryStatisticsReset")
    if any(
        not isinstance(value, str) or not value.strip()
        for value in (lpar_name_or_uuid, system_name_or_uuid)
    ):
        raise ValueError(
            "Statistics reset requires nonblank system and partition selectors"
        )
    if type(ownership_override) is not bool:
        raise TypeError("ownership_override must be a boolean")
    if not math.isfinite(hmc.config.ssh_timeout) or hmc.config.ssh_timeout <= 0:
        raise ValueError("Statistics reset requires a finite positive ssh_timeout")
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    before = await _context(hmc, system_uuid, lpar_name_or_uuid)
    _admit(before, reset)
    await authorize_lpar_mutation(
        hmc,
        before["system_name"],
        before["name"],
        ownership_override=ownership_override,
        max_output_bytes=65536,
    )
    statistics = await _statistics(hmc, before)
    result = {
        "system_uuid": before["system_uuid"],
        "system_name": before["system_name"],
        "lpar_uuid": before["uuid"],
        "lpar_name": before["name"],
        "partition_id": before["native_id"],
        "partition_kind": before["kind"],
        "request": reset.model_dump(),
        "native_completion": "not_submitted",
        "native_output": None,
        "submission_error": None,
        "before_statistics": statistics,
        "after_statistics": None,
        "after_error": None,
        "identity_preserved": None,
        "workflow_completed": False,
        "reset_verification": "unavailable",
    }
    if not statistics["usable"]:
        return result
    fresh = await _context(hmc, system_uuid, before["uuid"])
    _admit(fresh, reset)
    if _identity(fresh) != _identity(before):
        raise ValueError(
            "Partition changed before statistics reset; nothing was submitted"
        )
    await authorize_lpar_mutation(
        hmc,
        fresh["system_name"],
        fresh["name"],
        ownership_override=ownership_override,
        max_output_bytes=65536,
    )
    try:
        result["native_output"] = await reset_lpar_memory_statistics_native(
            hmc.config, fresh["system_name"], fresh["native_id"], reset
        )
        result["native_completion"] = "completed"
    except (HMCError, OSError, UnicodeError) as error:
        result["native_completion"] = (
            "nonzero_exit"
            if isinstance(error, HMCCLIError) and error.remote_nonzero_completed
            else "unknown"
        )
        result["submission_error"] = (
            f"Native reset did not complete successfully ({type(error).__name__}); inspect the HMC before retrying"
        )
    try:
        after = await _context(hmc, system_uuid, before["uuid"])
        result["identity_preserved"] = _identity(after) == _identity(before)
        if not result["identity_preserved"]:
            result["after_error"] = (
                "Partition context changed after reset; no statistics query was submitted"
            )
            return result
        result["after_statistics"] = await _statistics(hmc, after)
        final = await _context(hmc, system_uuid, before["uuid"])
        result["identity_preserved"] = _identity(final) == _identity(before)
        result["workflow_completed"] = (
            result["native_completion"] == "completed"
            and result["after_statistics"]["usable"]
            and result["identity_preserved"]
        )
    except (HMCError, OSError, ValueError, UnicodeError) as error:
        result["identity_preserved"] = None
        result["after_error"] = (
            f"Independent after-context unavailable ({type(error).__name__}); inspect the HMC"
        )
    return result
