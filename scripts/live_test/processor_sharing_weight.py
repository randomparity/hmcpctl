"""ST59: sequential owned inactive fixtures with independent sharing readback."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from fastmcp import Client

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar import LparResources, sharing_observation
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.core import LparCreation, create_and_stamp_lpar
from hmcpctl.resource_identity import resolve_system_uuid

from . import lpar_config as fixture
from .observation import Assertion, CallFailure
from .results import field, resource

if TYPE_CHECKING:
    from .runner import RunState

SUBTASK = 59
GROUP = "processor-sharing-weight"
_DEDICATED_RESOURCES: dict[str, Any] = {
    "min_memory": 1024,
    "desired_memory": 2048,
    "max_memory": 4096,
    "dedicated": True,
    "min_procs": 1,
    "desired_procs": 1,
    "max_procs": 1,
    "sharing_mode": "keep_idle_procs",
}
_IDENTITY = ("PartitionName", "PartitionType", "PartitionState")
_RANGE_ERROR = "uncapped_weight must be an integer from 0 to 255"
_SHARED_ERROR = "uncapped_weight requires uncapped mode; request explicit uncapping"
_DEDICATED_ERROR = (
    "Capping and uncapped_weight apply only to a shared-processor partition"
)


async def _read(client: Client, state: RunState, run: fixture._Run) -> dict[str, Any]:
    status, entry = await fixture._read_lpar(client, state, run)
    if status != "PASS" or not isinstance(entry, Mapping):
        raise ValueError("Independent sharing fixture read is unavailable")
    value = resource(entry)
    if (
        status != "PASS"
        or field(entry, "UUID") != run.uuid
        or tuple(value.get(key) for key in _IDENTITY)
        != (run.name, "AIX/Linux", "not activated")
    ):
        raise ValueError(
            "Independent sharing read found an unidentified or active fixture"
        )
    assert state.hmc_config is not None
    async with HMCClient(state.hmc_config) as hmc:
        system = await resolve_system_uuid(hmc, run.system)
        contained = await hmc.list_logical_partitions(system)
        if not any(item.get("UUID") == run.uuid for item in contained):
            raise ValueError("Sharing fixture is not contained in the selected system")
        document, _ = await hmc.read_logical_partition_document(run.uuid or "")
    observed = sharing_observation(document, _IDENTITY)
    if observed["uuid"].casefold() != str(run.uuid).casefold() or tuple(
        observed["requested"][key] for key in _IDENTITY
    ) != (run.name, "AIX/Linux", "not activated"):
        raise ValueError(
            "Independent sharing document identity/state does not match fixture"
        )
    return observed


def _context(observed: dict[str, Any], dedicated: bool) -> bool:
    configured, current = observed["configured"], observed["current"]
    return bool(
        configured is not None
        and current is not None
        and configured["dedicated"] == current["dedicated"] == dedicated
        and configured["mode"] == current["mode"]
        and (
            dedicated
            or configured["mode"] == "capped"
            or (
                configured["weight_status"] == current["weight_status"] == "valid"
                and configured["weight"] == current["weight"]
            )
        )
    )


async def _case(
    client: Client,
    state: RunState,
    run: fixture._Run,
    resources: dict[str, Any],
    expected_mode: str,
    expected_weight: int | None,
    refusal: str | None,
    checkpoint: Callable[[], None],
    dedicated: bool,
) -> bool:
    before = await _read(client, state, run)
    if not _context(before, dedicated):
        raise ValueError("Sharing fixture configured/current context changed")
    checkpoint()
    if await fixture._token_of(client, state, run, run.uuid or "") != run.token:
        raise PermissionError("Sharing fixture token changed; no resource call sent")
    status, result = await state.call(
        client,
        "hmc_dlpar_proc",
        lpar_name_or_uuid=run.uuid,
        system_name_or_uuid=run.system,
        resources=resources,
    )
    after = await _read(client, state, run)
    if refusal is not None:
        held = (
            status == "FAIL"
            and isinstance(result, CallFailure)
            and result.exception_type == "ToolError"
            and result.message
            == "ToolError: Error calling tool 'hmc_dlpar_proc': " + refusal
            and after == before
        )
    else:
        held = (
            status == "PASS"
            and field(result, "workflow_completed") is True
            and _context(after, dedicated)
            and all(
                after[branch]["mode"] == expected_mode
                for branch in ("configured", "current")
            )
            and (
                expected_weight is None
                or all(
                    after[branch]["weight"] == expected_weight
                    for branch in ("configured", "current")
                )
            )
        )
    state.record(
        SUBTASK,
        "hmc_dlpar_proc (sharing case)",
        "PASS" if held else "FAIL",
        {"resources": resources, "result": result, "independent": after},
        "Configured/current proof only; runtime is separately observed, no guest/profile proof",
    )
    checkpoint()
    return held


async def _cases(
    client: Client,
    state: RunState,
    run: fixture._Run,
    dedicated: bool,
    checkpoint: Callable[[], None],
) -> list[Assertion]:
    if dedicated:
        cases = [
            (
                {"sharing_mode": "keep_idle_procs"},
                "keep idle procs",
                None,
                None,
                "keep-idle-procs",
            ),
            (
                {"sharing_mode": "share_idle_procs"},
                "sre idle proces",
                None,
                None,
                "share-idle-procs",
            ),
            (
                {"sharing_mode": "share_idle_procs_active"},
                "sre idle procs active",
                None,
                None,
                "share-idle-procs-active",
            ),
            (
                {"sharing_mode": "share_idle_procs_always"},
                "sre idle procs always",
                None,
                None,
                "share-idle-procs-always",
            ),
            (
                {"uncapped_weight": 256},
                "",
                None,
                _RANGE_ERROR,
                "weight-256-refused-unchanged",
            ),
            (
                {"uncapped_weight": 0},
                "",
                None,
                _DEDICATED_ERROR,
                "weight-zero-refused-unchanged",
            ),
        ]
    else:
        cases = [
            (
                {"sharing_mode": "uncapped", "uncapped_weight": 64},
                "uncapped",
                64,
                None,
                "uncapped-weight-64",
            ),
            ({"uncapped_weight": 0}, "uncapped", 0, None, "uncapped-weight-zero"),
            ({"sharing_mode": "capped"}, "capped", None, None, "capped"),
            (
                {"sharing_mode": "uncapped", "uncapped_weight": 255},
                "uncapped",
                255,
                None,
                "uncapped-weight-255",
            ),
            (
                {"sharing_mode": "capped", "uncapped_weight": 0},
                "",
                None,
                _SHARED_ERROR,
                "capped-weight-refused-unchanged",
            ),
            (
                {"uncapped_weight": 256},
                "",
                None,
                _RANGE_ERROR,
                "weight-256-refused-unchanged",
            ),
        ]
    assertions = []
    for resources, mode, weight, refusal, name in cases:
        held = await _case(
            client, state, run, resources, mode, weight, refusal, checkpoint, dedicated
        )
        assertions.append(Assertion(name, held))
        if not held:
            break
    return assertions


async def _fixture(
    client: Client,
    state: RunState,
    baseline: tuple[fixture._Pools, int],
    dedicated: bool,
) -> bool:
    suffix = uuid.uuid4().hex[:8]
    run = fixture._Run(
        state.config.system_name,
        fixture.NAME_PREFIX + suffix,
        fixture.TOKEN_PREFIX + suffix,
        baseline[1],
        subtask=SUBTASK,
    )
    checkpoint_failed = False

    def checkpoint() -> None:
        nonlocal checkpoint_failed
        try:
            assert state.checkpoint is not None
            state.checkpoint()
        except BaseException:
            checkpoint_failed = True
            raise

    assertions = [Assertion("complete-sharing-sequence", False)]
    skipped = False
    try:
        state.record(
            SUBTASK,
            "sharing fixture intent",
            "PASS",
            {
                "name": run.name,
                "caller_token": run.token,
                "dedicated": dedicated,
                "baseline": fixture._pools_data(baseline[0]),
            },
        )
        checkpoint()
        run.create_attempted = True
        assert state.hmc_config is not None
        async with HMCClient(state.hmc_config) as hmc:
            created = await create_and_stamp_lpar(
                hmc,
                run.system,
                LparCreation(
                    run.name,
                    "AIX/Linux",
                    LparResources(
                        **(_DEDICATED_RESOURCES if dedicated else fixture.RESOURCES)
                    ),
                    caller_token=run.token,
                    stamp_policy="required",
                    apply_profile=False,
                ),
            )
        run.uuid = fixture._uuid_of(created.lpar)
        if (
            not created.resource_created
            or created.ownership_stamped is not True
            or run.uuid is None
        ):
            raise ValueError("Sharing fixture creation/ownership was not confirmed")
        run.created = True
        state.record(
            SUBTASK, "owning core create (no profile apply)", "PASS", asdict(created)
        )
        checkpoint()
        initial = await _read(client, state, run)
        state.record(SUBTASK, "initial independent sharing read", "PASS", initial)
        checkpoint()
        if not _context(initial, dedicated):
            skipped = True
            state.skip(
                SUBTASK,
                GROUP,
                "configured/current sharing context unavailable; no setup apply",
            )
        else:
            results = await _cases(client, state, run, dedicated, checkpoint)
            assertions = results + [
                Assertion(
                    "complete-sharing-sequence",
                    len(results) == 6 and all(a.holds for a in results),
                )
            ]
    except (HMCError, ValueError, TypeError, PermissionError) as error:
        if checkpoint_failed:
            raise
        state.record(SUBTASK, "sharing fixture", "FAIL", str(error))
    finally:
        try:
            await fixture._teardown(client, state, run, inactive_only=True)
        except Exception as error:  # noqa: BLE001 — retain cleanup failure without masking checkpoint error
            state.record(SUBTASK, "sharing cleanup", "FAIL", str(error))
    same = await fixture._compare(client, state, run, baseline[0])
    cleanup = "passed" if run.deleted and same else "failed"
    checkpoint()
    if run.created and not skipped:
        state.record_verified(
            SUBTASK,
            "hmc_dlpar_proc-dedicated-sharing"
            if dedicated
            else "hmc_dlpar_proc-shared-weight",
            operation="lpar.dlpar_proc",
            scenario="st59-lpar-dedicated-sharing"
            if dedicated
            else "st59-lpar-shared-weight",
            assertions=assertions,
            cleanup=cleanup,
            data={
                "runtime": "separately observed; no guest proof",
                "profile_persistence": "not-proven",
            },
        )
    return cleanup == "passed" and (skipped or all(a.holds for a in assertions))


async def exercise_processor_sharing_weight(client: Client, state: RunState) -> None:
    """Run only the named arm; no activation, profile apply, quantity or pool writes."""
    if state.group != GROUP or state.hmc_config is None or state.checkpoint is None:
        state.skip(
            SUBTASK,
            GROUP,
            "requires named group, HMC configuration and durable checkpoint",
        )
        return
    baseline = await fixture._read_pools(client, state, state.config.system_name)
    if baseline is None or fixture.scratch_partitions(baseline[0].names):
        state.skip(
            SUBTASK, GROUP, "baseline unavailable or stranded scratch; run recovery"
        )
        return
    for dedicated in (False, True):
        if not await _fixture(client, state, baseline, dedicated):
            break
