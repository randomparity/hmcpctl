"""Owned inactive native memory quantity proof; arm50 registration is separate."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import asdict
from typing import TYPE_CHECKING

from fastmcp import Client

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar import LparResources
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.core import LparCreation, create_and_stamp_lpar
from hmcpctl.xmlutil import leaf_text

from . import lpar_config as fixture
from .observation import Assertion, CallFailure
from .results import field, resource

if TYPE_CHECKING:
    from .runner import RunState

SUBTASK = 50
GROUP = "lpar-memory-quantities"
SCENARIO = "st50-lpar-memory-quantities"


async def _read_current(
    client: Client, state: RunState, run: fixture._Run
) -> tuple[int, int, int]:
    status, data = await fixture._read_lpar(client, state, run)
    if status != "PASS" or not isinstance(data, Mapping):
        raise ValueError("Independent memory oracle could not read the owned fixture")
    value = resource(data)
    if (
        str(data.get("UUID", "")).casefold() != str(run.uuid).casefold()
        or value.get("PartitionName") != run.name
        or value.get("PartitionState") != "not activated"
        or value.get("PartitionType") != "AIX/Linux"
    ):
        raise ValueError(
            "Independent memory oracle found a different or active fixture"
        )
    memory = value.get("PartitionMemoryConfiguration")
    if not isinstance(memory, Mapping):
        raise TypeError("Independent memory oracle has no memory configuration")
    values = []
    for key in ("CurrentMinimumMemory", "CurrentMemory", "CurrentMaximumMemory"):
        text = leaf_text(memory.get(key))
        if not isinstance(text, str) or not text.isascii() or not text.isdecimal():
            raise ValueError(
                "Independent memory oracle has missing/malformed current bounds"
            )
        values.append(int(text))
    minimum, current, maximum = values
    if not 0 < minimum <= current <= maximum:
        raise ValueError(
            "Independent memory oracle requires initialized positive current bounds"
        )
    return minimum, current, maximum


async def _case(
    client: Client,
    state: RunState,
    run: fixture._Run,
    operation: str,
    quantity: int,
    expected: tuple[int, int, int],
    outcome: str,
) -> bool:
    if await fixture._token_of(client, state, run, run.uuid or "") != run.token:
        raise PermissionError(
            "Memory fixture ownership token changed; no quantity call sent"
        )
    status, data = await state.call(
        client,
        "hmc_dlpar_mem",
        lpar_name_or_uuid=run.uuid,
        system_name_or_uuid=run.system,
        change={"operation": operation, "quantity_mib": quantity},
    )
    if outcome == "refused":
        matched = (
            status == "FAIL"
            and isinstance(data, CallFailure)
            and data.exception_type == "ValueError"
            and "exceeds the partition maximum" in data.message
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
        f"hmc_dlpar_mem ({operation} {quantity})",
        "PASS" if matched else "FAIL",
        {"result": data, "independent_current_bounds": independent},
        "Independent current-memory oracle; no guest effect claimed",
    )
    return matched


async def _cases(client: Client, state: RunState, run: fixture._Run) -> list[Assertion]:
    initial = await _read_current(client, state, run)
    minimum, current, maximum = initial
    if run.region < 1 or any(value % run.region for value in initial):
        raise ValueError(
            "Memory fixture bounds do not align with a positive system region"
        )
    if current + 2 * run.region > maximum:
        raise ValueError(
            "Memory fixture lacks headroom for the bounded set/add/remove proof"
        )
    cases = [
        (
            "set",
            current + run.region,
            (minimum, current + run.region, maximum),
            "applied",
            "native-set-read-back",
        ),
        (
            "add",
            run.region,
            (minimum, current + 2 * run.region, maximum),
            "applied",
            "native-add-read-back",
        ),
        ("remove", 2 * run.region, initial, "applied", "native-remove-read-back"),
        ("add", 0, initial, "noop", "zero-delta-noop"),
        (
            "add",
            maximum + run.region,
            initial,
            "refused",
            "invalid-quantity-refused-unchanged",
        ),
    ]
    assertions = []
    for operation, quantity, expected, outcome, name in cases:
        held = await _case(client, state, run, operation, quantity, expected, outcome)
        assertions.append(Assertion(name, held))
        if not held:
            break
    return assertions


async def exercise_memory_quantities(client: Client, state: RunState) -> None:
    """ST50: one inactive owned fixture, setup apply, three native writes, cleanup.

    The initial profile application belongs to existing creation SETUP, making
    current bounds available. Quantity operations never apply or write a profile.
    No power/activation, shared-pool mutation or foreign partition is admitted.
    """
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
    assertions = [Assertion("memory-quantity-sequence", False)]
    try:
        run.create_attempted = True
        async with HMCClient(state.hmc_config) as hmc:
            created = await create_and_stamp_lpar(
                hmc,
                run.system,
                LparCreation(
                    run.name,
                    "AIX/Linux",
                    LparResources(**fixture.RESOURCES),
                    caller_token=run.token,
                    stamp_policy="required",
                    apply_profile=True,
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
            raise ValueError("Memory fixture creation/ownership was not confirmed")
        run.created = True
        state.record(
            SUBTASK, "owning core create (SETUP profile apply)", "PASS", asdict(created)
        )
        results = await _cases(client, state, run)
        assertions = results + [
            Assertion(
                "memory-quantity-sequence",
                len(results) == 5 and all(a.holds for a in results),
            )
        ]
    except (HMCError, ValueError, TypeError, PermissionError) as error:
        state.record(SUBTASK, "memory quantities arm", "FAIL", str(error))
    finally:
        await fixture._teardown(client, state, run, inactive_only=True)
    same = await fixture._compare(client, state, run, baseline[0])
    if run.created:
        state.record_verified(
            SUBTASK,
            "hmc_dlpar_mem",
            operation="lpar.dlpar_mem",
            scenario=SCENARIO,
            assertions=assertions,
            cleanup="passed" if run.deleted and same else "failed",
            data={
                "guest_effect": "not-run",
                "AMS": "not-run",
                "forced_restart": "not-run",
            },
        )
