"""ST61: two existing owned CPU fixtures, finite proof and read-only recovery."""

from __future__ import annotations

import csv
import io
import shlex
from copy import deepcopy
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Any

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar_processors import (
    PROCESSOR_NUMERIC_TOKEN_BYTES,
    bounded_processor_number,
)
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar import memory
from hmcpctl.operations.lpar.ownership import authorize_lpar_ownership_description
from hmcpctl.operations.lpar.processor_move import _read_endpoint
from hmcpctl.resource_identity import (
    is_uuid,
    resolve_system_uuid,
    system_name_from_uuid,
)
from hmcpctl.ssh.processors import _FIELDS
from hmcpctl.ssh.profiles import get_lpar_description
from hmcpctl.ssh.transport import run_hmc_command

from . import evidence
from .memory_move import system_oracle
from .observation import Assertion, plain_data

if TYPE_CHECKING:
    from .runner import RunState

SUBTASK = 61
GROUP = "lpar-processor-move"
SCENARIO = "st61-lpar-processor-move"
BASELINE = "processor-move baseline"
_UNIT_FIELDS = {
    *(
        f"{prefix}_{bound}processing_units"
        for prefix in ("current", "pending")
        for bound in ("minimum_", "", "maximum_")
    ),
    "runtime_processing_units",
}
_COUNT_FIELDS = {
    *(
        f"{prefix}_{bound}processors"
        for prefix in ("current", "pending")
        for bound in ("minimum_", "", "maximum_")
    ),
    "runtime_processors",
}
_VECTOR = {
    "CurrentMinimumProcessingUnits": "current_minimum_processing_units",
    "CurrentProcessingUnits": "current_processing_units",
    "CurrentMaximumProcessingUnits": "current_maximum_processing_units",
    "CurrentMinimumVirtualProcessors": "current_minimum_processors",
    "AllocatedVirtualProcessors": "current_processors",
    "CurrentMaximumVirtualProcessors": "current_maximum_processors",
}
_SNAPSHOT_FIELDS = {
    *memory._MEMORY_FIELDS,
    *_UNIT_FIELDS,
    *_COUNT_FIELDS,
    "uuid",
    "name",
    "partition_id",
    "state",
    "partition_type",
    "rmc",
    "shared",
    "entitled_mib",
    "runtime_entitled_mib",
    "automatic_entitlement",
    "dynamic_memory",
    "operation_in_progress",
    "processors",
    "change_location",
    "processor_mode",
    "dynamic_processor",
}


def configured(config) -> tuple[str, str]:
    names = (config.lp3_name, config.scratch_name)
    if (
        any(not n.strip() or n.startswith("example-") for n in names)
        or names[0] == names[1]
        or any(n in config.protected_lpar_names for n in names)
    ):
        raise ValueError(
            "Configure two distinct owned non-protected processor-move fixtures"
        )
    return names


def _unit(value: object) -> str:
    number = bounded_processor_number(value)
    token = f"{number:.2f}"
    if not isinstance(value, str) or value != token:
        raise ValueError("ST61 requires a canonical two-place processor quantity")
    return token


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("ST61 requires strict integer counts and memory")
    return int(bounded_processor_number(value, whole=True))


def _encoded(value: Any) -> Any:
    value = plain_data(value)
    if isinstance(value, Decimal):
        return f"{bounded_processor_number(value):.2f}"
    if isinstance(value, dict):
        return {key: _encoded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_encoded(item) for item in value]
    if isinstance(value, float):
        raise TypeError("ST61 does not encode numeric floats")
    return value


def _same(left: Any, right: Any, key: str = "") -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same(left[k], right[k], k) for k in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same(a, b) for a, b in zip(left, right, strict=True)
        )
    if (
        key in {"uuid", "UUID", "system_uuid", "source", "destination"}
        and is_uuid(left)
        and is_uuid(right)
    ):
        return left.lower() == right.lower()
    return left == right


def _snapshot(peer: Any) -> dict[str, Any]:
    if not isinstance(peer, dict) or peer.keys() != _SNAPSHOT_FIELDS:
        raise ValueError("ST61 requires the complete processor snapshot")
    for key in _UNIT_FIELDS:
        _unit(peer[key])
    for key in {*_COUNT_FIELDS, *memory._MEMORY_FIELDS, "partition_id"}:
        _integer(peer[key])
    if (
        not isinstance(peer["uuid"], str)
        or not is_uuid(peer["uuid"])
        or peer["partition_id"] < 1
    ):
        raise ValueError("ST61 snapshot identity is invalid")
    if any(
        not isinstance(peer[k], str) or not peer[k].strip()
        for k in ("name", "state", "partition_type", "rmc", "processor_mode")
    ):
        raise ValueError("ST61 snapshot requires complete identity and state")
    if (
        type(peer["shared"]) is not bool
        or type(peer["operation_in_progress"]) is not bool
    ):
        raise ValueError("ST61 snapshot booleans are malformed")
    if (
        peer["automatic_entitlement"] is not None
        and type(peer["automatic_entitlement"]) is not bool
    ):
        raise ValueError("ST61 automatic entitlement is malformed")
    for key in ("dynamic_memory", "dynamic_processor"):
        if (
            peer[key] is not None
            and type(peer[key]) is not bool
            and peer[key] not in ("true", "false", "1", "0")
        ):
            raise ValueError("ST61 snapshot capability is malformed")
    for key in ("entitled_mib", "runtime_entitled_mib"):
        if peer[key] is not None and peer[key] != "auto":
            _integer(peer[key])
    location = peer["change_location"]
    if (
        not isinstance(location, dict)
        or location.keys() != {"current_profile_sync", "lives_in", "profile_name"}
        or any(v is not None and not isinstance(v, str) for v in location.values())
    ):
        raise ValueError("ST61 requires a complete profile location")
    vector = peer["processors"]
    if not isinstance(vector, dict) or vector.keys() != _VECTOR.keys():
        raise ValueError("ST61 requires the complete shared processor vector")
    for name, key in _VECTOR.items():
        if not isinstance(vector[name], str) or vector[name] != str(peer[key]):
            raise ValueError("ST61 processor vector differs from its snapshot")
    for prefix in ("current", "pending"):
        for quantity in ("processors", "processing_units"):
            values = [
                bounded_processor_number(peer[f"{prefix}_{bound}{quantity}"])
                for bound in ("minimum_", "", "maximum_")
            ]
            if not values[0] <= values[1] <= values[2]:
                raise ValueError("ST61 processor envelope is contradictory")
    for low, value, high in (
        ("current_minimum_mib", "current_memory_mib", "current_maximum_mib"),
        ("minimum_memory_mib", "desired_memory_mib", "maximum_memory_mib"),
    ):
        if not peer[low] <= peer[value] <= peer[high]:
            raise ValueError("ST61 memory envelope is contradictory")
    for quantity in ("processors", "processing_units"):
        current = bounded_processor_number(peer[f"current_{quantity}"])
        if (
            not bounded_processor_number(peer[f"pending_minimum_{quantity}"])
            <= current
            <= bounded_processor_number(peer[f"pending_maximum_{quantity}"])
        ):
            raise ValueError("ST61 current quantity is outside pending bounds")
    if (
        bounded_processor_number(peer["current_processing_units"])
        > peer["current_processors"]
    ):
        raise ValueError("ST61 entitlement exceeds virtual processor count")
    return peer


def _fixture(peer: dict[str, Any]) -> None:
    if (
        peer["state"] != "running"
        or peer["partition_type"] != "AIX/Linux"
        or peer["rmc"] != "active"
        or peer["processor_mode"] != "shared"
        or peer["shared"]
        or peer["operation_in_progress"]
        or peer["dynamic_processor"] not in (True, "true", "1")
        or peer["change_location"]["current_profile_sync"]
        not in ("Disabled", "Suspended")
        or peer["change_location"]["lives_in"] != "current-configuration"
        or peer["current_processors"] != peer["runtime_processors"]
        or peer["current_processing_units"] != peer["runtime_processing_units"]
        or peer["current_memory_mib"] != peer["runtime_memory_mib"]
    ):
        raise ValueError(
            "ST61 requires running RMC shared-CPU dedicated-memory fixtures with disabled/suspended profile sync"
        )


def _peers(peers: Any) -> list[dict[str, Any]]:
    if not isinstance(peers, list) or len(peers) != 2:
        raise ValueError("ST61 requires two complete peers")
    for peer in peers:
        _fixture(_snapshot(peer))
    if (
        peers[0]["uuid"].lower() == peers[1]["uuid"].lower()
        or peers[0]["partition_id"] == peers[1]["partition_id"]
    ):
        raise ValueError("ST61 requires distinct fixture identities")
    return peers


async def admit(
    hmc: HMCClient,
    system_uuid: str,
    system_name: str,
    selectors: list[str],
    protected_names: list[str],
) -> list[dict[str, Any]]:
    peers = []
    for selector in selectors:
        kind, raw = await _read_endpoint(hmc, system_uuid, system_name, selector)
        peer = _snapshot(_encoded(raw))
        _fixture(peer)
        if kind != "LogicalPartition" or peer["name"] in protected_names:
            raise ValueError("ST61 refuses protected or non-LPAR fixtures")
        description = await get_lpar_description(hmc.config, system_name, peer["name"])
        owner = authorize_lpar_ownership_description(
            hmc, system_name, peer["name"], description, operation="lpar-mutation"
        )
        if owner != (hmc.config.agent_id or "hmcpctl"):
            raise ValueError("ST61 requires an explicit matching ownership stamp")
        peers.append(peer)
    return _peers(peers)


def expected_oracle(peers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    observed = []
    for p in peers:
        cpu = {
            "lpar_id": p["partition_id"],
            "lpar_name": p["name"],
            "curr_proc_mode": p["processor_mode"],
            "pend_proc_mode": p["processor_mode"],
        }
        for prefix, native in (("current", "curr"), ("pending", "pend")):
            for quantity, suffix in (
                ("processors", "procs"),
                ("processing_units", "proc_units"),
            ):
                for bound, token in (
                    ("minimum_", "min_"),
                    ("", ""),
                    ("maximum_", "max_"),
                ):
                    cpu[f"{native}_{token}{suffix}"] = p[f"{prefix}_{bound}{quantity}"]
        cpu.update(
            run_procs=p["runtime_processors"],
            run_proc_units=p["runtime_processing_units"],
        )
        observed.append(
            {
                "uuid": p["uuid"],
                "processors": cpu,
                "memory": [
                    p["partition_id"],
                    *[
                        p[k]
                        for k in (
                            "current_minimum_mib",
                            "current_memory_mib",
                            "current_maximum_mib",
                            "runtime_memory_mib",
                        )
                    ],
                ],
                "error": None,
            }
        )
    return observed


async def oracle(
    hmc: HMCClient, system: str, peers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    observed = []
    for peer in peers:
        try:
            number = _integer(peer["partition_id"])
            if number < 1:
                raise ValueError(
                    "ST61 independent oracle requires a positive numeric ID"
                )
            anchor = f"-m {shlex.quote(system)} --level lpar --filter lpar_ids={number}"
            raw = await run_hmc_command(
                hmc.config,
                f"lshwres -r proc {anchor} -F {','.join(_FIELDS)}",
                max_output_bytes=65536,
            )
            rows = list(csv.reader(io.StringIO(raw), strict=True))
            if len(rows) != 1 or len(rows[0]) != len(_FIELDS):
                raise ValueError("ST61 independent CPU oracle requires eighteen fields")
            cpu: dict[str, Any] = dict(zip(_FIELDS, rows[0], strict=True))
            for key, value in cpu.items():
                if key in ("lpar_name", "curr_proc_mode", "pend_proc_mode"):
                    continue
                parsed = bounded_processor_number(value, whole="proc_units" not in key)
                cpu[key] = int(parsed) if "proc_units" not in key else f"{parsed:.2f}"
            raw = await run_hmc_command(
                hmc.config,
                f"lshwres -r mem {anchor} -F lpar_id,curr_min_mem,curr_mem,curr_max_mem,run_mem,mem_mode",
                max_output_bytes=65536,
            )
            cells = raw.strip().split(",")
            if len(cells) != 6 or cells[-1] != "ded":
                raise ValueError(
                    "ST61 independent memory oracle requires dedicated memory"
                )
            values = [int(bounded_processor_number(c, whole=True)) for c in cells[:5]]
            if (
                cpu["lpar_id"] != number
                or values[0] != number
                or cpu["lpar_name"] != peer["name"]
                or cpu["curr_proc_mode"] != "shared"
                or cpu["pend_proc_mode"] != "shared"
                or not values[1] <= values[2] <= values[3]
            ):
                raise ValueError(
                    "ST61 independent oracle identity, mode or bounds differ"
                )
            observed.append(
                {
                    "uuid": peer["uuid"],
                    "processors": cpu,
                    "memory": values,
                    "error": None,
                }
            )
        except (HMCError, ValueError, csv.Error) as error:
            observed.append(
                {
                    "uuid": peer["uuid"],
                    "processors": None,
                    "memory": None,
                    "error": str(error)[:2048],
                }
            )
    return observed


def _advance(peers: list[dict[str, Any]], reverse: bool) -> list[dict[str, Any]]:
    result = deepcopy(peers)
    with localcontext(prec=2 * PROCESSOR_NUMERIC_TOKEN_BYTES + 2):
        for peer, sign in zip(result, (1, -1) if reverse else (-1, 1), strict=True):
            for key in ("current_processing_units", "runtime_processing_units"):
                peer[key] = (
                    f"{bounded_processor_number(Decimal(peer[key]) + sign * Decimal('0.10')):.2f}"
                )
            peer["processors"]["CurrentProcessingUnits"] = peer[
                "current_processing_units"
            ]
    return _peers(result)


def _phase_result(
    data: Any, before: list[dict[str, Any]], after: list[dict[str, Any]], reverse: bool
) -> bool:
    order = (1, 0) if reverse else (0, 1)
    expected_move = {"processors": None, "processing_units": "0.10", "force": False}
    if (
        not isinstance(data, dict)
        or data.keys()
        != {
            "outcome",
            "workflow_completed",
            "move",
            "submission_error",
            "source",
            "destination",
        }
        or data["outcome"] != "applied"
        or data["workflow_completed"] is not True
        or data["submission_error"] is not None
        or not _same(data["move"], expected_move)
    ):
        return False
    for label, index in zip(("source", "destination"), order, strict=True):
        endpoint = data[label]
        if not isinstance(endpoint, dict) or endpoint.keys() != {
            "UUID",
            "outcome",
            "workflow_completed",
            "before",
            "after",
            "expected",
            "submission_error",
            "change_location",
            "warnings",
            "read_error",
            "verified_quantities",
            "observed_only_quantities",
        }:
            return False
        expected = {
            "processors": after[index]["current_processors"],
            "processing_units": after[index]["current_processing_units"],
            "reset": False,
            "deferred": False,
            "noop": False,
        }
        if (
            not _same(endpoint["before"], before[index])
            or not _same(endpoint["after"], after[index])
            or not _same(endpoint["expected"], expected)
            or not _same(endpoint["UUID"], before[index]["uuid"], "UUID")
            or endpoint["outcome"] != "applied"
            or endpoint["workflow_completed"] is not True
            or endpoint["submission_error"] is not None
            or endpoint["read_error"] is not None
            or endpoint["verified_quantities"] != ["processors", "processing_units"]
            or endpoint["observed_only_quantities"] != []
            or not _same(endpoint["change_location"], after[index]["change_location"])
            or not isinstance(endpoint["warnings"], list)
            or not all(isinstance(w, str) for w in endpoint["warnings"])
        ):
            return False
    return True


def phase_succeeded(
    status: str,
    data: Any,
    observed: list[dict[str, Any]],
    expected: list[dict[str, Any]],
) -> bool:
    data = _encoded(data)
    # The selected case is always shared/shared, with a fixed 0.10 transfer.
    for reverse in (False, True):
        try:
            before = _advance(expected, not reverse)
        except ValueError:
            continue
        if (
            status == "PASS"
            and _same(observed, expected_oracle(expected))
            and _phase_result(data, before, expected, reverse)
        ):
            return True
    return False


async def exercise_processor_move(client: Any, state: RunState) -> None:
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
                raise ValueError("ST61 system name is unresolved")
            peers = await admit(
                hmc,
                system_uuid,
                system,
                list(selectors),
                list(state.config.protected_lpar_names),
            )
            # Both directions must remain feasible before authorizing the first effect.
            _advance(peers, False)
            original = await oracle(hmc, system, peers)
            if not _same(original, expected_oracle(peers)):
                raise ValueError("ST61 independent baseline differs from both fixtures")
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
            expected = peers
            for phase, reverse in (("forward", False), ("reverse", True)):
                fresh = await admit(
                    hmc,
                    system_uuid,
                    system,
                    [p["uuid"] for p in expected],
                    list(state.config.protected_lpar_names),
                )
                if not _same(fresh, expected) or not _same(
                    await oracle(hmc, system, expected), expected_oracle(expected)
                ):
                    raise ValueError(
                        "ST61 drift before effect; stop for read-only recovery"
                    )
                order = (1, 0) if reverse else (0, 1)
                intent = {
                    "source": expected[order[0]]["uuid"],
                    "destination": expected[order[1]]["uuid"],
                    "processing_units": "0.10",
                    "before": fresh,
                }
                state.record(SUBTASK, f"processor-move {phase} intent", "PASS", intent)
                state.checkpoint()
                status, data = await state.call(
                    client,
                    "hmc_move_lpar_processors",
                    lpar_name_or_uuid=intent["source"],
                    target_lpar_name_or_uuid=intent["destination"],
                    system_name_or_uuid=system_uuid,
                    move={"processing_units": "0.10"},
                )
                expected = _advance(expected, reverse)
                observed = await oracle(hmc, system, expected)
                held = phase_succeeded(
                    status, data, observed, expected
                ) and _phase_result(_encoded(data), fresh, expected, reverse)
                assertions.append(Assertion(f"{phase}-both-independent", held))
                state.record(
                    SUBTASK,
                    f"processor-move {phase} observed",
                    "PASS" if held else "FAIL",
                    {"result": _encoded(data), "independent": observed},
                )
                state.checkpoint()
                if not held:
                    break
            final = await oracle(hmc, system, peers)
            same = (
                _same(final, original)
                and await system_oracle(hmc, system) == baseline["system_oracle"]
            )
            cleanup = "passed" if same else "failed"
            state.record(
                SUBTASK,
                "processor-move final baseline",
                "PASS" if same else "FAIL",
                final,
            )
    except (HMCError, ValueError, TypeError, PermissionError, OSError) as error:
        state.record(SUBTASK, GROUP, "FAIL", str(error))
    if baseline is not None:
        state.record_verified(
            SUBTASK,
            "hmc_move_lpar_processors",
            operation="lpar.move_processors",
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
                "dedicated": "not-run",
                "mixed": "not-run",
                "inactive": "not-run",
                "force": "not-run",
                "IBM_i": "not-run",
                "VIOS": "not-run",
            },
        )


def validate_finished(document: dict[str, Any], baseline: dict[str, Any]) -> None:
    try:
        if any(
            not isinstance(document.get(key), dict)
            for key in ("run", "config", "hmc", "artifacts")
        ):
            raise ValueError("ST61 finite proof envelope is incomplete")
        if document["run"].get("partial") is not False or not isinstance(
            document["results"], list
        ):
            raise ValueError("ST61 finite proof remains partial")
        if not isinstance(baseline, dict) or baseline.keys() != {
            "system_uuid",
            "system",
            "peers",
            "oracle",
            "system_oracle",
            "protected_names",
        }:
            raise ValueError("ST61 finite proof baseline is incomplete")
        if (
            not isinstance(baseline["system_uuid"], str)
            or not is_uuid(baseline["system_uuid"])
            or not isinstance(baseline["system"], str)
            or not baseline["system"].strip()
            or not isinstance(baseline["protected_names"], list)
            or any(not isinstance(n, str) for n in baseline["protected_names"])
        ):
            raise ValueError("ST61 finite proof system identity is invalid")
        expected = deepcopy(_peers(baseline["peers"]))
        if (
            not _same(baseline["oracle"], expected_oracle(expected))
            or not isinstance(baseline["system_oracle"], list)
            or len(baseline["system_oracle"]) != 3
            or any(not isinstance(v, str) or not v for v in baseline["system_oracle"])
        ):
            raise ValueError("ST61 finite proof original independent baselines differ")
        if any(not isinstance(row, dict) for row in document["results"]):
            raise ValueError("ST61 finite proof contains malformed rows")
        rows = [
            row
            for row in document["results"]
            if row.get("subtask") == SUBTASK
            and row.get("tool") != "hmc_move_lpar_processors"
        ]
        labels = [
            BASELINE,
            "processor-move forward intent",
            "processor-move forward observed",
            "processor-move reverse intent",
            "processor-move reverse observed",
            "processor-move final baseline",
        ]
        if (
            [r.get("tool") for r in rows] != labels
            or any(
                type(r.get("subtask")) is not int or r.get("status") != "PASS"
                for r in rows
            )
            or any(
                not {"subtask", "tool", "status", "result", "timestamp", "note", "data"}
                <= row.keys()
                or any(
                    not isinstance(row.get(key), str)
                    for key in ("tool", "status", "result", "timestamp", "note")
                )
                or not row["timestamp"]
                for row in rows
            )
            or not _same(rows[0].get("data"), baseline)
        ):
            raise ValueError("ST61 finite successful sequence is incomplete")
        for index, reverse in ((1, False), (3, True)):
            order = (1, 0) if reverse else (0, 1)
            intent = {
                "source": expected[order[0]]["uuid"],
                "destination": expected[order[1]]["uuid"],
                "processing_units": "0.10",
                "before": expected,
            }
            if not _same(rows[index].get("data"), intent):
                raise ValueError(
                    "ST61 finite intent identity, quantity or before-state differs"
                )
            after = _advance(expected, reverse)
            observed = rows[index + 1]["data"]
            if (
                not isinstance(observed, dict)
                or observed.keys() != {"result", "independent"}
                or not _phase_result(observed["result"], expected, after, reverse)
                or not _same(observed["independent"], expected_oracle(after))
            ):
                raise ValueError(
                    "ST61 finite phase lacks complete successful submission and observations"
                )
            expected = after
        if not _same(rows[-1]["data"], baseline["oracle"]):
            raise ValueError("ST61 finite final baseline differs")
    except (KeyError, TypeError, AttributeError, IndexError) as error:
        raise ValueError("ST61 finite proof is malformed or incomplete") from error


async def recover_document(hmc_config: HMCConfig, document: dict[str, Any]) -> None:
    try:
        run = document["run"]
        if (
            run.get("group") != GROUP
            or run.get("subtasks") != [SUBTASK]
            or any(type(n) is not int for n in run["subtasks"])
            or run.get("tree_clean") is not True
        ):
            raise ValueError("Exact clean ST61 provenance is required")
        current = evidence.run_provenance(
            [SUBTASK], GROUP, evidence.repository_root(), "", False
        )
        if (
            not current["tested_commit"]
            or current["tested_commit"] != run.get("tested_commit")
            or current["tree_clean"] is not True
        ):
            raise ValueError("Recovery requires the same clean candidate revision")
        if not _same(
            document["hmc"],
            {
                "host": hmc_config.host,
                "port": hmc_config.port,
                "user": hmc_config.user,
                "verify_ssl": hmc_config.verify_ssl,
            },
        ):
            raise ValueError("Recovery HMC destination differs")
        baselines = [
            r["data"]
            for r in document["results"]
            if isinstance(r, dict)
            and r.get("subtask") == SUBTASK
            and r.get("tool") == BASELINE
            and r.get("status") == "PASS"
        ]
        if len(baselines) != 1:
            raise ValueError("One original dual baseline is required")
        baseline = baselines[0]
        validate_finished(document, baseline)
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
            not _same(peers, baseline["peers"])
            or not _same(observed, baseline["oracle"])
            or pools != baseline["system_oracle"]
        ):
            raise ValueError(
                "ST61 original baselines differ; no recovery write was attempted"
            )
    except (KeyError, TypeError, AttributeError, IndexError) as error:
        raise ValueError("ST61 recovery proof is malformed or incomplete") from error
