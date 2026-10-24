"""ST53: one owned inactive dedicated fixture; finite Linux-role transitions."""

from __future__ import annotations

import asyncio
import uuid
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar import LparResources, LparSettings
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.core import LparCreation
from hmcpctl.operations.lpar.linux_roles import (
    ROLE_FIELDS,
    admit_role_creation,
    read_role_capabilities,
)
from hmcpctl.resource_identity import resolve_system_uuid
from hmcpctl.xmlutil import leaf_text

from .lpar_config import (
    NAME_PREFIX,
    TOKEN_PREFIX,
    _compare,
    _read_pools,
    _Run,
    _teardown,
    _token_of,
    _uuid_of,
    scratch_partitions,
)
from .observation import Assertion, plain_data

if TYPE_CHECKING:
    from fastmcp import Client

    from .runner import RunState

SUBTASK = 53
GROUP = "lpar-linux-roles"
SCENARIO = "st53-lpar-linux-roles"
RESOURCES: dict[str, Any] = {
    "min_memory": 1024,
    "desired_memory": 2048,
    "max_memory": 4096,
    "dedicated": True,
    "min_procs": 1,
    "desired_procs": 1,
    "max_procs": 1,
    "sharing_mode": "keep_idle_procs",
}
_PRESERVED = (
    "PartitionMemoryConfiguration",
    "PartitionProcessorConfiguration",
    "PendingProcessorCompatibilityMode",
    "KeyStoreSize",
    "AssociatedPersistentMemoryConfiguration",
    "AllowPerformanceDataCollection",
    "AvailabilityPriority",
    "IsTimeReferencePartition",
    "SimplifiedRemoteRestartCapable",
    "MigrationDisabled",
)


async def _read(client: Client, state: RunState, run: _Run) -> dict[str, Any]:
    status, data = await state.call(
        client,
        "hmc_get_lpar",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=run.uuid,
    )
    data = plain_data(data)
    resource = data.get("Resource") if isinstance(data, dict) else None
    if (
        status != "PASS"
        or not isinstance(resource, dict)
        or _uuid_of(data) != run.uuid
        or leaf_text(resource.get("PartitionName")) != run.name
        or leaf_text(resource.get("PartitionType")) != "AIX/Linux"
        or leaf_text(resource.get("PartitionState")) != "not activated"
        or leaf_text(resource.get("IsOperationInProgress")) != "false"
        or any(
            leaf_text(resource.get(rest)) not in ("true", "false")
            for rest in ROLE_FIELDS.values()
        )
    ):
        raise ValueError("Independent inactive fixture identity or roles unconfirmed")
    return deepcopy(resource)


def _roles(resource: dict[str, Any]) -> dict[str, bool]:
    return {
        name: leaf_text(resource.get(rest)) == "true"
        for name, rest in ROLE_FIELDS.items()
    }


def _fixture_resources(resource: dict[str, Any]) -> bool:
    memory = resource.get("PartitionMemoryConfiguration")
    processors = resource.get("PartitionProcessorConfiguration")
    if not isinstance(memory, dict) or not isinstance(processors, dict):
        return False
    dedicated = processors.get("DedicatedProcessorConfiguration")
    return (
        leaf_text(processors.get("HasDedicatedProcessors")) == "true"
        and leaf_text(processors.get("SharingMode")) == "keep idle procs"
        and isinstance(dedicated, dict)
        and all(
            leaf_text(dedicated.get(key)) == "1"
            for key in ("MinimumProcessors", "DesiredProcessors", "MaximumProcessors")
        )
        and all(
            leaf_text(memory.get(key)) == str(RESOURCES[name])
            for key, name in (
                ("MinimumMemory", "min_memory"),
                ("DesiredMemory", "desired_memory"),
                ("MaximumMemory", "max_memory"),
            )
        )
    )


async def _create(client: Client, state: RunState, run: _Run, controller: bool) -> dict:
    run.create_attempted = True
    status, data = await state.call(
        client,
        "hmc_create_lpar",
        system_name_or_uuid=run.system,
        name=run.name,
        partition_type="AIX/Linux",
        resources=RESOURCES,
        caller_token=run.token,
        settings={"powervm_mgmt_capable": False, "kvm_capable": True},
        apply_partition_profile=not controller,
    )
    data = plain_data(data)
    if status != "PASS" or not isinstance(data, dict):
        raise ValueError("Linux-role creation did not return structured success")
    run.uuid = _uuid_of(data.get("lpar"))
    run.created = run.uuid is not None
    steps = data.get("steps")
    if not (
        run.created
        and data.get("ownership_stamped") is True
        and data.get("workflow_completed") is True
        and isinstance(steps, list)
        and any(
            step.get("step") == "settings" and step.get("status") == "ok"
            for step in steps
            if isinstance(step, dict)
        )
    ):
        raise ValueError("Creation identity, ownership or Linux roles unconfirmed")
    if await _token_of(client, state, run, run.uuid or "") != run.token:
        raise ValueError("Fixture ownership unconfirmed after creation")
    resource = await _read(client, state, run)
    if not _fixture_resources(resource):
        raise ValueError(
            "Independent explicit dedicated never-share resources unconfirmed"
        )
    if _roles(resource) != {"powervm_mgmt_capable": False, "kvm_capable": True}:
        raise ValueError("Independent creation role readback differs")
    if leaf_text(resource.get("PendingProcessorCompatibilityMode")) != "POWER10":
        raise ValueError("Independent pending POWER10 companion unconfirmed")
    if not controller:
        status, compat = await state.call(
            client,
            "hmc_get_lpar_proc_compat",
            system_name_or_uuid=run.system,
            lpar_name_or_uuid=run.uuid,
        )
        if (
            status != "PASS"
            or not isinstance(compat, dict)
            or compat.get("profile_mode") != "POWER10"
        ):
            raise ValueError(
                "Independent classic profile POWER10 companion unconfirmed"
            )
    return resource


async def _update(
    client: Client, state: RunState, run: _Run, before: dict, settings: dict[str, bool]
) -> dict:
    if await _token_of(client, state, run, run.uuid or "") != run.token:
        raise ValueError("Fixture ownership changed; stopped before role update")
    status, result = await state.call(
        client,
        "hmc_configure_lpar",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=run.uuid,
        settings=settings,
    )
    result = plain_data(result)
    expected = _roles(before) | settings
    if (
        status != "PASS"
        or not isinstance(result, dict)
        or result.get("status") != "verified"
        or not isinstance(result.get("requested"), dict)
        or any(
            result["requested"].get(name) is not settings.get(name)
            for name in ROLE_FIELDS
        )
        or not isinstance(result.get("observed"), dict)
        or any(
            result["observed"].get(name) is not value
            for name, value in expected.items()
        )
    ):
        raise ValueError("Role update or known-preserved roles unverified")
    after = await _read(client, state, run)
    if _roles(after) != expected or any(
        after.get(key) != before.get(key) for key in _PRESERVED
    ):
        raise ValueError("Independent role or preserved-resource comparison failed")
    return after


async def exercise_lpar_linux_roles(client: Client, state: RunState) -> None:
    """Require eligibility before one fixture; clean and compare even on cancellation."""
    if state.group != GROUP or state.hmc_config is None:
        state.skip(SUBTASK, GROUP, "requires opt-in group and HMC configuration")
        return
    try:
        async with HMCClient(state.hmc_config) as hmc:
            system = await resolve_system_uuid(hmc, state.config.system_name)
            caps = await read_role_capabilities(hmc, system, need_kvm=True)
            admit_role_creation(
                LparCreation(
                    "eligibility-only",
                    partition_type="AIX/Linux",
                    resources=LparResources(**RESOURCES),
                    settings=LparSettings(powervm_mgmt_capable=False, kvm_capable=True),
                    apply_profile=not caps.controller_mode,
                ),
                caps,
            )
    except (HMCError, ValueError, TypeError) as error:
        state.skip(
            SUBTASK,
            GROUP,
            f"selected-system eligibility unavailable: {type(error).__name__}",
        )
        return
    baseline = await _read_pools(client, state, state.config.system_name)
    if baseline is None or scratch_partitions(baseline[0].names):
        state.skip(
            SUBTASK, GROUP, "baseline unavailable or stranded scratch; run recovery"
        )
        return
    suffix = uuid.uuid4().hex[:8]
    run = _Run(
        state.config.system_name,
        NAME_PREFIX + suffix,
        TOKEN_PREFIX + suffix,
        baseline[1],
        subtask=SUBTASK,
    )
    verified = False
    try:
        before = await _create(client, state, run, caps.controller_mode)
        for settings in (
            {"kvm_capable": False},
            {"powervm_mgmt_capable": True},
            {"powervm_mgmt_capable": False},
        ):
            before = await _update(client, state, run, before, settings)
        verified = True
    except asyncio.CancelledError:
        state.record(SUBTASK, GROUP, "FAIL", "Cancelled; mandatory recovery required")
        raise
    except (HMCError, ValueError, TypeError, PermissionError) as error:
        state.record(SUBTASK, GROUP, "FAIL", str(error))
    finally:
        same = False
        try:
            await _teardown(client, state, run, inactive_only=True)
        finally:
            same = await _compare(client, state, run, baseline[0])
            cleanup = "passed" if run.deleted and same else "failed"
            assertions = [Assertion("independent-finite-role-lifecycle", verified)]
            data = {
                "classic-profile-apply": not caps.controller_mode,
                "activation": "not-run",
                "recovery": "required",
            }
            state.record_verified(
                SUBTASK,
                "hmc_create_lpar",
                operation="lpar.create",
                scenario=SCENARIO,
                assertions=assertions,
                cleanup=cleanup,
                data=data,
            )
            state.record_verified(
                SUBTASK,
                "hmc_configure_lpar",
                operation="lpar.configure",
                scenario=SCENARIO,
                assertions=assertions,
                cleanup=cleanup,
                data=data,
            )
            state.record_verified(
                SUBTASK,
                "hmc_get_lpar",
                operation="lpar.get",
                scenario=SCENARIO,
                assertions=assertions,
                cleanup=cleanup,
                data=data,
            )
