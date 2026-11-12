"""Affinity and memory-optimization commands over the SSH transport."""

from __future__ import annotations

import re
import shlex
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any, Literal

from ..config import HMCConfig
from ..documents.native_records import build_attribute_record, build_filter
from .commands import _parse_lshwres_output, hmc_error_code, parse_hmc_result_rows
from .profiles import get_proc_compat_modes
from .transport import HMCCLIError, run_hmc_command

_MEMOPT_SELECTOR_SAFETY_CEILING_BYTES = 4096
_RESOURCE_GROUP_MEMOPT_MINIMUM_HMC = (11, 1, 1110)
_RESOURCE_GROUP_CURRENT_FIELDS = (
    "resource_group_name",
    "resource_group_id",
    "curr_score",
)
_RESOURCE_GROUP_CALCULATED_FIELDS = (
    *_RESOURCE_GROUP_CURRENT_FIELDS,
    "predicted_score",
    "requested_lpar_names",
    "requested_lpar_ids",
    "protected_lpar_names",
    "protected_lpar_ids",
)


def _validate_selector_names(names: tuple[str, ...], label: str) -> None:
    invalid = any(
        not isinstance(name, str)
        or not name.strip()
        or "," in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
        for name in names
    )
    if invalid:
        raise ValueError(
            f"{label} must be nonblank and contain no commas or control characters"
        )
    if len(set(names)) != len(names):
        raise ValueError(f"{label} must not contain duplicates")


def _validate_selector_ids(ids: tuple[int, ...], label: str, minimum: int) -> None:
    invalid = any(
        not isinstance(identifier, int)
        or isinstance(identifier, bool)
        or identifier < minimum
        for identifier in ids
    )
    if invalid:
        raise ValueError(
            f"{label} must be {('positive' if minimum else 'non-negative')} integers"
        )
    if len(set(ids)) != len(ids):
        raise ValueError(f"{label} must not contain duplicates")


@dataclass(frozen=True)
class MemoptLparSelector:
    """Select LPARs by name or ID for an affinity-planning scenario."""

    names: tuple[str, ...] = field(
        default=(), metadata={"description": "LPAR names in the planning scenario."}
    )
    ids: tuple[int, ...] = field(
        default=(), metadata={"description": "LPAR IDs in the planning scenario."}
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "names", tuple(self.names))
        object.__setattr__(self, "ids", tuple(self.ids))
        if not self.names and not self.ids:
            raise ValueError("memopt LPAR selector must not be empty")
        if self.names and self.ids:
            raise ValueError("memopt LPAR selector must contain names or ids, not both")
        if self.names:
            _validate_selector_names(self.names, "memopt LPAR selector names")
        if self.ids:
            _validate_selector_ids(self.ids, "memopt LPAR selector ids", 1)


@dataclass(frozen=True)
class MemoptResourceGroupSelector:
    """Select resource groups by name, ID, or the explicit all-groups mode."""

    names: tuple[str, ...] = field(
        default=(), metadata={"description": "Resource-group names."}
    )
    ids: tuple[int, ...] = field(
        default=(), metadata={"description": "Resource-group IDs."}
    )
    all: bool = field(
        default=False, metadata={"description": "Select all resource groups."}
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "names", tuple(self.names))
        object.__setattr__(self, "ids", tuple(self.ids))
        modes = sum((bool(self.names), bool(self.ids), self.all))
        if modes != 1:
            raise ValueError("resource-group selector must contain names, ids, or all")
        if self.names:
            _validate_selector_names(self.names, "resource-group names")
        if self.ids:
            _validate_selector_ids(self.ids, "resource-group ids", 0)
        if (
            len(_resource_group_selector_option(self).encode("utf-8"))
            > _MEMOPT_SELECTOR_SAFETY_CEILING_BYTES
        ):
            raise ValueError(
                "resource-group selector option exceeds "
                f"{_MEMOPT_SELECTOR_SAFETY_CEILING_BYTES} UTF-8 bytes"
            )


@dataclass(frozen=True)
class ResourceGroupMemoptQuery:
    """Raw SSH query result before presentation-neutral system resolution."""

    items: list[dict[str, object]]
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class MinimumAffinityPolicyQuery:
    """Raw minimum-affinity policy result from the SSH command boundary."""

    min_affinity_score: int | None
    min_affinity_score_action: Literal["none", "warn", "fail"] | None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class MinimumAffinityPolicy:
    """Validated values for the POWER11 minimum-affinity policy."""

    min_affinity_score: int = field(
        metadata={"description": "Required minimum affinity score from 0 through 100."}
    )
    min_affinity_score_action: Literal["none", "warn", "fail"] = field(
        metadata={
            "description": "Action when the minimum is missed: none, warn, or fail."
        }
    )


def validate_minimum_affinity_policy(
    policy: MinimumAffinityPolicy,
) -> MinimumAffinityPolicy:
    """Validate policy values before any HMC interaction."""
    score = policy.min_affinity_score
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise ValueError("min_affinity_score must be an integer from 0 through 100")
    if policy.min_affinity_score_action not in {"none", "warn", "fail"}:
        raise ValueError("min_affinity_score_action must be none, warn, or fail")
    return policy


def _resource_group_selector_option(selector: MemoptResourceGroupSelector) -> str:
    if selector.all:
        return "--gid all"
    if selector.names:
        return f"-g {shlex.quote(','.join(selector.names))}"
    return f"--gid {shlex.quote(','.join(str(group_id) for group_id in selector.ids))}"


def _parse_hmc_version(output: str) -> tuple[int, int, int] | None:
    compact = re.search(r"V(\d+)R(\d+)M(\d+)", output, re.IGNORECASE)
    if compact:
        return (int(compact.group(1)), int(compact.group(2)), int(compact.group(3)))
    labelled = re.search(
        r"Version:\s*(\d+).*?Release:\s*(\d+).*?Service Pack:\s*(\d+)",
        output,
        re.IGNORECASE | re.DOTALL,
    )
    if labelled is None:
        return None
    return (int(labelled.group(1)), int(labelled.group(2)), int(labelled.group(3)))


async def query_resource_group_memopt_scores(
    config: HMCConfig,
    system_name: str,
    selector: MemoptResourceGroupSelector,
    *,
    calculated: bool,
) -> ResourceGroupMemoptQuery:
    """Query resource-group scores or return an evidence-backed capability result."""
    version = _parse_hmc_version(await run_hmc_command(config, "lshmc -V"))
    if version is None or version < _RESOURCE_GROUP_MEMOPT_MINIMUM_HMC:
        return ResourceGroupMemoptQuery(
            [],
            "Resource-group affinity requires HMC V11R1M1110 or later; "
            "upgrade the HMC or verify its version output before retrying.",
        )
    fields = (
        _RESOURCE_GROUP_CALCULATED_FIELDS
        if calculated
        else _RESOURCE_GROUP_CURRENT_FIELDS
    )
    mode = "calcscore" if calculated else "currscore"
    command = (
        f"lsmemopt -m {shlex.quote(system_name)} -r resgroup -o {mode} "
        f"{_resource_group_selector_option(selector)} -F {','.join(fields)} --header"
    )
    try:
        output = await run_hmc_command(config, command)
    except HMCCLIError as error:
        if hmc_error_code(error) != "HSCLCA00":
            raise
        return ResourceGroupMemoptQuery(
            [],
            "The managed system does not support multiple resource groups; "
            "use POWER11 resource-group affinity on a supported system.",
        )
    rows = parse_hmc_result_rows(output, fields, "lsmemopt resource-group")
    required = {*_RESOURCE_GROUP_CURRENT_FIELDS}
    if calculated:
        required.add("predicted_score")
    for index, row in enumerate(rows, start=1):
        empty = sorted(field for field in required if not row[field])
        if empty:
            raise HMCCLIError(
                f"lsmemopt resource-group row {index} has empty required fields: "
                f"{', '.join(empty)}"
            )
    items: list[dict[str, object]] = [dict(row) for row in rows]
    if calculated:
        for item in items:
            item["prediction_guaranteed"] = False
    return ResourceGroupMemoptQuery(items)


async def query_minimum_affinity_policy(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
) -> MinimumAffinityPolicyQuery:
    """Read a validated minimum-affinity policy when POWER11 mode is available."""
    modes = await get_proc_compat_modes(config, system_name)
    if "POWER11" not in modes:
        return MinimumAffinityPolicyQuery(
            None,
            None,
            "Minimum-affinity policy requires system firmware that advertises "
            "POWER11 processor compatibility; update the system firmware and retry.",
        )
    fields = ("min_affinity_score", "min_affinity_score_action")
    command = (
        f"lssyscfg -r lpar -m {shlex.quote(system_name)} "
        f"--filter {shlex.quote(build_filter([('lpar_names', lpar_name)]))} "
        f"-F {','.join(fields)} --header"
    )
    rows = parse_hmc_result_rows(
        await run_hmc_command(config, command),
        fields,
        "lssyscfg minimum-affinity policy",
    )
    try:
        if len(rows) != 1:
            raise ValueError(f"expected exactly one policy row; received {len(rows)}")
        row = rows[0]
        score_text = row["min_affinity_score"]
        if not score_text.isdecimal():
            raise ValueError("min_affinity_score must be an integer from 0 through 100")
        score = int(score_text)
        if score > 100:
            raise ValueError("min_affinity_score must be an integer from 0 through 100")
        action = row["min_affinity_score_action"]
        if action not in {"none", "warn", "fail"}:
            raise ValueError("min_affinity_score_action must be none, warn, or fail")
    except ValueError as error:
        raise HMCCLIError(
            f"malformed lssyscfg minimum-affinity policy output: {error}"
        ) from error
    return MinimumAffinityPolicyQuery(
        score,
        action,
    )


async def require_minimum_affinity_policy_capability(
    config: HMCConfig,
    system_name: str,
) -> None:
    """Fail unless the system advertises the documented policy capability."""
    modes = await get_proc_compat_modes(config, system_name)
    if "POWER11" not in modes:
        raise HMCCLIError(
            "Minimum-affinity policy mutation requires system firmware that "
            "advertises POWER11 processor compatibility; update the system "
            "firmware and retry."
        )


async def set_minimum_affinity_policy_cli(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
    policy: MinimumAffinityPolicy,
) -> str:
    """Set both documented POWER11 minimum-affinity policy attributes."""
    validated = validate_minimum_affinity_policy(policy)
    await require_minimum_affinity_policy_capability(config, system_name)
    record = build_attribute_record(
        [
            ("name", lpar_name),
            ("min_affinity_score", validated.min_affinity_score),
            ("min_affinity_score_action", validated.min_affinity_score_action),
        ]
    )
    command = f"chsyscfg -r lpar -m {shlex.quote(system_name)} -i {shlex.quote(record)}"
    return await run_hmc_command(config, command)


def validate_memopt_scenario(
    prioritized: MemoptLparSelector | None,
    excluded: MemoptLparSelector | None,
) -> None:
    """Validate relationships between affinity-planning selectors."""
    if (
        prioritized is not None
        and excluded is not None
        and bool(prioritized.names) != bool(excluded.names)
    ):
        raise ValueError(
            "prioritized and excluded selectors must use the same representation"
        )
    prioritized_values = (prioritized.names or prioritized.ids) if prioritized else ()
    excluded_values = (excluded.names or excluded.ids) if excluded else ()
    if (
        prioritized is not None
        and excluded is not None
        and (set(prioritized_values) & set(excluded_values))
    ):
        raise ValueError("prioritized and excluded selectors must not overlap")
    option_package = _render_memopt_selector_options(prioritized, excluded)
    if len(option_package.encode("utf-8")) > _MEMOPT_SELECTOR_SAFETY_CEILING_BYTES:
        raise ValueError(
            "memopt LPAR selector option package exceeds "
            f"{_MEMOPT_SELECTOR_SAFETY_CEILING_BYTES} UTF-8 bytes"
        )


async def list_lpar_memopt_scores(
    config: HMCConfig,
    system_name: str,
    lpar_name: str | None = None,
) -> list[dict[str, Any]]:
    """List current memory-optimization scores for a system's LPARs via SSH."""
    command = f"lsmemopt -m {shlex.quote(system_name)} -r lpar -o currscore"
    if lpar_name is not None:
        if not lpar_name.strip():
            raise ValueError("lpar_name must not be empty")
        lpar_filter = build_filter([("lpar_names", lpar_name)])
        command += f" --filter {shlex.quote(lpar_filter)}"
    rows = _parse_lshwres_output(await run_hmc_command(config, command))
    required = {"lpar_name", "lpar_id", "curr_lpar_score"}
    for index, row in enumerate(rows, start=1):
        missing = sorted(required - row.keys())
        if missing:
            raise HMCCLIError(
                f"lsmemopt row {index} is missing required fields: {', '.join(missing)}"
            )
    if lpar_name is not None:
        if len(rows) > 1:
            raise HMCCLIError(
                f"lsmemopt filtered query for LPAR {lpar_name!r} returned "
                f"{len(rows)} rows; expected at most 1"
            )
        if rows and rows[0]["lpar_name"] != lpar_name:
            raise HMCCLIError(
                f"lsmemopt reported LPAR {rows[0]['lpar_name']!r}; "
                f"expected {lpar_name!r}"
            )
    return rows


async def get_lpar_memopt_score(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
) -> dict[str, Any]:
    """Return one LPAR's current memory-optimization score via SSH.

    Raises:
        ValueError: If *lpar_name* is empty.
        HMCCLIError: If the HMC reports no score row for the partition.
    """
    if not lpar_name.strip():
        raise ValueError("lpar_name must not be empty")
    rows = await list_lpar_memopt_scores(config, system_name, lpar_name)
    if not rows:
        raise HMCCLIError(
            f"lsmemopt query for LPAR {lpar_name!r} on system {system_name!r} "
            "returned 0 rows; expected exactly 1"
        )
    return rows[0]


def _render_memopt_selector_options(
    prioritized: MemoptLparSelector | None,
    excluded: MemoptLparSelector | None,
) -> str:
    """Render a planning scenario's fixed selector-option package."""
    options: list[str] = []
    for selector, name_flag, id_flag in (
        (prioritized, "-p", "--id"),
        (excluded, "-x", "--xid"),
    ):
        if selector is None:
            continue
        flag = name_flag if selector.names else id_flag
        values = selector.names or selector.ids
        rendered = shlex.quote(",".join(str(value) for value in values))
        options.append(f" {flag} {rendered}")
    return "".join(options)


def _memopt_selector_options(
    prioritized: MemoptLparSelector | None,
    excluded: MemoptLparSelector | None,
) -> str:
    """Validate a planning scenario and render its fixed selector options."""
    validate_memopt_scenario(prioritized, excluded)
    return _render_memopt_selector_options(prioritized, excluded)


def _validated_memopt_rows(
    output: str,
    required: Collection[str],
) -> list[dict[str, object]]:
    """Parse affinity-score rows and require every scope-specific field."""
    rows: list[dict[str, object]] = _parse_lshwres_output(output)
    for index, row in enumerate(rows, start=1):
        missing = sorted(set(required) - row.keys())
        if missing:
            raise HMCCLIError(
                f"lsmemopt row {index} is missing required fields: {', '.join(missing)}"
            )
        empty = sorted(field for field in required if row[field] == "")
        if empty:
            raise HMCCLIError(
                f"lsmemopt row {index} has empty required fields: {', '.join(empty)}"
            )
    return rows


async def get_system_memopt_score(
    config: HMCConfig, system_name: str
) -> dict[str, object]:
    """Return the current system memory-affinity score via SSH."""
    command = f"lsmemopt -m {shlex.quote(system_name)} -r sys -o currscore"
    output = await run_hmc_command(config, command)
    rows = _parse_lshwres_output(output)
    if len(rows) != 1:
        raise HMCCLIError(
            f"lsmemopt system query returned {len(rows)} rows; expected exactly 1"
        )
    return _validated_memopt_rows(output, {"curr_sys_score"})[0]


async def plan_lpar_memopt_scores(
    config: HMCConfig,
    system_name: str,
    prioritized: MemoptLparSelector | None = None,
    excluded: MemoptLparSelector | None = None,
) -> list[dict[str, object]]:
    """Return predicted LPAR memory-affinity scores via SSH."""
    command = f"lsmemopt -m {shlex.quote(system_name)} -r lpar -o calcscore"
    command += _memopt_selector_options(prioritized, excluded)
    rows = _validated_memopt_rows(
        await run_hmc_command(config, command),
        {"lpar_name", "lpar_id", "curr_lpar_score", "predicted_lpar_score"},
    )
    for row in rows:
        row["prediction_guaranteed"] = False
    return rows


async def plan_system_memopt_score(
    config: HMCConfig,
    system_name: str,
    prioritized: MemoptLparSelector | None = None,
    excluded: MemoptLparSelector | None = None,
) -> dict[str, object]:
    """Return a predicted system memory-affinity score via SSH."""
    command = f"lsmemopt -m {shlex.quote(system_name)} -r sys -o calcscore"
    command += _memopt_selector_options(prioritized, excluded)
    output = await run_hmc_command(config, command)
    rows = _parse_lshwres_output(output)
    if len(rows) != 1:
        raise HMCCLIError(
            f"lsmemopt system query returned {len(rows)} rows; expected exactly 1"
        )
    result = _validated_memopt_rows(output, {"curr_sys_score", "predicted_sys_score"})[
        0
    ]
    result["prediction_guaranteed"] = False
    return result
