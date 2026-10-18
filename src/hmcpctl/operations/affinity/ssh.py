"""Presentation-neutral, verified SSH affinity workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.operations.lpar.ownership import resolve_and_authorize_lpar_names
from hmcpctl.ssh.affinity import (
    MemoptLparSelector,
    MemoptResourceGroupSelector,
    MinimumAffinityPolicy,
    MinimumAffinityPolicyQuery,
    query_minimum_affinity_policy,
    query_resource_group_memopt_scores,
    set_minimum_affinity_policy_cli,
    validate_memopt_scenario,
    validate_minimum_affinity_policy,
)
from hmcpctl.ssh.affinity import (
    get_lpar_memopt_score as _get_lpar_memopt_score,
)
from hmcpctl.ssh.affinity import (
    get_system_memopt_score as _get_system_memopt_score,
)
from hmcpctl.ssh.affinity import (
    list_lpar_memopt_scores as _list_lpar_memopt_scores,
)
from hmcpctl.ssh.affinity import (
    plan_lpar_memopt_scores as _plan_lpar_memopt_scores,
)
from hmcpctl.ssh.affinity import (
    plan_system_memopt_score as _plan_system_memopt_score,
)
from hmcpctl.ssh.selectors import resolve_ssh_names


def _config(hmc: HMCClient | HMCConfig) -> HMCConfig:
    """Return the SSH settings owned by the client facade."""
    return hmc if isinstance(hmc, HMCConfig) else hmc.config


@dataclass(frozen=True)
class ResourceGroupAffinityResult:
    """Stable envelope separating affinity scores from capability absence."""

    capability: Literal["available", "capability-unavailable"]
    mode: Literal["current", "calculated"]
    system: str
    selector: MemoptResourceGroupSelector
    items: list[dict[str, object]]
    unavailable_reason: str | None


@dataclass(frozen=True)
class MinimumAffinityPolicyResult:
    """Stable envelope separating policy values from capability absence."""

    capability: Literal["available", "capability-unavailable"]
    system: str
    lpar: str
    min_affinity_score: int | None
    min_affinity_score_action: Literal["none", "warn", "fail"] | None
    unavailable_reason: str | None


async def get_lpar_memopt_score(
    hmc: HMCClient, lpar_name_or_uuid: str, *, system_name_or_uuid: str
) -> dict[str, object]:
    """Return one LPAR's current memory-optimization score."""
    config = _config(hmc)
    system_name, lpar_name = await resolve_ssh_names(
        hmc, system_name_or_uuid, lpar_name_or_uuid
    )
    return await _get_lpar_memopt_score(config, system_name, lpar_name)


async def list_lpar_memopt_scores(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str | None = None,
) -> list[dict[str, object]]:
    """Return current memory-optimization scores for selected system LPARs."""
    config = _config(hmc)
    system_name, lpar_name = await resolve_ssh_names(
        hmc, system_name_or_uuid, lpar_name_or_uuid
    )
    return await _list_lpar_memopt_scores(config, system_name, lpar_name)


async def get_system_memopt_score(
    hmc: HMCClient, system_name_or_uuid: str
) -> dict[str, object]:
    """Return a managed system's current memory-optimization score."""
    config = _config(hmc)
    system_name, _ = await resolve_ssh_names(hmc, system_name_or_uuid, None)
    return await _get_system_memopt_score(config, system_name)


async def plan_lpar_memopt_scores(
    hmc: HMCClient,
    system_name_or_uuid: str,
    prioritized: MemoptLparSelector | None = None,
    excluded: MemoptLparSelector | None = None,
) -> list[dict[str, object]]:
    """Return predicted LPAR scores for a read-only affinity scenario."""
    config = _config(hmc)
    validate_memopt_scenario(prioritized, excluded)
    system_name, _ = await resolve_ssh_names(hmc, system_name_or_uuid, None)
    return await _plan_lpar_memopt_scores(config, system_name, prioritized, excluded)


async def plan_system_memopt_score(
    hmc: HMCClient,
    system_name_or_uuid: str,
    prioritized: MemoptLparSelector | None = None,
    excluded: MemoptLparSelector | None = None,
) -> dict[str, object]:
    """Return a predicted system score for a read-only affinity scenario."""
    config = _config(hmc)
    validate_memopt_scenario(prioritized, excluded)
    system_name, _ = await resolve_ssh_names(hmc, system_name_or_uuid, None)
    return await _plan_system_memopt_score(config, system_name, prioritized, excluded)


async def _resource_group_memopt_scores(
    hmc: HMCClient,
    system_name_or_uuid: str,
    selector: MemoptResourceGroupSelector | None,
    *,
    calculated: bool,
) -> ResourceGroupAffinityResult:
    config = _config(hmc)
    selected = selector or MemoptResourceGroupSelector(all=True)
    system_name, _ = await resolve_ssh_names(hmc, system_name_or_uuid, None)
    resolved = system_name
    query = await query_resource_group_memopt_scores(
        config, resolved, selected, calculated=calculated
    )
    return ResourceGroupAffinityResult(
        capability=(
            "capability-unavailable" if query.unavailable_reason else "available"
        ),
        mode="calculated" if calculated else "current",
        system=resolved,
        selector=selected,
        items=query.items,
        unavailable_reason=query.unavailable_reason,
    )


async def list_resource_group_memopt_scores(
    hmc: HMCClient,
    system_name_or_uuid: str,
    selector: MemoptResourceGroupSelector | None = None,
) -> ResourceGroupAffinityResult:
    """Return current resource-group affinity scores when supported."""
    return await _resource_group_memopt_scores(
        hmc, system_name_or_uuid, selector, calculated=False
    )


async def plan_resource_group_memopt_scores(
    hmc: HMCClient,
    system_name_or_uuid: str,
    selector: MemoptResourceGroupSelector | None = None,
) -> ResourceGroupAffinityResult:
    """Return potential resource-group affinity scores without running DPO."""
    return await _resource_group_memopt_scores(
        hmc, system_name_or_uuid, selector, calculated=True
    )


async def get_minimum_affinity_policy(
    hmc: HMCClient, lpar_name_or_uuid: str, *, system_name_or_uuid: str
) -> MinimumAffinityPolicyResult:
    """Return an LPAR's minimum-affinity policy when supported."""
    config = _config(hmc)
    system_name, lpar_name = await resolve_ssh_names(
        hmc, system_name_or_uuid, lpar_name_or_uuid
    )
    resolved_system = system_name
    resolved_lpar = lpar_name
    query: MinimumAffinityPolicyQuery = await query_minimum_affinity_policy(
        config, resolved_system, resolved_lpar
    )
    return MinimumAffinityPolicyResult(
        capability=(
            "capability-unavailable" if query.unavailable_reason else "available"
        ),
        system=resolved_system,
        lpar=resolved_lpar,
        min_affinity_score=query.min_affinity_score,
        min_affinity_score_action=query.min_affinity_score_action,
        unavailable_reason=query.unavailable_reason,
    )


async def set_minimum_affinity_policy(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    policy: MinimumAffinityPolicy,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> str:
    """Authorize and apply an LPAR minimum-affinity policy."""
    validate_minimum_affinity_policy(policy)
    names = await resolve_and_authorize_lpar_names(
        hmc,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
        system_name_or_uuid=system_name_or_uuid,
    )
    return await set_minimum_affinity_policy_cli(hmc.config, *names, policy)
