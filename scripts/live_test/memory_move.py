"""Two existing owned running fixtures, two bounded cases and read-only recovery."""

from __future__ import annotations

import shlex
from copy import deepcopy
from typing import Any

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.memory_move import _read_endpoint, _system_bounds
from hmcpctl.operations.lpar.ownership import authorize_lpar_ownership_description
from hmcpctl.resource_identity import resolve_system_uuid, system_name_from_uuid
from hmcpctl.ssh.profiles import get_lpar_description
from hmcpctl.ssh.transport import run_hmc_command

from . import evidence
from .observation import Assertion
from .results import field

SUBTASK = 54
GROUP = "lpar-memory-move"
SCENARIO = "st54-lpar-memory-move"
BASELINE = "memory-move baseline"


def configured(config) -> tuple[str, str]:
    names = (config.lp3_name, config.scratch_name)
    if (
        any(not value.strip() or value.startswith("example-") for value in names)
        or names[0] == names[1]
        or any(n in config.protected_lpar_names for n in names)
    ):
        raise ValueError(
            "Configure two distinct owned non-protected memory-move fixtures"
        )
    return names


async def system_oracle(hmc, system: str) -> list[str]:
    quoted = shlex.quote(system)
    commands = (
        f"lssyscfg -r lpar -m {quoted} -F name",
        f"lshwres -r proc -m {quoted} --level sys -F curr_avail_sys_proc_units",
        f"lshwres -r mem -m {quoted} --level sys -F curr_avail_sys_mem,mem_region_size",
    )
    return [
        (await run_hmc_command(hmc.config, command)).strip() for command in commands
    ]


async def oracle(hmc, system: str, peers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for peer in peers:
        try:
            number = peer["partition_id"]
            if type(number) is not int or number < 1:
                raise ValueError("Independent oracle requires a positive numeric ID")
            command = (
                f"lshwres -r mem -m {shlex.quote(system)} --level lpar "
                f"--filter lpar_ids={number} -F lpar_id,curr_min_mem,curr_mem,curr_max_mem,run_mem,mem_mode"
            )
            raw = (await run_hmc_command(hmc.config, command)).strip().splitlines()
            cells = raw[0].split(",") if len(raw) == 1 else []
            if (
                len(cells) != 6
                or cells[-1] != "ded"
                or any(not c.isascii() or not c.isdecimal() for c in cells[:5])
            ):
                raise ValueError(
                    "Independent memory oracle has malformed or non-dedicated row"
                )
            values = [int(c) for c in cells[:5]]
            if values[0] != number or not values[1] <= values[2] <= values[3]:
                raise ValueError("Independent memory oracle identity/bounds differ")
            results.append({"uuid": peer["uuid"], "values": values, "error": None})
        except (HMCError, ValueError) as error:
            results.append(
                {"uuid": peer["uuid"], "values": None, "error": str(error)[:2048]}
            )
    return results


def expected_oracle(peers):
    return [
        {
            "uuid": p["uuid"],
            "values": [
                p["partition_id"],
                p["current_minimum_mib"],
                p["current_memory_mib"],
                p["current_maximum_mib"],
                p["runtime_memory_mib"],
            ],
            "error": None,
        }
        for p in peers
    ]


async def admit(hmc, system_uuid, system_name, selectors, protected_names):
    peers = []
    for selector in selectors:
        kind, peer = await _read_endpoint(hmc, system_uuid, selector)
        if peer["name"] in protected_names:
            raise ValueError(
                "Arm54 resolved a protected fixture; no effect is authorized"
            )
        if (
            kind != "LogicalPartition"
            or peer["partition_type"] != "AIX/Linux"
            or peer["state"] != "running"
            or peer["rmc"] != "active"
            or peer["shared"]
            or peer["dynamic_memory"] not in (True, "true", "1")
            or peer["operation_in_progress"]
            or peer["change_location"]["current_profile_sync"]
            not in ("Disabled", "Suspended")
            or peer["current_memory_mib"] != peer["runtime_memory_mib"]
        ):
            raise ValueError(
                "Arm54 requires ordinary owned running RMC dedicated-memory fixtures"
            )
        description = await get_lpar_description(hmc.config, system_name, peer["name"])
        owner = authorize_lpar_ownership_description(
            hmc, system_name, peer["name"], description, operation="lpar-mutation"
        )
        if owner != (hmc.config.agent_id or "hmcpctl"):
            raise ValueError("Arm54 requires an explicit matching ownership stamp")
        peers.append(peer)
    if (
        peers[0]["uuid"] == peers[1]["uuid"]
        or peers[0]["partition_id"] == peers[1]["partition_id"]
    ):
        raise ValueError("Arm54 requires distinct fixtures")
    return peers


async def exercise_memory_move(client, state):
    if state.group != GROUP or state.hmc_config is None:
        state.skip(
            SUBTASK, GROUP, "requires named opt-in group and configured credentials"
        )
        return
    baseline = None
    assertions = []
    cleanup = "failed"
    try:
        selectors = configured(state.config)
        if state.checkpoint is None:
            raise ValueError("Private durable checkpoint is required before effects")
        async with HMCClient(state.hmc_config) as hmc:
            system_uuid = await resolve_system_uuid(hmc, state.config.system_name)
            system = await system_name_from_uuid(hmc, system_uuid)
            if not system:
                raise ValueError("Arm54 system name is unresolved")
            peers = await admit(
                hmc, system_uuid, system, selectors, state.config.protected_lpar_names
            )
            region, _ = await _system_bounds(hmc, system_uuid, system)
            if (
                peers[0]["current_memory_mib"] - region
                < peers[0]["current_minimum_mib"]
                or peers[1]["current_memory_mib"] + region
                > peers[1]["current_maximum_mib"]
            ):
                raise ValueError(
                    "Arm54 requires one region of source and destination headroom"
                )
            original = await oracle(hmc, system, peers)
            if original != expected_oracle(peers):
                raise ValueError(
                    "Arm54 independent baseline does not match both fixtures"
                )
            baseline = {
                "system_uuid": system_uuid,
                "system": system,
                "peers": peers,
                "oracle": original,
                "system_oracle": await system_oracle(hmc, system),
                "protected_names": list(state.config.protected_lpar_names),
            }
            state.record(SUBTASK, BASELINE, "PASS", baseline)
            state.checkpoint()
            expected = deepcopy(peers)
            for phase, order, deltas in (
                ("forward", (0, 1), (-region, region)),
                ("reverse", (1, 0), (region, -region)),
            ):
                fresh = await admit(
                    hmc,
                    system_uuid,
                    system,
                    [p["uuid"] for p in expected],
                    state.config.protected_lpar_names,
                )
                if fresh != expected or await oracle(
                    hmc, system, expected
                ) != expected_oracle(expected):
                    raise ValueError(
                        "Arm54 drift before effect; stop for read-only recovery"
                    )
                state.record(
                    SUBTASK,
                    f"memory-move {phase} intent",
                    "PASS",
                    {
                        "source": expected[order[0]]["uuid"],
                        "destination": expected[order[1]]["uuid"],
                        "quantity_mib": region,
                        "before": fresh,
                    },
                )
                state.checkpoint()
                status, data = await state.call(
                    client,
                    "hmc_move_lpar_memory",
                    lpar_name_or_uuid=expected[order[0]]["uuid"],
                    target_lpar_name_or_uuid=expected[order[1]]["uuid"],
                    system_name_or_uuid=system_uuid,
                    move={"quantity_mib": region},
                )
                for peer, delta in zip(expected, deltas, strict=True):
                    peer["current_memory_mib"] += delta
                    peer["runtime_memory_mib"] += delta
                observed = await oracle(hmc, system, expected)
                held = (
                    status == "PASS"
                    and field(data, "workflow_completed") is True
                    and field(data, "outcome") == "applied"
                    and observed == expected_oracle(expected)
                )
                assertions.append(Assertion(f"{phase}-both-independent", held))
                state.record(
                    SUBTASK,
                    f"memory-move {phase} observed",
                    "PASS" if held else "FAIL",
                    {"result": data, "independent": observed},
                )
                state.checkpoint()
                if not held:
                    break
            final = await oracle(hmc, system, peers)
            same = (
                final == original
                and await system_oracle(hmc, system) == baseline["system_oracle"]
            )
            cleanup = "passed" if same else "failed"
            state.record(
                SUBTASK, "memory-move final baseline", "PASS" if same else "FAIL", final
            )
    except (HMCError, ValueError, TypeError, PermissionError, OSError) as error:
        state.record(SUBTASK, GROUP, "FAIL", str(error))
    if baseline is not None:
        state.record_verified(
            SUBTASK,
            "hmc_move_lpar_memory",
            operation="lpar.move_memory",
            scenario=SCENARIO,
            assertions=[
                *assertions,
                Assertion(
                    "two-case-sequence",
                    len(assertions) == 2 and all(a.holds for a in assertions),
                ),
            ],
            cleanup=cleanup,
            data={
                "inactive": "not-run",
                "force": "not-run",
                "IBM_i": "not-run",
                "VIOS": "not-run",
            },
        )


async def recover_document(hmc_config, document):
    run = document.get("run", {})
    if (
        run.get("group") != GROUP
        or run.get("subtasks") != [SUBTASK]
        or run.get("tree_clean") is not True
    ):
        raise ValueError("Exact clean arm54 provenance is required")
    current = evidence.run_provenance(
        [SUBTASK], GROUP, evidence.repository_root(), "", False
    )
    if (
        not current["tested_commit"]
        or current["tested_commit"] != run.get("tested_commit")
        or current["tree_clean"] is not True
    ):
        raise ValueError("Recovery requires the same clean candidate revision")
    if document.get("hmc") != {
        "host": hmc_config.host,
        "port": hmc_config.port,
        "user": hmc_config.user,
        "verify_ssl": hmc_config.verify_ssl,
    }:
        raise ValueError("Recovery HMC destination differs")
    baselines = [
        row["data"]
        for row in document.get("results", [])
        if row.get("subtask") == SUBTASK
        and row.get("tool") == BASELINE
        and row.get("status") == "PASS"
    ]
    if len(baselines) != 1:
        raise ValueError("One original dual baseline is required")
    baseline = baselines[0]
    async with HMCClient(hmc_config) as hmc:
        peers = await admit(
            hmc,
            baseline["system_uuid"],
            baseline["system"],
            [p["uuid"] for p in baseline["peers"]],
            baseline["protected_names"],
        )
        observed = await oracle(hmc, baseline["system"], baseline["peers"])
        pools = await system_oracle(hmc, baseline["system"])
    if (
        peers != baseline["peers"]
        or observed != baseline["oracle"]
        or pools != baseline["system_oracle"]
    ):
        raise ValueError(
            "Memory move baselines differ; no recovery write was attempted"
        )
