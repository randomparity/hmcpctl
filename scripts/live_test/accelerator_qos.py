"""One explicit GZIP QoS change, one separately authorized restoration, read-only proof."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar_accelerator import LparAcceleratorQos
from hmcpctl.errors import HMCError
from hmcpctl.operations.inventory.composite import resource_container
from hmcpctl.operations.lpar.ownership import authorize_lpar_ownership_description
from hmcpctl.operations.partition_state import PARTITION_STATES
from hmcpctl.resource_identity import (
    is_uuid,
    resolve_system_uuid,
    system_name_from_uuid,
)
from hmcpctl.ssh.profiles import get_lpar_description
from hmcpctl.ssh.transport import run_hmc_command
from hmcpctl.xmlutil import leaf_text

from . import evidence
from .config import LiveTestConfig
from .observation import Assertion, CallFailure, plain_data

SUBTASK = 64
GROUP = "lpar-accelerator-qos"
SCENARIO = "st64-lpar-accelerator-qos"
BASELINE = "accelerator-qos baseline"
TOOL = "hmc_set_lpar_accelerator_qos"


def configured(config: LiveTestConfig) -> tuple[str, LparAcceleratorQos]:
    names = (config.system_name, config.lp3_name)
    if (
        any(not value.strip() or value.startswith("example-") for value in names)
        or config.lp3_name in config.protected_lpar_names
        or not config.accelerator_qos_request_json.strip()
    ):
        raise ValueError(
            "Arm64 requires an explicit system, owned unprotected fixture and QoS request"
        )
    return config.lp3_name, LparAcceleratorQos.model_validate_json(
        config.accelerator_qos_request_json, strict=True
    )


def candidate(expected: dict | None = None) -> dict:
    current = evidence.run_provenance(
        [SUBTASK], GROUP, evidence.repository_root(), "", False
    )
    if (
        not current.get("tested_commit")
        or current.get("tree_clean") is not True
        or expected is not None
        and current["tested_commit"] != expected["tested_commit"]
    ):
        raise ValueError("Arm64 requires the same clean deployed candidate")
    return {"tested_commit": current["tested_commit"], "tree_clean": True}


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str) or not is_uuid(value):
        raise ValueError("Arm64 requires a valid UUID identity")
    return value.lower()


async def admit(hmc, system_uuid, system_name, selector, protected, change):
    system_uuid = _canonical_uuid(system_uuid)
    matches = []
    for kind, entries in (
        ("LogicalPartition", await hmc.list_logical_partitions(system_uuid)),
        ("VirtualIOServer", await hmc.list_vios(system_uuid)),
    ):
        for entry in entries:
            uuid = entry.get("UUID")
            name = leaf_text(resource_container(entry, "Resource").get("PartitionName"))
            if (
                is_uuid(selector)
                and isinstance(uuid, str)
                and uuid.casefold() == selector.casefold()
            ) or (not is_uuid(selector) and name == selector):
                matches.append((kind, uuid))
    if (
        len(matches) != 1
        or not isinstance(matches[0][1], str)
        or not is_uuid(matches[0][1])
    ):
        raise ValueError("Arm64 requires one contained partition with valid UUID")
    kind, uuid = matches[0]
    uuid = _canonical_uuid(uuid)
    read = hmc.get_vios if kind == "VirtualIOServer" else hmc.get_logical_partition
    entry = await read(uuid)
    resource = resource_container(entry or {}, "Resource")
    name = leaf_text(resource.get("PartitionName"))
    number = leaf_text(resource.get("PartitionID"))
    partition_type = leaf_text(resource.get("PartitionType"))
    state = leaf_text(resource.get("PartitionState"))
    rmc = leaf_text(resource.get("ResourceMonitoringControlState"))
    if (
        not entry
        or _canonical_uuid(entry.get("UUID")) != uuid
        or not isinstance(name, str)
        or not name.strip()
        or name in protected
        or not is_uuid(selector)
        and name != selector
        or not isinstance(number, str)
        or not number.isascii()
        or not number.isdecimal()
        or int(number) < 1
        or partition_type
        not in (
            ("Virtual IO Server",)
            if kind == "VirtualIOServer"
            else ("AIX/Linux", "OS400")
        )
        or state not in PARTITION_STATES
        or state == "Unknown"
        or leaf_text(resource.get("IsOperationInProgress")) != "false"
    ):
        raise ValueError("Arm64 partition identity, protection or admission changed")
    if partition_type == "OS400":
        if (
            change.force
            or change.wait_minutes is not None
            or change.detail_level is not None
        ):
            raise ValueError("Arm64 OS400 request cannot use force, wait or detail")
    elif (
        rmc not in ("active", "inactive", "none")
        or rmc != "active"
        and not change.force
    ):
        raise ValueError(
            "Arm64 requires recognized RMC and explicit force without active RMC"
        )
    system_entry = await hmc.get_managed_system(system_uuid)
    system = resource_container(system_entry or {}, "Resource")
    caps = resource_container(system, "AssociatedSystemCapabilities")
    types = resource_container(system, "SupportedHardwareAcceleratorTypes").get(
        "HardwareAcceleratorType"
    )
    types = types if isinstance(types, list) else [types]
    advertised = [
        leaf_text(t.get("Type")) if isinstance(t, dict) else None for t in types
    ]
    if (
        not system_entry
        or _canonical_uuid(system_entry.get("UUID")) != system_uuid
        or leaf_text(system.get("SystemName")) != system_name
        or leaf_text(system.get("State")) != "operating"
        or leaf_text(caps.get("HardwareAcceleratorCapable")) != "true"
        or partition_type == "OS400"
        and leaf_text(caps.get("IBMiHardwareAcceleratorCapable")) != "true"
        or advertised.count("gzip") != 1
        or any(not isinstance(t, str) for t in advertised)
    ):
        raise ValueError(
            "Arm64 selected-system identity or accelerator capability changed"
        )
    description = await get_lpar_description(hmc.config, system_name, name)
    owner = authorize_lpar_ownership_description(
        hmc, system_name, name, description, operation="lpar-mutation"
    )
    if owner != (hmc.config.agent_id or "hmcpctl"):
        raise ValueError("Arm64 requires an actual matching ownership stamp")
    return {
        "kind": kind,
        "uuid": uuid,
        "name": name,
        "partition_id": int(number),
        "partition_type": partition_type,
        "state": state,
        "rmc": rmc,
        "owner": owner,
        "system": {
            "uuid": system_uuid,
            "name": system_name,
            "state": leaf_text(system.get("State")),
            "general": leaf_text(caps.get("HardwareAcceleratorCapable")),
            "ibmi": leaf_text(caps.get("IBMiHardwareAcceleratorCapable")),
            "types": advertised,
        },
    }


async def oracle(hmc, system: str, partition_id: int | None) -> list:
    """Parse a fixed field projection directly, independently of product QoS readers."""
    if partition_id is not None and (type(partition_id) is not int or partition_id < 1):
        raise ValueError("Arm64 oracle requires a positive numeric partition ID")
    suffix = (
        "sys -F type,max_qos,curr_avail_qos,pend_avail_qos"
        if partition_id is None
        else (
            f"lpar --filter lpar_ids={partition_id} -F lpar_id,type,curr_qos,pend_qos"
        )
    )
    raw = (
        (
            await run_hmc_command(
                hmc.config,
                f"lshwres -r accel -m {shlex.quote(system)} --rsubtype gzip --level {suffix}",
                max_output_bytes=65536,
            )
        )
        .strip()
        .splitlines()
    )
    cells = raw[0].split(",") if len(raw) == 1 else []
    type_index = 0 if partition_id is None else 1
    if (
        len(cells) != 4
        or cells[type_index] != "gzip"
        or any(
            not c.isascii() or not c.isdecimal()
            for i, c in enumerate(cells)
            if i != type_index
        )
    ):
        raise ValueError(
            "Arm64 independent oracle requires one numeric matching gzip row"
        )
    values = [c if i == type_index else int(c) for i, c in enumerate(cells)]
    if partition_id is not None and values[0] != partition_id:
        raise ValueError("Arm64 independent oracle partition ID differs")
    return values


def succeeded(status, data, observed, number, quantity):
    data = plain_data(data)
    return (
        status == "PASS"
        and isinstance(data, Mapping)
        and data.get("submission") == "completed"
        and "submission_error" in data
        and data["submission_error"] is None
        and data.get("workflow_completed") is True
        and data.get("outcome") == "observed"
        and type(data.get("requested_qos")) is int
        and data["requested_qos"] == quantity
        and isinstance(observed, list)
        and len(observed) == 4
        and all(type(observed[i]) is int for i in (0, 2, 3))
        and observed == [number, "gzip", quantity, quantity]
    )


async def before_effect(hmc, state, baseline, change, quantity):
    candidate(baseline["candidate"])
    if state.hmc_config is None or baseline["hmc"] != {
        key: getattr(state.hmc_config, key)
        for key in ("host", "port", "user", "verify_ssl")
    }:
        raise ValueError("Arm64 HMC destination changed before effect")
    selector, configured_request = configured(state.config)
    system_uuid = _canonical_uuid(
        await resolve_system_uuid(hmc, state.config.system_name)
    )
    system_name = await system_name_from_uuid(hmc, system_uuid)
    if not system_name:
        raise ValueError("Arm64 selected system has no current name")
    if (
        selector != baseline["target"]["name"]
        or configured_request.model_dump() != baseline["request"]
        or list(state.config.protected_lpar_names) != baseline["protected_names"]
        or (system_uuid, system_name) != (baseline["system_uuid"], baseline["system"])
    ):
        raise ValueError(
            "Arm64 configured target, request, protection or system changed"
        )
    fresh = await admit(
        hmc,
        system_uuid,
        system_name,
        baseline["target"]["uuid"],
        state.config.protected_lpar_names,
        change,
    )
    if fresh != baseline["target"]:
        raise ValueError("Arm64 identity, ownership or admission drifted before effect")
    native = await oracle(hmc, system_name, fresh["partition_id"])
    await oracle(hmc, system_name, None)
    if native != [fresh["partition_id"], "gzip", quantity, quantity]:
        raise ValueError(
            "Arm64 identity, ownership, admission or native quantities drifted before effect"
        )


async def exercise_accelerator_qos(client, state) -> None:
    if state.group != GROUP or state.hmc_config is None:
        state.skip(
            SUBTASK, GROUP, "requires named opt-in group and configured credentials"
        )
        return
    baseline: dict[str, Any] | None = None
    cleanup, phases = "failed", []
    final_equal = False
    try:
        selector, request = configured(state.config)
        if state.checkpoint is None:
            raise ValueError(
                "Arm64 requires a private durable checkpoint before effects"
            )
        provenance = candidate()
        async with HMCClient(state.hmc_config) as hmc:
            system_uuid = _canonical_uuid(
                await resolve_system_uuid(hmc, state.config.system_name)
            )
            system_name = await system_name_from_uuid(hmc, system_uuid)
            if not system_name:
                raise ValueError("Arm64 selected system has no current name")
            target = await admit(
                hmc,
                system_uuid,
                system_name,
                selector,
                state.config.protected_lpar_names,
                request,
            )
            native = await oracle(hmc, system_name, target["partition_id"])
            system_native = await oracle(hmc, system_name, None)
            if native[2] != native[3] or request.qos == native[2]:
                raise ValueError(
                    "Arm64 requires equal numeric current/pending and a different explicit request"
                )
            baseline = {
                "candidate": provenance,
                "system_uuid": system_uuid,
                "system": system_name,
                "hmc": {
                    key: getattr(hmc.config, key)
                    for key in ("host", "port", "user", "verify_ssl")
                },
                "target": target,
                "native": native,
                "system_native": system_native,
                "request": request.model_dump(),
                "protected_names": list(state.config.protected_lpar_names),
            }
            state.record(SUBTASK, BASELINE, "PASS", deepcopy(baseline))
            state.checkpoint()
            restore = request.model_copy(update={"qos": native[2]})
            for phase, change, expected in (
                ("forward", request, native[2]),
                ("restore", restore, request.qos),
            ):
                await before_effect(hmc, state, baseline, change, expected)
                state.record(
                    SUBTASK,
                    f"accelerator-qos {phase} intent",
                    "PASS",
                    {
                        "uuid": target["uuid"],
                        "system_uuid": system_uuid,
                        "before": [target["partition_id"], "gzip", expected, expected],
                        "change": change.model_dump(),
                    },
                )
                state.checkpoint()
                await before_effect(hmc, state, baseline, change, expected)
                status, data = await state.call(
                    client,
                    "hmc_set_lpar_accelerator_qos",
                    lpar_name_or_uuid=target["uuid"],
                    system_name_or_uuid=system_uuid,
                    change=change.model_dump(),
                )
                observed, error = None, None
                try:
                    observed = await oracle(hmc, system_name, target["partition_id"])
                except (HMCError, ValueError) as exc:
                    error = str(exc)[:2048]
                held = succeeded(
                    status, data, observed, target["partition_id"], change.qos
                )
                phases.append(Assertion(f"{phase}-current-pending", held))
                state.record(
                    SUBTASK,
                    f"accelerator-qos {phase} observed",
                    "PASS" if held else "FAIL",
                    {
                        "tool_status": status,
                        "result": data.message
                        if isinstance(data, CallFailure)
                        else plain_data(data),
                        "independent": observed,
                        "error": error,
                    },
                )
                state.checkpoint()
                if not held:
                    break
            final = await oracle(hmc, system_name, target["partition_id"])
            final_system = await oracle(hmc, system_name, None)
            fresh = await admit(
                hmc,
                system_uuid,
                system_name,
                target["uuid"],
                state.config.protected_lpar_names,
                request,
            )
            candidate(provenance)
            final_equal = (
                len(phases) == 2
                and all(a.holds for a in phases)
                and final == native
                and final_system == system_native
                and fresh == target
            )
            cleanup = "passed" if final_equal else "failed"
            state.record(
                SUBTASK,
                "accelerator-qos final baseline",
                "PASS" if final_equal else "FAIL",
                {"native": final, "system_native": final_system, "target": fresh},
            )
            state.checkpoint()
    except (HMCError, ValueError, TypeError, PermissionError, OSError) as error:
        cleanup = "failed"
        state.record(SUBTASK, GROUP, "FAIL", str(error))
    if baseline is not None:
        state.record_verified(
            SUBTASK,
            TOOL,
            operation="lpar.set_accelerator_qos",
            scenario=SCENARIO,
            assertions=[
                Assertion("forward-current-pending", bool(phases) and phases[0].holds),
                Assertion(
                    "restore-current-pending", len(phases) == 2 and phases[1].holds
                ),
                Assertion("original-baseline", final_equal and cleanup == "passed"),
                Assertion(
                    "exact-two-phase-sequence",
                    len(phases) == 2 and all(a.holds for a in phases),
                ),
            ],
            cleanup=cleanup,
            data={
                "partition_type": baseline["target"]["partition_type"],
                "state": baseline["target"]["state"],
                "force": baseline["request"]["force"],
                "effective_runtime": "unavailable",
                "profile_persistence": "unverified",
                "other_contexts": "not-run",
            },
        )


def validate_finished(document, baseline):
    if document["run"].get("partial") is not False:
        raise ValueError("Arm64 run remains partial or completion flag is missing")
    results = document.get("results")
    if not isinstance(results, list) or any(
        not isinstance(row, Mapping) for row in results
    ):
        raise ValueError("Arm64 finite proof contains malformed rows")
    if any(
        type(row.get("subtask")) is not int or row["subtask"] != SUBTASK
        for row in results
    ):
        raise ValueError("Arm64 finite proof contains a foreign subtask")
    rows = results
    if rows and rows[-1].get("tool") == TOOL:
        if rows[-1].get("status") != "PASS":
            raise ValueError("Arm64 verified result failed")
        rows = rows[:-1]
    names = [
        BASELINE,
        "accelerator-qos forward intent",
        "accelerator-qos forward observed",
        "accelerator-qos restore intent",
        "accelerator-qos restore observed",
        "accelerator-qos final baseline",
    ]
    if [row.get("tool") for row in rows] != names or any(
        row.get("status") != "PASS" for row in rows
    ):
        raise ValueError(
            "Arm64 requires the complete ordered successful finite sequence"
        )
    request = LparAcceleratorQos.model_validate(baseline["request"], strict=True)
    native = baseline["native"]
    number = baseline["target"]["partition_id"]
    system_native = baseline["system_native"]
    if (
        not isinstance(system_native, list)
        or len(system_native) != 4
        or system_native[0] != "gzip"
        or any(type(value) is not int or value < 0 for value in system_native[1:])
        or not isinstance(native, list)
        or len(native) != 4
        or type(number) is not int
        or number < 1
        or native[:2] != [number, "gzip"]
        or type(native[0]) is not int
        or any(type(value) is not int or value < 0 for value in native[2:])
        or native[2] != native[3]
        or request.qos == native[2]
    ):
        raise ValueError("Arm64 original baseline is inconsistent")
    for index, change, before in (
        (1, request.model_dump(), native[2]),
        (3, {**request.model_dump(), "qos": native[2]}, request.qos),
    ):
        intent = rows[index].get("data")
        if not isinstance(intent, Mapping):
            raise TypeError("Arm64 finite intent is malformed")
        LparAcceleratorQos.model_validate(intent.get("change"), strict=True)
        before_values = intent.get("before")
        if (
            not isinstance(before_values, list)
            or len(before_values) != 4
            or any(type(before_values[i]) is not int for i in (0, 2, 3))
            or intent
            != {
                "uuid": baseline["target"]["uuid"],
                "system_uuid": baseline["system_uuid"],
                "before": [number, "gzip", before, before],
                "change": change,
            }
        ):
            raise ValueError("Arm64 finite intent identity, quantity or options differ")
        observed = rows[index + 1].get("data")
        if (
            not isinstance(observed, Mapping)
            or observed.get("error") is not None
            or not succeeded(
                observed.get("tool_status"),
                observed.get("result"),
                observed.get("independent"),
                number,
                change["qos"],
            )
        ):
            raise ValueError(
                "Arm64 finite phase lacks completed submission and independent observation"
            )
    if rows[-1].get("data") != {
        "native": native,
        "system_native": baseline["system_native"],
        "target": baseline["target"],
    }:
        raise ValueError("Arm64 final proof differs from original baseline")


async def recover_document(hmc_config: HMCConfig, document: dict[str, Any]) -> None:
    run = document.get("run", {})
    if (
        run.get("group") != GROUP
        or run.get("subtasks") != [SUBTASK]
        or any(type(number) is not int for number in run["subtasks"])
        or run.get("tree_clean") is not True
    ):
        raise ValueError("Arm64 requires exact clean group/subtask provenance")
    provenance = candidate({"tested_commit": run.get("tested_commit")})
    destination = document.get("hmc")
    if (
        not isinstance(destination, dict)
        or any(
            type(destination.get(key)) is not expected
            for key, expected in (
                ("host", str),
                ("port", int),
                ("user", str),
                ("verify_ssl", bool),
            )
        )
        or destination
        != {
            key: getattr(hmc_config, key)
            for key in ("host", "port", "user", "verify_ssl")
        }
    ):
        raise ValueError("Arm64 recovery HMC destination differs")
    baselines = [
        row.get("data")
        for row in document.get("results", [])
        if isinstance(row, Mapping)
        and row.get("subtask") == SUBTASK
        and row.get("tool") == BASELINE
    ]
    if len(baselines) != 1 or not isinstance(baselines[0], dict):
        raise ValueError("Arm64 requires one original baseline")
    baseline = baselines[0]
    if (
        baseline.get("candidate") != provenance
        or baseline.get("hmc") != document["hmc"]
    ):
        raise ValueError("Arm64 baseline candidate or destination differs")
    target = baseline["target"]
    if (
        not isinstance(target, Mapping)
        or not is_uuid(target.get("uuid", ""))
        or not is_uuid(baseline["system_uuid"])
    ):
        raise ValueError("Arm64 requires valid original target and system identities")
    if not isinstance(baseline.get("protected_names"), list) or not all(
        isinstance(name, str) for name in baseline["protected_names"]
    ):
        raise ValueError("Arm64 requires the original protection inventory")
    change = LparAcceleratorQos.model_validate(baseline["request"], strict=True)
    async with HMCClient(hmc_config) as hmc:
        uuid = _canonical_uuid(await resolve_system_uuid(hmc, baseline["system_uuid"]))
        name = await system_name_from_uuid(hmc, uuid)
        if not name:
            raise ValueError("Arm64 recovery system has no current name")
        fresh = await admit(
            hmc, uuid, name, target["uuid"], baseline["protected_names"], change
        )
        print(
            "OBSERVATION: read-only recovery identity "
            + json.dumps(
                {
                    "system_uuid": uuid,
                    "partition_uuid": fresh["uuid"],
                    "partition_id": fresh["partition_id"],
                }
            )
        )
        witnesses = {}
        for label, number in (("partition", target["partition_id"]), ("system", None)):
            try:
                witnesses[label] = await oracle(hmc, name, number)
                print(
                    f"OBSERVATION: {label} native quantities "
                    + json.dumps(witnesses[label])
                )
            except (HMCError, ValueError, TypeError) as error:
                witnesses[label] = None
                print(f"OBSERVATION: {label} read unavailable ({type(error).__name__})")
        native, system = witnesses["partition"], witnesses["system"]
    if (
        uuid != baseline["system_uuid"]
        or name != baseline["system"]
        or fresh != target
        or native != baseline["native"]
        or system != baseline["system_native"]
    ):
        raise ValueError(
            "Arm64 original baselines differ; no recovery writes attempted"
        )
    validate_finished(document, baseline)
