"""Owned inactive processor-quantity hypothesis with independent CPU/memory reads."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import asdict, replace
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from fastmcp import Client

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar import LparResources
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.core import LparCreation, create_and_stamp_lpar
from hmcpctl.operations.lpar.ownership import resolve_and_authorize_lpar_names
from hmcpctl.resource_identity import resolve_system_uuid
from hmcpctl.ssh.lpar import apply_lpar_profile_via_cli
from hmcpctl.ssh.processors import read_lpar_processor_quantities
from hmcpctl.xmlutil import leaf_text

from . import lpar_config as fixture
from .observation import Assertion, CallFailure
from .results import field, resource

if TYPE_CHECKING:
    from .runner import RunState

SUBTASK = 56
GROUP = "lpar-processor-quantities"
SCENARIO = "st56-lpar-processor-quantities"
_CPU = (
    "CurrentMinimumVirtualProcessors",
    "AllocatedVirtualProcessors",
    "CurrentMaximumVirtualProcessors",
    "CurrentMinimumProcessingUnits",
    "CurrentProcessingUnits",
    "CurrentMaximumProcessingUnits",
)
_NATIVE = (
    "curr_min_procs",
    "curr_procs",
    "curr_max_procs",
    "curr_min_proc_units",
    "curr_proc_units",
    "curr_max_proc_units",
)
_MEMORY = ("CurrentMinimumMemory", "CurrentMemory", "CurrentMaximumMemory")
_INITIAL = ("1", "2", "4", "0.10", "0.50", "1.00", "1024", "2048", "4096")


def _number(value: object, *, units: bool = False) -> str:
    # Same encoded numeric budget as processor results; validation stays independent.
    budget = 65536 // 62
    token = str(leaf_text(value))
    if len(token) > budget or len(token.encode("utf-8")) > budget:
        raise ValueError("Independent processor oracle exceeds numeric token budget")
    try:
        number = Decimal(token)
    except InvalidOperation as error:
        raise ValueError(
            "Independent processor oracle has malformed quantity"
        ) from error
    if not number.is_finite() or number < 0:
        raise ValueError("Independent processor oracle has invalid quantity precision")
    sign, digits, exponent = number.as_tuple()
    fractional = -(int(exponent) + (2 if units else 0))
    if fractional > 0 and any(digits[-fractional:]):
        raise ValueError("Independent processor oracle has invalid quantity precision")
    integer_digits = 1 if number.is_zero() else max(1, number.adjusted() + 1)
    if sign + integer_digits + (3 if units else 0) + 2 > budget:
        raise ValueError(
            "Independent processor oracle exceeds numeric rendering budget"
        )
    return format(number, ".2f" if units else ".0f")


async def _read_current(
    client: Client,
    state: RunState,
    run: fixture._Run,
    *,
    allow_uninitialized: bool = False,
) -> tuple[str, ...]:
    status, data = await fixture._read_lpar(client, state, run)
    if status != "PASS" or not isinstance(data, Mapping):
        raise ValueError(
            "Independent processor oracle could not read the owned fixture"
        )
    value = resource(data)
    if (
        str(data.get("UUID", "")).casefold() != str(run.uuid).casefold()
        or value.get("PartitionName") != run.name
        or value.get("PartitionState") != "not activated"
        or value.get("PartitionType") != "AIX/Linux"
    ):
        raise ValueError(
            "Independent processor oracle found a different or active fixture"
        )
    config = value.get("PartitionProcessorConfiguration", {})
    if not isinstance(config, Mapping) or leaf_text(
        config.get("CurrentHasDedicatedProcessors")
    ) not in (False, "false"):
        raise ValueError("Independent processor oracle requires shared processors")
    cpu, memory = (
        config.get("CurrentSharedProcessorConfiguration"),
        value.get("PartitionMemoryConfiguration"),
    )
    if (
        not isinstance(cpu, Mapping)
        or not isinstance(memory, Mapping)
        or state.hmc_config is None
    ):
        raise ValueError("Independent processor oracle lacks CPU/memory evidence")
    partition_id = int(_number(value.get("PartitionID")))
    row = await read_lpar_processor_quantities(
        state.hmc_config, run.system, partition_id
    )
    if row.get("lpar_name") != run.name or row.get("curr_proc_mode") != "shared":
        raise ValueError("Independent native processor identity or mode differs")
    values = tuple(_number(cpu.get(key), units=i >= 3) for i, key in enumerate(_CPU))
    native = tuple(_number(row.get(key), units=i >= 3) for i, key in enumerate(_NATIVE))
    if values != native:
        raise ValueError("Independent native and REST processor quantities disagree")
    values += tuple(_number(memory.get(key)) for key in _MEMORY)
    if allow_uninitialized and all(Decimal(value) == 0 for value in values):
        return values
    if any(
        not 0
        < Decimal(values[start])
        <= Decimal(values[start + 1])
        <= Decimal(values[start + 2])
        for start in (0, 3, 6)
    ):
        raise ValueError(
            "Independent processor oracle requires initialized CPU and memory bounds"
        )
    return values


async def _initialize_current(
    client: Client, state: RunState, run: fixture._Run
) -> None:
    before = await _read_current(client, state, run, allow_uninitialized=True)
    if any(Decimal(value) for value in before):
        return
    if state.hmc_config is None:
        raise ValueError("Processor setup requires HMC configuration")
    async with HMCClient(state.hmc_config) as hmc:
        system_name, lpar_name = await resolve_and_authorize_lpar_names(
            hmc, run.uuid or "", system_name_or_uuid=run.system
        )
        if (
            lpar_name != run.name
            or await fixture._token_of(client, state, run, run.uuid or "") != run.token
        ):
            raise PermissionError(
                "Processor setup requires the matching fixture identity and ownership token"
            )
        if await _read_current(client, state, run, allow_uninitialized=True) != before:
            raise ValueError("Processor setup refused changed CPU/memory evidence")
        await apply_lpar_profile_via_cli(hmc.config, system_name, lpar_name)
    await _read_current(client, state, run)
    state.record(SUBTASK, "owned default-profile apply (SETUP)", "PASS", {})


async def _case(
    client: Client,
    state: RunState,
    run: fixture._Run,
    operation: str,
    units: str,
    expected: tuple[str, ...],
    outcome: str,
) -> bool:
    if await fixture._token_of(client, state, run, run.uuid or "") != run.token:
        raise PermissionError(
            "Processor fixture ownership token changed; no quantity call sent"
        )
    await _read_current(client, state, run)
    status, data = await state.call(
        client,
        "hmc_dlpar_proc",
        lpar_name_or_uuid=run.uuid,
        system_name_or_uuid=run.system,
        change={"operation": operation, "processing_units": units},
    )
    if outcome == "refused":
        matched = (
            status == "FAIL"
            and isinstance(data, CallFailure)
            and data.exception_type == "ToolError"
            and data.message
            == "ToolError: Error calling tool 'hmc_dlpar_proc': Requested processor target exceeds partition maximum"
        )
    else:
        matched = (
            status == "PASS"
            and field(data, "outcome") == outcome
            and field(data, "workflow_completed") is True
        )
    independent = await _read_current(client, state, run)
    matched = matched and independent == expected
    state.record(
        SUBTASK,
        f"hmc_dlpar_proc ({operation} {units})",
        "PASS" if matched else "FAIL",
        {"result": data, "independent_cpu_memory": independent},
        "Independent current CPU/memory oracle; no guest effect claimed",
    )
    return matched


async def _cases(client: Client, state: RunState, run: fixture._Run) -> list[Assertion]:
    initial = await _read_current(client, state, run)
    if initial != _INITIAL:
        raise ValueError(
            "Processor fixture does not match its declared CPU/memory quantities"
        )
    reduced = (*initial[:4], "0.40", *initial[5:])
    cases = [
        ("remove", "0.10", reduced, "applied", "native-remove-read-back"),
        ("add", "0.00", reduced, "noop", "zero-delta-noop"),
        ("add", "1.10", reduced, "refused", "invalid-quantity-refused-unchanged"),
        ("set", "0.50", initial, "applied", "explicit-set-read-back"),
    ]
    assertions = []
    for operation, units, expected, outcome, name in cases:
        held = await _case(client, state, run, operation, units, expected, outcome)
        assertions.append(Assertion(name, held))
        if not held:
            break
    return assertions


async def _admit_fixture(
    hmc: HMCClient, run: fixture._Run, pools: fixture._Pools
) -> None:
    system_uuid = await resolve_system_uuid(hmc, run.system)
    system = await hmc.get_managed_system(system_uuid)
    value = resource(system or {})
    if (
        str((system or {}).get("UUID", "")).lower() != system_uuid.lower()
        or value.get("SystemName") != run.system
        or value.get("State") != "operating"
    ):
        raise ValueError("Processor fixture requires the selected operating system")
    cpu = value.get("AssociatedSystemProcessorConfiguration", {})
    if not isinstance(cpu, Mapping):
        raise TypeError("Processor fixture requires system processor limits")
    floor = Decimal(
        _number(cpu.get("MinimumProcessorUnitsPerVirtualProcessor"), units=True)
    )
    limits = [
        Decimal(_number(cpu.get(key)))
        for key in (
            "MaximumAllowedVirtualProcessorsPerPartition",
            "CurrentMaximumVirtualProcessorsPerAIXOrLinuxPartition",
        )
    ]
    if (
        not 0 < floor <= Decimal("0.10")
        or min(limits) < 4
        or Decimal(_number(pools.procs, units=True)) < Decimal("0.50")
        or int(_number(pools.memory)) < 2048
    ):
        raise ValueError(
            "Processor fixture exceeds available capacity or supported units/vCPU limits"
        )
    if run.region < 1 or any(value % run.region for value in (1024, 2048, 4096)):
        raise ValueError(
            "Processor fixture memory must align to the selected system region"
        )


async def exercise_processor_quantities(client: Client, state: RunState) -> None:
    """ST56: named owned inactive shared fixture; no activation, force or replay."""
    if state.group != GROUP or state.hmc_config is None:
        state.skip(SUBTASK, GROUP, "requires named opt-in group and HMC configuration")
        return
    baseline = await fixture._read_pools(client, state, state.config.system_name)
    if baseline is None or fixture.scratch_partitions(baseline[0].names):
        state.skip(
            SUBTASK,
            GROUP,
            "baseline unavailable or stranded scratch exists; run recovery",
        )
        return
    suffix = uuid.uuid4().hex[:8]
    run = fixture._Run(
        state.config.system_name,
        fixture.NAME_PREFIX + suffix,
        fixture.TOKEN_PREFIX + suffix,
        baseline[1],
        subtask=SUBTASK,
    )
    assertions = [Assertion("inactive-processor-quantity-sequence", False)]
    try:
        async with HMCClient(state.hmc_config) as hmc:
            await _admit_fixture(hmc, run, baseline[0])
            run.create_attempted = True
            created = await create_and_stamp_lpar(
                hmc,
                run.system,
                LparCreation(
                    run.name,
                    "AIX/Linux",
                    replace(
                        LparResources(**fixture.RESOURCES), desired_vcpus=2, max_vcpus=4
                    ),
                    caller_token=run.token,
                    stamp_policy="required",
                    apply_profile=False,
                ),
            )
        run.uuid = fixture._uuid_of(created.lpar) if created.lpar is not None else None
        if (
            not created.resource_created
            or created.ownership_stamped is not True
            or run.uuid is None
            or (
                created.apply_step is not None
                and created.apply_step.status not in {"ok", "skipped"}
            )
        ):
            raise ValueError("Processor fixture creation/ownership was not confirmed")
        run.created = True
        state.record(
            SUBTASK, "owning core create (no profile apply)", "PASS", asdict(created)
        )
        await _initialize_current(client, state, run)
        results = await _cases(client, state, run)
        assertions = results + [
            Assertion(
                "inactive-processor-quantity-sequence",
                len(results) == 4 and all(a.holds for a in results),
            )
        ]
    except (HMCError, ValueError, TypeError, PermissionError) as error:
        state.record(SUBTASK, "processor quantities arm", "FAIL", str(error))
    finally:
        await fixture._teardown(client, state, run, inactive_only=True)
    same = await fixture._compare(client, state, run, baseline[0])
    if run.created:
        state.record_verified(
            SUBTASK,
            "hmc_dlpar_proc",
            operation="lpar.dlpar_proc",
            scenario=SCENARIO,
            assertions=assertions,
            cleanup="passed" if run.deleted and same else "failed",
            data={
                "dedicated": "not-run",
                "vcpu_only": "not-run",
                "active_rmc": "not-run",
                "forced_restart": "not-run",
                "IBM_i": "not-run",
                "coupled_reset": "not-run",
            },
        )
