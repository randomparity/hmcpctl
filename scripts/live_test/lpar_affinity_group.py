"""ST57: serial inactive affinity fixtures with independent native observations."""

from __future__ import annotations

import shlex
import uuid
from typing import TYPE_CHECKING

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.linux_roles import read_creation_authority
from hmcpctl.resource_identity import is_uuid, resolve_lpar_uuid, resolve_system_uuid
from hmcpctl.ssh.commands import build_filter, parse_hmc_result_rows
from hmcpctl.xmlutil import leaf_text

from .lpar_config import (
    NAME_PREFIX,
    RESOURCES,
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

SUBTASK = 57
GROUP = "lpar-affinity-group"
SCENARIO = "st57-lpar-affinity-group-creation"


async def _create(client: Client, state: RunState, run: _Run, value: str | int) -> None:
    run.create_attempted = True
    status, data = await state.call(
        client,
        "hmc_create_lpar",
        system_name_or_uuid=run.system,
        name=run.name,
        partition_type="AIX/Linux",
        resources=RESOURCES,
        caller_token=run.token,
        affinity_group_id=value,
        apply_partition_profile=False,
    )
    data = plain_data(data)
    if status != "PASS" or not isinstance(data, dict):
        raise ValueError("Affinity creation did not return structured success")
    identity = _uuid_of(data.get("lpar"))
    run.uuid = identity if identity and is_uuid(identity) else None
    run.created = run.uuid is not None
    steps = [
        step
        for step in data.get("steps", [])
        if isinstance(step, dict) and step.get("step") == "affinity_group"
    ]
    if (
        not run.created
        or data.get("workflow_completed") is not True
        or data.get("ownership_stamped") is not True
        or len(steps) != 1
        or steps[0].get("status") != "ok"
        or not isinstance(steps[0].get("result"), dict)
        or steps[0]["result"].get("status") != "verified"
        or steps[0]["result"].get("requested") != value
    ):
        raise ValueError(
            "Creation identity, ownership or affinity workflow unconfirmed"
        )


async def _read(
    client: Client,
    state: RunState,
    run: _Run,
    system_uuid: str,
    controller: bool,
    value: str | int,
) -> dict[str, str]:
    if await _token_of(client, state, run, run.uuid or "") != run.token:
        raise ValueError("Fixture ownership changed")
    assert state.hmc_config is not None and run.uuid is not None
    async with HMCClient(state.hmc_config) as hmc:
        await resolve_lpar_uuid(hmc, run.uuid, system_name_or_uuid=system_uuid)
        entry = await hmc.get_logical_partition(run.uuid)
    resource = (entry or {}).get("Resource") or {}
    partition_id = leaf_text(resource.get("PartitionID"))
    if (
        _uuid_of(entry) != run.uuid
        or leaf_text(resource.get("PartitionName")) != run.name
        or leaf_text(resource.get("PartitionType")) != "AIX/Linux"
        or leaf_text(resource.get("PartitionState")) != "not activated"
        or not isinstance(partition_id, str)
        or not partition_id.isascii()
        or not partition_id.isdecimal()
        or str(int(partition_id)) != partition_id
    ):
        raise ValueError("Independent inactive fixture identity unconfirmed")
    observed = {}
    for kind in ("lpar",) if controller else ("lpar", "prof"):
        filters = [("lpar_ids", partition_id)]
        expected = {"lpar_id": partition_id, "name": run.name, "lpar_env": "aixlinux"}
        if kind == "prof":
            filters.append(("profile_names", "default_profile"))
            expected = {
                "lpar_id": partition_id,
                "lpar_name": run.name,
                "name": "default_profile",
                "lpar_env": "aixlinux",
            }
        fields = (*expected, "affinity_group_id")
        status, raw = await state.call(
            client,
            "hmc_run_command",
            cmd=f"lssyscfg -r {kind} -m {shlex.quote(run.system)} "
            f"--filter {shlex.quote(build_filter(filters))} -F {','.join(fields)} --header",
        )
        if (
            status != "PASS"
            or not isinstance(raw, str)
            or len(raw.splitlines()) != 2
            or raw.splitlines()[0] != ",".join(fields)
        ):
            raise ValueError("Independent affinity observation unavailable")
        rows = parse_hmc_result_rows(raw, fields, "independent affinity observation")
        if len(rows) != 1 or any(
            rows[0][field] != wanted
            for field, wanted in {**expected, "affinity_group_id": str(value)}.items()
        ):
            raise ValueError("Independent affinity observation mismatched")
        observed["native-partition" if kind == "lpar" else "native-profile"] = rows[0][
            "affinity_group_id"
        ]
    return observed


async def exercise_lpar_affinity_group(client: Client, state: RunState) -> None:
    """Stop before a second fixture unless the first proof and cleanup both pass."""
    if state.group != GROUP or state.hmc_config is None:
        state.skip(SUBTASK, GROUP, "requires opt-in group and HMC configuration")
        return
    try:
        async with HMCClient(state.hmc_config) as hmc:
            system_uuid = await resolve_system_uuid(hmc, state.config.system_name)
            _, controller = await read_creation_authority(
                hmc, system_uuid, need_management=False, context="Affinity group"
            )
    except (HMCError, OSError, ValueError, TypeError) as error:
        state.skip(SUBTASK, GROUP, f"authority unavailable: {type(error).__name__}")
        return
    baseline = await _read_pools(client, state, state.config.system_name)
    if baseline is None or scratch_partitions(baseline[0].names):
        state.skip(
            SUBTASK, GROUP, "baseline unavailable or stranded scratch; run recovery"
        )
        return
    observations = []
    cleaned = False
    for value in ("none", 1):
        suffix = uuid.uuid4().hex[:8]
        run = _Run(
            state.config.system_name,
            NAME_PREFIX + suffix,
            TOKEN_PREFIX + suffix,
            baseline[1],
            subtask=SUBTASK,
        )
        observed = None
        try:
            await _create(client, state, run, value)
            observed = await _read(client, state, run, system_uuid, controller, value)
        except (HMCError, OSError, ValueError, TypeError) as error:
            state.record(SUBTASK, GROUP, "FAIL", type(error).__name__)
        finally:
            await _teardown(client, state, run, inactive_only=True)
        same = await _compare(client, state, run, baseline[0])
        cleaned = run.deleted and same
        if observed is None or not cleaned:
            break
        observations.append(observed)
    state.record_verified(
        SUBTASK,
        "hmc_create_lpar",
        operation="lpar.create",
        scenario=SCENARIO,
        assertions=[Assertion("native-affinity-assignments", len(observations) == 2)],
        cleanup="passed" if cleaned else "failed",
        data={
            "authority": "controller" if controller else "classic",
            "requested": ["none", 1],
            "observations": observations,
            "activation": "not-run",
            "runtime-equivalence": "not-tested",
        },
    )
